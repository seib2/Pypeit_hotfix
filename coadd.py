""" Class for coaddition
"""
import numpy as np
from numpy.ma.core import MaskedArray
import scipy

from matplotlib import pyplot as plt
from matplotlib import gridspec
from matplotlib.backends.backend_pdf import PdfPages

from astropy.io import fits
from astropy import units, constants, stats, convolution
c_kms = constants.c.to('km/s').value

from linetools.spectra.xspectrum1d import XSpectrum1D
from linetools.spectra.utils import collate

from pypeit import msgs
from pypeit.core import load
from pypeit.core import flux
from pypeit import utils
from pypeit.core.wavecal import wvutils
from pypeit import debugger
from pkg_resources import resource_filename

from IPython import embed

# TODO
    # Shift spectra
    # Scale by poly
    # Better rejection
    # Grow mask in final_rej?
    # QA
    # Should we get rid of masked array?

## Plotting parameters
plt.rcdefaults()
plt.rcParams['font.family'] = 'times new roman'
plt.rcParams["xtick.top"] = True
plt.rcParams["ytick.right"] = True
plt.rcParams["xtick.minor.visible"] = True
plt.rcParams["ytick.minor.visible"] = True
plt.rcParams["ytick.direction"] = 'in'
plt.rcParams["xtick.direction"] = 'in'
plt.rcParams["xtick.labelsize"] = 17
plt.rcParams["ytick.labelsize"] = 17
plt.rcParams["axes.labelsize"] = 17

def new_wave_grid(waves, wave_method='iref', iref=0, wave_grid_min=None, wave_grid_max=None,
                  A_pix=None, v_pix=None, **kwargs):
    """ Create a new wavelength grid for the
    spectra to be rebinned and coadded on

    Parameters
    ----------
    waves : masked ndarray
        Set of N original wavelength arrays
        Nspec, Npix
    wave_method : str, optional
        Desired method for creating new wavelength grid.
        'iref' -- Use the first wavelength array (default)
        'velocity' -- Constant velocity
        'pixel' -- Constant pixel grid
        'concatenate' -- Meld the input wavelength arrays
    iref : int, optional
      Reference spectrum
    wave_grid_min: float, optional
      min wavelength value for the final grid
    wave_grid_max: float, optional
      max wavelength value for the final grid
    A_pix : float
      Pixel size in same units as input wavelength array (e.g. Angstroms)
    v_pix : float
      Pixel size in km/s for velocity method
      If not input, the median km/s per pixel is calculated and used

    Returns
    -------
    wave_grid : ndarray
        New wavelength grid, not masked
    """
    # Eventually add/change this to also take in slf, which has
    # slf._argflag['reduce']['pixelsize'] = 2.5?? This won't work
    # if running coadding outside of PypeIt, which we'd like as an
    # option!
    if not isinstance(waves, MaskedArray):
        waves = np.ma.array(waves)

    if wave_method == 'velocity':  # Constant km/s
        # Find the median velocity of a pixel in the input
        # wavelength grid

        spl = 299792.458
        if v_pix is None:
            dv = spl * np.abs(waves - np.roll(waves,1)) / waves   # km/s
            v_pix = np.median(dv)

        # Generate wavelenth array
        if wave_grid_min is None:
            wave_grid_min = np.min(waves)
        if wave_grid_max is None:
            wave_grid_max = np.max(waves)
        x = np.log10(v_pix/spl + 1)
        npix = int(np.log10(wave_grid_max/wave_grid_min) / x) + 1
        wave_grid = wave_grid_min * 10**(x*np.arange(npix))

        #while max(wave_grid) <= wave_grid_max:
        #    # How do we determine a reasonable constant velocity? (the 100. here is arbitrary)
        #    step = wave_grid[count] * (100. / 299792.458)
        #    wave_grid.append(wave_grid[count] + step)
        #    count += 1

#        wave_grid = np.asarray(wave_grid)

    elif wave_method == 'pixel': # Constant Angstrom
        if A_pix is None:
            msgs.error("Need to provide pixel size with A_pix for with this method")
        #
        if wave_grid_min is None:
            wave_grid_min = np.min(waves)
        if wave_grid_max is None:
            wave_grid_max = np.max(waves)

        wave_grid = np.arange(wave_grid_min, wave_grid_max + A_pix, A_pix)

    elif wave_method == 'concatenate':  # Concatenate
        # Setup
        waves_ma = np.ma.array(waves, mask = waves <= 1.0)
        loglam = np.ma.log10(waves) # This deals with padding (0's) just fine, i.e. they get masked..
        nspec = waves.shape[0]
        newloglam = loglam[iref, :].compressed()  # Deals with mask
        # Loop
        for j in range(nspec):
            if j == iref:
                continue
            #
            iloglam = loglam[j,:].compressed()
            dloglam_0 = (newloglam[1]-newloglam[0])
            dloglam_n =  (newloglam[-1] - newloglam[-2]) # Assumes sorted
            if (newloglam[0] - iloglam[0]) > dloglam_0:
                kmin = np.argmin(np.abs(iloglam - newloglam[0] - dloglam_0))
                newloglam = np.concatenate([iloglam[:kmin], newloglam])
            #
            if (iloglam[-1] - newloglam[-1]) > dloglam_n:
                kmin = np.argmin(np.abs(iloglam - newloglam[-1] - dloglam_n))
                newloglam = np.concatenate([newloglam, iloglam[kmin:]])
        # Finish
        wave_grid = 10**newloglam
    elif wave_method == 'iref':  # Concatenate
        wave_grid = waves[iref, :].compressed()
    else:
        msgs.error("Bad method for scaling: {:s}".format(wave_method))
    # Concatenate of any wavelengths in other indices that may extend beyond that of wavelengths[0]?

    return wave_grid


def gauss1(x, parameters):
    """ Simple Gaussian
    Parameters
    ----------
    x : ndarray
    parameters : ??

    Returns
    -------

    """
    sz = x.shape[0]
    
    if sz+1 == 5:
        smax = float(26)
    else:
        smax = 13.
    
    if len(parameters) >= 3:
        norm = parameters[2]
    else:
        norm = 1.
        
    u = ( (x - parameters[0]) / max(np.abs(parameters[1]), 1e-20) )**2.
    
    x_mask = np.where(u < smax**2)[0]
    norm = norm / (np.sqrt(2. * np.pi)*parameters[1])
                   
    return norm * x_mask * np.exp(-0.5 * u * x_mask)

def unpack_spec(spectra, all_wave=False):
    """ Unpack the spectra.  Default is to give only one wavelength array

    Parameters
    ----------
    spectra
    all_wave : bool, optional
      Return all the wavelength arrays

    Returns
    -------
    fluxes : ndarray (nspec, npix)
      Any masked values (there should be none) are set to 0.
    sigs : ndarray (nspec, npix)
    wave : ndarray (npix)

    """
    fluxes = spectra.data['flux'].filled(0.)
    sigs = spectra.data['sig'].filled(0.)
    if all_wave:
        wave = spectra.data['wave'].filled(0.)
    else:
        wave = np.array(spectra.data['wave'][0,:])

    # Return
    return fluxes, sigs, wave


def sn_weights(flux, sig, mask, wave, dv_smooth=10000.0, const_weights=False, debug=False, verbose=False):
    """ Calculate the S/N of each input spectrum and create an array of (S/N)^2 weights to be used
    for coadding.

    Parameters
    ----------
    fluxes: float ndarray, shape = (nexp, nspec)
        Stack of (nexp, nspec) spectra where nexp = number of exposures, and nspec is the length of the spectrum.
    sigs: float ndarray, shape = (nexp, nspec)
        1-sigm noise vectors for the spectra
    mask: bool ndarray, shape = (nexp, nspec)
        Mask for stack of spectra. True=Good, False=Bad.
    wave: flota ndarray, shape = (nspec,) or (nexp, nspec)
        Reference wavelength grid for all the spectra. If wave is a 1d array the routine will assume
        that all spectra are on the same wavelength grid. If wave is a 2-d array, it will use the individual

    Optional Parameters:
    --------------------
    dv_smooth: float, 10000.0
         Velocity smoothing used for determining smoothly varying S/N ratio weights.

    Returns
    -------
    rms_sn : array
        Root mean square S/N value for each input spectra
    weights : ndarray
        Weights to be applied to the spectra. These are signal-to-noise squared weights.
    """

    if flux.ndim == 1:
        nstack = 1
        nspec = flux.shape[0]
        flux_stack = flux.reshape((nstack, nspec))
        sig_stack = sig.reshape((nstack,nspec))
        mask_stack = mask.reshape((nstack, nspec))
    elif flux.ndim == 2:
        nstack = flux.shape[0]
        nspec = flux.shape[1]
        flux_stack = flux
        sig_stack = sig
        mask_stack = mask
    else:
        msgs.error('Unrecognized dimensionality for flux')

    # if the wave
    if wave.ndim == 1:
        wave_stack = np.outer(np.ones(nstack), wave)
    elif wave.ndim == 2:
        wave_stack = wave
    else:
        msgs.error('wavelength array has an invalid size')

    ivar_stack = utils.calc_ivar(sig_stack**2)
    # Calculate S/N
    sn_val = flux_stack*np.sqrt(ivar_stack)
    sn_val_ma = np.ma.array(sn_val, mask = np.invert(mask_stack))
    sn_sigclip = stats.sigma_clip(sn_val_ma, sigma=3, maxiters=5)
    sn2 = (sn_sigclip.mean(axis=1).compressed())**2 #S/N^2 value for each spectrum
    rms_sn = np.sqrt(sn2) # Root Mean S/N**2 value for all spectra
    rms_sn_stack = np.sqrt(np.mean(sn2))

    if rms_sn_stack <= 3.0 or const_weights:
        if verbose:
            msgs.info("Using constant weights for coadding, RMS S/N = {:g}".format(rms_sn_stack))
        weights = np.outer(sn2, np.ones(nspec))
        return rms_sn, weights
    else:
        if verbose:
            msgs.info("Using wavelength dependent weights for coadding")
        weights = np.ones_like(flux_stack) #((fluxes.shape[0], fluxes.shape[1]))
        spec_vec = np.arange(nspec)
        for ispec in range(nstack):
            imask = mask_stack[ispec,:]
            wave_now = wave_stack[ispec, imask]
            spec_now = spec_vec[imask]
            dwave = (wave_now - np.roll(wave_now,1))[1:]
            dv = (dwave/wave_now[1:])*c_kms
            dv_pix = np.median(dv)
            med_width = int(np.round(dv_smooth/dv_pix))
            sn_med1 = scipy.ndimage.filters.median_filter(sn_val[ispec,imask]**2, size=med_width, mode='reflect')
            sn_med2 = np.interp(spec_vec, spec_now, sn_med1)
            #sn_med2 = np.interp(wave_stack[ispec,:], wave_now,sn_med1)
            sig_res = np.fmax(med_width/10.0, 3.0)
            gauss_kernel = convolution.Gaussian1DKernel(sig_res)
            sn_conv = convolution.convolve(sn_med2, gauss_kernel)
            weights[ispec,:] = sn_conv

        # Finish
        return rms_sn, weights


def grow_mask(initial_mask, n_grow=1):
    """ Grows sigma-clipped mask by n_grow pixels on each side

    Parameters
    ----------
    initial_mask : ndarray
        Initial mask for the flux + variance arrays.  True = Good. Bad = False.
    n_grow : int, optional
        Number of pixels to grow the initial mask by
        on each side. Defaults to 1 pixel

    Returns
    -------
    grow_mask : ndarray
        Final mask for the flux + variance arrays
    """
    if not isinstance(n_grow, int):
        msgs.error("n_grow must be an integer")
    # Init
    grow_mask = np.ma.copy(initial_mask)
    npix = grow_mask.size

    # Loop on spectra
    bad_pix = np.where(np.invert(initial_mask))[0]
    for idx in bad_pix:
        msk_p = idx + np.arange(-1*n_grow, n_grow+1)
        # Restrict
        gdp = (msk_p >= 0) & (msk_p < npix)
        # Apply
        grow_mask[msk_p[gdp]] = False
    # Return
    return grow_mask


def median_ratio_flux(spec, smask, ispec, iref, nsig=3., niter=5, **kwargs):
    """ Calculate the median ratio between two spectra
    Parameters
    ----------
    spec
    smask:
       True = Good, False = Bad
    ispec
    iref
    nsig
    niter
    kwargs

    Returns
    -------
    med_scale : float
      Median of reference spectrum to input spectrum
    """
    # Setup
    fluxes, sigs, wave = unpack_spec(spec)
    # Mask
    okm = smask[iref,:] & smask[ispec,:]
    # Insist on positive values
    okf = (fluxes[iref,:] > 0.) & (fluxes[ispec,:] > 0)
    allok = okm & okf
    # Ratio
    med_flux = fluxes[iref,allok] / fluxes[ispec,allok]
    # Clip
    mn_scale, med_scale, std_scale = stats.sigma_clipped_stats(med_flux, sigma=nsig, maxiters=niter, **kwargs)
    # Return
    return med_scale


'''
def median_flux(spec, smask, nsig=3., niter=5, **kwargs):
    """ Calculate the characteristic, median flux of a spectrum

    Parameters
    ----------
    spec : XSpectrum1D
    mask : ndarray, optional
      Additional input mask with True = masked
      This needs to have the same size as the masked spectrum
    nsig : float, optional
      Clip sigma
    niter : int, optional
      Number of clipping iterations
    **kwargs : optional
      Passed to each call of sigma_clipped_stats

    Returns
    -------
    med_spec, std_spec
    """
    debugger.set_trace()   # This routine is not so useful
    # Setup
    fluxes, sigs, wave = unpack_spec(spec)
    # Mask locally
    mfluxes = np.ma.array(fluxes, mask=smask)
    #goodpix = WHERE(refivar GT 0.0 AND finite(refflux) AND finite(refivar) $
    #            AND refmask EQ 1 AND refivar LT 1.0d8)
    mean_spec, med_spec, std_spec = stats.sigma_clipped_stats(mfluxes, sigma=nsig, iters=niter, **kwargs)
    # Clip a bit
    #badpix = np.any([spec.flux.value < 0.5*np.abs(med_spec)])
    badpix = mfluxes.filled(0.) < 0.5*np.abs(med_spec)
    mean_spec, med_spec, std_spec = stats.sigma_clipped_stats(mfluxes.filled(0.), mask=badpix,
                                                        sigma=nsig, iters=niter, **kwargs)
    debugger.set_trace()
    # Return
    return med_spec, std_spec

'''

# TODO Rewrite this routine to take flux, wave, sig and not an Xspectrum object
def scale_spectra(spectra, smask, rms_sn, iref=0, scale_method='auto', hand_scale=None,
                  SN_MAX_MEDSCALE=2., SN_MIN_MEDSCALE=0.5, **kwargs):
    """
    Parameters
    ----------
    spectra : XSpectrum1D
      Rebinned spectra
      These should be registered, i.e. pixel 0 has the same wavelength for all
    smask:
       True = Good, False = Bad.
    rms_sn : ndarray
      Root mean square signal-to-noise estimate for each spectrum. Computed by sn_weights routine.
    iref : int, optional
      Index of reference spectrum
    scale_method : str, optional
      Method for scaling
       'auto' -- Use automatic method based on RMS of S/N
       'hand' -- Use input scale factors
       'median' -- Use calcualted median value
    SN_MIN_MEDSCALE : float, optional
      Maximum RMS S/N allowed to automatically apply median scaling
    SN_MAX_MEDSCALE : float, optional
      Maximum RMS S/N allowed to automatically apply median scaling

    Returns
    -------
    scales : list of float or ndarray
      Scale value (or arrays) that was applied to the data
    omethod : str
      Method applied (mainly useful if auto was adopted)
       'hand'
       'median'
       'none_SN'
    """
    # Init
    med_ref = None
    # Check for wavelength registration
    #gdp = np.all(~spectra.data['flux'].mask, axis=0)
    #gidx = np.where(gdp)[0]
    #if not np.isclose(spectra.data['wave'][0,gidx[0]],spectra.data['wave'][1,gidx[0]]):
    #    msgs.error("Input spectra are not registered!")
    # Loop on exposures

    rms_sn_stack = np.sqrt(np.mean(rms_sn**2))

    scales = []
    for qq in range(spectra.nspec):
        if scale_method == 'hand':
            omethod = 'hand'
            # Input?
            if hand_scale is None:
                msgs.error("Need to provide hand_scale parameter, one value per spectrum")
            spectra.data['flux'][qq,:] *= hand_scale[qq]
            spectra.data['sig'][qq,:] *= hand_scale[qq]
            #arrsky[*, j] = HAND_SCALE[j]*sclsky[*, j]
            scales.append(hand_scale[qq])
            #
        elif ((rms_sn_stack <= SN_MAX_MEDSCALE) and (rms_sn_stack > SN_MIN_MEDSCALE)) or scale_method=='median':
            omethod = 'median_flux'
            if qq == iref:
                scales.append(1.)
                continue
            # Median ratio (reference to spectrum)
            med_scale = median_ratio_flux(spectra, smask, qq, iref)
            # Apply
            med_scale= np.minimum(med_scale, 10.0)
            spectra.data['flux'][qq,:] *= med_scale
            spectra.data['sig'][qq,:] *= med_scale
            #
            scales.append(med_scale)
        elif rms_sn_stack <= SN_MIN_MEDSCALE:
            omethod = 'none_SN'
        elif (rms_sn_stack > SN_MAX_MEDSCALE) or scale_method=='poly':
            msgs.work("Should be using poly here, not median")
            omethod = 'median_flux'
            if qq == iref:
                scales.append(1.)
                continue
            # Median ratio (reference to spectrum)
            med_scale = median_ratio_flux(spectra, smask, qq, iref)
            # Apply
            med_scale= np.minimum(med_scale, 10.0)
            spectra.data['flux'][qq,:] *= med_scale
            spectra.data['sig'][qq,:] *= med_scale
            #
            scales.append(med_scale)
        else:
            msgs.error("Scale method not recognized! Check documentation for available options")
    # Finish
    return scales, omethod


def bspline_cr(spectra, n_grow_mask=1, cr_nsig=5., debug=False):
    """ Experimental and not so successful..

    Parameters
    ----------
    spectra
    n_grow_mask
    cr_nsig

    Returns
    -------

    """
    # Unpack
    fluxes, sigs, wave = unpack_spec(spectra, all_wave=True)

    # Concatenate
    all_f = fluxes.flatten()
    all_s = sigs.flatten()
    all_w = wave.flatten()

    # Sort
    srt = np.argsort(all_w)

    # Bad pix
    goodp = all_s[srt] > 0.

    # Fit
    # FW: everyn is not supported by robust_polyfit
    mask, bspl = utils.robust_polyfit(all_w[srt][goodp], all_f[srt][goodp], 3,
                                        function='bspline', sigma=cr_nsig, #everyn=2*spectra.nspec,
                                        weights=1./np.sqrt(all_s[srt][goodp]), maxone=False)
    # Plot?
    if debug:
        from matplotlib import pyplot as plt
        plt.clf()
        ax = plt.gca()
        ax.scatter(all_w[srt][goodp], all_f[srt][goodp], color='k')
        #
        x = np.linspace(np.min(all_w), np.max(all_w), 30000)
        y = utils.func_val(bspl, x, 'bspline')
        ax.plot(x,y)
        # Masked
        ax.scatter(all_w[srt][goodp][mask==1], all_f[srt][goodp][mask==1], color='r')
        # Range
        stdf = np.std(all_f[srt][goodp])
        ax.set_ylim(-2*stdf, 3*stdf)
        plt.show()
        debugger.set_trace()


def clean_cr(spectra, smask, n_grow_mask=1, cr_nsig=7., nrej_low=5.,
    debug=False, cr_everyn=6, cr_bsigma=5., cr_two_alg='bspline', **kwargs):
    """ Sigma-clips the flux arrays to remove obvious CR

    Parameters
    ----------
    spectra :
    smask : ndarray
      Data mask. True  = Good, False = bad
    n_grow_mask : int, optional
        Number of pixels to grow the initial mask by
        on each side. Defaults to 1 pixel
    cr_nsig : float, optional
      Number of sigma for rejection for CRs

    Returns
    -------
    """
    # Init
    fluxes, sigs, wave = unpack_spec(spectra)
    npix = wave.size

    def rej_bad(smask, badchi, n_grow_mask, ispec):
        # Grow?
        if n_grow_mask > 0:
            badchi = grow_mask(badchi, n_grow=n_grow_mask)
        # Mask
        smask[ispec,badchi] = False
        msgs.info("Rejecting {:d} CRs in exposure {:d}".format(np.sum(badchi),ispec))
        return

    if spectra.nspec == 2:
        msgs.info("Only 2 exposures.  Using custom procedure")
        if cr_two_alg == 'diff':
            diff = fluxes[0,:] - fluxes[1,:]
            # Robust mean/median
            med, mad = utils.robust_meanstd(diff)
            # Spec0?
            cr0 = (diff-med) > cr_nsig*mad
            if n_grow_mask > 0:
                cr0 = grow_mask(cr0, n_grow=n_grow_mask)
            msgs.info("Rejecting {:d} CRs in exposure 0".format(np.sum(cr0)))
            smask[0,cr0] = False
            if debug:
                debugger.plot1d(wave, fluxes[0,:], xtwo=wave[cr0], ytwo=fluxes[0,cr0], mtwo='s')
            # Spec1?
            cr1 = (-1*(diff-med)) > cr_nsig*mad
            if n_grow_mask > 0:
                cr1 = grow_mask(cr1, n_grow=n_grow_mask)
            smask[1,cr1] = False
            if debug:
                debugger.plot1d(wave, fluxes[1,:], xtwo=wave[cr1], ytwo=fluxes[1,cr1], mtwo='s')
            msgs.info("Rejecting {:d} CRs in exposure 1".format(np.sum(cr1)))
        elif cr_two_alg == 'ratio':
            diff = fluxes[0,:] - fluxes[1,:]
            rtio = fluxes[0,:] / fluxes[1,:]
            # Robust mean/median
            rmed, rmad = utils.robust_meanstd(rtio)
            dmed, dmad = utils.robust_meanstd(diff)
            # Spec0?            med, mad = utils.robust_meanstd(diff)
            cr0 = ((rtio-rmed) > cr_nsig*rmad) & ((diff-dmed) > cr_nsig*dmad)
            if n_grow_mask > 0:
                cr0 = grow_mask(cr0, n_grow=n_grow_mask)
            msgs.info("Rejecting {:d} CRs in exposure 0".format(np.sum(cr0)))
            smask[0,cr0] = False
            if debug:
                debugger.plot1d(wave, fluxes[0,:], xtwo=wave[cr0], ytwo=fluxes[0,cr0], mtwo='s')
            # Spec1?
            cr1 = (-1*(rtio-rmed) > cr_nsig*rmad) & (-1*(diff-dmed) > cr_nsig*dmad)
            if n_grow_mask > 0:
                cr1 = grow_mask(cr1, n_grow=n_grow_mask)
            smask[1,cr1] = False
            if debug:
                debugger.plot1d(wave, fluxes[1,:], xtwo=wave[cr1], ytwo=fluxes[1,cr1], mtwo='s')
            msgs.info("Rejecting {:d} CRs in exposure 1".format(np.sum(cr1)))
        elif cr_two_alg == 'bspline':
            # Package Data for convenience
            waves = spectra.data['wave'].flatten()  # Packed 0,1
            flux = fluxes.flatten()
            sig = sigs.flatten()
            #
            gd = np.where(sig > 0.)[0]
            srt = np.argsort(waves[gd])
            idx = gd[srt]
            # The following may eliminate bright, narrow emission lines
            good, spl = utils.robust_polyfit_djs(waves[idx], flux[idx], 3, function='bspline',
                                                 sigma=sig[gd][srt], lower=cr_bsigma, upper=cr_bsigma, use_mad=False)
            mask = ~good
            # Reject CR (with grow)
            spec_fit = utils.func_val(spl, wave, 'bspline')
            for ii in range(2):
                diff = fluxes[ii,:] - spec_fit
                cr = (diff > cr_nsig*sigs[ii,:]) & (sigs[ii,:]>0.)
                if debug:
                    debugger.plot1d(spectra.data['wave'][0,:], spectra.data['flux'][ii,:], spec_fit, xtwo=spectra.data['wave'][0,cr], ytwo=spectra.data['flux'][ii,cr], mtwo='s')
                if n_grow_mask > 0:
                    cr = grow_mask(cr, n_grow=n_grow_mask)
                # Mask
                smask[ii,cr] = False
                msgs.info("Cleaning {:d} CRs in exposure {:d}".format(np.sum(cr),ii))
            # Reject Low
            if nrej_low > 0.:
                for ii in range(2):
                    diff = spec_fit - fluxes[ii,:]
                    rej_low = (diff > nrej_low*sigs[ii,:]) & (sigs[ii,:]>0.)
                    if False:
                        debugger.plot1d(spectra.data['wave'][0,:], spectra.data['flux'][ii,:], spec_fit, xtwo=spectra.data['wave'][0,rej_low], ytwo=spectra.data['flux'][ii,rej_low], mtwo='s')
                    msgs.info("Removing {:d} low values in exposure {:d}".format(np.sum(rej_low),ii))
                    smask[ii,rej_low] = False
            else:
                msgs.error("Bad algorithm for combining two spectra!")
        # Check
        if debug:
            gd0 = smask[0,:]
            gd1 = smask[1,:]
            debugger.plot1d(wave[gd0], fluxes[0,gd0], xtwo=wave[gd1], ytwo=fluxes[1,gd1])
            #debugger.set_trace()

    else:
        # Median of the masked array -- Best for 3 or more spectra
        mflux = np.ma.array(fluxes, mask=np.invert(smask))
        refflux = np.ma.median(mflux,axis=0)
        diff = fluxes - refflux.filled(0.)

        # Loop on spectra
        for ispec in range(spectra.nspec):
            # Generate ivar
            gds = (smask[ispec,:]) & (sigs[ispec,:] > 0.)
            ivar = np.zeros(npix)
            ivar[gds] = 1./sigs[ispec,gds]**2
            # Single pixel events
            chi2 = diff[ispec]**2 * ivar
            badchi = (ivar > 0.0) & (chi2 > cr_nsig**2)
            if np.any(badchi) > 0:
                rej_bad(smask, badchi, n_grow_mask, ispec)
            # Dual pixels [CRs usually affect 2 (or more) pixels]
            tchi2 = chi2 + np.roll(chi2,1)
            badchi = (ivar > 0.0) & (tchi2 > 2*cr_nsig**2)
            if np.any(badchi) > 0:
                rej_bad(smask, badchi, n_grow_mask, ispec)
    # Return
    return


def one_d_coadd(spectra, smask, weights, debug=False, **kwargs):
    """ Performs a weighted coadd of the spectra in 1D.

    Parameters
    ----------
    spectra : XSpectrum1D
    smask: mask
        True = Good, False = Bad
    weights : ndarray
      Should be masked

    Returns
    -------
    coadd : XSpectrum1D

    """
    # Setup
    fluxes, sigs, wave = unpack_spec(spectra)
    variances = (sigs > 0.) * sigs**2
    inv_variances = (sigs > 0.)/(sigs**2 + (sigs==0.))

    # Sum weights
    mweights = np.ma.array(weights, mask=np.invert(smask))
    sum_weights = np.ma.sum(mweights, axis=0).filled(0.)


    # Coadd
    new_flux = np.ma.sum(mweights*fluxes, axis=0) / (sum_weights + (sum_weights == 0.0).astype(int))
    var = (variances != 0.0).astype(float) / (inv_variances + (inv_variances == 0.0).astype(float))
    new_var = np.ma.sum((mweights**2.)*var, axis=0) / ((sum_weights + (sum_weights == 0.0).astype(int))**2.)

    # Replace masked values with zeros
    new_flux = new_flux.filled(0.)
    new_sig = np.sqrt(new_var.filled(0.))

    # New obj (for passing around)
    wave_in = wave if isinstance(wave,units.quantity.Quantity) else wave*units.AA
    new_spec = XSpectrum1D.from_tuple((wave_in, new_flux, new_sig), masking='none')

    if debug:
        debugger.plot1d(wave, new_flux, new_sig)
        #debugger.set_trace()
    # Return
    return new_spec


def load_spec(files, iextensions=None, extract='OPT', flux=True):
    """ Load a list of spectra into one XSpectrum1D object

    Parameters
    ----------
    files : list
      List of filenames
    iextensions : int or list, optional
      List of extensions, 1 per filename
      or an int which is the extension in each file
    extract : str, optional
      Extraction method ('opt', 'box')
    flux : bool, optional
      Apply to fluxed spectra?

    Returns
    -------
    spectra : XSpectrum1D
      -- All spectra are collated in this one object
    """
    # Extensions
    if iextensions is None:
        msgs.warn("Extensions not provided.  Assuming first extension for all")
        extensions = np.ones(len(files), dtype='int8')
    elif isinstance(iextensions, int):
        extensions = np.ones(len(files), dtype='int8') * iextensions
    else:
        extensions = np.array(iextensions)

    # Load spectra
    spectra_list = []
    for ii, fname in enumerate(files):
        msgs.info("Loading extension {:d} of spectrum {:s}".format(extensions[ii], fname))
        spectrum = load.load_1dspec(fname, exten=extensions[ii], extract=extract, flux=flux)
        # Polish a bit -- Deal with NAN, inf, and *very* large values that will exceed
        #   the floating point precision of float32 for var which is sig**2 (i.e. 1e38)
        bad_flux = np.any([np.isnan(spectrum.flux), np.isinf(spectrum.flux),
                           np.abs(spectrum.flux) > 1e30,
                           spectrum.sig**2 > 1e10,
                           ], axis=0)
        if np.sum(bad_flux):
            msgs.warn("There are some bad flux values in this spectrum.  Will zero them out and mask them (not ideal)")
            spectrum.data['flux'][spectrum.select][bad_flux] = 0.
            spectrum.data['sig'][spectrum.select][bad_flux] = 0.
        # Append
        spectra_list.append(spectrum)
    # Join into one XSpectrum1D object
    spectra = collate(spectra_list)
    # Return
    return spectra


def get_std_dev(irspec, rmask, ispec1d, s2n_min=2., wvmnx=None, **kwargs):
    """
    Parameters
    ----------
    irspec : XSpectrum1D
      Array of spectra
    ispec1d : XSpectrum1D
      Coadded spectum
    rmask : ndarray
       True = Good. False = Bad.
    s2n_min : float, optional
      Minimum S/N for calculating std_dev
    wvmnx : tuple, optional
      Limit analysis to a wavelength interval

    Returns
    -------
    std_dev : float
      Standard devitation in good pixels
      TODO : Should restrict to higher S/N pixels
    dev_sig: ndarray
      Deviate, relative to sigma
    """
    # Setup
    fluxes, sigs, wave = unpack_spec(irspec)
    iflux = ispec1d.data['flux'][0,:].filled(0.)
    isig = ispec1d.data['sig'][0,:].filled(0.)
    cmask = rmask.copy()  # Starting mask
    # Mask locally
    mfluxes = np.ma.array(fluxes, mask=np.invert(rmask))
    msigs = np.ma.array(sigs, mask=np.invert(rmask))
    #
    msgs.work("We should restrict this to high S/N regions in the spectrum")
    # Mask on S/N_min
    bad_s2n = np.where(mfluxes/msigs < s2n_min)
    cmask[bad_s2n] = False
    # Limit by wavelength?
    if wvmnx is not None:
        msgs.info("Restricting std_dev calculation to wavelengths {}".format(wvmnx))
        bad_wv = np.any([(wave < wvmnx[0]), (wave > wvmnx[1])], axis=0)
        cmask[bad_wv] = False
    # Only calculate on regions with 2 or more spectra
    sum_msk = np.sum(cmask, axis=0)
    gdp = (sum_msk > 1) & (isig > 0.)
    if not np.any(gdp):
        msgs.warn("No pixels satisfying s2n_min in std_dev")
        return 1., None
    # Here we go
    dev_sig = (fluxes[:,gdp] - iflux[gdp]) / np.sqrt(sigs[:,gdp]**2 + isig[gdp]**2)
    std_dev = np.std(stats.sigma_clip(dev_sig, sigma=5, maxiters=2))
    return std_dev, dev_sig


def coadd_spectra(spectrograph, gdfiles, spectra, wave_grid_method='concatenate', niter=5,
                  flux_scale=None,
                  scale_method='auto', do_offset=False, sigrej_final=3.,
                  do_var_corr=True, qafile=None, outfile=None,
                  do_cr=True, debug=False,**kwargs):
    """

    Args:
        spectra:
        wave_grid_method:
        niter:
        flux_scale (dict):  Use input info to scale the final spectrum to a photometric magnitude
        scale_method:
        do_offset:
        sigrej_final:
        do_var_corr:
        qafile:
        outfile:
        do_cr:
        debug:
        **kwargs:
    """
    # Init
    if niter <= 0:
        msgs.error('Not prepared for zero iterations')

    # Single spectrum?
    if spectra.nspec == 1:
        msgs.info('Only one spectrum.  Writing, as desired, and ending..')
        if outfile is not None:
            write_to_disk(spectra, outfile)
        return spectra

    if 'echelle' in kwargs:
        echelle = kwargs['echelle']
    else:
        echelle =  False

    # Final wavelength array
    new_wave = new_wave_grid(spectra.data['wave'], wave_method=wave_grid_method, **kwargs)

    # Rebin
    rspec = spectra.rebin(new_wave*units.AA, all=True, do_sig=True, grow_bad_sig=True,
                          masking='none')

    # Define mask -- THIS IS THE ONLY ONE TO USE
    rmask = rspec.data['sig'].filled(0.) > 0.0

    fluxes, sigs, wave = unpack_spec(rspec)


    # S/N**2, weights
    rms_sn, weights = sn_weights(fluxes, sigs, rmask, wave)

    # Scale (modifies rspec in place)
    if echelle:
        if scale_method is None:
            msgs.warn('No scaling betweeen different exposures/orders.')
        else:
            msgs.work('Need add a function to scale Echelle spectra.')
            #scales, omethod = scale_spectra(rspec, rmask, sn2, scale_method='median', **kwargs)
    else:
        scales, omethod = scale_spectra(rspec, rmask, rms_sn, scale_method=scale_method, **kwargs)

    # Clean bad CR :: Should be run *after* scaling
    if do_cr:
        clean_cr(rspec, rmask, **kwargs)

    # Initial coadd
    spec1d = one_d_coadd(rspec, rmask, weights)

    # Init standard deviation
    # FW: Not sure why calling this function as you initial the std_dev = 0. in the following.
    std_dev, _ = get_std_dev(rspec, rmask, spec1d, **kwargs)
    msgs.info("Initial std_dev = {:g}".format(std_dev))

    iters = 0
    std_dev = 0.
    var_corr = 1.

    # Scale the standard deviation
    while np.absolute(std_dev - 1.) >= 0.1 and iters < niter:
        iters += 1
        msgs.info("Iterating on coadding... iter={:d}".format(iters))

        # Setup (strip out masks, if any)
        tspec = spec1d.copy()
        tspec.unmask()
        newvar = tspec.data['sig'][0,:].filled(0.)**2  # JFH Interpolates over bad values?
        newflux = tspec.data['flux'][0,:].filled(0.)
        newflux_now = newflux  # JFH interpolates
        # Convenient for coadding
        uspec = rspec.copy()
        uspec.unmask()

        # Loop on images to update noise model for rejection
        for qq in range(rspec.nspec):

            # Grab full spectrum (unmasked)
            iflux = uspec.data['flux'][qq,:].filled(0.)
            sig = uspec.data['sig'][qq,:].filled(0.)
            ivar = np.zeros_like(sig)
            gd = sig > 0.
            ivar[gd] = 1./sig[gd]**2

            # var_tot
            var_tot = newvar + utils.calc_ivar(ivar)
            ivar_real = utils.calc_ivar(var_tot)
            # smooth out possible outliers in noise
            #var_med = medfilt(var_tot, 5)
            #var_smooth = medfilt(var_tot, 99)#, boundary = 'reflect')
            var_med = scipy.ndimage.filters.median_filter(var_tot, size=5, mode='reflect')
            var_smooth = scipy.ndimage.filters.median_filter(var_tot, size=99, mode='reflect')
            # conservatively always take the largest variance
            var_final = np.maximum(var_med, var_smooth)
            ivar_final = utils.calc_ivar(var_final)
            # Cap S/N ratio at SN_MAX to prevent overly aggressive rejection
            SN_MAX = 20.0
            ivar_cap = np.minimum(ivar_final,(SN_MAX/(newflux_now + (newflux_now <= 0.0)))**2)
            #; adjust rejection to reflect the statistics of the distribtuion
            #; of errors. This fixes cases where for not totally understood
            #; reasons the noise model is not quite right and
            #; many pixels are rejected.

            #; Is the model offset relative to the data? If so take it out
            if do_offset:
                diff1 = iflux-newflux_now
                #idum = np.where(arrmask[*, j] EQ 0, nnotmask)
                debugger.set_trace() # GET THE MASK RIGHT!
                nnotmask = np.sum(rmask)
                nmed_diff = np.maximum(nnotmask//20, 10)
                #; take out the smoothly varying piece
                #; JXP -- This isnt going to work well if the data has a bunch of
                #; null values in it
                w = np.ones(5, 'd')
                diff_med = scipy.ndimage.filters.median_filter(diff1*(rmask), size = nmed_diff, mode='reflect')
                diff_sm = np.convolve(diff_med, w/w.sum(),mode='same')
                chi2 = (diff1-diff_sm)**2*ivar_real
                goodchi = (rmask) & (ivar_real > 0.0) & (chi2 <= 36.0) # AND masklam, ngd)
                if np.sum(goodchi) == 0:
                    goodchi = np.array([True]*iflux.size)
#                debugger.set_trace()  # Port next line to Python to use this
                #djs_iterstat, (arrflux[goodchi, j]-newflux_now[goodchi]) $
                #   , invvar = ivar_real[goodchi], mean = offset_mean $
                #   , median = offset $
            else:
                offset = 0.
            chi2 = (iflux-newflux_now - offset)**2*ivar_real
            goodchi = rmask[qq,:] & (ivar_real > 0.0) & (chi2 <= 36.0) # AND masklam, ngd)
            ngd = np.sum(goodchi)
            if ngd == 0:
                goodchi = np.array([True]*iflux.size)
            #; evalute statistics of chi2 for good pixels and excluding
            #; extreme 6-sigma outliers
            chi2_good = chi2[goodchi]
            chi2_srt = chi2_good.copy()
            chi2_srt.sort()
            #; evaluate at 1-sigma and then scale
            gauss_prob = 1.0 - 2.0*(1.-scipy.stats.norm.cdf(1.)) #gaussint(-double(1.0d))
            sigind = int(np.round(gauss_prob*ngd))
            chi2_sigrej = chi2_srt[sigind]
            one_sigma = np.minimum(np.maximum(np.sqrt(chi2_sigrej),1.0),5.0)
            sigrej_eff = sigrej_final*one_sigma
            chi2_cap = (iflux-newflux_now - offset)**2*ivar_cap
            # Grow??
            #Is this correct? This is not growing mask
            #chi_mask = (chi2_cap > sigrej_eff**2) & (~rmask[qq,:])
            chi_mask = (chi2_cap > sigrej_eff**2) | np.invert(rmask[qq,:])
            nrej = np.sum(chi_mask)
            # Apply
            if nrej > 0:
                msgs.info("Rejecting {:d} pixels in exposure {:d}".format(nrej,qq))
                #print(rspec.data['wave'][qq,chi_mask])
                rmask[qq,chi_mask] = False
                #rspec.select = qq
                #rspec.add_to_mask(chi_mask)
            #outmask[*, j] = (arrmask[*, j] EQ 1) OR (chi2_cap GT sigrej_eff^2)

        # Incorporate saving of each dev/sig panel onto one page? Currently only saves last fit
        #qa_plots(wavelengths, masked_fluxes, masked_vars, new_wave, new_flux, new_var)

        # Coadd anew
        spec1d = one_d_coadd(rspec, rmask, weights, **kwargs)
        # Calculate std_dev
        std_dev, _ = get_std_dev(rspec, rmask, spec1d, **kwargs)
        #var_corr = var_corr * std_dev
        msgs.info("Desired variance correction: {:g}".format(var_corr))
        msgs.info("New standard deviation: {:g}".format(std_dev))

        if do_var_corr:
            msgs.info("Correcting variance")
            for ispec in range(rspec.nspec):
                rspec.data['sig'][ispec] *= np.sqrt(std_dev)
            spec1d = one_d_coadd(rspec, rmask, weights)

    if iters == 0:
        msgs.warn("No iterations on coadding done")
        #qa_plots(wavelengths, masked_fluxes, masked_vars, new_wave, new_flux, new_var)
    else: #if iters > 0:
        msgs.info("Final correction to initial variances: {:g}".format(var_corr))

    # QA
    if qafile is not None:
        msgs.info("Writing QA file: {:s}".format(qafile))
        coaddspec_qa(spectra, rspec, rmask, spec1d, qafile=qafile,debug=debug)

    # Scale the flux??
    if flux_scale is not None:
        spec1d, _ = flux.scale_in_filter(spec1d, flux_scale)

    # Write to disk?
    if outfile is not None:
        write_to_disk(spectrograph, gdfiles, spec1d, outfile)
    return spec1d


def write_to_disk(spectrograph, gdfiles, spec1d, outfile):
    """ Small method to write file to disk
    """
    # Header
    header_cards = spectrograph.header_cards_for_spec()
    orig_headers = [fits.open(gdfile)[0].header for gdfile in gdfiles]
    spec1d_header = {}
    for card in header_cards:
        # Special cases
        if card == 'exptime':   # Total
            tot_time = np.sum([ihead['EXPTIME'] for ihead in orig_headers])
            spec1d_header['EXPTIME'] = tot_time
        elif card.upper() in ['AIRMASS', 'MJD']:  # Average
            mean = np.mean([ihead[card.upper()] for ihead in orig_headers])
            spec1d_header[card.upper()] = mean
        elif card.upper() in ['MJD-OBS', 'FILENAME']:  # Skip
            continue
        else:
            spec1d_header[card.upper()] = orig_headers[0][card.upper()]
    # INSTRUME
    spec1d_header['INSTRUME'] = spectrograph.camera.strip()
    # Add em
    spec1d.meta['headers'][0] = spec1d_header
    #
    if '.hdf5' in outfile:
        spec1d.write_to_hdf5(outfile)
    elif '.fits' in outfile:
        spec1d.write_to_fits(outfile)
    return

def coaddspec_qa(ispectra, rspec, rmask, spec1d, qafile=None, yscale=8.,debug=False):
    """  QA plot for 1D coadd of spectra

    Parameters
    ----------
    ispectra : XSpectrum1D
      Multi-spectra object
    rspec : XSpectrum1D
      Rebinned spectra with updated variance
    rmask:
      True = Good. False = Bad.
    spec1d : XSpectrum1D
      Final coadd
    yscale : float, optional
      Scale median flux by this parameter for the spectral plot

    """

    plt.figure(figsize=(12,6))
    ax1 = plt.axes([0.07, 0.13, 0.6, 0.4])
    ax2 = plt.axes([0.07, 0.55,0.6, 0.4])
    ax3 = plt.axes([0.72,0.13,0.25,0.8])
    plt.setp(ax2.get_xticklabels(), visible=False)

    # Deviate
    std_dev, dev_sig = get_std_dev(rspec, rmask, spec1d)
    #dev_sig = (rspec.data['flux'] - spec1d.flux) / (rspec.data['sig']**2 + spec1d.sig**2)
    #std_dev = np.std(sigma_clip(dev_sig, sigma=5, iters=2))
    if dev_sig is not None:
        flat_dev_sig = dev_sig.flatten()

    xmin = -5
    xmax = 5
    n_bins = 50

    # Deviation
    if dev_sig is not None:
        hist, edges = np.histogram(flat_dev_sig, range=(xmin, xmax), bins=n_bins)
        area = len(flat_dev_sig)*((xmax-xmin)/float(n_bins))
        xppf = np.linspace(scipy.stats.norm.ppf(0.0001), scipy.stats.norm.ppf(0.9999), 100)
        ax3.plot(xppf, area*scipy.stats.norm.pdf(xppf), color='black', linewidth=2.0)
        ax3.bar(edges[:-1], hist, width=((xmax-xmin)/float(n_bins)), alpha=0.5)
    ax3.set_xlabel('Residual Distribution')
    ax3.set_title('New sigma = %s'%str(round(std_dev,2)),fontsize=17)

    # Coadd on individual
    # yrange
    medf = np.median(spec1d.flux)
    #ylim = (medf/10., yscale*medf)
    ylim = (np.sort([0.-1.5*medf, yscale*medf]))
    # Plot
    cmap = plt.get_cmap('RdYlBu_r')
    # change to plotting the scaled spectra
    #for idx in range(ispectra.nspec):
    #    ispectra.select = idx
    #    color = cmap(float(idx) / ispectra.nspec)
    #    ax1.plot(ispectra.wavelength, ispectra.flux, color=color)
    for idx in range(rspec.nspec):
        rspec.select = idx
        color = cmap(float(idx) / rspec.nspec)
        ind_good =  rspec.sig>0
        ind_mask = (rspec.sig>0) & np.invert(rmask[idx, :])
        ax1.plot(rspec.wavelength[ind_good], rspec.flux[ind_good], color=color,alpha=0.5)
        ax1.scatter(rspec.wavelength[ind_mask], rspec.flux[ind_mask],
                    marker='s',facecolor='None',edgecolor='k')

    if (np.max(spec1d.wavelength)>(9000.0*units.AA)):
        skytrans_file = resource_filename('pypeit', '/data/skisim/atm_transmission_secz1.5_1.6mm.dat')
        skycat = np.genfromtxt(skytrans_file,dtype='float')
        scale = 0.8*ylim[1]
        ax2.plot(skycat[:,0]*1e4,skycat[:,1]*scale,'m-',alpha=0.5)

    ax2.plot(spec1d.wavelength, spec1d.sig, ls='steps-',color='0.7')
    ax2.plot(spec1d.wavelength, spec1d.flux, ls='steps-',color='b')

    ax1.set_xlim([np.min(spec1d.wavelength.value),np.max(spec1d.wavelength.value)])
    ax1.set_ylim(ylim)
    ax2.set_xlim([np.min(spec1d.wavelength.value),np.max(spec1d.wavelength.value)])
    ax2.set_ylim(ylim)
    ax1.set_xlabel('Wavelength (Angstrom)')
    ax1.set_ylabel('Flux')
    ax2.set_ylabel('Flux')

    plt.tight_layout(pad=0.2,h_pad=0.,w_pad=0.2)
    if qafile is not None:
        if len(qafile.split('.'))==1:
            msgs.info("No fomat given for the qafile, save to PDF format.")
            qafile = qafile+'.pdf'
        pp = PdfPages(qafile)
        pp.savefig(bbox_inches='tight')
        pp.close()
        msgs.info("Wrote coadd QA: {:s}".format(qafile))
    if debug:
        plt.show()
    plt.close()

    return


### Start Echelle functionality

def spec_from_array(wave,flux,sig,**kwargs):
    """
    Make an XSpectrum1D from numpy arrays of wave, flux and sig
    Parameters
    ----------
        If wave is unitless, Angstroms are assumed
        If flux is unitless, it is made dimensionless
        The units for sig and co are taken from flux.
    Return spectrum from arrays of wave, flux and sigma
    """

    # Get rid of 0 wavelength
    good_wave = (wave>1.0*units.AA)
    wave,flux,sig = wave[good_wave],flux[good_wave],sig[good_wave]
    ituple = (wave, flux, sig)
    spectrum = XSpectrum1D.from_tuple(ituple, **kwargs)
    # Polish a bit -- Deal with NAN, inf, and *very* large values that will exceed
    #   the floating point precision of float32 for var which is sig**2 (i.e. 1e38)
    bad_flux = np.any([np.isnan(spectrum.flux), np.isinf(spectrum.flux),
                       np.abs(spectrum.flux) > 1e30,
                       spectrum.sig ** 2 > 1e10,
                       ], axis=0)
    if np.sum(bad_flux):
        msgs.warn("There are some bad flux values in this spectrum.  Will zero them out")
        spectrum.data['flux'][spectrum.select][bad_flux] = 0.
        spectrum.data['sig'][spectrum.select][bad_flux] = 0.
    return spectrum

def order_phot_scale(spectra, phot_scale_dicts, nsig=3.0, niter=5, debug=False):
    '''
    Scale coadded spectra with photometric data.
    Parameters:
      spectra: XSpectrum1D spectra (longslit) or spectra list (echelle)
      phot_scale_dicts: A dict contains photometric information of each orders (if echelle).
        An example is given below.
        phot_scale_dicts = {'0': {'filter': None, 'mag': None, 'mag_type': None, 'masks': None},
                            '1': {'filter': 'UKIRT-Y', 'mag': 20.33, 'mag_type': 'AB', 'masks': None},
                            '2': {'filter': 'UKIRT-J', 'mag': 20.19, 'mag_type': 'AB', 'masks': None},
                            '3': {'filter': 'UKIRT-H', 'mag': 20.02, 'mag_type': 'AB', 'masks': None},
                            '4': {'filter': 'UKIRT-K', 'mag': 19.92, 'mag_type': 'AB', 'masks': None}}
      Show QA plot if debug=True
    Return a new scaled XSpectrum1D spectra
    '''

    from pypeit.core.flux import scale_in_filter

    norder = spectra.nspec

    # scaling spectrum order by order, also from red to blue to be consistent with median scale.
    spectra_list_new = []
    scales = np.ones(norder)
    scale_success_flag = np.zeros(norder,dtype=bool)
    for i in range(norder):
        iord = norder - i - 1
        phot_scale_dict = phot_scale_dicts[str(iord)]
        if (phot_scale_dict['filter'] is not None) & (phot_scale_dict['mag'] is not None):
            speci, scale = scale_in_filter(spectra[iord], phot_scale_dict)
            scale_success_flag[iord] = True
            scales[iord] = scale
        else:
            #First try to use the scale factor from redder order, then bluer order. If both failed then use the median
            # scale factor.

            try:
                if scale_success_flag[iord+1]:
                    med_scale = scales[iord+1]
                else:
                    phot_scale_dict = phot_scale_dicts[str(iord+1)]
                    spec0 = spectra[iord + 1].copy()
                    spec1, scale = scale_in_filter(spectra[iord+1], phot_scale_dict)
                    med_scale = np.minimum(scale, 5.0)
                msgs.info('Using the redder order scaling factor {:} for order {:}'.format(med_scale, iord))
                speci = spectra[iord].copy()
                speci.data['flux'] *= med_scale
                speci.data['sig'] *= med_scale
                scale_success_flag[iord] = True
                scales[iord] = scale
            except:
                try:
                    phot_scale_dict = phot_scale_dicts[str(iord - 1)]
                    spec0 = spectra[iord - 1].copy()
                    spec1, scale = scale_in_filter(spectra[iord - 1], phot_scale_dict)
                    speci = spectra[iord]
                    med_scale = np.minimum(scale, 5.0)
                    msgs.info('Using the bluer order scaling factor {:} for order {:}'.format(med_scale, iord))
                    speci.data['flux'] *= med_scale
                    speci.data['sig'] *= med_scale
                    scale_success_flag[iord] = True
                    scales[iord] = scale
                except:
                    msgs.warn('Was not able to scale order {:} based on photometry. Will use median scale factor at the end'.format(iord))
                    speci = spectra[iord]
        spectra_list_new.append(speci)

        if debug:
            gdp = speci.sig>0
            plt.figure(figsize=(12, 6))
            plt.plot(spectra[iord].wavelength[gdp], spectra[iord].flux[gdp], 'k-', label='raw spectrum')
            plt.plot(speci.wavelength[gdp], speci.flux[gdp], 'b-',
                     label='scaled spectrum')
            mny, medy, stdy = stats.sigma_clipped_stats(speci.flux[gdp], sigma=3, iters=5)
            plt.ylim([0.1 * medy, 4.0 * medy])
            plt.legend()
            plt.xlabel('wavelength')
            plt.ylabel('Flux')
            plt.show()

    # If any order failed above then use median scale factor from other orders to correct it.
    if sum(scale_success_flag)<norder:
        med_scale = np.median(scales[scale_success_flag > 0])
        inds = np.where(scale_success_flag == 0)[0]
        for i in range(len(inds)):
            iord = inds[i]
            spectra_list_new[iord].data['flux'] *= med_scale
            spectra_list_new[iord].data['sig'] *= med_scale
            msgs.info('Scaled order {:} by {:}'.format(iord,med_scale))

    return collate(spectra_list_new)

def order_median_scale(wave, wave_mask, fluxes_in, ivar_in, sigrej=3.0, nsig=3.0, niter=5, num_min_pixels=21, min_overlap_pix=21, overlapfrac=0.03, min_overlap_frac=0.03, max_rescale_percent=50.0, sn_min=1.0, SN_MIN_MEDSCALE=1.0, debug=False):

    #### HOTFIX
    sigrej=nsig
    min_overlap_frac = overlapfrac
    min_overlap_pix = num_min_pixels
    sn_min = SN_MIN_MEDSCALE
    '''
    Scale different orders using the median of overlap regions. It starts from the reddest order, i.e. scale H to K,
      and then scale J to H+K, etc.
    Parameters:

    wave: float ndarray (nspec,)
       Common wavelength grid for all the orders
    wave_mask: bool ndarray (nspec,)
       Boolean array indicating the wavelengths that are populated by each order. True = pixel covered, False= not covered
    fluxes_in: float ndarray (nspec, norders)
       Fluxes on the common wavelength grid
    ivar_in: float ndarray (nspec, norders)
       Inverse variance on the common wavelength grid
    sigrej: float
        outlier rejection threshold used for sigma_clipping to compute median
    niter: int
        number of iterations for sigma_clipping median
    min_overlap_pix: float
        minmum number of overlapping pixels required to compute the median and rescale. These are pixels
        common to both the scaled spectrum and the reference spectrum.
    min_overlap_frac: float
        minmum fraction of the total number of good pixels in an order that need to be overlapping with the neighboring
        order to perform rescaling.
    max_rescale_percent: float
        maximum percentage to rescale by
    sn_min: float
        Only pixels with per pixel S/N ratio above this value are used in the median computation

      Show QA plot if debug=True
    Return:
        No return, but the spectra is already scaled after executing this function.
    '''

    fluxes_out = np.zeros_like(fluxes_in)
    ivar_out = np.zeros_like(ivar_in)
    norders = fluxes_in.shape[1]

    #fluxes_raw = fluxes.copy()
    #norder = fluxes.shape[1]
    #fluxes, sigs, wave = unpack_spec(spectra, all_wave=False)
    #fluxes_raw = fluxes.copy()

    # scaling spectrum order by order. We start from the reddest order and work towards the blue
    # as slit losses in redder orders are smaller.
    orders_rev = np.arange(norders-1)[::-1] # order indices in reverse order excluding the
    for iord_scl in orders_rev:
        iord_ref = iord_scl+1
        good_scl = wave_mask[:, iord_scl] & (ivar_in[:, iord_scl] > 0)
        good_ref = wave_mask[:, iord_ref] & (ivar_in[:, iord_ref] > 0)
        overlap = good_scl & good_ref
        noverlap = np.sum(overlap)
        nscl = np.sum(good_scl)
        nref = np.sum(good_ref)
        # S/N in the overlap regions
        sn_scl = fluxes_in[:, iord_scl]*np.sqrt(ivar_in[:, iord_scl])
        sn_ref = fluxes_in[:, iord_ref]*np.sqrt(ivar_in[:, iord_ref])
        sn_scl_mn, sn_scl_med, sn_scl_std = stats.sigma_clipped_stats(sn_scl[overlap], sigma=sigrej, iters=niter)
        sn_ref_mn, sn_ref_med, sn_ref_std = stats.sigma_clipped_stats(sn_ref[overlap], sigma=sigrej, iters=niter)
        rescale_criteria = (float(noverlap/nscl) > min_overlap_frac) & (float(noverlap/nref) > min_overlap_frac) & \
                       (noverlap > min_overlap_pix) & (sn_scl_med > sn_min) & (sn_ref_med > sn_min)
        if rescale_criteria:
            # Ratio
            #med_flux = fluxes_in[iord, allok]/fluxes_in[iord - 1, allok]
            # Determine medians using sigma clipping
            usepix = overlap & (sn_scl > 0.0)
            mn_iord_ref, med_iord_ref, std_iord_ref = \
                stats.sigma_clipped_stats(fluxes_in[iord_ref, allok], sigma=nsig, iters=niter)
            # Determine medians using sigma clipping
            mn_iord_scale, med_iord_scl, std_iord_scl = stats.sigma_clipped_stats(fluxes_in[iord, allok], sigma=nsig, iters=niter)
            # Do not allow for rescalings greater than max_rescale %
            med_scale = np.fmax((np.fmin(med_iord_ref/med_iord_scl,
                                         (1.0 + max_rescale_percent/100.0)),(1.0 - max_rescale_percent/100.0)))

            fluxes_out[iord_scl, :] *= med_scale
            sigs_out[iord_scl, :] *= med_scale
            msgs.info('Scaled %s order by a factor of %s'%(iord,str(med_scale)))

            if debug:
                plt.figure(figsize=(12, 6))
                plt.plot(wave[allok_iord_ref], fluxes_out[iord_ref, allok_iord_ref], '-',lw=10,color='0.7', label='Scale region')
                plt.plot(wave[allok_iord_scl], fluxes_out[iord,allok_iord_scl], 'r-', label='reference spectrum')
                plt.plot(wave[allok_iord_scl], fluxes_in[iord,allok_iord_scl], 'k-', label='raw spectrum')
                plt.plot(wave[allok_iord_scl], fluxes_out[iord, allok_iord_scl], 'b-',label='scaled spectrum')
                mny, medy, stdy = stats.sigma_clipped_stats(fluxes_in[iord, allok_iord_scl], sigma=nsig, iters=niter)
                plt.ylim([0.1 * medy, 4.0 * medy])
                plt.xlim([np.min(wave[allok_iord_ref]), np.max(wave[allok_iord_scl])])
                plt.legend()
                plt.xlabel('wavelength')
                plt.ylabel('Flux')
                plt.show()
        else:
            msgs.warn('Not enough spectral overlap to rescale spectra in order {:d}.'.format(iord_scl) + ' Not recaling for this order'
                      'Consider decreasing min_overlap_frac = {:5.3f}'.format(min_overlap_frac))
            fluxes_out = fluxes_in[iord_scl-1,:]
            sigs_out = sigs_in[iord_scl-1,:]



def merge_order(spectra, wave_grid, spectrograph, files, extract='OPT', orderscale='median', niter=5, sigrej_final=3., SN_MIN_MEDSCALE = 5.0,
                overlapfrac = 0.01, num_min_pixels=10,phot_scale_dicts=None, qafile=None, outfile=None, debug=False):
    """
        routines for merging orders of echelle spectra.
    parameters:
        spectra: spectra in the format of Xspectrum1D
        wave_grid (numpy array): The final wavelength grid for the output. Note that the wave_grid should be
          consistent with your spectra, if not please rebin it first.
        extract (str): 'OPT' or 'BOX'
        orderscale (str): which method you want use to scale different orders: median, photometry or None.
        niter (int): number of iteration for rejections in the overlapped part
        sigrej_final (float): sigma for rejections in the overlapped part
        SN_MIN_MEDSCALE (float): minimum SNR for scaling different orders
        overlapfrac (float): minimum overlap fraction for scaling different orders.
        phot_scale_dicts (dict): A dict contains photometric information of each order. An example is given below.
          phot_scale_dicts = {'0': {'filter': None, 'mag': None, 'mag_type': None, 'masks': None},
                              '1': {'filter': 'UKIRT-Y', 'mag': 20.33, 'mag_type': 'AB', 'masks': None},
                              '2': {'filter': 'UKIRT-J', 'mag': 20.19, 'mag_type': 'AB', 'masks': None},
                              '3': {'filter': 'UKIRT-H', 'mag': 20.02, 'mag_type': 'AB', 'masks': None},
                              '4': {'filter': 'UKIRT-K', 'mag': 19.92, 'mag_type': 'AB', 'masks': None}}
        qafile (str): name of qafile
        outfile (str): name of file to be saved for your final spectrum
        debug (bool): show debug plots?
    returns:
        spec1d: XSpectrum1D after order merging.
    """

    ## Scaling different orders
    orderscale = 'None' #### BECAUSE IT'S CURRENTLY BROKEN
    if orderscale == 'photometry':
        # Only tested on NIRES.
        if phot_scale_dicts is not None:
            spectra = order_phot_scale(spectra, phot_scale_dicts, debug=debug)
        else:
            msgs.warn('No photometric information is provided. Will use median scale.')
            orderscale = 'median'
    if orderscale == 'median':
        rmask = spectra.data['sig'].filled(0.) > 0.
        # sn2, weights = coadd.sn_weights(fluxes, sigs, rmask, wave)
        ## scaling different orders
        #norder = spectra.nspec
        fluxes, sigs, wave = unpack_spec(spectra, all_wave=False)
        fluxes_scale, sigs_scale = order_median_scale(wave, rmask, fluxes, sigs, nsig=sigrej_final, niter=niter,
                                                      overlapfrac=overlapfrac, num_min_pixels=num_min_pixels,
                                                      SN_MIN_MEDSCALE=SN_MIN_MEDSCALE, debug=debug)
        spectra = spec_from_array(wave*units.AA, fluxes_scale, sigs_scale)

    if orderscale not in ['photometry', 'median']:
        msgs.warn('No any scaling is performed between different orders.')

    ## Megering orders
    msgs.info('Merging different orders')
    fluxes, sigs, wave = unpack_spec(spectra, all_wave=True)
    ## ToDo: Joe claimed not to use pixel depedent weighting.
    weights = 1.0 / sigs ** 2
    weights[~np.isfinite(weights)] = 0.0
    weight_combine = np.sum(weights, axis=0)
    weight_norm = weights / weight_combine
    weight_norm[np.isnan(weight_norm)] = 1.0
    flux_final = np.sum(fluxes * weight_norm, axis=0)
    sig_final = np.sqrt(np.sum((weight_norm * sigs) ** 2, axis=0))

    # Keywords for Table
    rsp_kwargs = {}
    rsp_kwargs['wave_tag'] = '{:s}_WAVE'.format(extract)
    rsp_kwargs['flux_tag'] = '{:s}_FLAM'.format(extract)
    rsp_kwargs['sig_tag'] = '{:s}_FLAM_SIG'.format(extract)

    spec1d_final = spec_from_array(wave_grid * units.AA, flux_final, sig_final, **rsp_kwargs)

    if outfile is not None:
        msgs.info('Saving the final calibrated spectrum as {:s}'.format(outfile))
        write_to_disk(spectrograph, files, spec1d_final, outfile)

    if (qafile is not None) or (debug):
        # plot and save qa
        plt.figure(figsize=(12, 6))
        ax1 = plt.axes([0.07, 0.13, 0.9, 0.4])
        ax2 = plt.axes([0.07, 0.55, 0.9, 0.4])
        plt.setp(ax2.get_xticklabels(), visible=False)

        medf = np.median(spec1d_final.flux)
        ylim = (np.sort([0. - 0.3 * medf, 5 * medf]))
        cmap = plt.get_cmap('RdYlBu_r')
        for idx in range(spectra.nspec):
            spectra.select = idx
            color = cmap(float(idx) / spectra.nspec)
            ind_good = spectra.sig > 0
            ax1.plot(spectra.wavelength[ind_good], spectra.flux[ind_good], color=color)

        if (np.max(spec1d_final.wavelength) > (9000.0 * units.AA)):
            skytrans_file = resource_filename('pypeit', '/data/skisim/atm_transmission_secz1.5_1.6mm.dat')
            skycat = np.genfromtxt(skytrans_file, dtype='float')
            scale = 0.85 * ylim[1]
            ax2.plot(skycat[:, 0] * 1e4, skycat[:, 1] * scale, 'm-', alpha=0.5)

        ax2.plot(spec1d_final.wavelength, spec1d_final.sig, ls='steps-', color='0.7')
        ax2.plot(spec1d_final.wavelength, spec1d_final.flux, ls='steps-', color='b')

        ax1.set_xlim([np.min(spec1d_final.wavelength.value), np.max(spec1d_final.wavelength.value)])
        ax2.set_xlim([np.min(spec1d_final.wavelength.value), np.max(spec1d_final.wavelength.value)])
        ax1.set_ylim(ylim)
        ax2.set_ylim(ylim)
        ax1.set_xlabel('Wavelength (Angstrom)')
        ax1.set_ylabel('Flux')
        ax2.set_ylabel('Flux')

        plt.tight_layout(pad=0.2, h_pad=0., w_pad=0.2)

        if len(qafile.split('.')) == 1:
            msgs.info("No fomat given for the qafile, save to PDF format.")
            qafile = qafile + '.pdf'
        if qafile:
            plt.savefig(qafile)
            msgs.info("Wrote coadd QA: {:s}".format(qafile))
        if debug:
            plt.show()
        plt.close()

        ### Do NOT remove this part althoug it is deprecated.
        # we may need back to using this pieces of code after fixing the coadd_spectra problem on first order.
        # kwargs['echelle'] = True
        # kwargs['wave_grid_min'] = np.min(wave_grid)
        # kwargs['wave_grid_max'] = np.max(wave_grid)
        # spec1d_final = coadd_spectra(spectra, wave_grid_method=wave_grid_method, niter=niter,
        #                                  scale_method=scale_method, do_offset=do_offset, sigrej_final=sigrej_final,
        #                                  do_var_corr=do_var_corr, qafile=qafile, outfile=outfile,
        #                                  do_cr=do_cr, debug=debug, **kwargs)
    return spec1d_final

def ech_coadd(files,spectrograph,objids=None,extract='OPT',flux=True,giantcoadd=False,orderscale='median',mergeorder=True,
              wave_grid_method='velocity', niter=5,wave_grid_min=None, wave_grid_max=None,v_pix=None,
              scale_method='auto', do_offset=False, sigrej_final=3.,do_var_corr=False,
              SN_MIN_MEDSCALE = 5.0, overlapfrac = 0.01, num_min_pixels=10,phot_scale_dicts=None,
              qafile=None, outfile=None,do_cr=True, debug=False,**kwargs):
    """
    routines for coadding spectra observed with echelle spectrograph.
    parameters:
        files (list): file names
        objids (str): objid
        extract (str): 'OPT' or 'BOX'
        flux (bool): fluxed or not
        giantcoadd (bool): coadding order by order or do it at once?
        wave_grid_method (str): default velocity
        niter (int): number of iteration for rejections
        wave_grid_min (float): min wavelength, None means it will find the min value from your spectra
        wave_grid_max (float): max wavelength, None means it will find the max value from your spectra
        v_pix (float): delta velocity, see coadd.py
        scale_method (str): see coadd.py
        do_offset (str): see coadd.py, not implemented yet.
        sigrej_final (float): see coadd.py
        do_var_corr (bool): see coadd.py, default False. It seems True will results in a large error
        SN_MIN_MEDSCALE (float): minimum SNR for scaling different orders
        overlapfrac (float): minimum overlap fraction for scaling different orders.
        qafile (str): name of qafile
        outfile (str): name of coadded spectrum
        do_cr (bool): remove cosmic rays?
        debug (bool): show debug plots?
        kwargs: see coadd.py
    returns:
        spec1d: coadded XSpectrum1D
    """

    nfile = len(files)

    if nfile>1:
        msgs.info('Coadding {:} spectra.'.format(nfile))
        fname = files[0]
        ext_final = fits.getheader(fname, -1)
        nam = spectrograph.spectrograph
        if (nam=='vlt_xshooter_vis'):
            norder = ext_final['ECHORDER'] - 1 ### HOTFIX FOR XSHOOTER-VIS
        elif (nam=='vlt_xshooter_nir'):
            norder = 16
        else:
            norder = ext_final['ECHORDER'] + 1 
        msgs.info('spectrum {:s} has {:d} orders'.format(fname, norder))
        if norder <= 1:
            msgs.error('The number of orders have to be greater than one for echelle. Longslit data?')

        if giantcoadd:
            msgs.info('Coadding all orders and exposures at once')
            spectra = load.ech_load_spec(files, objid=objids, order=None, extract=extract, flux=flux)
            wave_grid = np.zeros((2, spectra.nspec))
            for i in range(spectra.nspec):
                wave_grid[0, i] = spectra[i].wvmin.value
                wave_grid[1, i] = spectra[i].wvmax.value
            ech_kwargs = {'echelle': True, 'wave_grid_min': np.min(wave_grid), 'wave_grid_max': np.max(wave_grid),
                          'v_pix': v_pix}
            kwargs.update(ech_kwargs)
            # Coadding
            spec1d_final = coadd_spectra(spectrograph, files, spectra, wave_grid_method=wave_grid_method, niter=niter,
                                   scale_method=scale_method, do_offset=do_offset, sigrej_final=sigrej_final,
                                   do_var_corr=do_var_corr, qafile=qafile, outfile=outfile,
                                   do_cr=do_cr, debug=debug, **kwargs)
            return spec1d_final
        else:
            msgs.info('Coadding individual orders.')
            spectra_list = []
            # Keywords for Table
            rsp_kwargs = {}
            rsp_kwargs['wave_tag'] = '{:s}_WAVE'.format(extract)
            rsp_kwargs['flux_tag'] = '{:s}_FLAM'.format(extract)
            rsp_kwargs['sig_tag'] = '{:s}_FLAM_SIG'.format(extract)
            # wave_grid = np.zeros((2,norder))
            for iord in range(norder):
                spectra = load.ech_load_spec(files, spectrograph, objid=objids, order=iord, extract=extract, flux=flux)
                ech_kwargs = {'echelle': False, 'wave_grid_min': spectra.wvmin.value,
                              'wave_grid_max': spectra.wvmax.value, 'v_pix': v_pix}
                # wave_grid[0,iord] = spectra.wvmin.value
                # wave_grid[1,iord] = spectra.wvmax.value
                kwargs.update(ech_kwargs)
                # Coadding the individual orders

                if qafile is not None:
                    if len(qafile.split('.')) == 1:
                        msgs.info("No fomat given for the qafile, save to PDF format.")
                        qafile_iord = qafile + '.pdf'
                    else:
                        qafile_iord = qafile.split('.')[0] + '_ORDER{:04d}.'.format(iord) + qafile.split('.')[1]
                else:
                    qafile_iord = None
                spec1d_iord = coadd_spectra(spectrograph, files, spectra, wave_grid_method=wave_grid_method, niter=niter,
                                            scale_method=scale_method, do_offset=do_offset, sigrej_final=sigrej_final,
                                            do_var_corr=do_var_corr, qafile=qafile_iord, outfile=None, 
                                            do_cr=do_cr, debug=debug, **kwargs)
                spectrum = spec_from_array(spec1d_iord.wavelength, spec1d_iord.flux, spec1d_iord.sig, **rsp_kwargs)
                spectra_list.append(spectrum)

            spectra_coadd = collate(spectra_list)

        # Final wavelength array
        kwargs['wave_grid_min'] = np.min(spectra_coadd.data['wave'][spectra_coadd.data['wave'] > 0])
        kwargs['wave_grid_max'] = np.max(spectra_coadd.data['wave'][spectra_coadd.data['wave'] > 0])
        wave_grid = new_wave_grid(spectra_coadd.data['wave'], wave_method=wave_grid_method, **kwargs)
        # The rebin function in linetools can not work on collated spectra (i.e. filled 0).
        # Thus I have to rebin the spectra first and then collate again.
        spectra_list_new = []
        for i in range(spectra_coadd.nspec):
            speci = spectra_list[i].rebin(wave_grid * units.AA, all=True, do_sig=True, grow_bad_sig=True,
                                          masking='none')
            spectra_list_new.append(speci)
        spectra_coadd_rebin = collate(spectra_list_new)

        if mergeorder:
            spec1d_final = merge_order(spectra_coadd_rebin, wave_grid, spectrograph, files, extract=extract, orderscale=orderscale,
                                       niter=niter, sigrej_final=sigrej_final, SN_MIN_MEDSCALE=SN_MIN_MEDSCALE,
                                       overlapfrac=overlapfrac, num_min_pixels=num_min_pixels,
                                       phot_scale_dicts=phot_scale_dicts, qafile=qafile, outfile=outfile, debug=debug)
            return spec1d_final
        else:
            msgs.warn('Skipped merging orders')
            if outfile is not None:
                for iord in range(len(spectra_list)):
                    outfile_iord = outfile.replace('.fits', '_ORDER{:04d}.fits'.format(iord))
                    msgs.info('Saving the final calibrated spectrum of order {:d} as {:s}'.format(iord, outfile))
                    spectra_list[iord].write_to_fits(outfile_iord)
            return spectra_list

    elif nfile==1:
        msgs.info('Only find one spectrum. Thus only order merging will be performed.')
        spectra = load.ech_load_spec(files, objid=objids, order=None, extract=extract, flux=flux)

        # Get wave_grid
        norder = spectra.nspec
        flux, sig, wave = unpack_spec(spectra, all_wave=True)
        dloglam = np.median(np.log10(wave[0,1:])-np.log10(wave[0,:-1]))
        wave_grid_max = np.max(wave)
        wave_grid_min = np.min(wave)
        loglam_grid = wvutils.wavegrid(np.log10(wave_grid_min), np.log10(wave_grid_max)+dloglam, dloglam)
        wave_grid = 10**loglam_grid

        # populate spectra to the full wavelength grid
        # Keywords for Table
        rsp_kwargs = {}
        rsp_kwargs['wave_tag'] = '{:s}_WAVE'.format(extract)
        rsp_kwargs['flux_tag'] = '{:s}_FLAM'.format(extract)
        rsp_kwargs['sig_tag'] = '{:s}_FLAM_SIG'.format(extract)

        spectra_list = []
        for iord in range(norder):
            wave_iord = wave[iord,:]
            flux_iord = flux[iord,:]
            sig_iord = sig[iord,:]
            ind_lower = np.argmin(np.abs(wave_grid - wave_iord.min()))
            ind_upper = np.argmin(np.abs(wave_grid - wave_iord.max())) + 1
            flux_iord_full = np.zeros(len(wave_grid))
            flux_iord_full[ind_lower:ind_upper] = flux_iord
            sig_iord_full = np.zeros(len(wave_grid))
            sig_iord_full[ind_lower:ind_upper] = sig_iord

            spectrum = spec_from_array(wave_grid* units.AA, flux_iord_full, sig_iord_full, **rsp_kwargs)
            spectra_list.append(spectrum)
        spectra_coadd = collate(spectra_list)

        # Merge orders
        spec1d_final = merge_order(spectra_coadd, wave_grid, extract=extract, orderscale=orderscale,
                                   niter=niter, sigrej_final=sigrej_final, SN_MIN_MEDSCALE=SN_MIN_MEDSCALE,
                                   overlapfrac=overlapfrac, num_min_pixels=num_min_pixels,
                                   phot_scale_dicts=phot_scale_dicts, qafile=qafile, outfile=outfile, debug=debug)
    else:
        msgs.error('No spectrum is found.')
