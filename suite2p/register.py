import glob, h5py, time, os, shutil
import numpy as np
from scipy.fftpack import next_fast_len
from numpy import random as rnd
import multiprocessing
#import scipy.fftpack as fft
from numpy import fft
from numba import vectorize, complex64, float32
import math
from scipy.signal import medfilt
from scipy.ndimage import laplace
from suite2p import nonrigid, utils, regmetrics
from skimage.external.tifffile import TiffWriter
import gc
import multiprocessing
N_threads = int(multiprocessing.cpu_count() / 2)
import numexpr3 as ne3
ne3.set_nthreads(N_threads)

HAS_GPU=False
try:
    import cupy as cp
    from cupyx.scipy.fftpack import fftn, ifftn, get_fft_plan
    HAS_GPU=True
except ImportError:
    HAS_GPU=False

HAS_FFTW=False
try:
    import mkl_fft
    #print('imported mkl_fft successfully')
    HAS_MKL=True
    print(HAS_MKL)
except ImportError:
    HAS_MKL=False
    #print('failed to import mkl_fft - please see issue #182 to fix')

def fft2(data, s=None):
    if s==None:
        s=(data.shape[-2], data.shape[-1])
    if HAS_FFTW:
        x = pyfftw.empty_aligned(data.shape, dtype=np.float32)
        x[:] = data
        fft_object = pyfftw.builders.fftn(x, s=s, axes=(-2,-1),threads=2)
        data = fft_object()
    elif HAS_MKL:
        data = mkl_fft.fft2(data,shape=s,axes=(-2,-1))
    else:
        data = fft.fft2(data, s, axes=(-2,-1))
    return data

def ifft2(data, s=None):
    if s==None:
        s=(data.shape[-2], data.shape[-1])
    if HAS_FFTW:
        x = pyfftw.empty_aligned(data.shape, dtype=np.complex64)
        x[:] = data
        fft_object = pyfftw.builders.ifftn(data, s=s, axes=(-2,-1),threads=2)
        data = fft_object()
    elif HAS_MKL:
        data = mkl_fft.ifft2(data, shape=s, axes=(-2,-1))
    else:
        data = fft.ifft2(data, s, axes=(-2,-1))
    return data


def tic():
    return time.time()
def toc(i0):
    return time.time() - i0

eps0 = 1e-5
sigL = 0.85 # smoothing width for up-sampling kernels, keep it between 0.5 and 1.0...
hp = 60


def gaussian_fft(sig, Ly, Lx):
    ''' gaussian filter in the fft domain with std sig and size Ly,Lx '''
    x = np.arange(0, Lx)
    y = np.arange(0, Ly)
    x = np.abs(x - x.mean())
    y = np.abs(y - y.mean())
    xx, yy = np.meshgrid(x, y)
    hgx = np.exp(-np.square(xx/sig) / 2)
    hgy = np.exp(-np.square(yy/sig) / 2)
    hgg = hgy * hgx
    hgg /= hgg.sum()
    fhg = np.real(fft.fft2(fft.ifftshift(hgg))); # smoothing filter in Fourier domain
    return fhg

def spatial_taper(sig, Ly, Lx):
    ''' spatial taper  on edges with gaussian of std sig '''
    x = np.arange(0, Lx)
    y = np.arange(0, Ly)
    x = np.abs(x - x.mean())
    y = np.abs(y - y.mean())
    xx, yy = np.meshgrid(x, y)
    mY = y.max() - 2*sig
    mX = x.max() - 2*sig
    maskY = 1./(1.+np.exp((yy-mY)/sig))
    maskX = 1./(1.+np.exp((xx-mX)/sig))
    maskMul = maskY * maskX
    return maskMul

def spatial_smooth(data,N):
    ''' spatially smooth data using cumsum over axis=1,2 with window N'''
    pad = np.zeros((data.shape[0], int(N/2), data.shape[2]))
    dsmooth = np.concatenate((pad, data, pad), axis=1)
    pad = np.zeros((dsmooth.shape[0], dsmooth.shape[1], int(N/2)))
    dsmooth = np.concatenate((pad, dsmooth, pad), axis=2)
    # in X
    cumsum = np.cumsum(dsmooth, axis=1)
    dsmooth = (cumsum[:, N:, :] - cumsum[:, :-N, :]) / float(N)
    # in Y
    cumsum = np.cumsum(dsmooth, axis=2)
    dsmooth = (cumsum[:, :, N:] - cumsum[:, :, :-N]) / float(N)
    return dsmooth

def spatial_high_pass(data, N):
    ''' high pass filters data over axis=1,2 with window N'''
    norm = spatial_smooth(np.ones((1, data.shape[1], data.shape[2])), N).squeeze()
    data -= spatial_smooth(data, N) / norm
    return data

def one_photon_preprocess(data, ops):
    ''' pre filtering for one-photon data '''
    if ops['pre_smooth'] > 0:
        ops['pre_smooth'] = int(np.ceil(ops['pre_smooth']/2) * 2)
        data = spatial_smooth(data, ops['pre_smooth'])

    #for n in range(data.shape[0]):
    #    data[n,:,:] = laplace(data[n,:,:])
    ops['spatial_hp'] = int(np.ceil(ops['spatial_hp']/2) * 2)
    data = spatial_high_pass(data, ops['spatial_hp'])
    return data

def prepare_masks(refImg0, ops):
    refImg = refImg0.copy()
    if ops['1Preg']:
        maskSlope    = ops['spatial_taper'] # slope of taper mask at the edges
    else:
        maskSlope    = 3 * ops['smooth_sigma'] # slope of taper mask at the edges
    Ly,Lx = refImg.shape
    maskMul = spatial_taper(maskSlope, Ly, Lx)

    if ops['1Preg']:
        refImg = one_photon_preprocess(refImg[np.newaxis,:,:], ops).squeeze()
    maskOffset = refImg.mean() * (1. - maskMul);

    # reference image in fourier domain
    if ops['pad_fft']:
        cfRefImg   = np.conj(fft.fft2(refImg,
                            (next_fast_len(ops['Ly']), next_fast_len(ops['Lx']))))
    else:
        cfRefImg   = np.conj(fft.fft2(refImg))

    if ops['do_phasecorr']:
        absRef     = np.absolute(cfRefImg);
        cfRefImg   = cfRefImg / (eps0 + absRef)

    # gaussian filter in space
    fhg = gaussian_fft(ops['smooth_sigma'], cfRefImg.shape[0], cfRefImg.shape[1])
    cfRefImg *= fhg

    maskMul = maskMul.astype('float32')
    maskOffset = maskOffset.astype('float32')
    cfRefImg = cfRefImg.astype('complex64')
    cfRefImg = np.reshape(cfRefImg, (1, cfRefImg.shape[0], cfRefImg.shape[1]))
    return maskMul, maskOffset, cfRefImg

def correlation_map(X, refAndMasks, do_phasecorr):
    maskMul    = refAndMasks[0]
    maskOffset = refAndMasks[1]
    cfRefImg   = refAndMasks[2]
    #nimg, Ly, Lx = X.shape
    X = X * maskMul + maskOffset
    X = fft2(X, (cfRefImg.shape[-2], cfRefImg.shape[-1]))
    if do_phasecorr:
        X = X / (eps0 + np.absolute(X))
    X *= cfRefImg
    cc = np.real(ifft2(X))
    cc = fft.fftshift(cc, axes=(-2,-1))
    return cc

def shift_data(X, ymax, xmax, m0):
    ''' rigid shift of X by ymax and xmax '''
    ymax = ymax.flatten()
    xmax = xmax.flatten()
    if X.ndim<3:
        X = X[np.newaxis,:,:]
    nimg, Ly, Lx = X.shape
    for n in range(nimg):
        X[n] = np.roll(X[n], (-ymax[n], -xmax[n]), axis=(0,1))
        yrange = np.arange(0, Ly,1,int) + ymax[n]
        xrange = np.arange(0, Lx,1,int) + xmax[n]
        yrange = yrange[np.logical_or(yrange<0, yrange>Ly-1)] - ymax[n]
        xrange = xrange[np.logical_or(xrange<0, xrange>Lx-1)] - xmax[n]
        X[n][yrange, :] = m0
        X[n][:, xrange] = m0
    return X

@vectorize([complex64(complex64, complex64)], nopython=True, target = 'parallel')
def apply_dotnorm(Y, cfRefImg):
    return (Y*cfRefImg) / (eps0 + np.abs(Y*cfRefImg))

def phasecorr(data, refAndMasks, ops):
    ''' compute registration offsets
        uses phase correlation if ops['do_phasecorr'] '''
    nimg, Ly, Lx = data.shape
    maskMul    = refAndMasks[0]
    maskOffset = refAndMasks[1]
    cfRefImg   = refAndMasks[2].squeeze()

    # maximum registration shift allowed
    maxregshift = np.round(ops['maxregshift'] *np.maximum(Ly, Lx))
    lcorr = int(np.minimum(maxregshift, np.floor(np.minimum(Ly,Lx)/2.)))

    # preprocessing for 1P recordings
    if ops['1Preg']:
        #data = data.copy().astype(np.float32)
        X = one_photon_preprocess(data.copy().astype(np.float32), ops).astype(np.float32)
    else:
        X = data.copy().astype(np.float32)

    X *= maskMul
    X += maskOffset

    ymax, xmax, cmax = phasecorr_cpu(X, cfRefImg, lcorr)

    return ymax, xmax, cmax

def phasecorr_cpu(X, cfRefImg, lcorr):
    nimg = X.shape[0]
    ly,lx = cfRefImg.shape[-2:]
    lyhalf = int(np.floor(ly/2))
    lxhalf = int(np.floor(lx/2))

    # shifts and corrmax
    ymax = np.zeros((nimg,), np.int32)
    xmax = np.zeros((nimg,), np.int32)
    cmax = np.zeros((nimg,), np.float32)

    Y = np.zeros((X.shape[0], ly, lx), 'complex64')
    for t in np.arange(nimg):
        Y[t] = fft2(X[t], s=(ly,lx))
    Y = apply_dotnorm(Y, cfRefImg)
    for t in np.arange(nimg):
        output = np.real(ifft2(Y[t]))
        output = fft.fftshift(output, axes=(-2,-1))
        cc = output[np.ix_(np.arange(lyhalf-lcorr,lyhalf+lcorr+1,1,int),
                        np.arange(lxhalf-lcorr,lxhalf+lcorr+1,1,int))]
        ymax[t], xmax[t] = np.unravel_index(np.argmax(cc, axis=None), cc.shape)
        cmax[t] = cc[ymax[t], xmax[t]]

    ymax, xmax = ymax-lcorr, xmax-lcorr
    return ymax, xmax, cmax

def phasecorr_gpu(X, cfRefImg, lcorr):
    ''' not being used - speed ups only ~30% '''
    nimg,Ly,Lx = X.shape
    ly,lx = cfRefImg.shape[-2:]
    lyhalf = int(np.floor(ly/2))
    lxhalf = int(np.floor(lx/2))

    # put on GPU
    ref_gpu = cp.asarray(cfRefImg)
    x_gpu = cp.asarray(X)

    # phasecorrelation
    x_gpu = fftn(x_gpu, axes=(1,2), overwrite_x=True) * np.sqrt(Ly-1) * np.sqrt(Lx-1)
    for t in range(x_gpu.shape[0]):
        tmp = x_gpu[t,:,:]
        tmp = cp.multiply(tmp, ref_gpu)
        tmp = cp.divide(tmp, cp.absolute(tmp) + 1e-5)
        x_gpu[t,:,:] = tmp
    x_gpu = ifftn(x_gpu, axes=(1,2), overwrite_x=True)  * np.sqrt(Ly-1) * np.sqrt(Lx-1)
    x_gpu = cp.fft.fftshift(cp.real(x_gpu), axes=(1,2))

    # get max index
    x_gpu = x_gpu[cp.ix_(np.arange(0,nimg,1,int),
                    np.arange(lyhalf-lcorr,lyhalf+lcorr+1,1,int),
                    np.arange(lxhalf-lcorr,lxhalf+lcorr+1,1,int))]
    ix = cp.argmax(cp.reshape(x_gpu, (nimg, -1)), axis=1)
    cmax = x_gpu[np.arange(0,nimg,1,int), ix]
    ymax,xmax = cp.unravel_index(ix, (2*lcorr+1,2*lcorr+1))
    cmax = cp.asnumpy(cmax).flatten()
    ymax = cp.asnumpy(ymax)
    xmax = cp.asnumpy(xmax)
    ymax,xmax = ymax-lcorr, xmax-lcorr
    return ymax, xmax, cmax

def register_data(data, refAndMasks, ops):
    ''' register data matrix to reference image and shift '''
    ''' need reference image ops['refImg']'''
    ''' run refAndMasks = prepare_refAndMasks(ops) to get fft'ed masks '''
    ''' calls phasecorr '''
    if ops['bidiphase']!=0:
        data = shift_bidiphase(data.copy(), ops['bidiphase'])
    nr=False
    yxnr = []
    if ops['nonrigid'] and len(refAndMasks)>3:
        nb = ops['nblocks'][0] * ops['nblocks'][1]
        nr=True

    # rigid registration
    ymax, xmax, cmax = phasecorr(data, refAndMasks[:3], ops)
    Y = shift_data(data.copy(), ymax, xmax, ops['refImg'].mean())
    # non-rigid registration
    if nr:
        ymax1, xmax1, cmax1 = nonrigid.phasecorr(Y, refAndMasks[3:], ops)
        yxnr = [ymax1,xmax1,cmax1]
        Y = nonrigid.shift_data(Y, ops, ymax1, xmax1)
    return Y, ymax, xmax, cmax, yxnr

def get_nFrames(ops):
    if 'keep_movie_raw' in ops and ops['keep_movie_raw']:
        try:
            nbytes = os.path.getsize(ops['raw_file'])
        except:
            print('no raw')
            nbytes = os.path.getsize(ops['reg_file'])
    else:
        nbytes = os.path.getsize(ops['reg_file'])


    nFrames = int(nbytes/(2* ops['Ly'] *  ops['Lx']))
    return nFrames

def subsample_frames(ops, nsamps):
    ''' get nsamps frames from binary file for initial reference image'''
    nFrames = ops['nframes']
    Ly = ops['Ly']
    Lx = ops['Lx']
    frames = np.zeros((nsamps, Ly, Lx), dtype='int16')
    nbytesread = 2 * Ly * Lx
    istart = np.linspace(0, nFrames, 1+nsamps).astype('int64')
    if 'keep_movie_raw' in ops and ops['keep_movie_raw'] and 'raw_file' in ops and os.path.isfile(ops['raw_file']):
        if ops['nchannels']>1:
            if ops['functional_chan'] == ops['align_by_chan']:
                reg_file = open(ops['raw_file'], 'rb')
            else:
                reg_file = open(ops['raw_file_chan2'], 'rb')
        else:
            reg_file = open(ops['raw_file'], 'rb')
    else:
        if ops['nchannels']>1:
            if ops['functional_chan'] == ops['align_by_chan']:
                reg_file = open(ops['reg_file'], 'rb')
            else:
                reg_file = open(ops['reg_file_chan2'], 'rb')
        else:
            reg_file = open(ops['reg_file'], 'rb')
    for j in range(0,nsamps):
        reg_file.seek(nbytesread * istart[j], 0)
        buff = reg_file.read(nbytesread)
        data = np.frombuffer(buff, dtype=np.int16, offset=0)
        buff = []
        frames[j,:,:] = np.reshape(data, (Ly, Lx))
    reg_file.close()
    return frames

def get_bidiphase(frames):
    ''' computes the bidirectional phase offset
        sometimes in line scanning there will be offsets between lines
        if ops['do_bidiphase'], then bidiphase is computed and applied
    '''
    Ly = frames.shape[1]
    Lx = frames.shape[2]
    # lines scanned in 1 direction
    yr1 = np.arange(1, np.floor(Ly/2)*2, 2, int)
    # lines scanned in the other direction
    yr2 = np.arange(0, np.floor(Ly/2)*2, 2, int)

    # compute phase-correlation between lines in x-direction
    d1 = fft.fft(frames[:, yr1, :], axis=2)
    d2 = np.conj(fft.fft(frames[:, yr2, :], axis=2))
    d1 = d1 / (np.abs(d1) + eps0)
    d2 = d2 / (np.abs(d2) + eps0)

    #fhg =  gaussian_fft(1, int(np.floor(Ly/2)), Lx)
    cc = np.real(fft.ifft(d1 * d2 , axis=2))#* fhg[np.newaxis, :, :], axis=2))
    cc = cc.mean(axis=1).mean(axis=0)
    cc = fft.fftshift(cc)
    ix = np.argmax(cc[(np.arange(-10,11,1) + np.floor(Lx/2)).astype(int)])
    ix -= 10
    bidiphase = -1*ix

    return bidiphase

def shift_bidiphase(frames, bidiphase):
    ''' shift frames by bidirectional phase offset, bidiphase '''
    bidiphase = int(bidiphase)
    nt, Ly, Lx = frames.shape
    yr = np.arange(1, np.floor(Ly/2)*2, 2, int)
    ntr = np.arange(0, nt, 1, int)
    if bidiphase > 0:
        xr = np.arange(bidiphase, Lx, 1, int)
        xrout = np.arange(0, Lx-bidiphase, 1, int)
        frames[np.ix_(ntr, yr, xr)] = frames[np.ix_(ntr, yr, xrout)]
    else:
        xr = np.arange(0, bidiphase+Lx, 1, int)
        xrout = np.arange(-bidiphase, Lx, 1, int)
        frames[np.ix_(ntr, yr, xr)] = frames[np.ix_(ntr, yr, xrout)]
    return frames


def pick_init_init(ops, frames):
    nimg = frames.shape[0]
    frames = np.reshape(frames, (nimg,-1)).astype('float32')
    frames = frames - np.reshape(frames.mean(axis=1), (nimg, 1))
    cc = frames @ np.transpose(frames)
    ndiag = np.sqrt(np.diag(cc))
    cc = cc / np.outer(ndiag, ndiag)
    CCsort = -np.sort(-cc, axis = 1)
    bestCC = np.mean(CCsort[:, 1:20], axis=1);
    imax = np.argmax(bestCC)
    indsort = np.argsort(-cc[imax, :])
    refImg = np.mean(frames[indsort[0:20], :], axis = 0)
    refImg = np.reshape(refImg, (ops['Ly'], ops['Lx']))
    return refImg

def refine_init(ops, frames, refImg):
    niter = 8
    nmax  = np.minimum(100, int(frames.shape[0]/2))
    for iter in range(0,niter):
        ops['refImg'] = refImg
        maskMul, maskOffset, cfRefImg = prepare_masks(refImg, ops)
        freg, ymax, xmax, cmax, yxnr = register_data(frames, [maskMul, maskOffset, cfRefImg], ops)
        ymax = ymax.astype(np.float32)
        xmax = xmax.astype(np.float32)
        isort = np.argsort(-cmax)
        nmax = int(frames.shape[0] * (1.+iter)/(2*niter))
        refImg = freg[isort[1:nmax], :, :].mean(axis=0).squeeze()
        dy, dx = -ymax[isort[1:nmax]].mean(), -xmax[isort[1:nmax]].mean()
        # shift data requires an array of shifts
        dy = np.array([int(np.round(dy))])
        dx = np.array([int(np.round(dx))])
        refImg = shift_data(refImg, dy, dx, refImg.mean()).squeeze()
        ymax, xmax = ymax+dy, xmax+dx
    return refImg

def pick_init(ops):
    ''' compute initial reference image from ops['nimg_init'] frames '''
    Ly = ops['Ly']
    Lx = ops['Lx']
    nFrames = ops['nframes']
    nFramesInit = np.minimum(ops['nimg_init'], nFrames)
    frames = subsample_frames(ops, nFramesInit)
    if ops['do_bidiphase'] and ops['bidiphase']==0:
        ops['bidiphase'] = get_bidiphase(frames)
        print('computed bidiphase %d'%ops['bidiphase'])
    if ops['bidiphase'] != 0:
        frames = shift_bidiphase(frames.copy(), ops['bidiphase'])
    refImg = pick_init_init(ops, frames)
    refImg = refine_init(ops, frames, refImg)
    return refImg

def prepare_refAndMasks(refImg,ops):
    maskMul, maskOffset, cfRefImg = prepare_masks(refImg, ops)
    if ops['nonrigid']:
        maskMulNR, maskOffsetNR, cfRefImgNR = nonrigid.prepare_masks(refImg, ops)
        refAndMasks = [maskMul, maskOffset, cfRefImg, maskMulNR, maskOffsetNR, cfRefImgNR]
    else:
        refAndMasks = [maskMul, maskOffset, cfRefImg]
    return refAndMasks

def init_offsets(ops):
    yoff = np.zeros((0,),np.float32)
    xoff = np.zeros((0,),np.float32)
    corrXY = np.zeros((0,),np.float32)
    if ops['nonrigid']:
        nb = ops['nblocks'][0] * ops['nblocks'][1]
        yoff1 = np.zeros((0,nb),np.float32)
        xoff1 = np.zeros((0,nb),np.float32)
        corrXY1 = np.zeros((0,nb),np.float32)
        offsets = [yoff,xoff,corrXY,yoff1,xoff1,corrXY1]
    else:
        offsets = [yoff,xoff,corrXY]

    return offsets

def compute_crop(ops):
    ''' determines ops['badframes'] (using ops['th_badframes'])
        and excludes these ops['badframes'] when computing valid ranges
        from registration in y and x
    '''
    dx = ops['xoff'] - medfilt(ops['xoff'], 101)
    dy = ops['yoff'] - medfilt(ops['yoff'], 101)
    # offset in x and y (normed by mean offset)
    dxy = (dx**2 + dy**2)**.5
    dxy /= dxy.mean()
    # phase-corr of each frame with reference (normed by median phase-corr)
    cXY = ops['corrXY'] / medfilt(ops['corrXY'], 101)
    # exclude frames which have a large deviation and/or low correlation
    px = dxy / np.maximum(0, cXY)
    ops['badframes'] = np.logical_or(px > ops['th_badframes'] * 100, ops['badframes'])
    ymin = np.maximum(0, np.ceil(np.amax(ops['yoff'][np.logical_not(ops['badframes'])])))
    ymax = ops['Ly'] + np.minimum(0, np.floor(np.amin(ops['yoff'])))
    xmin = np.maximum(0, np.ceil(np.amax(ops['xoff'][np.logical_not(ops['badframes'])])))
    xmax = ops['Lx'] + np.minimum(0, np.floor(np.amin(ops['xoff'])))
    ops['yrange'] = [int(ymin), int(ymax)]
    ops['xrange'] = [int(xmin), int(xmax)]
    return ops

def write_tiffs(data, ops, k, ichan):
    if ichan==0:
        if ops['functional_chan']==ops['align_by_chan']:
            tifroot = os.path.join(ops['save_path'], 'reg_tif')
        else:
            tifroot = os.path.join(ops['save_path'], 'reg_tif_chan2')
    else:
        if ops['functional_chan']==ops['align_by_chan']:
            tifroot = os.path.join(ops['save_path'], 'reg_tif')
        else:
            tifroot = os.path.join(ops['save_path'], 'reg_tif_chan2')
    if not os.path.isdir(tifroot):
        os.makedirs(tifroot)
    fname = 'file_chan%0.3d.tif'%k
    with TiffWriter(os.path.join(tifroot, fname)) as tif:
        for i in range(data.shape[0]):
            tif.save(data[i])
    #io.imsave(, data)

def bin_paths(ops, raw):
    raw_file_align = []
    raw_file_alt = []
    reg_file_align = []
    reg_file_alt = []
    if raw:
        if ops['nchannels']>1:
            if ops['functional_chan'] == ops['align_by_chan']:
                raw_file_align = ops['raw_file']
                raw_file_alt = ops['raw_file_chan2']
                reg_file_align = ops['reg_file']
                reg_file_alt = ops['reg_file_chan2']
            else:
                raw_file_align = ops['raw_file_chan2']
                raw_file_alt = ops['raw_file']
                reg_file_align = ops['reg_file_chan2']
                reg_file_alt = ops['reg_file']
        else:
            raw_file_align = ops['raw_file']
            reg_file_align = ops['reg_file']
    else:
        if ops['nchannels']>1:
            if ops['functional_chan'] == ops['align_by_chan']:
                reg_file_align = ops['reg_file']
                reg_file_alt = ops['reg_file_chan2']
            else:
                reg_file_align = ops['reg_file_chan2']
                reg_file_alt = ops['reg_file']
        else:
            reg_file_align = ops['reg_file']
    return reg_file_align, reg_file_alt, raw_file_align, raw_file_alt

def register_binary_to_ref(ops, refImg, reg_file_align, raw_file_align):
    ''' register binary data to reference image refImg '''
    offsets = init_offsets(ops)
    refAndMasks = prepare_refAndMasks(refImg,ops)

    nbatch = ops['batch_size']
    Ly = ops['Ly']
    Lx = ops['Lx']
    nbytesread = 2 * Ly * Lx * nbatch
    raw = 'keep_movie_raw' in ops and ops['keep_movie_raw'] and 'raw_file' in ops and os.path.isfile(ops['raw_file'])
    if raw:
        reg_file_align = open(reg_file_align, 'wb')
        raw_file_align = open(raw_file_align, 'rb')
    else:
        reg_file_align = open(reg_file_align, 'r+b')

    meanImg = np.zeros((Ly, Lx))
    k=0
    nfr=0
    k0 = tic()
    while True:
        if raw:
            buff = raw_file_align.read(nbytesread)
        else:
            buff = reg_file_align.read(nbytesread)
        data = np.frombuffer(buff, dtype=np.int16, offset=0)
        buff = []
        if data.size==0:
            break
        data = np.reshape(data, (-1, Ly, Lx))

        dout = register_data(data, refAndMasks, ops)
        data = np.minimum(dout[0], 2**15 - 2)
        meanImg += data.sum(axis=0)
        data = data.astype('int16')

        # write to reg_file_align
        if not raw:
            reg_file_align.seek(-2*data.size,1)
        reg_file_align.write(bytearray(data))

        # compile offsets (dout[1:])
        for n in range(len(dout)-1):
            if n < 3:
                offsets[n] = np.hstack((offsets[n], dout[n+1]))
            else:
                # add on nonrigid stats
                for m in range(len(dout[-1])):
                    offsets[n+m] = np.vstack((offsets[n+m], dout[-1][m]))

        # write registered tiffs
        if ops['reg_tif']:
            write_tiffs(data, ops, k, 0)

        nfr += data.shape[0]
        k += 1
        if k%5==0:
            print('registered %d/%d frames in time %4.2f'%(nfr, ops['nframes'], toc(k0)))

    print('registered %d/%d frames in time %4.2f'%(nfr, ops['nframes'], toc(k0)))

    # mean image across all frames
    if ops['nchannels']==1 or ops['functional_chan']==ops['align_by_chan']:
        ops['meanImg'] = meanImg/ops['nframes']
    else:
        ops['meanImg_chan2'] = meanImg/ops['nframes']

    reg_file_align.close()
    if raw:
        raw_file_align.close()
    return ops, offsets

def apply_shifts_to_binary(ops, offsets, reg_file_alt, raw_file_alt):
    ''' apply registration shifts to binary data'''
    nbatch = ops['batch_size']
    Ly = ops['Ly']
    Lx = ops['Lx']
    nbytesread = 2 * Ly * Lx * nbatch
    raw = 'keep_movie_raw' in ops and ops['keep_movie_raw']
    ix = 0
    meanImg = np.zeros((Ly, Lx))
    k=0
    k0 = tic()
    if raw:
        reg_file_alt = open(reg_file_alt, 'wb')
        raw_file_alt = open(raw_file_alt, 'rb')
    else:
        reg_file_alt = open(reg_file_alt, 'r+b')
    while True:
        if raw:
            buff = raw_file_alt.read(nbytesread)
        else:
            buff = reg_file_alt.read(nbytesread)

        data = np.frombuffer(buff, dtype=np.int16, offset=0)
        buff = []
        if data.size==0:
            break
        data = np.reshape(data[:int(np.floor(data.shape[0]/Ly/Lx)*Ly*Lx)], (-1, Ly, Lx))
        nframes = data.shape[0]

        # register by pre-determined amount
        iframes = ix + np.arange(0,nframes,1,int)
        if ops['bidiphase']!=0:
            data = shift_bidiphase(data.copy(), ops['bidiphase'])
        ymax, xmax = offsets[0][iframes].astype(np.int32), offsets[1][iframes].astype(np.int32)
        data = shift_data_worker((data.copy(), ymax, xmax, ops['refImg'].mean()))
        if ops['nonrigid']==True:
            ymax1, xmax1 = offsets[3][iframes], offsets[4][iframes]
            data = nonrigid.shift_data_worker((data, ops, ymax1, xmax1))
        data = np.minimum(data, 2**15 - 2)
        meanImg += data.mean(axis=0)
        data = data.astype('int16')
        # write to binary
        if not raw:
            reg_file_alt.seek(-2*data.size,1)
        reg_file_alt.write(bytearray(data))

        # write registered tiffs
        if ops['reg_tif_chan2']:
            write_tiffs(data, ops, k, 1)
        ix += nframes
        k+=1
    if ops['functional_chan']!=ops['align_by_chan']:
        ops['meanImg'] = meanImg/k
    else:
        ops['meanImg_chan2'] = meanImg/k
    print('registered second channel in time %4.2f'%(toc(k0)))

    reg_file_alt.close()
    if raw:
        raw_file_alt.close()
    return ops

def register_binary(ops, refImg=None):
    ''' registration of binary files '''
    # if ops is a list of dictionaries, each will be registered separately
    if (type(ops) is list) or (type(ops) is np.ndarray):
        for op in ops:
            op = register_binary(op)
        return ops

    # make blocks for nonrigid
    if ops['nonrigid']:
        ops = utils.make_blocks(ops)

    ops['nframes'] = get_nFrames(ops)

    # check number of frames and print warnings
    if ops['nframes']<50:
        raise Exception('the total number of frames should be at least 50 ')
    if ops['nframes']<200:
        print('number of frames is below 200, unpredictable behaviors may occur')

    if 'do_regmetrics' in ops:
        do_regmetrics = ops['do_regmetrics']
    else:
        do_regmetrics = True

    k0 = tic()

    # compute reference image
    if refImg is not None:
        print('using reference frame given')
        print('will not compute registration metrics')
        do_regmetrics = False
    else:
        refImg = pick_init(ops)
        print('computed reference frame for registration in time %4.2f'%(toc(k0)))
    ops['refImg'] = refImg

    # get binary file paths
    raw = 'keep_movie_raw' in ops and ops['keep_movie_raw'] and 'raw_file' in ops and os.path.isfile(ops['raw_file'])
    reg_file_align, reg_file_alt, raw_file_align, raw_file_alt = bin_paths(ops, raw)

    k = 0
    nfr = 0

    # register binary to reference image
    ops, offsets = register_binary_to_ref(ops, refImg, reg_file_align, raw_file_align)

    if ops['nchannels']>1:
        ops = apply_shifts_to_binary(ops, offsets, reg_file_alt, raw_file_alt)

    ops['yoff'] = offsets[0]
    ops['xoff'] = offsets[1]
    ops['corrXY'] = offsets[2]
    if ops['nonrigid']:
        ops['yoff1'] = offsets[3]
        ops['xoff1'] = offsets[4]
        ops['corrXY1'] = offsets[5]

    # compute valid region
    # ignore user-specified bad_frames.npy
    ops['badframes'] = np.zeros((ops['nframes'],), np.bool)
    if os.path.isfile(os.path.join(ops['data_path'][0], 'bad_frames.npy')):
        badframes = np.load(os.path.join(ops['data_path'][0], 'bad_frames.npy'))
        badframes = badframes.flatten().astype(int)
        ops['badframes'][badframes] = True
        print(ops['badframes'].sum())
    # return frames which fall outside range
    ops = compute_crop(ops)

    if 'ops_path' in ops:
        np.save(ops['ops_path'], ops)

    # compute metrics for registration
    if do_regmetrics and ops['nframes']>=2000:
        ops = regmetrics.get_pc_metrics(ops)
        print('computed registration metrics in time %4.2f'%(toc(k0)))

    if 'ops_path' in ops:
        np.save(ops['ops_path'], ops)
    return ops



def register_npy(Z, ops):
    # if ops does not have refImg, get a new refImg
    if 'refImg' not in ops:
        ops['refImg'] = Z.mean(axis=0)
    ops['nframes'], ops['Ly'], ops['Lx'] = Z.shape

    if ops['nonrigid']:
        ops = utils.make_blocks(ops)

    Ly = ops['Ly']
    Lx = ops['Lx']

    nbatch = ops['batch_size']
    meanImg = np.zeros((Ly, Lx)) # mean of this stack

    yoff = np.zeros((0,),np.float32)
    xoff = np.zeros((0,),np.float32)
    corrXY = np.zeros((0,),np.float32)
    if ops['nonrigid']:
        yoff1 = np.zeros((0,nb),np.float32)
        xoff1 = np.zeros((0,nb),np.float32)
        corrXY1 = np.zeros((0,nb),np.float32)

    maskMul, maskOffset, cfRefImg = prepare_masks(refImg, ops) # prepare masks for rigid registration
    if ops['nonrigid']:
        # prepare masks for non- rigid registration
        maskMulNR, maskOffsetNR, cfRefImgNR = nonrigid.prepare_masks(refImg, ops)
        refAndMasks = [maskMul, maskOffset, cfRefImg, maskMulNR, maskOffsetNR, cfRefImgNR]
        nb = ops['nblocks'][0] * ops['nblocks'][1]
    else:
        refAndMasks = [maskMul, maskOffset, cfRefImg]

    k = 0
    nfr = 0
    Zreg = np.zeros((nframes, Ly, Lx,), 'int16')
    while True:
        irange = np.arange(nfr, nfr+nbatch)
        data = Z[irange, :,:]
        if data.size==0:
            break
        data = np.reshape(data, (-1, Ly, Lx))
        dwrite, ymax, xmax, cmax, yxnr = phasecorr(data, refAndMasks, ops)
        dwrite = dwrite.astype('int16') # need to hold on to this
        meanImg += dwrite.sum(axis=0)
        yoff = np.hstack((yoff, ymax))
        xoff = np.hstack((xoff, xmax))
        corrXY = np.hstack((corrXY, cmax))
        if ops['nonrigid']:
            yoff1 = np.vstack((yoff1, yxnr[0]))
            xoff1 = np.vstack((xoff1, yxnr[1]))
            corrXY1 = np.vstack((corrXY1, yxnr[2]))
        nfr += dwrite.shape[0]
        Zreg[irange] = dwrite

        k += 1
        if k%5==0:
            print('registered %d/%d frames in time %4.2f'%(nfr, ops['nframes'], toc(k0)))

    # compute some potentially useful info
    ops['th_badframes'] = 100
    dx = xoff - medfilt(xoff, 101)
    dy = yoff - medfilt(yoff, 101)
    dxy = (dx**2 + dy**2)**.5
    cXY = corrXY / medfilt(corrXY, 101)
    px = dxy/np.mean(dxy) / np.maximum(0, cXY)
    ops['badframes'] = px > ops['th_badframes']
    ymin = np.maximum(0, np.ceil(np.amax(yoff[np.logical_not(ops['badframes'])])))
    ymax = ops['Ly'] + np.minimum(0, np.floor(np.amin(yoff)))
    xmin = np.maximum(0, np.ceil(np.amax(xoff[np.logical_not(ops['badframes'])])))
    xmax = ops['Lx'] + np.minimum(0, np.floor(np.amin(xoff)))
    ops['yrange'] = [int(ymin), int(ymax)]
    ops['xrange'] = [int(xmin), int(xmax)]
    ops['corrXY'] = corrXY

    ops['yoff'] = yoff
    ops['xoff'] = xoff

    if ops['nonrigid']:
        ops['yoff1'] = yoff1
        ops['xoff1'] = xoff1
        ops['corrXY1'] = corrXY1

    ops['meanImg'] = meanImg/ops['nframes']

    return Zreg, ops

def shift_data_subpixel(inputs):
    ''' rigid shift of X by ymax and xmax '''
    ''' allows subpixel shifts '''
    ''' ** not being used ** '''
    X, ymax, xmax, pad_fft = inputs
    ymax = ymax.flatten()
    xmax = xmax.flatten()
    if X.ndim<3:
        X = X[np.newaxis,:,:]

    nimg, Ly0, Lx0 = X.shape
    if pad_fft:
        X = fft2(X.astype('float32'), (next_fast_len(Ly0), next_fast_len(Lx0)))
    else:
        X = fft2(X.astype('float32'))
    nimg, Ly, Lx = X.shape
    Ny = fft.ifftshift(np.arange(-np.fix(Ly/2), np.ceil(Ly/2)))
    Nx = fft.ifftshift(np.arange(-np.fix(Lx/2), np.ceil(Lx/2)))
    [Nx,Ny] = np.meshgrid(Nx,Ny)
    Nx = Nx.astype('float32') / Lx
    Ny = Ny.astype('float32') / Ly
    dph = Nx * np.reshape(xmax, (-1,1,1)) + Ny * np.reshape(ymax, (-1,1,1))
    Y = np.real(ifft2(X * np.exp((2j * np.pi) * dph)))
    # crop back to original size
    if Ly0<Ly or Lx0<Lx:
        Lyhalf = int(np.floor(Ly/2))
        Lxhalf = int(np.floor(Lx/2))
        Y = Y[np.ix_(np.arange(0,nimg,1,int),
                     np.arange(-np.fix(Ly0/2), np.ceil(Ly0/2),1,int) + Lyhalf,
                     np.arange(-np.fix(Lx0/2), np.ceil(Lx0/2),1,int) + Lxhalf)]
    return Y
