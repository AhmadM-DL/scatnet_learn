import torch
import torch.nn as nn
import torch.nn.functional as func
from pytorch_wavelets.dtcwt.coeffs import biort as _biort, qshift as _qshift
from pytorch_wavelets.dtcwt.lowlevel import prep_filt
from scatnet_learn.lowlevel import mode_to_int
from scatnet_learn.lowlevel import ScatLayerj1_f, ScatLayerj1_rot_f
from scatnet_learn.lowlevel import ScatLayerj2_f, ScatLayerj2_rot_f
import torch.nn.init as init
import numpy as np


def random_postconv_impulse(C, F):
    """ Creates a random filter with +/- 1 in one location for a
    3x3 convolution. The idea being that we can randomly offset filters from
    each other"""
    z = torch.zeros((F, C, 3, 3))
    x = np.random.randint(-1, 2, size=(F, C))
    y = np.random.randint(-1, 2, size=(F, C))
    for i in range(F):
        for j in range(C):
            z[i, j, y[i,j], x[i,j]] = 1
    return z


def random_postconv_smooth(C, F, σ=1):
    """ Creates a random filter by shifting a gaussian with std σ. Meant to
    be a smoother version of random_postconv_impulse."""
    x = np.arange(-2, 3)
    a = 1/np.sqrt(2*np.pi*σ**2) * np.exp(-x**2/σ**2)
    b = np.outer(a, a)
    z = np.zeros((F, C, 3, 3))
    x = np.random.randint(-1, 2, size=(F, C))
    y = np.random.randint(-1, 2, size=(F, C))
    for i in range(F):
        for j in range(C):
            z[i, j] = np.roll(b, (y[i,j], x[i,j]), axis=(0,1))[1:-1,1:-1]
        z[i] /= np.sqrt(np.sum(z[i]**2))
    return torch.tensor(z, dtype=torch.float32)


def dct_bases():
    from scipy.fftpack import idct
    """ Get the top 3 dct bases """
    x = np.zeros((1,1,3,3))
    x[0,0,0,0] = 1/9
    lp = idct(idct(x, axis=-2, norm='ortho'), axis=-1, norm='ortho')
    x[0,0,0,0] = 0
    x[0,0,0,1] = 1/9
    horiz = idct(idct(x, axis=-2, norm='ortho'), axis=-1, norm='ortho')
    x[0,0,0,1] = 0
    x[0,0,1,0] = 1/9
    vertic = idct(idct(x, axis=-2, norm='ortho'), axis=-1, norm='ortho')

    return (torch.tensor(lp, dtype=torch.float32),
            torch.tensor(horiz, dtype=torch.float32),
            torch.tensor(vertic, dtype=torch.float32))


class InvariantLayerj1(nn.Module):
    """ Also can be called the learnable scatternet layer.

    Takes a single order scatternet layer, and mixes the outputs to give a new
    set of outputs. You can select the style of mixing, the default being a
    single 1x1 convolutional layer, but other options include a 3x3
    convolutional mixing and a 1x1 mixing with random offsets.

    Inputs:
        C (int): The number of input channels
        F (int): The number of output channels. None by default, in which case
            the number of output channels is 7*C.
        stride (int): The downsampling factor
        k (int): The mixing kernel size
        alpha (str): A fixed kernel to increase the spatial size of the mixing.
            Can be::

                - None (no expansion),
                - 'impulse' (randomly shifts bands left/right and up/down by 1
                    pixel),
                - 'smooth' (randomly shifts a gaussian left/right and up/down
                    by 1 pixel and uses the mixing matrix to expand this.
        biort (str): which biorthogonal filters to use.

    Returns:
        y (torch.tensor): The output

    """
    def __init__(self, C, F=None, stride=2, k=1, alpha=None,
                 biort='near_sym_a', mode='symmetric', magbias=1e-2):
        super().__init__()
        if F is None:
            F = 7*C
        if k > 1 and alpha is not None:
            raise ValueError("Only use alpha when k=1")

        self.scat = ScatLayerj1(biort=biort, mode=mode, magbias=magbias)
        self.stride = stride
        # Create the learned mixing weights and possibly the expansion kernel
        self.A = nn.Parameter(torch.randn(F, 7*C, k, k))
        init.xavier_uniform_(self.A, gain=1.5)
        self.b = nn.Parameter(torch.zeros(F,))
        self.C = C
        self.F = F
        self.k = k
        self.alpha_t = alpha
        self.biort = biort
        if alpha == 'impulse':
            self.alpha = nn.Parameter(
                random_postconv_impulse(7*C, F), requires_grad=False)
            self.pad = 1
        elif alpha == 'smooth':
            self.alpha = nn.Parameter(
                random_postconv_smooth(7*C, F, σ=1), requires_grad=False)
            self.pad = 1
        elif alpha == 'random':
            self.alpha = nn.Parameter(
                torch.randn(F, 7*C, 3, 3), requires_grad=False)
            init.xavier_uniform(self.alpha)
            self.pad = 1
        elif alpha is None:
            self.alpha = 1
            self.pad = (k-1) // 2
        else:
            raise ValueError

    def forward(self, x):
        z = self.scat(x)
        As = self.A * self.alpha
        y = func.conv2d(z, As, self.b, padding=self.pad)
        #  y = func.relu(y)
        if self.stride == 1:
            y = func.interpolate(y, scale_factor=2, mode='bilinear',
                                 align_corners=False)
        return y

    def extra_repr(self):
        return '{}, {}, stride={}, k={}, alpha={}'.format(
               self.C, self.F, self.stride, self.k, self.alpha_t)


class InvariantLayerj1_dct(nn.Module):
    """ Also can be called the learnable scatternet layer.

    Takes a single order scatternet layer, and mixes the outputs to give a new
    set of outputs. This version expands the spatial support of the mixing by
    taking the top 3 dct coefficients and learning 3 1x1 mixing matrices

    Inputs:
        C (int): The number of input channels
        F (int): The number of output channels. None by default, in which case
            the number of output channels is 7*C.
        stride (int): The downsampling factor

    Returns:
        y (torch.tensor): The output

    """
    def __init__(self, C, F, stride=2):
        super().__init__()
        self.scat = ScatLayerj1()
        self.A1 = nn.Parameter(torch.randn(C*7, F, 1, 1))
        self.A2 = nn.Parameter(torch.randn(C*7, F, 1, 1))
        self.A3 = nn.Parameter(torch.randn(C*7, F, 1, 1))
        self.b = nn.Parameter(torch.zeros(F,1,1))
        lp, h, v = dct_bases()
        self.lp = nn.Parameter(lp, requires_grad=False)
        self.h = nn.Parameter(h, requires_grad=False)
        self.v = nn.Parameter(v, requires_grad=False)
        self.stride = stride
        init.xavier_uniform_(self.A1)
        init.xavier_uniform_(self.A2)
        init.xavier_uniform_(self.A3)

    def forward(self, x):
        A1 = self.A1 * self.lp
        A2 = self.A2 * self.h
        A3 = self.A3 * self.v
        z = self.scat(x)
        s = z.shape
        z = z.view(s[0], s[1]*s[2], s[3], s[4])
        y = (func.conv2d(z, A1, padding=1) +
             func.conv2d(z, A2, padding=1) +
             func.conv2d(z, A3, padding=1) + self.b)
        y = func.relu(y)
        if self.stride == 1:
            y = func.interpolate(y, scale_factor=2, mode='bilinear',
                                 align_corners=False)
        return y


class InvariantLayerj1_compress(nn.Module):
    """ Also can be called the learnable scatternet layer.

    Takes a single order scatternet layer, and mixes the outputs to give a new
    set of outputs. You can select the style of mixing, the default being a
    single 1x1 convolutional layer, but other options include a 3x3
    convolutional mixing and a 1x1 mixing with random offsets.

    Inputs:
        C (int): The number of input channels
        F (int): The number of output channels. None by default, in which case
            the number of output channels is 7*C.
        stride (int): The downsampling factor
        k (int): The mixing kernel size
        alpha (str): A fixed kernel to increase the spatial size of the mixing.
            Can be::

                - None (no expansion),
                - 'impulse' (randomly shifts bands left/right and up/down by 1
                    pixel),
                - 'smooth' (randomly shifts a gaussian left/right and up/down
                    by 1 pixel and uses the mixing matrix to expand this.

    Returns:
        y (torch.tensor): The output

    """
    def __init__(self, C, F=None, stride=2, k=1, alpha=None):
        super().__init__()
        if F is None:
            F = 7*(C//2)
        if k > 1 and alpha is not None:
            raise ValueError("Only use alpha when k=1")

        # Create the learned mixing weights and possibly the expansion kernel
        C1 = C//2
        self.compress = nn.Conv2d(C, C1, 1)
        self.gain = InvariantLayerj1(C1, F, stride, k, alpha)

    def forward(self, x):
        y = self.compress(x)
        y = self.gain(y)
        return y

    def init(self, std):
        self.gain.init(std)


class ScatLayerj1(nn.Module):
    """ Does one order of scattering at a single scale. Can be made into a
    second order scatternet by stacking two of these layers.

    Inputs:
        biort (str): the biorthogonal filters to use. if 'near_sym_b_bp' will
            use the rotationally symmetric filters. These have 13 and 19 taps
            so are quite long. They also require 7 1D convolutions instead of 6.
        x (torch.tensor): Input of shape (N, C, H, W)
        mode (str): padding mode. Can be 'symmetric' or 'zero'
        magbias (float): the magnitude bias to use for smoothing
        combine_colour (bool): if true, will only have colour lowpass and have
            greyscale bandpass

    Returns:
        y (torch.tensor): y has the lowpass and invariant U terms stacked along
            the channel dimension, and so has shape (N, 7*C, H/2, W/2). Where
            the first C channels are the lowpass outputs, and the next 6C are
            the magnitude highpass outputs.
    """
    def __init__(self, biort='near_sym_a', mode='symmetric', magbias=1e-2,
                 combine_colour=False):
        super().__init__()
        self.biort = biort
        # Have to convert the string to an int as the grad checks don't work
        # with string inputs
        self.mode_str = mode
        self.mode = mode_to_int(mode)
        self.magbias = magbias
        self.combine_colour = combine_colour
        if biort == 'near_sym_b_bp':
            self.bandpass_diag = True
            h0o, _, h1o, _, h2o, _ = _biort(biort)
            self.h0o = torch.nn.Parameter(prep_filt(h0o, 1), False)
            self.h1o = torch.nn.Parameter(prep_filt(h1o, 1), False)
            self.h2o = torch.nn.Parameter(prep_filt(h2o, 1), False)
        else:
            self.bandpass_diag = False
            h0o, _, h1o, _ = _biort(biort)
            self.h0o = torch.nn.Parameter(prep_filt(h0o, 1), False)
            self.h1o = torch.nn.Parameter(prep_filt(h1o, 1), False)

    def forward(self, x):
        # Do the single scale DTCWT
        # If the row/col count of X is not divisible by 2 then we need to
        # extend X
        _, ch, r, c = x.shape
        if r % 2 != 0:
            x = torch.cat((x, x[:,:,-1:]), dim=2)
        if c % 2 != 0:
            x = torch.cat((x, x[:,:,:,-1:]), dim=3)

        if self.combine_colour:
            assert ch == 3

        if self.bandpass_diag:
            Z = ScatLayerj1_rot_f.apply(
                x, self.h0o, self.h1o, self.h2o, self.mode, self.magbias,
                self.combine_colour)
        else:
            Z = ScatLayerj1_f.apply(
                x, self.h0o, self.h1o, self.mode, self.magbias,
                self.combine_colour)
        if not self.combine_colour:
            b, _, c, h, w = Z.shape
            Z = Z.view(b, 7*c, h, w)
        return Z

    def extra_repr(self):
        return "biort='{}', mode='{}', magbias={}".format(
               self.biort, self.mode_str, self.magbias)


class ScatLayerj2(nn.Module):
    """ Does one order of scattering at a single scale. Can be made into a
    second order scatternet by stacking two of these layers.

    Inputs:
        biort (str): the biorthogonal filters to use. if 'near_sym_b_bp' will
            use the rotationally symmetric filters. These have 13 and 19 taps
            so are quite long. They also require 7 1D convolutions instead of 6.
        x (torch.tensor): Input of shape (N, C, H, W)
        mode (str): padding mode. Can be 'symmetric' or 'zero'

    Returns:
        y (torch.tensor): y has the lowpass and invariant U terms stacked along
            the channel dimension, and so has shape (N, 7*C, H/2, W/2). Where
            the first C channels are the lowpass outputs, and the next 6C are
            the magnitude highpass outputs.
    """
    def __init__(self, biort='near_sym_a', qshift='qshift_a', mode='symmetric',
                 magbias=1e-2, combine_colour=False):
        super().__init__()
        self.biort = biort
        self.qshift = biort
        # Have to convert the string to an int as the grad checks don't work
        # with string inputs
        self.mode_str = mode
        self.mode = mode_to_int(mode)
        self.magbias = magbias
        self.combine_colour = combine_colour
        if biort == 'near_sym_b_bp':
            assert qshift == 'qshift_b_bp'
            self.bandpass_diag = True
            h0o, _, h1o, _, h2o, _ = _biort(biort)
            self.h0o = torch.nn.Parameter(prep_filt(h0o, 1), False)
            self.h1o = torch.nn.Parameter(prep_filt(h1o, 1), False)
            self.h2o = torch.nn.Parameter(prep_filt(h2o, 1), False)
            h0a, h0b, _, _, h1a, h1b, _, _, h2a, h2b, _, _ = _qshift('qshift_b_bp')
            self.h0a = torch.nn.Parameter(prep_filt(h0a, 1), False)
            self.h0b = torch.nn.Parameter(prep_filt(h0b, 1), False)
            self.h1a = torch.nn.Parameter(prep_filt(h1a, 1), False)
            self.h1b = torch.nn.Parameter(prep_filt(h1b, 1), False)
            self.h2a = torch.nn.Parameter(prep_filt(h2a, 1), False)
            self.h2b = torch.nn.Parameter(prep_filt(h2b, 1), False)
        else:
            self.bandpass_diag = False
            h0o, _, h1o, _ = _biort(biort)
            self.h0o = torch.nn.Parameter(prep_filt(h0o, 1), False)
            self.h1o = torch.nn.Parameter(prep_filt(h1o, 1), False)
            h0a, h0b, _, _, h1a, h1b, _, _ = _qshift(qshift)
            self.h0a = torch.nn.Parameter(prep_filt(h0a, 1), False)
            self.h0b = torch.nn.Parameter(prep_filt(h0b, 1), False)
            self.h1a = torch.nn.Parameter(prep_filt(h1a, 1), False)
            self.h1b = torch.nn.Parameter(prep_filt(h1b, 1), False)

    def forward(self, x):
        # Ensure the input size is divisible by 8
        ch, r, c = x.shape[1:]
        rem = r % 8
        if rem != 0:
            rows_after = (9-rem)//2
            rows_before = (8-rem) // 2
            x = torch.cat((x[:,:,:rows_before], x,
                           x[:,:,-rows_after:]), dim=2)
        rem = c % 8
        if rem != 0:
            cols_after = (9-rem)//2
            cols_before = (8-rem) // 2
            x = torch.cat((x[:,:,:,:cols_before], x,
                           x[:,:,:,-cols_after:]), dim=3)

        if self.combine_colour:
            assert ch == 3

        if self.bandpass_diag:
            pass
            Z = ScatLayerj2_rot_f.apply(
                x, self.h0o, self.h1o, self.h2o, self.h0a, self.h0b, self.h1a,
                self.h1b, self.h2a, self.h2b, self.mode, self.magbias,
                self.combine_colour)
        else:
            Z = ScatLayerj2_f.apply(
                x, self.h0o, self.h1o, self.h0a, self.h0b, self.h1a,
                self.h1b, self.mode, self.magbias, self.combine_colour)

        if not self.combine_colour:
            b, _, c, h, w = Z.shape
            Z = Z.view(b, 49*c, h, w)
        return Z

    def extra_repr(self):
        return "biort='{}', mode='{}', magbias={}".format(
               self.biort, self.mode_str, self.magbias)
