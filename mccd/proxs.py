# -*- coding: utf-8 -*-

r"""PROXIMAL OPERATORS.

Defines proximal operators to be fed to ModOpt algorithm that are
specific to MCCD(or rather, not currently in ``modopt.opt.proximity``).

: Authors: Tobias Liaudat <tobiasliaudat@gmail.com>,
           Morgan Schmitz <github @MorganSchmitz>

"""

from __future__ import absolute_import, print_function
import numpy as np
from modopt.signal.wavelet import filter_convolve
from modopt.opt.proximity import ProximityParent
import mccd.utils as utils
import tensorflow as tf
from . import saving_unets as unet_model
from . import saving_learnlets as learnlet_model
from mccd.denoising.learnlets.learnlet_model import Learnlet
from sklearn.model_selection import train_test_split
from tensorflow.keras.optimizers import Adam


class LinRecombine(ProximityParent):
    r"""Multiply eigenvectors ``S`` and (factorized) weights ``A``.

    Maintain the knowledge about the linear operator norm which is calculated
    as the spectral norm (highest eigenvalue of the matrix).
    The recombination is done with ``S`` living in the tranformed domain.

    Parameters
    ----------
    A: numpy.ndarray
        Matrix defining the linear operator.
    filters: numpy.ndarray
        Filters used by the wavelet transform.
    compute_norm: bool
        Computation of the matrix spectral radius in the initialization.
    """

    def __init__(self, A, filters, compute_norm=False):
        r"""Initialize class attributes."""
        self.A = A
        self.op = self.recombine
        self.adj_op = self.adj_rec
        self.filters = filters
        if compute_norm:
            U, s, Vt = np.linalg.svd(self.A.dot(self.A.T), full_matrices=False)
            self.norm = np.sqrt(s[0])

    def recombine(self, transf_S):
        r"""Recombine new S and return it."""
        S = np.array([filter_convolve(transf_Sj, self.filters, filter_rot=True)
                      for transf_Sj in transf_S])
        return utils.rca_format(S).dot(self.A)

    def adj_rec(self, Y):
        r"""Return the adjoint operator of ``recombine``."""
        return utils.apply_transform(Y.dot(self.A.T), self.filters)

    def update_A(self, new_A, update_norm=True):
        r"""Update the ``A`` matrix.

        Also calculate the operator norm of A.
        """
        self.A = new_A
        if update_norm:
            U, s, Vt = np.linalg.svd(self.A.dot(self.A.T), full_matrices=False)
            self.norm = np.sqrt(s[0])

class LinRecombineNoFilters(ProximityParent):
    r"""Multiply eigenvectors ``S`` and (factorized) weights ``A``.

    Maintain the knowledge about the linear operator norm which is calculated
    as the spectral norm (highest eigenvalue of the matrix).
    The recombination is done with ``S`` living in the direct domain.

    Parameters
    ----------
    A: numpy.ndarray
        Matrix defining the linear operator.
    compute_norm: bool
        Computation of the matrix spectral radius in the initialization.
    """

    def __init__(self, A, compute_norm=False):
        r"""Initialize class attributes."""
        self.A = A
        self.op = self.recombine
        self.adj_op = self.adj_rec
        if compute_norm:
            U, s, Vt = np.linalg.svd(self.A.dot(self.A.T), full_matrices=False)
            self.norm = np.sqrt(s[0])

    def recombine(self, S):
        r"""Recombine new S and return it."""
        return S.dot(self.A)

    def adj_rec(self, Y):
        r"""Return the adjoint operator of ``recombine``."""
        return Y.dot(self.A.T)

    def update_A(self, new_A, update_norm=True):
        r"""Update the ``A`` matrix.

        Also calculate the operator norm of A.
        """
        self.A = new_A
        if update_norm:
            U, s, Vt = np.linalg.svd(self.A.dot(self.A.T), full_matrices=False)
            self.norm = np.sqrt(s[0])

class KThreshold(ProximityParent):
    r"""Define linewise hard-thresholding operator with variable thresholds.

    Parameters
    ----------
    iter_func: function
        Input function that calcultates the number of non-zero values to keep
        in each line at each iteration.
    """

    def __init__(self, iter_func):
        r"""Initialize class attributes."""
        self.iter_func = iter_func
        self.iter = 0

    def reset_iter(self):
        r"""Set iteration counter to zero."""
        self.iter = 0

    def op(self, data, extra_factor=1.0):
        r"""Return input data after thresholding."""
        self.iter += 1

        return utils.lineskthresholding(data, self.iter_func(self.iter,
                                                             data.shape[1]))

    def cost(self, x):
        r"""Return cost.

        (Indicator of :math:`\Omega` is either 0 or infinity).
        """
        return 0


class StarletThreshold(ProximityParent):
    r"""Apply soft thresholding in wavelet(default Starlet) domain.

    Parameters
    ----------
    threshold: numpy.ndarray
        Threshold levels.
    thresh_type: str
        Whether soft- or hard-thresholding should be used.
        Default is ``'soft'``.
    """

    def __init__(self, threshold, thresh_type='soft'):
        r"""Initialize class attributes."""
        self.threshold = threshold
        self._thresh_type = thresh_type

    def update_threshold(self, new_threshold, new_thresh_type=None):
        r"""Update starlet threshold."""
        self.threshold = new_threshold
        if new_thresh_type in ['soft', 'hard']:
            self._thresh_type = new_thresh_type

    def op(self, transf_data, **kwargs):
        r"""Apply wavelet transform and perform thresholding."""
        # Threshold all scales but the coarse
        transf_data[:, :-1] = utils.SoftThresholding(transf_data[:, :-1],
                                                     self.threshold[:, :-1])
        return transf_data

    def cost(self, x, y):
        r"""Return cost."""
        return 0

class ProxLearnlets(ProximityParent):
    r"""Learnlets denoising proximal operator.

    Parameters
    ----------
    model: tf.Model
        Denoising model. Learnlet in this case.
    im_shape: tuple
        Image shape.
    den_rad: float
        Radius for the denoising window.
    """

    def __init__(self, model=None, im_shape=(51,51), den_win_rad=13., items=None):
        r"""Initialize class attributes."""
        self.im_shape = im_shape
        self.den_win_rad = den_win_rad
        self.model = model
        self.noise = None
        self.noise_window = None

        # Init noise window
        self.init_noise_win()

    def init_noise_win(self):
        # Calculate window function for estimating the noise
        self.noise_window = np.ones(self.im_shape, dtype=bool)
        for coord_x in range(self.im_shape[0]):
            for coord_y in range(self.im_shape[1]):
                if np.sqrt((coord_x - self.im_shape[0]/2)**2 + (coord_y - self.im_shape[1]/2)**2) <= self.den_win_rad :
                    self.noise_window[coord_x, coord_y] = False

    def mad(self, x):
        r"""Compute an estimation of the standard deviation
        of a Gaussian distribution using the robust
        MAD (Median Absolute Deviation) estimator."""
        return 1.4826*np.median(np.abs(x - np.median(x)))

    def noise_estimator(self, image):
        r"""Estimate the noise level of the image."""
        # Calculate noise std dev
        return self.mad(image[self.noise_window])

    @staticmethod
    def convert_and_pad(image):
        r""" Convert images to tensorflow's tensor and add an extra 4th dimension."""
        return tf.expand_dims(tf.convert_to_tensor(image), axis=3)

    @staticmethod
    def crop_and_convert(image):
        r"""Convert to numpy array and remove the 4th dimension."""
        return image.numpy()[:,:,:,0]

    @staticmethod
    def scale_img(image):
        r"""Scale image to [-0.5, 0.5]. 
        
        Return the scaled image.
        """
        img_max = np.max(image)
        img_min = np.min(image)

        image -= img_min
        image /= (img_max - img_min)
        image -= 0.5

        return image

    @staticmethod
    def rescale_img(image, new_max, new_min):
        r"""Rescale the image from [-0.5, 0.5] to [min, max]."""
        image += 0.5
        image *= (new_max - new_min)
        image += new_min
        return image

    def op(self, images, **kwargs):
        r"""Apply Learnlets denoising."""
        imgs = utils.reg_format(np.copy(images))
        # Transform all eigenPSFs into positive (avoid sign indetermination)
        multiple = np.array([np.sum(im)>0 for im in imgs]) * 2. - 1.
        imgs *= multiple.reshape((-1, 1, 1))
        # Scale 
        img_maxs = np.amax(imgs, axis=(1,2))
        img_mins = np.amin(imgs, axis=(1,2))
        imgs = np.array([self.scale_img(im) for im in imgs])
         # Calculate noise
        self.noise = np.array([self.noise_estimator(im) for im in imgs])
        self.noise = tf.reshape(tf.convert_to_tensor(self.noise), [imgs.shape[0], 1])
        # Convert to tensorflow and expand 4th dimension
        imgs = self.convert_and_pad(imgs)
        # Denoise
        imgs = self.model.predict((imgs, self.noise))
        # Rescale the images to the original max and min values
        imgs = np.array([
            self.rescale_img(im, _max, _min) 
            for im, _max, _min in zip(imgs, img_maxs, img_mins)
        ])
        # Retransform eigenPSF into their original sign
        imgs = tf.math.multiply(multiple.reshape((-1, 1, 1, 1)), imgs)
        return utils.rca_format(self.crop_and_convert(imgs))

    def cost(self, x, y):
        r"""Return cost."""
        return 0

class ProxUnets(ProximityParent):
    r"""Unet denoising proximal operator.

    Parameters
    ----------
    model: tf.Model
        Denoising model. Unet in this case.
    im_shape: tuple
        Image shape.

    """

    def __init__(self, model=None, im_shape=(51,51), items=None):
        r"""Initialize class attributes."""
        self.im_shape = im_shape
        self.model = model

    @staticmethod
    def convert_and_pad(image):
        r""" Convert images to tensorflow's tensor and add an extra 4th dimension."""
        return tf.expand_dims(tf.convert_to_tensor(image), axis=3)

    @staticmethod
    def crop_and_convert(image):
        r"""Convert to numpy array and remove the 4th dimension."""
        return image.numpy()[:,:,:,0]

    @staticmethod
    def scale_img(image):
        r"""Scale image to [-0.5, 0.5]. 
        
        Return the scaled image.
        """
        img_max = np.max(image)
        img_min = np.min(image)

        image -= img_min
        image /= (img_max - img_min)
        image -= 0.5

        return image

    @staticmethod
    def rescale_img(image, new_max, new_min):
        r"""Rescale the image from [-0.5, 0.5] to [min, max]."""
        image += 0.5
        image *= (new_max - new_min)
        image += new_min
        return image

    def op(self, images, **kwargs):
        r"""Apply Learnlets denoising."""
        imgs = utils.reg_format(np.copy(images))
        # Transform all eigenPSFs into positive (avoid sign indetermination)
        multiple = np.array([np.sum(im)>0 for im in imgs]) * 2. - 1.
        imgs *= multiple.reshape((-1, 1, 1))
        # Scale 
        img_maxs = np.amax(imgs, axis=(1,2))
        img_mins = np.amin(imgs, axis=(1,2))
        imgs = np.array([self.scale_img(im) for im in imgs])
        # Convert to tensorflow and expand 4th dimension
        imgs = self.convert_and_pad(imgs)
        # Denoise
        imgs = self.model.predict(imgs)
        # Rescale the images to the original max and min values
        imgs = np.array([
            self.rescale_img(im, _max, _min) 
            for im, _max, _min in zip(imgs, img_maxs, img_mins)
        ])
        # Retransform eigenPSF into their original sign
        imgs = tf.math.multiply(multiple.reshape((-1, 1, 1, 1)), imgs)
        return utils.rca_format(self.crop_and_convert(imgs))
 
    def cost(self, x, y):
        r"""Return cost."""
        return 0


class WaveletDenoising(ProximityParent):
    r"""Apply soft thresholding in wavelet(default Starlet) domain.
    
    This amounts as a soft thresholding in wavelet(default Starlet) domain.

    Parameters
    ----------
    Phi_filters: numpy.ndarray
        Wavelet filters.
    threshold: numpy.ndarray
        Threshold levels.
    thresh_type: str
        Whether soft- or hard-thresholding should be used.
        Default is ``soft``.
    """

    def __init__(self, Phi_filters, threshold, thresh_type='soft'):
        r"""Initialize class attributes."""
        self.Phi_filters = Phi_filters
        self.threshold = threshold
        self._thresh_type = thresh_type

    def update_threshold(self, new_threshold, new_thresh_type=None):
        r"""Update starlet threshold."""
        self.threshold = new_threshold
        if new_thresh_type in ['soft', 'hard']:
            self._thresh_type = new_thresh_type

    def op(self, data, **kwargs):
        r"""Apply wavelet transform and perform thresholding."""
        # Transform the data into teh wavelet sapce
        transf_data = utils.apply_transform(data, self.Phi_filters)
        # Threshold all scales but the coarse
        transf_data[:, :-1] = utils.SoftThresholding(
            transf_data[:, :-1],
            self.threshold[:, :-1]
        )
        # Come back to the original domain
        data = np.array([
            filter_convolve(transf_Sj, self.Phi_filters, filter_rot=True)
            for transf_Sj in transf_data
        ])
        return utils.rca_format(data)

    def cost(self, x, y):
        r"""Return cost."""
        return 0

class OLD_Learnlets(ProximityParent):
    r"""Apply Learnlets denoising.

    Parameters
    ----------
    model: str
        Which denoising algorithm to use.
        We couldn't save the whole architecture of the model, thus we use the weights of the model. However, this requires a
        first step of initialization that we didn't need for the U-Nets.

    """

    def __init__(self, items=None):
        r"""Initialize class attributes."""
        self.im_shape = (51,51)

        # Calculate window function for estimating the noise
        # We couldn't use Galsim to estimate the moments, so we chose to work with the real center of the image (25.5,25.5)
        # instead of using the real centroid. Also, we use 13 instead of 5*obs_sigma, so that we are sure to cut all the flux
        # from the star
        self.noise_window = np.ones(self.im_shape, dtype=bool)
        for coord_x in range(self.im_shape[0]):
            for coord_y in range(self.im_shape[1]):
                if np.sqrt((coord_x - 25.5)**2 + (coord_y - 25.5)**2) <= 13 :
                    self.noise_window[coord_x, coord_y] = False

        im_val = tf.convert_to_tensor(np.random.rand(2, self.im_shape[0], self.im_shape[1], 1))
        std_val = tf.convert_to_tensor(np.random.rand(2))
        run_params = {
            'denoising_activation': 'dynamic_soft_thresholding',
            'learnlet_analysis_kwargs':{
                'n_tiling': 256,
                'mixing_details': False,
                'skip_connection': True,
            },
            'learnlet_synthesis_kwargs': {
                'res': True,
            },
            'threshold_kwargs':{
                'noise_std_norm': True,
            },
        #     'wav_type': 'bior',
            'n_scales': 5,
            'n_reweights_learn': 1,
            'clip': False,
        }
        learnlets = Learnlet(**run_params)
        learnlets.compile(
            optimizer=Adam(lr=1e-3),
            loss='mse',
        )
        learnlets.fit(
            (im_val, std_val),
            im_val,
            validation_data = ((im_val, std_val), im_val),
            steps_per_epoch = 1,
            epochs = 1,
            batch_size=12,
        )
        learnlets.load_weights(learnlet_model.__path__[0] + '/cp.h5')
        self.model = learnlets
        self.noise = None

    def mad(self, x):
        r"""Compute an estimation of the standard deviation
        of a Gaussian distribution using the robust
        MAD (Median Absolute Deviation) estimator."""
        return 1.4826*np.median(np.abs(x - np.median(x)))

    def noise_estimator(self, image):
        r"""Estimate the noise level of the image."""
        # Calculate noise std dev
        return self.mad(image[self.noise_window])

    # def OLD_convert_and_pad(self, image):
    #     r"""Convert images to 64x64x1 shaped tensors to feed the model, using zero-padding."""
    #     image = tf.reshape(
    #         tf.convert_to_tensor(image),
    #         [np.shape(image)[0], np.shape(image)[1], np.shape(image)[2], 1]
    #     )
    #     # pad = tf.constant([[0,0], [6,7],[6,7], [0,0]])
    #     # return tf.pad(image, pad, "CONSTANT")
    #     return image

    # def OLD_crop_and_convert(self, image):
    #     r"""Crop back the image to its original size and convert it to np.array"""
    #     #image = tf.reshape(tf.image.crop_to_bounding_box(image, 6, 6, 51, 51), [np.shape(image)[0], 51, 51])
    #     image = tf.reshape(image, [np.shape(image)[0], 51, 51])
    #     return image.numpy()

    @staticmethod
    def convert_and_pad(image):
        r""" Convert images to tensorflow's tensor and add an extra 4th dimension."""
        return tf.expand_dims(tf.convert_to_tensor(image), axis=3)

    @staticmethod
    def crop_and_convert(image):
        r"""Convert to numpy array and remove the 4th dimension."""
        return image.numpy()[:,:,:,0]

    @staticmethod
    def scale_img(image):
        r"""Scale image to [-0.5, 0.5]. 
        
        Return the scaled image.
        """
        img_max = np.max(image)
        img_min = np.min(image)

        image -= img_min
        image /= (img_max - img_min)
        image -= 0.5

        return image

    @staticmethod
    def rescale_img(image, new_max, new_min):
        r"""Rescale the image from [-0.5, 0.5] to [min, max]."""
        image += 0.5
        image *= (new_max - new_min)
        image += new_min
        return image

    def op(self, image, **kwargs):
        r"""Apply Learnlets denoising."""
        # TODO check if the np.copy() is necessary
        imgs = utils.reg_format(np.copy(image))
        # Transform all eigenPSFs into positive (avoid sign indetermination)
        multiple = np.array([np.sum(im)>0 for im in imgs]) * 2. - 1.
        imgs *= multiple.reshape((-1, 1, 1))
        # Scale 
        img_maxs = np.amax(imgs, axis=(1,2))
        img_mins = np.amin(imgs, axis=(1,2))
        imgs = np.array([self.scale_img(im) for im in imgs])
         # Calculate noise
        self.noise = np.array([self.noise_estimator(im) for im in imgs])
        self.noise = tf.reshape(tf.convert_to_tensor(self.noise), [len(imgs), 1])
        # Convert to tensorflow and expand 4th dimension
        imgs = self.convert_and_pad(imgs)
        # Denoise
        imgs = self.model.predict((imgs, self.noise))
        # Rescale the images to the original max and min values
        imgs = np.array([
            self.rescale_img(im, _max, _min) 
            for im, _max, _min in zip(imgs, img_maxs, img_mins)
        ])
        # Retransform eigenPSF into their original sign
        imgs = tf.math.multiply(multiple.reshape((-1, 1, 1, 1)), imgs)
        imgs = utils.rca_format(self.crop_and_convert(imgs))
        return imgs

    def cost(self, x, y):
        r"""Return cost."""
        return 0

class OLD_Unets(ProximityParent):
    r"""Apply Unets denoising.

    Parameters
    ----------
    model: str
        Which denoising algorithm to use.

    """
    def __init__(self, items=None):
        r"""Initialize class attributes."""
        self.model = tf.keras.models.load_model(unet_model.__path__[0])

    def convert_and_pad(self, image):
        r"""Convert images to 64x64x1 shaped tensors to feed the model, using zero-padding."""
        image = tf.reshape(tf.convert_to_tensor(image),
                           [np.shape(image)[0], np.shape(image)[1], np.shape(image)[2], 1])
        # pad = tf.constant([[0,0], [6,7],[6,7], [0,0]])
        # return tf.pad(image, pad, "CONSTANT")
        return image

    def crop_and_convert(self, image):
        r"""Crop back the image to its original size and convert it to np.array"""
        #image = tf.reshape(tf.image.crop_to_bounding_box(image, 6, 6, 51, 51), [np.shape(image)[0], 51, 51])
        image = tf.reshape(image, [np.shape(image)[0], 51, 51])
        return image.numpy()

    def op(self, image, **kwargs):
        r"""Apply Unets denoising."""
        # Threshold all scales but the coarse
        image = utils.reg_format(image)
        multiple = np.array([np.sum(image[i,:,:])>0 for i in np.arange(len(image))]) * 2. - 1.
        image *= multiple.reshape((-1, 1, 1))
        image = self.convert_and_pad(image)
        image = self.model.predict(image)
        image = tf.math.multiply(multiple.reshape((-1, 1, 1, 1)), image)
        return utils.rca_format(self.crop_and_convert(image))

    def cost(self, x, y):
        r"""Return cost."""
        return 0


class proxNormalization(ProximityParent):
    r"""Normalize rows or columns of :math:`x` relatively to L2 norm.

    Parameters
    ----------
    type: str
        String defining the axis to normalize. If is `lines`` or ``columns``.
        Default is ``columns``.
    """

    def __init__(self, type='columns'):
        r"""Initialize class attributes."""
        self.op = self.normalize
        self.type = type

    def normalize(self, x, extra_factor=1.0):
        r"""Apply normalization.

        Following the prefered type.
        """
        # if self.type == 'lines':
        #     x_norm = np.linalg.norm(x, axis=1).reshape(-1, 1)
        # else:
        #     x_norm = np.linalg.norm(x, axis=0).reshape(1, -1)

        # return x / x_norm
        return x

        # Not using a prox normalization as it is constraining the model
        # too strong.
        return x

    def cost(self, x):
        r"""Return cost."""
        return 0


class PositityOff(ProximityParent):
    r"""Project to the positive subset, taking into acount an offset."""

    def __init__(self, offset):
        r"""Initialize class attibutes."""
        self.offset = offset
        self.op = self.off_positive_part

    def update_offset(self, new_offset):
        r"""Update the offset value."""
        self.offset = new_offset

    def off_positive_part(self, x, extra_factor=1.0):
        r"""Perform the projection accounting for the offset."""
        prox_x = np.zeros(x.shape)
        pos_idx = (x > - self.offset)
        neg_idx = np.array(1 - pos_idx).astype(bool)
        prox_x[pos_idx] = x[pos_idx]
        prox_x[neg_idx] = - self.offset[neg_idx]
        return prox_x

    def cost(self, x):
        r"""Return cost."""
        return 0


class LinRecombineAlpha(ProximityParent):
    r"""Compute alpha recombination.

    Multiply alpha and VT/Pi matrices (in this function named M) and
    compute the operator norm.
    """

    def __init__(self, M):
        r"""Initialize class attributes."""
        self.M = M
        self.op = self.recombine
        self.adj_op = self.adj_rec

        U, s, Vt = np.linalg.svd(self.M.dot(self.M.T), full_matrices=False)
        self.norm = np.sqrt(s[0])

    def recombine(self, x):
        r"""Return recombination."""
        return x.dot(self.M)

    def adj_rec(self, y):
        r"""Return adjoint recombination."""
        return y.dot(self.M.T)


class GMCAlikeProxL1(ProximityParent):
    """Classic l1 prox with GMCA-like decreasing weighting values.

    GMCA stand for Generalized Morphological Component Analysis.

    Parameters
    ----------
    iter_func: function
        Input function that calcultates the number of non-zero values to keep
        in each line at each iteration.

    Notes
    -----
    Not being used by the MCCD algorithm for the moment.
    """

    def __init__(self, iter_func, kmax):
        r"""Initialize class attributes."""
        self.iter_func = iter_func
        self.iter = 0
        self.iter_max = kmax

    def reset_iter(self):
        r"""Set iteration counter to zero."""
        self.iter = 0

    def op(self, data, extra_factor=1.0):
        r"""Return input data after thresholding."""
        self.iter += 1
        return self.op_tobi_prox_l1(data, self.iter, self.iter_max)

    def op_tobi_prox_l1(self, mat, k, kmax):
        r"""Apply GMCA hard-thresholding to each line of input matrix."""
        mat_out = np.copy(mat)
        shap = mat.shape
        for j in range(0, shap[0]):
            # GMCA-like threshold calculation
            line = mat_out[j, :]
            idx = np.floor(
                len(line) * np.max([0.9 - (k / kmax) * 3, 0.2])).astype(int)
            idx_thr = np.argsort(abs(line))[idx]
            thresh = abs(line[idx_thr])

            # Linear norm_inf decrease
            # thresh = np.max(mat_out[j,:])*np.max([0.9-(k/kmax)*3,0.2])
            # mat_out[j,:] = utils.SoftThresholding(mat[j,:],thresh)
            mat_out[j, :] = self.HardThresholding(mat_out[j, :], thresh)
        return mat_out

    @staticmethod
    def HardThresholding(data, thresh):
        r"""Perform element-wise hard thresholding."""
        data[data < thresh] = 0.
        return data

    def cost(self, x):
        r"""Cost function. To do."""
        return 0


class ClassicProxL2(ProximityParent):
    r"""This class defines the classic l2 prox.

    Notes
    -----
    ``prox_weights``: Corresponds to the weights of the weighted norm l_{w,2}.
    They are set by default to ones. Not being used in this implementation.
    ``beta_param``: Corresponds to the beta (or lambda) parameter that goes
    with the fucn tion we will calculate the prox on prox_{lambda f(.)}(y).
    ``iter``: Iteration number, just to follow track of the iterations.
    It could be part of the lambda update strategy for the prox calculation.

    Reference: « Mixed-norm estimates for the M/EEG inverse problem using
    accelerated gradient methods
    Alexandre Gramfort, Matthieu Kowalski, Matti Hämäläinen »
    """

    def __init__(self):
        r"""Initialize class attributes."""
        self.beta_param = 0
        self.iter = 0

    def set_beta_param(self, beta_param):
        r"""Set ``beta_param``."""
        self.beta_param = beta_param

    def reset_iter(self):
        """Set iteration counter to zero."""
        self.iter = 0

    def op(self, data, extra_factor=1.0):
        r"""Return input data after thresholding.

        The extra factor is the beta_param!
        Should be used on the proximal operator function.
        """
        self.iter += 1  # not used in this prox

        return self.op_tobi_prox_l2(data)

    def op_tobi_prox_l2(self, data):
        r"""Apply the opterator on the whole data matrix.

        for a vector: :math:`x = prox_{lambda || . ||^{2}_{w,2}}(y)`
        :math:`=> x_i = y_i /(1 + lambda w_i)`
        The operator can be used for the whole data matrix at once.
        """
        dividing_weight = 1. + self.beta_param

        return data / dividing_weight

    def cost(self, x):
        r"""Cost function. To do."""
        return 0
