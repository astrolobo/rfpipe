from __future__ import print_function, division, absolute_import, unicode_literals
from builtins import bytes, dict, object, range, map, input#, str
from future.utils import itervalues, viewitems, iteritems, listvalues, listitems
from io import open

import numpy as np
from numba import jit, guvectorize, int64
import pyfftw
from rfpipe import util, candidates, source
import scipy.stats

import logging
logger = logging.getLogger(__name__)

try:
    import rfgpu
except ImportError:
    pass


def dedisperse(data, delay, parallel=False):
    """ Shift data in time (axis=0) by channel-dependent value given in
    delay. Returns new array with time length shortened by max delay in
    integrations. wraps _dedisperse to add logging.
    Can set mode to "single" or "multi" to use different functions.
    """

    if not np.any(data):
        return np.array([])

    logger.info('Dedispersing up to delay shift of {0} integrations'
                .format(delay.max()))

    nint, nbl, nchan, npol = data.shape
    newsh = (nint-delay.max(), nbl, nchan, npol)
    if parallel:
        data = data.copy()
        _ = _dedisperse_gu(np.swapaxes(data, 0, 1), delay)
        return data[0:len(data)-delay.max()]
    else:
        result = np.zeros(shape=newsh, dtype=data.dtype)
        _dedisperse_jit(np.require(data, requirements='W'), delay, result)
        return result


@jit(nogil=True, nopython=True)
def _dedisperse_jit(data, delay, result):

    nint, nbl, nchan, npol = data.shape
    for k in range(nchan):
        for i in range(nint-delay.max()):
            iprime = i + delay[k]
            for l in range(npol):
                for j in range(nbl):
                    result[i, j, k, l] = data[iprime, j, k, l]


@guvectorize([str("void(complex64[:,:,:], int64[:])")], str("(n,m,l),(m)"),
             target='parallel', nopython=True)
def _dedisperse_gu(data, delay):
    b""" Multicore dedispersion via numpy broadcasting.
    Requires that data be in axis order (nbl, nint, nchan, npol), so typical
    input visibility array must have view from "np.swapaxis(data, 0, 1)".
    """

    if delay.max() > 0:
        for i in range(data.shape[0]-delay.max()):
            for j in range(data.shape[1]):
                iprime = i + delay[j]
                for k in range(data.shape[2]):
                    data[i, j, k] = data[iprime, j, k]


def resample(data, dt, parallel=False):
    """ Resample (integrate) by factor dt and return new data structure
    wraps _resample to add logging.
    Can set mode to "single" or "multi" to use different functions.
    """

    if not np.any(data):
        return np.array([])

    len0 = data.shape[0]
    logger.info('Resampling data of length {0} by a factor of {1}'
                .format(len0, dt))

    nint, nbl, nchan, npol = data.shape
    newsh = (int64(nint//dt), nbl, nchan, npol)

    if parallel:
        data = data.copy()
        _ = _resample_gu(np.swapaxes(data, 0, 3), dt)
        return data[:len0//dt]
    else:
        result = np.zeros(shape=newsh, dtype=data.dtype)
        _resample_jit(np.require(data, requirements='W'), dt, result)
        return result


@jit(nogil=True, nopython=True)
def _resample_jit(data, dt, result):

    nint, nbl, nchan, npol = data.shape
    for j in range(nbl):
        for k in range(nchan):
            for l in range(npol):
                for i in range(int64(nint//dt)):
                    iprime = int64(i*dt)
                    result[i, j, k, l] = data[iprime, j, k, l]
                    for r in range(1, dt):
                        result[i, j, k, l] += data[iprime+r, j, k, l]
                    result[i, j, k, l] = result[i, j, k, l]/dt


@guvectorize([str("void(complex64[:], int64)")], str("(n),()"),
             target="parallel", nopython=True)
def _resample_gu(data, dt):
    b""" Multicore resampling via numpy broadcasting.
    Requires that data be in nint axisto be last, so input
    visibility array must have view from "np.swapaxis(data, 0, 3)".
    *modifies original memory space* (unlike _resample_jit)
    """

    if dt > 1:
        for i in range(data.shape[0]//dt):
            iprime = int64(i*dt)
            data[i] = data[iprime]
            for r in range(1, dt):
                data[i] += data[iprime+r]
            data[i] = data[i]/dt


def dedisperseresample(data, delay, dt, parallel=False):
    """ Dedisperse and resample in single function.
    parallel controls use of multicore versions of algorithms.
    """

    if not np.any(data):
        return np.array([])

    logger.info('Correcting by delay/resampling {0}/{1} ints in {2} mode'
                .format(delay.max(), dt, ['single', 'parallel'][parallel]))

    nint, nbl, nchan, npol = data.shape
    newsh = (int64(nint-delay.max())//dt, nbl, nchan, npol)

    if parallel:
        data = data.copy()
        _ = _dedisperseresample_gu(np.swapaxes(data, 0, 1),
                                   delay, dt)
        return data[0:(len(data)-delay.max())//dt]
    else:
        result = np.zeros(shape=newsh, dtype=data.dtype)
        _dedisperseresample_jit(data, delay, dt, result)
        return result


@jit(nogil=True, nopython=True)
def _dedisperseresample_jit(data, delay, dt, result):

    nint, nbl, nchan, npol = data.shape
    nintout = int64(len(result))

    for j in range(nbl):
        for l in range(npol):
            for k in range(nchan):
                for i in range(nintout):
                    weight = int64(0)
                    for r in range(dt):
                        iprime = int64(i*dt + delay[k] + r)
                        val = data[iprime, j, k, l]
                        result[i, j, k, l] += val
                        if val != 0j:
                            weight += 1

                    if weight > 0:
                        result[i, j, k, l] = result[i, j, k, l]/weight
                    else:
                        result[i, j, k, l] = weight

    return result


@guvectorize([str("void(complex64[:,:,:], int64[:], int64)")],
             str("(n,m,l),(m),()"), target="parallel", nopython=True)
def _dedisperseresample_gu(data, delay, dt):

    if delay.max() > 0 or dt > 1:
        nint, nchan, npol = data.shape
        for l in range(npol):
            for k in range(nchan):
                for i in range((nint-delay.max())//dt):
                    weight = int64(0)
                    for r in range(dt):
                        iprime = int64(i*dt + delay[k] + r)
                        val = data[iprime, k, l]
                        if r == 0:
                            data[i, k, l] = val
                        else:
                            data[i, k, l] += val
                        if val != 0j:
                            weight += 1
                    if weight > 0:
                        data[i, k, l] = data[i, k, l]/weight
                    else:
                        data[i, k, l] = weight


#
# searching, imaging, thresholding
#

def prep_and_search(st, segment, data):
    """ Bundles prep and search functions to improve performance in distributed.
    """

    data = source.data_prep(st, segment, data)

    if st.prefs.fftmode == "cuda":
        candcollection = dedisperse_image_cuda(st, segment, data)
    elif st.prefs.fftmode == "fftw":
        candcollection = dedisperse_image_fftw(st, segment, data)
    else:
        logger.warn("fftmode {0} not recognized (cuda, fftw allowed)"
                    .format(st.prefs.fftmode))

    candidates.save_cands(st, candcollection)

    return candcollection


def dedisperse_image_cuda(st, segment, data, devicenum=None):
    """ Run dedispersion, resample for all dm and dt.
    Grid and image on GPU.
    rfgpu is built from separate repo.
    Uses state to define integrations to image based on segment, dm, and dt.
    devicenum can force the gpu to use, but can be inferred via distributed.
    """

    assert st.dtarr[0] == 1, "st.dtarr[0] assumed to be 1"
    assert all([st.dtarr[dtind]*2 == st.dtarr[dtind+1]
                for dtind in range(len(st.dtarr)-1)]), ("dtarr must increase "
                                                        "by factors of 2")

    if not np.any(data):
        logger.info("Data is all zeros. Skipping search.")
        return candidates.CandCollection(prefs=st.prefs,
                                         metadata=st.metadata)

    if devicenum is None:
        # assume first gpu, but try to infer from worker name
        devicenum = 0
        try:
            from distributed import get_worker
            name = get_worker().name
            devicenum = int(name.split('g')[1])
            logger.debug("Using name {0} to set GPU devicenum to {1}"
                         .format(name, devicenum))
        except IndexError:
            logger.warn("Could not parse worker name {0}. Using default GPU devicenum {1}"
                        .format(name, devicenum))
        except ValueError:
            logger.warn("No worker found. Using default GPU devicenum {0}"
                        .format(devicenum))
        except ImportError:
            logger.warn("distributed not available. Using default GPU devicenum {0}"
                        .format(devicenum))

    candcollection = candidates.CandCollection(prefs=st.prefs,
                                               metadata=st.metadata)

    rfgpu.cudaSetDevice(devicenum)

    bytespercd = 8*(st.npixx*st.npixy + st.prefs.timewindow*st.nchan*st.npol)

    beamnum = 0
    uvw = util.get_uvw_segment(st, segment)

    upix = st.npixx
    vpix = st.npixy//2 + 1

    grid = rfgpu.Grid(st.nbl, st.nchan, st.readints, upix, vpix)
    image = rfgpu.Image(st.npixx, st.npixy)
    image.add_stat('rms')
    image.add_stat('max')

    # Data buffers on GPU
    vis_raw = rfgpu.GPUArrayComplex((st.nbl, st.nchan, st.readints))
    vis_grid = rfgpu.GPUArrayComplex((upix, vpix))
    img_grid = rfgpu.GPUArrayReal((st.npixx, st.npixy))

    # Convert uv from lambda to us
    u, v, w = uvw
    u_us = 1e6*u[:, 0]/(1e9*st.freq[0])
    v_us = 1e6*v[:, 0]/(1e9*st.freq[0])

    # Q: set input units to be uv (lambda), freq in GHz?
    grid.set_uv(u_us, v_us)  # u, v in us
    grid.set_freq(st.freq*1e3)  # freq in MHz
    grid.set_cell(st.uvres)  # uv cell size in wavelengths (== 1/FoV(radians))

    # Compute gridding transform
    grid.compute()

    # move Stokes I data in (assumes dual pol data)
    vis_raw.data[:] = np.rollaxis(data.mean(axis=3), 0, 3)
    vis_raw.h2d()  # Send it to GPU memory

    grid.conjugate(vis_raw)

    canddatalist = []
    for dtind in range(len(st.dtarr)):
        for dmind in range(len(st.dmarr)):
            delay = util.calc_delay(st.freq, st.freq.max(), st.dmarr[dmind],
                                    st.inttime)

            if dtind > 0:
                grid.downsample(vis_raw)

            grid.set_shift(delay >> dtind)  # dispersion shift per chan in samples

            integrations = st.get_search_ints(segment, dmind, dtind)
            if len(integrations) == 0:
                continue
            minint = min(integrations)
            maxint = max(integrations)

            logger.info('Imaging {0} ints ({1}-{2}) in seg {3} at DM/dt {4:.1f}/{5}'
                        ' with image {6}x{7} (uvres {8}) with gpu {9}'
                        .format(len(integrations), minint, maxint, segment,
                                st.dmarr[dmind], st.dtarr[dtind], st.npixx,
                                st.npixy, st.uvres, devicenum))

            for i in integrations:
                # grid and FFT
                grid.operate(vis_raw, vis_grid, i)
                image.operate(vis_grid, img_grid)

                # calc snr
                stats = image.stats(img_grid)
                if stats['rms'] != 0.:
                    peak_snr = stats['max']/stats['rms']
                else:
                    peak_snr = 0.

                # threshold image on GPU and optionally save it
                if peak_snr > st.sigma_image1:
                    img_grid.d2h()
                    img_data = np.fft.fftshift(img_grid.data)  # shift zero pixel in middle
                    l, m = st.pixtolm(np.where(img_data == img_data.max()))
                    candloc = (segment, i, dmind, dtind, beamnum)

                    logger.info("Got one! SNR {0:.1f} candidate at {1} and (l,m) = ({2},{3})"
                                .format(peak_snr, candloc, l, m))

                    data_corr = dedisperseresample(data, delay,
                                                   st.dtarr[dtind],
                                                   parallel=st.prefs.nthread > 1)
                    data_corr = data_corr[max(0, i-st.prefs.timewindow//2):
                                          min(i+st.prefs.timewindow//2,
                                          len(data))]
                    util.phase_shift(data_corr, uvw, l, m)
                    data_corr = data_corr.mean(axis=1)
                    canddatalist.append(candidates.CandData(state=st,
                                                            loc=candloc,
                                                            image=img_data,
                                                            data=data_corr))

                    # TODO: add safety against triggering return of all images
                    if len(canddatalist)*bytespercd/1000**3 > st.prefs.memory_limit:
                        logger.info("Accumulated CandData size exceeds "
                                    "memory limit of {0:.1f}. "
                                    "Running calc_features..."
                                    .format(st.prefs.memory_limit))
                        candcollection += candidates.calc_features(canddatalist)
                        canddatalist = []

    candcollection += candidates.calc_features(canddatalist)

    logger.info("{0} candidates returned for seg {1}"
                .format(len(candcollection), segment))

    return candcollection


def dedisperse_image_fftw(st, segment, data, wisdom=None, integrations=None):
    """ Fuse the dediserpse, resample, search, threshold functions.
    """

    candcollection = candidates.CandCollection(prefs=st.prefs,
                                               metadata=st.metadata)
    for dtind in range(len(st.dtarr)):
        for dmind in range(len(st.dmarr)):
            delay = util.calc_delay(st.freq, st.freq.max(), st.dmarr[dmind],
                                    st.inttime)

            data_corr = dedisperseresample(data, delay, st.dtarr[dtind],
                                           parallel=st.prefs.nthread > 1)

            candcollection += search_thresh_fftw(st, segment, data_corr, dmind,
                                                 dtind, wisdom=wisdom,
                                                 integrations=integrations)

    logger.info("{0} candidates returned for seg {1}"
                .format(len(candcollection), segment))

    return candcollection


def search_thresh_fftw(st, segment, data, dmind, dtind, integrations=None,
                       beamnum=0, wisdom=None):
    """ Take dedispersed, resampled data, image with fftw and threshold.
    Returns list of CandData objects that define candidates with
    candloc, image, and phased visibility data.
    Integrations can define subset of all available in data to search.
    Default will take integrations not searched in neighboring segments.

    ** only supports threshold > image max (no min)
    ** dmind, dtind, beamnum assumed to represent current state of data
    """

    candcollection = candidates.CandCollection(prefs=st.prefs,
                                               metadata=st.metadata)

    if not np.any(data):
        logger.info("Data is all zeros. Skipping search.")
        return candcollection

    bytespercd = 8*(st.npixx*st.npixy + st.prefs.timewindow*st.nchan*st.npol)

    # assumes dedispersed/resampled data has only back end trimmed off
    if integrations is None:
        integrations = st.get_search_ints(segment, dmind, dtind)
    elif isinstance(integrations, int):
        integrations = [integrations]

    assert isinstance(integrations, list), ("integrations should be int, list "
                                            "of ints, or None.")
    if len(integrations) == 0:
        return candcollection
    minint = min(integrations)
    maxint = max(integrations)

    # some prep if kalman filter is to be applied
    if st.prefs.searchtype in ['image1k', 'imagearmk']:
        # TODO: check that this is ok if pointing at bright source
        offints = np.random.choice(len(data), max(10, len(data)//10),
                                   replace=False)
        spec_std = data.real.mean(axis=1).mean(axis=2).take(offints, axis=0).std(axis=0)
        sig_ts, kalman_coeffs = kalman_prepare_coeffs(spec_std)

    # TODO: add check that manually set integrations are safe for given dt

    logger.info('{0} search of {1} ints ({2}-{3}) in seg {4} at DM/dt {5:.1f}/{6} with '
                'image {7}x{8} (uvres {9}) with fftw'
                .format(st.prefs.searchtype, len(integrations), minint, maxint,
                        segment, st.dmarr[dmind], st.dtarr[dtind], st.npixx,
                        st.npixy, st.uvres))

    uvw = util.get_uvw_segment(st, segment)

    if st.prefs.searchtype in ['image1', 'image1k']:
        images = grid_image(data, uvw, st.npixx, st.npixy, st.uvres,
                            'fftw', st.prefs.nthread, wisdom=wisdom,
                            integrations=integrations)

        canddatalist = []
        for i, image in enumerate(images):
            candloc = (segment, integrations[i], dmind, dtind, beamnum)
            peak_snr = image.max()/util.madtostd(image)
            if peak_snr > st.sigma_image1:
                l, m = st.pixtolm(np.where(image == image.max()))

                # if set, use sigma_kalman as second stage filter
                if st.prefs.searchtype == 'image1k':
                    spec = data.take([integrations[i]], axis=0)
                    util.phase_shift(spec, uvw, l, m)
                    spec = spec[0].real.mean(axis=2).mean(axis=0)
                    significance_kalman = kalman_significance(spec, spec_std,
                                                              sig_ts=sig_ts,
                                                              coeffs=kalman_coeffs)
                    significance_image = -scipy.stats.norm.logsf(peak_snr)
                    total_snr = np.sqrt(2*(significance_kalman + significance_image))
                    if total_snr > st.prefs.sigma_kalman:
                        logger.info("Got one! SNR1 {0:.1f} and SNRk {1:.1f} candidate at {2} and (l,m) = ({3},{4})"
                                    .format(peak_snr, total_snr, candloc, l, m))
                        dataph = data[max(0, integrations[i]-st.prefs.timewindow//2):
                                      min(integrations[i]+st.prefs.timewindow//2, len(data))].copy()
                        util.phase_shift(dataph, uvw, l, m)
                        dataph = dataph.mean(axis=1)
                        canddatalist.append(candidates.CandData(state=st,
                                                                loc=candloc,
                                                                image=image,
                                                                data=dataph,
                                                                snrk=total_snr))
                elif st.prefs.searchtype == 'image1':
                    logger.info("Got one! SNR1 {0:.1f} candidate at {1} and (l, m) = ({2},{3})"
                                .format(peak_snr, candloc, l, m))
                    dataph = data[max(0, integrations[i]-st.prefs.timewindow//2):
                                  min(integrations[i]+st.prefs.timewindow//2, len(data))].copy()
                    util.phase_shift(dataph, uvw, l, m)
                    dataph = dataph.mean(axis=1)
                    canddatalist.append(candidates.CandData(state=st,
                                                            loc=candloc,
                                                            image=image,
                                                            data=dataph))
                else:
                    logger.warn("searchtype {0} not recognized"
                                .format(st.prefs.searchtype))

                if len(canddatalist)*bytespercd/1000**3 > st.prefs.memory_limit:
                    logger.info("Accumulated CandData size is {0:.1f} GB, "
                                "which exceeds memory limit of {1:.1f}. "
                                "Running calc_features..."
                                .format(len(canddatalist)*bytespercd/1000**3,
                                        st.prefs.memory_limit))
                    candcollection += candidates.calc_features(canddatalist)
                    canddatalist = []

        candcollection += candidates.calc_features(canddatalist)

    elif st.prefs.searchtype in ['imagearm', 'imagearmk']:
        arm1, arm2, arm3 = image_arms(st, data, uvw, integrations=integrations)
        # TODO: fix!
        map_arm123 = fakemap(len(arm1[0]))
        candisnr = search_thresh_arms(arm1, arm2, arm3, map_arm123,
                                      st.prefs.sigma_arm,
                                      st.prefs.sigma_arms)
        canddatalist = []
        for i, snrarm in candisnr:
            candloc = (segment, integrations[i], dmind, dtind, beamnum)
            image = grid_image(data, uvw, st.npixx, st.npixy, st.uvres,
                               'fftw', st.prefs.nthread, wisdom=wisdom,
                               integrations=integrations[i])
            peak_snr = image.max()/util.madtostd(image)
            l, m = st.pixtolm(np.where(image == image.max()))

            if peak_snr > st.sigma_image1:
                # if set, use sigma_kalman as second stage filter
                if st.prefs.searchtype == 'imagearmk':
                    spec = data.take([integrations[i]], axis=0)
                    util.phase_shift(spec, uvw, l, m)
                    spec = spec[0].real.mean(axis=2).mean(axis=0)
                    significance_kalman = kalman_significance(spec, spec_std,
                                                              sig_ts=sig_ts,
                                                              coeffs=kalman_coeffs)
                    significance_image = -scipy.stats.norm.logsf(peak_snr)
                    total_snr = np.sqrt(2*(significance_kalman + significance_image))
                    if total_snr > st.prefs.sigma_kalman:
                        logger.info("Got one! SNRarm {0:.1f} and SNR1 {1:.1f} and SNRk {2:.1f} candidate at {3} and (l,m) = ({4},{5})"
                                    .format(snrarm, peak_snr, total_snr, candloc, l, m))
                        dataph = data[max(0, integrations[i]-st.prefs.timewindow//2):
                                      min(integrations[i]+st.prefs.timewindow//2, len(data))].copy()
                        util.phase_shift(dataph, uvw, l, m)
                        dataph = dataph.mean(axis=1)
                        canddatalist.append(candidates.CandData(state=st,
                                                                loc=candloc,
                                                                image=image,
                                                                data=dataph,
                                                                snrarm=snrarm,
                                                                snrk=total_snr))
                elif st.prefs.searchtype == 'imagearm':
                    logger.info("Got one! SNRarm {0:.1f} and SNR1 {1:.1f} candidate at {2} and (l, m) = ({3},{4})"
                                .format(snrarm, peak_snr, candloc, l, m))
                    dataph = data[max(0, integrations[i]-st.prefs.timewindow//2):
                                  min(integrations[i]+st.prefs.timewindow//2, len(data))].copy()
                    util.phase_shift(dataph, uvw, l, m)
                    dataph = dataph.mean(axis=1)
                    canddatalist.append(candidates.CandData(state=st,
                                                            loc=candloc,
                                                            image=armimage,
                                                            data=dataph,
                                                            snrarm=snrarm))
                else:
                    logger.warn("searchtype {0} not recognized"
                                .format(st.prefs.searchtype))

    else:
        raise NotImplemented("only searchtype=image1 or image1k implemented")

    logger.info("{0} candidates returned for (seg, dmind, dtind) = "
                "({1}, {2}, {3})".format(len(candcollection), segment, dmind,
                                         dtind))

    return candcollection


def grid_image(data, uvw, npixx, npixy, uvres, fftmode, nthread, wisdom=None,
               integrations=None):
    """ Grid and image data.
    Optionally image integrations in list i.
    fftmode can be fftw or cuda.
    nthread is number of threads to use
    """

    if integrations is None:
        integrations = list(range(len(data)))
    elif isinstance(integrations, int):
        integrations = [integrations]

    if fftmode == 'fftw':
        logger.debug("Imaging with fftw on {0} threads".format(nthread))
        grids = grid_visibilities(data.take(integrations, axis=0), uvw, npixx,
                                  npixy, uvres, parallel=nthread > 1)
        images = image_fftw(grids, nthread=nthread, wisdom=wisdom)
    elif fftmode == 'cuda':
        logger.warn("Imaging with cuda not yet supported.")
        images = image_cuda()
    else:
        logger.warn("Imaging fftmode {0} not supported.".format(fftmode))

    return images


def image_cuda():
    """ Run grid and image with rfgpu
    TODO: update to use rfgpu
    """

    pass


def image_fftw(grids, nthread=1, wisdom=None, axes=(1, 2)):
    """ Plan pyfftw inverse fft and run it on input grids.
    Allows fft on 1d (time, npix) or 2d (time, npixx, npixy) grids.
    axes refers to dimensions of fft, so (1, 2) will do 2d fft on
    last two axes of (time, npixx, nipxy) data, while (1) will do
    1d fft on last axis of (time, npix) data.
    Returns recentered fftoutput for each integration.
    """

    if wisdom:
        logger.debug('Importing wisdom...')
        pyfftw.import_wisdom(wisdom)

    logger.debug("Starting pyfftw ifft2")
    images = np.zeros_like(grids)

#    images = pyfftw.interfaces.numpy_fft.ifft2(grids, auto_align_input=True,
#                                               auto_contiguous=True,
#                                               planner_effort='FFTW_MEASURE',
#                                               overwrite_input=True,
#                                               threads=nthread)
#    nints, npixx, npixy = images.shape
#
#   return np.fft.fftshift(images.real, (npixx//2, npixy//2))

    fft_obj = pyfftw.FFTW(grids, images, axes=axes, direction="FFTW_BACKWARD")
    fft_obj.execute()

    logger.debug('Recentering fft output...')

    return np.fft.fftshift(images.real, axes=axes)


def grid_visibilities(data, uvw, npixx, npixy, uvres, parallel=False):
    """ Grid visibilities into rounded uv coordinates """

    logger.debug('Gridding {0} ints at ({1}, {2}) pix and {3} '
                 'resolution in {4} mode.'.format(len(data), npixx, npixy,
                                                  uvres,
                                                  ['single', 'parallel'][parallel]))
    u, v, w = uvw
    grids = np.zeros(shape=(data.shape[0], npixx, npixy),
                     dtype=np.complex64)

    if parallel:
        _ = _grid_visibilities_gu(data, u, v, w, npixx, npixy, uvres, grids)
    else:
        _grid_visibilities_jit(data, u, v, w, npixx, npixy, uvres, grids)

    return grids


@jit(nogil=True, nopython=True)
def _grid_visibilities_jit(data, u, v, w, npixx, npixy, uvres, grids):
    b""" Grid visibilities into rounded uv coordinates using jit on single core.
    Rounding not working here, so minor differences with original and
    guvectorized versions.
    """

    nint, nbl, nchan, npol = data.shape

# rounding not available in numba
#    ubl = np.round(us/uvres, 0).astype(np.int32)
#    vbl = np.round(vs/uvres, 0).astype(np.int32)

    for j in range(nbl):
        for k in range(nchan):
            ubl = int64(u[j, k]/uvres)
            vbl = int64(v[j, k]/uvres)
            if (np.abs(ubl < npixx//2)) and (np.abs(vbl < npixy//2)):
                umod = int64(np.mod(ubl, npixx))
                vmod = int64(np.mod(vbl, npixy))
                for i in range(nint):
                    for l in range(npol):
                        grids[i, umod, vmod] += data[i, j, k, l]

    return grids


@guvectorize([str("void(complex64[:,:,:], float32[:,:], float32[:,:], float32[:,:], int64, int64, int64, complex64[:,:])")],
             str("(n,m,l),(n,m),(n,m),(n,m),(),(),(),(o,p)"),
             target='parallel', nopython=True)
def _grid_visibilities_gu(data, us, vs, ws, npixx, npixy, uvres, grid):
    b""" Grid visibilities into rounded uv coordinates for multiple cores"""

    ubl = np.zeros(us.shape, dtype=int64)
    vbl = np.zeros(vs.shape, dtype=int64)

    for j in range(data.shape[0]):
        for k in range(data.shape[1]):
            ubl[j, k] = int64(np.round(us[j, k]/uvres, 0))
            vbl[j, k] = int64(np.round(vs[j, k]/uvres, 0))
            if (np.abs(ubl[j, k]) < npixx//2) and \
               (np.abs(vbl[j, k]) < npixy//2):
                u = np.mod(ubl[j, k], npixx)
                v = np.mod(vbl[j, k], npixy)
                for l in range(data.shape[2]):
                    grid[u, v] += data[j, k, l]


def fakemap(npix):
    return np.random.normal(0, 1, size=(npix, npix))


@jit(nopython=True)
def search_thresh_arms(arm1, arm2, arm3, map_arm123, sigma_arm, sigma_arms, stds=None):
    """
    """

    # TODO: assure stds is calculated over larger sample than 1 int
    if stds is not None:
        std_arm1, std_arm2, std_arm3 = stds
    else:
        std_arm1 = arm1.std()  # over all ints and pixels
        std_arm2 = arm2.std()
        std_arm3 = arm3.std()

    print(std_arm1, std_arm2, std_arm3)
    eta_arm1 = -scipy.stats.norm.logsf(sigma_arm)
    eta_arm2 = -scipy.stats.norm.logsf(sigma_arm)
    eta_trigger = -scipy.stats.norm.logsf(sigma_arms)

    effective_eta_trigger = eta_trigger * (std_arm1**2 + std_arm2**2 + std_arm3**2)**0.5

    indices_arr1 = np.nonzero(arm1 > eta_arm1*std_arm1)[0]
    indices_arr2 = np.nonzero(arm2 > eta_arm2*std_arm2)[0]
    candisnr = []
    for i in range(len(arm1)):
        for ind1 in indices_arr1:
            for ind2 in indices_arr2:
                ind3 = map_arm123[ind1, ind2]
                score = arm1[i, ind1]+arm2[i, ind2]+arm3[i, ind3]
                if score > effective_eta_trigger:
                    snrarm = np.sqrt(2*score)  # TODO: check on definition of score
                    candisnr.append((i, snrarm))

    return candisnr


def image_arms(st, data, uvw, integrations=None):
    """ Calculate grids for all three arms of VLA.
    """

    if integrations is None:
        integrations = list(range(len(data)))
    elif isinstance(integrations, int):
        integrations = [integrations]

    # TODO: calculate npix properly
    npix = max(st.npixx, st.npixy)

    # TODO: check if there is a center ant that can be counted in all arms
    ind_narm = np.where(np.all(st.blarr_arms == 'N', axis=1))[0]
    grids_narm = grid_arm(data.take(integrations, axis=0), uvw, ind_narm, npix,
                          st.uvres)
    images_narm = image_fftw(grids_narm, axes=(1,))

    ind_earm = np.where(np.all(st.blarr_arms == 'E', axis=1))[0]
    grids_earm = grid_arm(data.take(integrations, axis=0), uvw, ind_earm, npix,
                          st.uvres)
    images_earm = image_fftw(grids_earm, axes=(1,))

    ind_warm = np.where(np.all(st.blarr_arms == 'W', axis=1))[0]
    grids_warm = grid_arm(data.take(integrations, axis=0), uvw, ind_warm, npix,
                          st.uvres)
    images_warm = image_fftw(grids_warm, axes=(1,))

    return images_narm, images_earm, images_warm


def grid_arm(data, uvw, arminds, npix, uvres):
    """ Grids visibilities along 1d arms of array.
    arminds defines a subset of baselines that for a linear array.
    Returns FFT output (time vs pixel) from gridded 1d visibilities.
    """

    u, v, w = uvw
    # TODO: check colinearity, "w", and definition of uv distance
    uvd = np.sqrt(u.take(arminds, axis=0)**2 + v.take(arminds, axis=0)**2)

    grids = np.zeros(shape=(data.shape[0], npix), dtype=np.complex64)
    grid_visibilities_arm_jit(data.take(arminds, axis=1), uvd, npix,
                              uvres, grids)

    return grids


@jit(nopython=True)
def change_table_indices(table, indices_out):
    """
    changes a table that takes T[i,j] = k and returns
    T[i,k] = j if indices_out == (1,3) or
    T[j,k] = i if indices_out == (2,3) or
    :param table:
    :param indices_out: (either (1,3) or (2,3))
    :return:
    """

    output_table = np.zeros_like(table)
    for i in range(output_table.shape[0]):
        for j in range(output_table.shape[1]):
            if indices_out == (1, 3):
                output_table[i][table[i, j]] = j
            if indices_out == (2, 3):
                output_table[j][table[i, j]] = i
    return output_table


@jit(nogil=True, nopython=True)
def grid_visibilities_arm_jit(data, uvd, npix, uvres, grids):
    b""" Grid visibilities into rounded uvd coordinates using jit on single core.
    data/uvd are selected for a single arm
    """

    nint, nbl, nchan, npol = data.shape

# rounding not available in numba
#    ubl = np.round(us/uvres, 0).astype(np.int32)
#    vbl = np.round(vs/uvres, 0).astype(np.int32)

    for j in range(nbl):
        for k in range(nchan):
            uvbl = int64(uvd[j, k]/uvres)
            if (np.abs(uvbl < npix//2)):
                uvmod = int64(np.mod(uvbl, npix))
                for i in range(nint):
                    for l in range(npol):
                        grids[i, uvmod] += data[i, j, k, l]

    return grids


def kalman_significance(spec, spec_std, sig_ts=[], coeffs=[]):
    """ Calculate kalman significance for given 1d spec and per-channel error.
    If no coeffs input, it will calculate with random number generation.
    From Barak Zackay
    """

    if not len(sig_ts):
        sig_ts = [x*np.median(spec_std) for x in [0.3, 0.1, 0.03, 0.01]]
    if not len(coeffs):
        coeffs = kalman_prepare_coeffs(spec_std, sig_ts)

    assert len(sig_ts) == len(coeffs)
    logger.debug("Calculating max Kalman significance for {0} channel spectrum"
                 .format(len(spec)))

    significances = []
    for i, sig_t in enumerate(sig_ts):
        score = kalman_filter_detector(spec, spec_std, sig_t)
        coeff = coeffs[i]
        x_coeff, const_coeff = coeff
        significances.append(x_coeff * score + const_coeff)

    return np.max(significances) * np.log(2)  # given in units of nats


@jit(nopython=True)
def kalman_filter_detector(spec, spec_std, sig_t, A_0=None, sig_0=None):
    """ Core calculation of Kalman estimator of input 1d spectrum data.
    spec/spec_std are 1d spectra in same units.
    sig_t sets the smoothness scale of model (A) change.
    Number of changes is sqrt(nchan)*sig_t/mean(spec_std).
    Frequency scale is 1/sig_t**2
    A_0/sig_0 are initial guesses of model value in first channel.
    Returns score, which is the likelihood of presence of signal.
    From Barak Zackay
    """

    if A_0 is None:
        A_0 = spec.mean()
    if sig_0 is None:
        sig_0 = np.median(spec_std)

    spec = spec - np.mean(spec)  # likelihood calc expects zero mean spec

    cur_mu, cur_state_v = A_0, sig_0**2
    cur_log_l = 0
    for i in range(len(spec)):
        cur_z = spec[i]
        cur_spec_v = spec_std[i]**2
        # computing consistency with the data
        cur_log_l += -(cur_z-cur_mu)**2 / (cur_state_v + cur_spec_v + sig_t**2)/2 - 0.5*np.log(2*np.pi*(cur_state_v + cur_spec_v + sig_t**2))

        # computing the best state estimate
        cur_mu = (cur_mu / cur_state_v + cur_z/cur_spec_v) / (1/cur_state_v + 1/cur_spec_v)
        cur_state_v = cur_spec_v * cur_state_v / (cur_spec_v + cur_state_v) + sig_t**2

    H_0_log_likelihood = -np.sum(spec**2 / spec_std**2 / 2) - np.sum(0.5*np.log(2*np.pi * spec_std**2))
    return cur_log_l - H_0_log_likelihood


def kalman_prepare_coeffs(data_std, sig_ts=None, n_trial=10000):
    """ Measure kalman significance distribution in random data.
    data_std is the noise vector per channel.
    sig_ts can be single float or list of values.
    returns tuple (sig_ts, coeffs)
    From Barak Zackay
    """

    if sig_ts is None:
        sig_ts = [x*np.median(data_std) for x in [0.3, 0.1, 0.03, 0.01]]
    elif not isinstance(sig_ts, list):
        sig_ts = [sig_ts]
    else:
        logger.warn("Not sure what to do with sig_ts {0}".format(sig_ts))

    logger.info("Measuring Kalman significance distribution for sig_ts {0}".format(sig_ts))

    coeffs = []
    for sig_t in sig_ts:
        nchan = len(data_std)
        random_scores = []
        for i in range(n_trial):
            normaldist = np.random.normal(0, data_std, size=nchan)
            normaldist -= normaldist.mean()
            random_scores.append(kalman_filter_detector(normaldist, data_std, sig_t))

        # Approximating the tail of the distribution as an  exponential tail (probably is justified)
        coeffs.append(np.polyfit([np.percentile(random_scores, 100 * (1 - 2 ** (-i))) for i in range(3, 10)], range(3, 10), 1))
        # TODO: check distribution out to 1/1e6

    return sig_ts, coeffs


def kalman_significance_canddata(canddata, sig_ts=[]):
    """ Go from canddata to total ignificance with kalman significance
    Calculates coefficients from data and then adds significance to image snr.
    From Barak Zackay
    """

    # TODO check how to automate candidate selection of on/off integrations
    onint = 15
    offints = list(range(0, 10))+list(range(20, 30))
    spec_std = canddata.data.real.mean(axis=2).take(offints, axis=0).std(axis=0)
    spec = canddata.data.real.mean(axis=2)[onint]

    sig_ts, coeffs = kalman_prepare_coeffs(spec_std)
    significance_kalman = kalman_significance(spec, spec_std, sig_ts=sig_ts, coeffs=coeffs)

    # TODO: better pixel std calculation needed?
    snr_image = canddata.image.max()/canddata.image.std()
    significance_image = -scipy.stats.norm.logsf(snr_image)

#   snr = scipy.stats.norm.isf(significance)
    snr = np.sqrt(2*(significance_kalman + significance_image))
    return snr


def set_wisdom(npixx, npixy):
    """ Run single 2d ifft like image to prep fftw wisdom in worker cache """

    logger.info('Calculating FFT wisdom...')
    arr = pyfftw.empty_aligned((npixx, npixy), dtype='complex64', n=16)
    fft_arr = pyfftw.interfaces.numpy_fft.ifft2(arr, auto_align_input=True,
                                                auto_contiguous=True,
                                                planner_effort='FFTW_MEASURE')
    return pyfftw.export_wisdom()
