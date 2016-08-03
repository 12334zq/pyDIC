# -*- coding: utf-8 -*-
__author__ = 'Domen Gorjup'

'''
Core of the Digital Image Correlation algorithm, implemented for use with the pyDIC application.
'''

import numpy as np
import scipy.ndimage
import scipy.signal
import scipy.interpolate


def zncc(im1, im2):
    '''
    Calculate the zero normalized cross-correlation coefficient of input images.

    :param im1: First input image.
    :param im2: Second input image.
    :return: zncc ([0,1]). If 1, input images match perfectly.
    '''
    nom = np.mean((im1-im1.mean())*(im2-im2.mean()))
    den = im1.std()*im2.std()
    if den == 0:
        return 0
    return nom/den


def get_initial_guess(target, roi_image):
    '''
    Get initial guess for subset ROI position in targer image, using FFT based  zero-mean cross correlation.

    :param target: Target image as 2D numpy array.
    :param roi_image: Region of Interest image as 2D numpy array.
    :return: (y,x) of the estimated translation vector.
    '''
    target = target - np.mean(target)
    roi_image = roi_image[::-1, ::-1] - np.mean(roi_image)

    corr_fft = scipy.signal.fftconvolve(target, roi_image, mode='valid')
    in_guess_fft = np.unravel_index(corr_fft.argmax(), corr_fft.shape)
    return np.array(in_guess_fft, dtype=np.float64), corr_fft


def get_gradient(image, kernel='central_fd', prefilter_gauss=True):
    '''
    Computes gradient of inputimage, using the specified convoluton kernels.

    :param image: Image to compute gradient of.
    :param kernel: Tuple of convolution kernels in x and y direction. Central finite difference used if left blank.
    :param prefilter_gauss: If True, the gradient kernel is first filtered with a Gauss filter to eliminate noise.
    :return: [gx, gy] (numpy array): Gradient images with respect to x and y direction.
    '''

    if kernel == 'central_fd':
        if prefilter_gauss:
            x_kernel = np.array([[-0.14086616, -0.20863973,  0.,  0.20863973,  0.14086616]])
        else:
            x_kernel = np.array([[1, -8, 0, 8, -1]], dtype=float)/12
        y_kernel = np.transpose(x_kernel)
    elif len(kernel) == 2 and len(kernel[0]) >= 3:
        x_kernel = kernel[0]
        y_kernel = kernel[1]
    else:
        raise ValueError('Please input valid gradient convolution kernels!')

    g_x = scipy.signal.convolve2d(image, x_kernel, mode='same')
    g_y = scipy.signal.convolve2d(image, y_kernel, mode='same')
    return np.array([g_x, g_y], dtype=np.float64)


def jacobian_rigid(h, w):
    '''
    Returns the jacobian of a rigid body (only translations and rotations) affine wrap function with 3 parameters.
    All elements are structured in the original ROI shape, to allow for simple summation later in the process.

    :param h: Height of the ROI, used for parameter optimization.
    :param w: Width of the ROI, used for parameter optimization.
    :return: jac (2x3 numpy array: Jacobian matrix of a 3-parameter affine warp function.

        jac = [[dWx/dp1, dWx/dp2, dWx/dp3],
               [dWy/dp1, dWy/dp2, dWy/dp3]]
        (p1 = vy, p2 = ux, p2 = phi)
    '''
    ones = np.ones((h, w), dtype=np.float64)
    zeros = np.zeros((h, w), dtype=np.float64)
    x,y = np.meshgrid(np.arange(w).astype(np.float64), np.arange(h).astype(np.float64))
    jac = np.array([[zeros, ones, -y],
                    [ones, zeros, x]])
    return jac


def sd_images(grad, jac):
    '''
    Calculates the steepest descent images - the product of a given gradient and jacobian.
    Shaped(1, n_param), where n_param is the number of transformation parameters.

    :param grad: Gradient vector of initial ROI.
    :param jac: Jacobian matrix of the warp function.
    :return: sd_images: Array of steepest-descent images.
    '''
    gx, gy = grad
    jx, jy = jac
    sd_x = gx * jx
    sd_y = gy * jy
    sd_image = sd_x + sd_y
    return sd_image.astype(np.float64)


def hessian(sd_im, n_param):
    '''
    Calculates the symmetric Hessian matrix form given steepest descent images.
    Performs the 'dot product' sd_im.T * sd_im, multiplying the elements of steepest
    descent images element-wise, to easily perform a summation over all coordinates.

    :param sd_im: Steepest-descent images array of initial ROI.
    :param n_param: Number of warp function parameters.
    :return: H: Hessian matrix of second order partial derivatives for initial ROI.
    '''
    H = np.zeros((n_param, n_param))
    for i in range(n_param):
        h1 = sd_im[i]
        for j in range(n_param):
            if j >= i :
                h2 = sd_im[j]
                H[i, j] = np.sum(h1*h2)
            else:
                H[i, j] = H[j, i]
    return H.astype(np.float64)


def rigid_transform_matrix(p):
    '''
    Given the three transformation parameters, returns the corresponding transformation matrix.

    :param p: Array of three transformation parameters.
        p = [y, x, phi]
    :return: 3x3 matrix of the rigid-body (translations and rotation only) affine warp function.
    '''
    matrix = np.array([[np.cos(p[2]), -np.sin(p[2]), p[1]],
                    [np.sin(p[2]), np.cos(p[2]), p[0]],
                    [0, 0, 1]], dtype=np.float64)
    return matrix


def param_from_rt_matrix(matrix):
    '''
    Get array of transformation parameters from the rigid transform warp matrix.

    :param matrix: Transformation matrix with 3 parameters (y-translation, x-translation, clockwise rotation).
    :return: Array of transformation parameters.
        p = [vy, ux, phi]
    '''
    vy = matrix[1, -1]
    ux = matrix[0, -1]
    try:
        phi = np.arcsin(matrix[1, 0])
    except ValueError:
        phi = np.arccos(matrix[0, 0])
    return np.array([vy, ux, phi], dtype=np.float64)



def coordinate_warp(matrix, output_shape):
    '''
    Wraps the initial coordinate set of given shape, with reference at (0,0), using the given transformation parameters.

    :param matrix: x3 matrix of the affine warp function.
    :param output_shape: Shape of ROI, used to produce the initial coordinate grid to transform.
    :return: Tuple of (x, y) coordinates of the warped ROI, at which gray values must then be interpolated.
    '''
    h, w = output_shape
    x_array = np.tile(np.arange(w), h)
    y_array = np.repeat(np.arange(h), w)
    xy_h = np.vstack((np.vstack((x_array, y_array)), np.ones(w*h))).astype(np.float64)
    uv_h = np.dot(matrix, xy_h)
    uv = uv_h[:-1]
    return uv[0], uv[1]


def interpolate_warp(xi, yi, target, output_shape, spl=None, order=3):
    '''
    Returns the subimage of target at persumably non-integer coordinates xi, yi, using bivariate spline
    interpolatio of given order. The output is reshaped into the original ROI shape, specified in output_shape.
    If spline object is given, only use it to compute gray values at (yi, xi).

    :param xi: Array of x-axis coordiates, at which interpolated gray values are computed.
    :param yi: Array of y-axis coordiates, at which interpolated gray values are computed.
    :param target: The image to extract interpolated gray values from (the current, target image in DIC).
    :param output_shape: ROI shape, to which the resulting arrays are reshaped. Must match the input coordiante arrays.
    :param spl: scipy.interpolate.RectBivariateSpline object, used to compute gray values of current image.
    :param order: Order of bivariate spline interpolation. Default: 3.
    :return: Image of the new, warped ROI, extracted from target image at input coordinates.
    '''
    if not spl:
        h,w = target.shape
        spl = scipy.interpolate.RectBivariateSpline(x=np.arange(h),
                                                    y=np.arange(w),
                                                    z=target,
                                                    kx=order,
                                                    ky=order,
                                                    s=0)
    values = spl.ev(yi, xi).astype(np.float64)
    warped_ROI = values.reshape(output_shape)
    return warped_ROI


def get_error_image(f, f_stats, g):
    '''
    Computes the "error image" according to the Zero-Normalized Sum of Squares criterion.

    :param f: Reference Region Of Interest image.
    :param f_stats: Mean and standard deviation of reference image gray values.
    :param g: The current (target) ROI image, warped using the current transformation parameters p.
    :return: The error image.
    '''
    f_, sd_f = f_stats
    g_ = np.mean(g)
    sd_g = np.std(g)
    err_im = (f - f_) - sd_f/sd_g * (g - g_)
    return err_im.astype(np.float64)


def get_sd_error_vector(sd_im, error_im, n_param):
    '''
    Calculates the right-side vector of the warp parameter optimization equation system.
    Performs the 'dot product' of sd_im.T and error_im , multiplying the elements of steepest
    descent images element-wise, to easily perform a summation over all coordinates.

    :param sd_im: Steepest-descent images array of initial ROI.
    :param error_im: Error image according to the Zero-Normalized Sum of Squares criterion.
    :param n_param: Number of warp function parameters.
    :return: Right-side vector of parameter optimization equation system.
    '''
    b = np.zeros(n_param)
    for i in range(n_param):
        h1 = sd_im[i]
        b[i] = np.sum(h1*error_im)
    return b.astype(np.float64)


def warp_update(inv_H, b, warp):
    '''
    Updates the current warp function with optimal parameters, according to ZNSSD criterion and the inverse composite
    Gauss-Newton optimization algorithm.

    :param inv_H: Inverse of the reference image Hessian matrix.
    :param b: Right-side vector of parameter optimization equation system.
    :param warp: The current warp function matrix to be updated.
    :return: The updated warp function transformation matrix.
    '''
    dp = np.dot(inv_H, b)
    inverse_increment_warp = np.linalg.inv(rigid_transform_matrix(dp))
    updated_warp = np.dot(warp, inverse_increment_warp)
    return updated_warp