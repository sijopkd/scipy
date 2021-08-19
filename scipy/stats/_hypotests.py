from collections import namedtuple
from dataclasses import make_dataclass
import numpy as np
import warnings
from itertools import combinations
import scipy.stats
from scipy.optimize import shgo
from . import distributions
from ._continuous_distns import chi2, norm
from scipy.special import gamma, kv, gammaln
from . import _wilcoxon_data
import scipy.stats.stats
from functools import wraps
from scipy._lib._docscrape import FunctionDoc, Parameter
import inspect
from ._hypotests_pythran import _Q, _P, _a_ij_Aij_Dij2

__all__ = ['epps_singleton_2samp', 'cramervonmises', 'somersd',
           'barnard_exact', 'boschloo_exact', 'cramervonmises_2samp']


# TODO: add support for `axis` tuples
def _remove_nans(samples, paired):
    "Remove nans from paired or unpaired samples"
    # potential optimization: don't copy arrays that don't contain nans
    if not paired:
        return [sample[~np.isnan(sample)] for sample in samples]

    # for paired samples, we need to remove the whole pair when any part
    # has a nan
    nans = np.isnan(samples[0])
    for sample in samples[1:]:
        nans = nans | np.isnan(sample)
    not_nans = ~nans
    return [sample[not_nans] for sample in samples]


def _check_empty_inputs(samples, axis):
    """
    Check for empty sample; return appropriate output for a vectorized hypotest
    """
    # if none of the samples are empty, we need to perform the test
    if not any((sample.size == 0 for sample in samples)):
        return None
    # otherwise, the statistic and p-value will be either empty arrays or
    # arrays with NaNs. Produce the appropriate array and return it.
    output_shape = scipy.stats.stats._broadcast_array_shapes(samples, axis)
    output = np.ones(output_shape) * np.nan
    return output


# Standard docstring / signature entries for `axis` and `nan_policy`
_name = 'axis'
_type = "int or None, default: 0"
_desc = (
    """If an int, the axis of the input along which to compute the statistic.
    The statistic of each axis-slice (e.g. row) of the input will appear in a
    corresponding element of the output.
    If ``None``, the input will be raveled before computing the statistic."""
    .split('\n'))
_axis_parameter_doc = Parameter(_name, _type, _desc)
_axis_parameter = inspect.Parameter(_name,
                                    inspect.Parameter.KEYWORD_ONLY,
                                    default=0)

_name = 'nan_policy'
_type = "{'propagate', 'omit', 'raise'}"
_desc = (
    """Defines how to handle input NaNs.

    - ``propagate``: if a NaN is present in the axis slice (e.g. row) along
      which the  statistic is computed, the corresponding entry of the output
      will be NaN.
    - ``omit``: NaNs will be omitted when performing the calculation.
      If insufficient data remains in the axis slice along which the
      statistic is computed, the corresponding entry of the output will be
      NaN.
    - ``raise``: if a NaN is present, a ``ValueError`` will be raised."""
    .split('\n'))
_nan_policy_parameter_doc = Parameter(_name, _type, _desc)
_nan_policy_parameter = inspect.Parameter(_name,
                                          inspect.Parameter.KEYWORD_ONLY,
                                          default='propagate')


def _vectorize_hypotest_factory(result_object, default_axis=0,
                                n_samples=1, paired=False,
                                result_unpacker=None, too_small=0):
    """Factory for a wrapper that adds axis/nan_policy params to a function.

    Parameters
    ----------
    result_object : callable
        Callable that returns an object of the type returned by the function
        being wrapped (e.g. the namedtuple or dataclass returned by a
        statistical test) provided the separate components (e.g. statistic,
        pvalue).
    default_axis : int, default: 0
        The default value of the axis argument. Standard is 0 except when
        backwards compatibility demands otherwise (e.g. `None`).
    n_samples : int, default: 1
        The number of data samples accepted by the function. For example,
        `gmean` accepts one sample, `ks_2samp` accepts two samples. `None`
        means an arbitrary number of samples (e.g. `fligner`).
    paired : {False, True}
        Whether the function being wrapped treats the samples as paired (i.e.
        corresponding elements of each sample should be considered as different
        components of the same sample.)
    result_unpacker : callable, optional
        Function that unpacks the results of the function being wrapped into
        a tuple. This is essentially the inverse of `result_object`. Default
        is `None`, which is appropriate for statistical tests that return a
        statistic, pvalue tuple (rather than, e.g., a non-iterable datalass).
    too_small : int, default: 0
        The largest unnacceptably small sample for the function being wrapped.
        For example, some functions require samples of size two or more or they
        raise an error. This argument prevents the error from being raised when
        input is not 1D and instead places a NaN in the corresponding element
        of the result.
    """

    if result_unpacker is None:
        def result_unpacker(res):
            return res[..., 0], res[..., 1]

    def is_too_small(samples):
        for sample in samples:
            if len(sample) <= too_small:
                return True
        return False

    def vectorize_hypotest_decorator(hypotest_fun_in):
        @wraps(hypotest_fun_in)
        def vectorize_hypotest_wrapper(*args, _no_deco=False, **kwds):

            if _no_deco:  # for testing, decorator does nothing
                return hypotest_fun_in(*args, **kwds)

            # We need to be flexible about whether position or keyword
            # arguments are used, but we need to make sure users don't pass
            # both for the same parameter. To complicate matters, some
            # functions accept samples with *args, and some functions already
            # accept `axis` and `nan_policy` as positional arguments.
            # The strategy is to make sure that there is no duplication
            # between `args` and `kwds`, combine the two into `kwds`, then
            # the samples, `nan_policy`, and `axis` from `kwds`, as they are
            # dealt with separately.

            # Check for intersection between positional and keyword args
            params = list(inspect.signature(hypotest_fun_in).parameters)
            if n_samples is None:
                # Give unique names to each positional sample argument
                # Note that *args can't be provided as a keyword argument
                params = [f"arg{i}" for i in range(len(args))] + params[1:]
            n_samp = n_samples or len(args)  # rename avoids UnboundLocalError
            d_args = dict(zip(params, args))
            intersection = set(d_args) & set(kwds)
            if intersection:
                message = (f"{hypotest_fun_in.__name__}() got multiple values "
                           f"for argument '{list(intersection)[0]}'")
                raise TypeError(message)

            # Consolidate other positional and keyword args into `kwds`
            kwds.update(d_args)

            # Extract the things we need here
            samples = [np.atleast_1d(kwds.pop(param))
                       for param in params[:n_samp]]
            vectorized = True if 'axis' in params else False
            axis = kwds.pop('axis', default_axis)
            nan_policy = kwds.pop('nan_policy', 'propagate')
            del args  # avoid the possibility of passing both `args` and `kwds`

            if axis is None:
                samples = [sample.ravel() for sample in samples]
                axis = 0

            # if axis is not needed, just handle nan_policy and return
            ndims = np.array([sample.ndim for sample in samples])
            if np.all(ndims <= 1):
                # Addresses nan_policy == "raise"
                contains_nans = []
                for sample in samples:
                    contains_nan, _ = (
                        scipy.stats.stats._contains_nan(sample, nan_policy))
                    contains_nans.append(contains_nan)

                # Addresses nan_policy == "propagate"
                # Consider adding option to let function propagate nans, but
                # currently the hypothesis tests this is applied to do not
                # propagate nans in a sensible way
                if any(contains_nans) and nan_policy == 'propagate':
                    return result_object(np.nan, np.nan)

                # Addresses nan_policy == "omit"
                if any(contains_nans) and nan_policy == 'omit':
                    # consider passing in contains_nans
                    samples = _remove_nans(samples, paired)

                # ideally, this is what the behavior would be, but some
                # existing functions raise exceptions, so overriding it
                # would break backward compatibility.
                # if is_too_small(samples):
                #     return result_object(np.nan, np.nan)

                return hypotest_fun_in(*samples, **kwds)

            # check for empty input
            # ideally, move this to the top, but some existing functions raise
            # exceptions for empty input, so overriding it would break
            # backward compatibility.
            empty_output = _check_empty_inputs(samples, axis)
            if empty_output is not None:
                statistic = empty_output
                pvalue = empty_output.copy()
                return result_object(statistic, pvalue)

            # otherwise, concatenate all samples along axis, remembering where
            # each separate sample begins
            lengths = np.array([sample.shape[axis] for sample in samples])
            split_indices = np.cumsum(lengths)
            x = scipy.stats.stats._broadcast_concatenate(samples, axis)

            # Addresses nan_policy == "raise"
            contains_nan, _ = (
                scipy.stats.stats._contains_nan(x, nan_policy))

            if vectorized and not contains_nan:
                return hypotest_fun_in(*samples, axis=axis, **kwds)

            # Addresses nan_policy == "omit"
            if contains_nan and nan_policy == 'omit':
                def hypotest_fun(x):
                    samples = np.split(x, split_indices)[:n_samp]
                    samples = _remove_nans(samples, paired)
                    if is_too_small(samples):
                        return result_object(np.nan, np.nan)
                    return hypotest_fun_in(*samples, **kwds)

            # Addresses nan_policy == "propagate"
            elif contains_nan and nan_policy == 'propagate':
                def hypotest_fun(x):
                    if np.isnan(x).any():
                        return result_object(np.nan, np.nan)
                    samples = np.split(x, split_indices)[:n_samp]
                    return hypotest_fun_in(*samples, **kwds)

            else:
                def hypotest_fun(x):
                    samples = np.split(x, split_indices)[:n_samp]
                    return hypotest_fun_in(*samples, **kwds)

            x = np.moveaxis(x, axis, -1)
            res = np.apply_along_axis(hypotest_fun, axis=-1, arr=x)
            return result_object(*result_unpacker(res))

        doc = FunctionDoc(vectorize_hypotest_wrapper)
        parameter_names = [param.name for param in doc['Parameters']]
        if 'axis' in parameter_names:
            doc['Parameters'][parameter_names.index('axis')] = (
                _axis_parameter_doc)
        else:
            doc['Parameters'].append(_axis_parameter_doc)
        if 'nan_policy' in parameter_names:
            doc['Parameters'][parameter_names.index('nan_policy')] = (
                _nan_policy_parameter_doc)
        else:
            doc['Parameters'].append(_nan_policy_parameter_doc)
        doc = str(doc).split("\n", 1)[1]  # remove signature
        vectorize_hypotest_wrapper.__doc__ = str(doc)

        sig = inspect.signature(vectorize_hypotest_wrapper)
        parameters = sig.parameters
        parameter_list = list(parameters.values())
        if 'axis' not in parameters:
            parameter_list.append(_axis_parameter)
        if 'nan_policy' not in parameters:
            parameter_list.append(_nan_policy_parameter)
        sig = sig.replace(parameters=parameter_list)
        vectorize_hypotest_wrapper.__signature__ = sig

        return vectorize_hypotest_wrapper
    return vectorize_hypotest_decorator


Epps_Singleton_2sampResult = namedtuple('Epps_Singleton_2sampResult',
                                        ('statistic', 'pvalue'))


@_vectorize_hypotest_factory(Epps_Singleton_2sampResult, n_samples=2,
                             too_small=5)
def epps_singleton_2samp(x, y, t=(0.4, 0.8)):
    """Compute the Epps-Singleton (ES) test statistic.

    Test the null hypothesis that two samples have the same underlying
    probability distribution.

    Parameters
    ----------
    x, y : array-like
        The two samples of observations to be tested. Input must not have more
        than one dimension. Samples can have different lengths.
    t : array-like, optional
        The points (t1, ..., tn) where the empirical characteristic function is
        to be evaluated. It should be positive distinct numbers. The default
        value (0.4, 0.8) is proposed in [1]_. Input must not have more than
        one dimension.

    Returns
    -------
    statistic : float
        The test statistic.
    pvalue : float
        The associated p-value based on the asymptotic chi2-distribution.

    See Also
    --------
    ks_2samp, anderson_ksamp

    Notes
    -----
    Testing whether two samples are generated by the same underlying
    distribution is a classical question in statistics. A widely used test is
    the Kolmogorov-Smirnov (KS) test which relies on the empirical
    distribution function. Epps and Singleton introduce a test based on the
    empirical characteristic function in [1]_.

    One advantage of the ES test compared to the KS test is that is does
    not assume a continuous distribution. In [1]_, the authors conclude
    that the test also has a higher power than the KS test in many
    examples. They recommend the use of the ES test for discrete samples as
    well as continuous samples with at least 25 observations each, whereas
    `anderson_ksamp` is recommended for smaller sample sizes in the
    continuous case.

    The p-value is computed from the asymptotic distribution of the test
    statistic which follows a `chi2` distribution. If the sample size of both
    `x` and `y` is below 25, the small sample correction proposed in [1]_ is
    applied to the test statistic.

    The default values of `t` are determined in [1]_ by considering
    various distributions and finding good values that lead to a high power
    of the test in general. Table III in [1]_ gives the optimal values for
    the distributions tested in that study. The values of `t` are scaled by
    the semi-interquartile range in the implementation, see [1]_.

    References
    ----------
    .. [1] T. W. Epps and K. J. Singleton, "An omnibus test for the two-sample
       problem using the empirical characteristic function", Journal of
       Statistical Computation and Simulation 26, p. 177--203, 1986.

    .. [2] S. J. Goerg and J. Kaiser, "Nonparametric testing of distributions
       - the Epps-Singleton two-sample test using the empirical characteristic
       function", The Stata Journal 9(3), p. 454--465, 2009.

    """
    x, y, t = np.asarray(x), np.asarray(y), np.asarray(t)
    # check if x and y are valid inputs
    if x.ndim > 1:
        raise ValueError('x must be 1d, but x.ndim equals {}.'.format(x.ndim))
    if y.ndim > 1:
        raise ValueError('y must be 1d, but y.ndim equals {}.'.format(y.ndim))
    nx, ny = len(x), len(y)
    if (nx < 5) or (ny < 5):
        raise ValueError('x and y should have at least 5 elements, but len(x) '
                         '= {} and len(y) = {}.'.format(nx, ny))
    if not np.isfinite(x).all():
        raise ValueError('x must not contain nonfinite values.')
    if not np.isfinite(y).all():
        raise ValueError('y must not contain nonfinite values.')
    n = nx + ny

    # check if t is valid
    if t.ndim > 1:
        raise ValueError('t must be 1d, but t.ndim equals {}.'.format(t.ndim))
    if np.less_equal(t, 0).any():
        raise ValueError('t must contain positive elements only.')

    # rescale t with semi-iqr as proposed in [1]; import iqr here to avoid
    # circular import
    from scipy.stats import iqr
    sigma = iqr(np.hstack((x, y))) / 2
    ts = np.reshape(t, (-1, 1)) / sigma

    # covariance estimation of ES test
    gx = np.vstack((np.cos(ts*x), np.sin(ts*x))).T  # shape = (nx, 2*len(t))
    gy = np.vstack((np.cos(ts*y), np.sin(ts*y))).T
    cov_x = np.cov(gx.T, bias=True)  # the test uses biased cov-estimate
    cov_y = np.cov(gy.T, bias=True)
    est_cov = (n/nx)*cov_x + (n/ny)*cov_y
    est_cov_inv = np.linalg.pinv(est_cov)
    r = np.linalg.matrix_rank(est_cov_inv)
    if r < 2*len(t):
        warnings.warn('Estimated covariance matrix does not have full rank. '
                      'This indicates a bad choice of the input t and the '
                      'test might not be consistent.')  # see p. 183 in [1]_

    # compute test statistic w distributed asympt. as chisquare with df=r
    g_diff = np.mean(gx, axis=0) - np.mean(gy, axis=0)
    w = n*np.dot(g_diff.T, np.dot(est_cov_inv, g_diff))

    # apply small-sample correction
    if (max(nx, ny) < 25):
        corr = 1.0/(1.0 + n**(-0.45) + 10.1*(nx**(-1.7) + ny**(-1.7)))
        w = corr * w

    p = chi2.sf(w, r)

    return Epps_Singleton_2sampResult(w, p)


class CramerVonMisesResult:
    def __init__(self, statistic, pvalue):
        self.statistic = statistic
        self.pvalue = pvalue

    def __repr__(self):
        return (f"{self.__class__.__name__}(statistic={self.statistic}, "
                f"pvalue={self.pvalue})")


def _psi1_mod(x):
    """
    psi1 is defined in equation 1.10 in Csorgo, S. and Faraway, J. (1996).
    This implements a modified version by excluding the term V(x) / 12
    (here: _cdf_cvm_inf(x) / 12) to avoid evaluating _cdf_cvm_inf(x)
    twice in _cdf_cvm.

    Implementation based on MAPLE code of Julian Faraway and R code of the
    function pCvM in the package goftest (v1.1.1), permission granted
    by Adrian Baddeley. Main difference in the implementation: the code
    here keeps adding terms of the series until the terms are small enough.
    """

    def _ed2(y):
        z = y**2 / 4
        b = kv(1/4, z) + kv(3/4, z)
        return np.exp(-z) * (y/2)**(3/2) * b / np.sqrt(np.pi)

    def _ed3(y):
        z = y**2 / 4
        c = np.exp(-z) / np.sqrt(np.pi)
        return c * (y/2)**(5/2) * (2*kv(1/4, z) + 3*kv(3/4, z) - kv(5/4, z))

    def _Ak(k, x):
        m = 2*k + 1
        sx = 2 * np.sqrt(x)
        y1 = x**(3/4)
        y2 = x**(5/4)

        e1 = m * gamma(k + 1/2) * _ed2((4 * k + 3)/sx) / (9 * y1)
        e2 = gamma(k + 1/2) * _ed3((4 * k + 1) / sx) / (72 * y2)
        e3 = 2 * (m + 2) * gamma(k + 3/2) * _ed3((4 * k + 5) / sx) / (12 * y2)
        e4 = 7 * m * gamma(k + 1/2) * _ed2((4 * k + 1) / sx) / (144 * y1)
        e5 = 7 * m * gamma(k + 1/2) * _ed2((4 * k + 5) / sx) / (144 * y1)

        return e1 + e2 + e3 + e4 + e5

    x = np.asarray(x)
    tot = np.zeros_like(x, dtype='float')
    cond = np.ones_like(x, dtype='bool')
    k = 0
    while np.any(cond):
        z = -_Ak(k, x[cond]) / (np.pi * gamma(k + 1))
        tot[cond] = tot[cond] + z
        cond[cond] = np.abs(z) >= 1e-7
        k += 1

    return tot


def _cdf_cvm_inf(x):
    """
    Calculate the cdf of the Cramér-von Mises statistic (infinite sample size).

    See equation 1.2 in Csorgo, S. and Faraway, J. (1996).

    Implementation based on MAPLE code of Julian Faraway and R code of the
    function pCvM in the package goftest (v1.1.1), permission granted
    by Adrian Baddeley. Main difference in the implementation: the code
    here keeps adding terms of the series until the terms are small enough.

    The function is not expected to be accurate for large values of x, say
    x > 4, when the cdf is very close to 1.
    """
    x = np.asarray(x)

    def term(x, k):
        # this expression can be found in [2], second line of (1.3)
        u = np.exp(gammaln(k + 0.5) - gammaln(k+1)) / (np.pi**1.5 * np.sqrt(x))
        y = 4*k + 1
        q = y**2 / (16*x)
        b = kv(0.25, q)
        return u * np.sqrt(y) * np.exp(-q) * b

    tot = np.zeros_like(x, dtype='float')
    cond = np.ones_like(x, dtype='bool')
    k = 0
    while np.any(cond):
        z = term(x[cond], k)
        tot[cond] = tot[cond] + z
        cond[cond] = np.abs(z) >= 1e-7
        k += 1

    return tot


def _cdf_cvm(x, n=None):
    """
    Calculate the cdf of the Cramér-von Mises statistic for a finite sample
    size n. If N is None, use the asymptotic cdf (n=inf).

    See equation 1.8 in Csorgo, S. and Faraway, J. (1996) for finite samples,
    1.2 for the asymptotic cdf.

    The function is not expected to be accurate for large values of x, say
    x > 2, when the cdf is very close to 1 and it might return values > 1
    in that case, e.g. _cdf_cvm(2.0, 12) = 1.0000027556716846.
    """
    x = np.asarray(x)
    if n is None:
        y = _cdf_cvm_inf(x)
    else:
        # support of the test statistic is [12/n, n/3], see 1.1 in [2]
        y = np.zeros_like(x, dtype='float')
        sup = (1./(12*n) < x) & (x < n/3.)
        # note: _psi1_mod does not include the term _cdf_cvm_inf(x) / 12
        # therefore, we need to add it here
        y[sup] = _cdf_cvm_inf(x[sup]) * (1 + 1./(12*n)) + _psi1_mod(x[sup]) / n
        y[x >= n/3] = 1

    if y.ndim == 0:
        return y[()]
    return y


@_vectorize_hypotest_factory(
        CramerVonMisesResult, n_samples=1, too_small=1,
        result_unpacker=np.vectorize(lambda res: (res.statistic, res.pvalue)))
def cramervonmises(rvs, cdf, args=()):
    """Perform the one-sample Cramér-von Mises test for goodness of fit.

    This performs a test of the goodness of fit of a cumulative distribution
    function (cdf) :math:`F` compared to the empirical distribution function
    :math:`F_n` of observed random variates :math:`X_1, ..., X_n` that are
    assumed to be independent and identically distributed ([1]_).
    The null hypothesis is that the :math:`X_i` have cumulative distribution
    :math:`F`.

    Parameters
    ----------
    rvs : array_like
        A 1-D array of observed values of the random variables :math:`X_i`.
    cdf : str or callable
        The cumulative distribution function :math:`F` to test the
        observations against. If a string, it should be the name of a
        distribution in `scipy.stats`. If a callable, that callable is used
        to calculate the cdf: ``cdf(x, *args) -> float``.
    args : tuple, optional
        Distribution parameters. These are assumed to be known; see Notes.

    Returns
    -------
    res : object with attributes
        statistic : float
            Cramér-von Mises statistic.
        pvalue : float
            The p-value.

    See Also
    --------
    kstest, cramervonmises_2samp

    Notes
    -----
    .. versionadded:: 1.6.0

    The p-value relies on the approximation given by equation 1.8 in [2]_.
    It is important to keep in mind that the p-value is only accurate if
    one tests a simple hypothesis, i.e. the parameters of the reference
    distribution are known. If the parameters are estimated from the data
    (composite hypothesis), the computed p-value is not reliable.

    References
    ----------
    .. [1] Cramér-von Mises criterion, Wikipedia,
           https://en.wikipedia.org/wiki/Cram%C3%A9r%E2%80%93von_Mises_criterion
    .. [2] Csorgo, S. and Faraway, J. (1996). The Exact and Asymptotic
           Distribution of Cramér-von Mises Statistics. Journal of the
           Royal Statistical Society, pp. 221-234.

    Examples
    --------

    Suppose we wish to test whether data generated by ``scipy.stats.norm.rvs``
    were, in fact, drawn from the standard normal distribution. We choose a
    significance level of alpha=0.05.

    >>> from scipy import stats
    >>> rng = np.random.default_rng()
    >>> x = stats.norm.rvs(size=500, random_state=rng)
    >>> res = stats.cramervonmises(x, 'norm')
    >>> res.statistic, res.pvalue
    (0.49121480855028343, 0.04189256516661377)

    The p-value 0.79 exceeds our chosen significance level, so we do not
    reject the null hypothesis that the observed sample is drawn from the
    standard normal distribution.

    Now suppose we wish to check whether the same samples shifted by 2.1 is
    consistent with being drawn from a normal distribution with a mean of 2.

    >>> y = x + 2.1
    >>> res = stats.cramervonmises(y, 'norm', args=(2,))
    >>> res.statistic, res.pvalue
    (0.07400330012187435, 0.7274595666160468)

    Here we have used the `args` keyword to specify the mean (``loc``)
    of the normal distribution to test the data against. This is equivalent
    to the following, in which we create a frozen normal distribution with
    mean 2.1, then pass its ``cdf`` method as an argument.

    >>> frozen_dist = stats.norm(loc=2)
    >>> res = stats.cramervonmises(y, frozen_dist.cdf)
    >>> res.statistic, res.pvalue
    (0.07400330012187435, 0.7274595666160468)

    In either case, we would reject the null hypothesis that the observed
    sample is drawn from a normal distribution with a mean of 2 (and default
    variance of 1) because the p-value 0.04 is less than our chosen
    significance level.

    """
    if isinstance(cdf, str):
        cdf = getattr(distributions, cdf).cdf

    vals = np.sort(np.asarray(rvs))

    if vals.size <= 1:
        raise ValueError('The sample must contain at least two observations.')
    if vals.ndim > 1:
        raise ValueError('The sample must be one-dimensional.')

    n = len(vals)
    cdfvals = cdf(vals, *args)

    u = (2*np.arange(1, n+1) - 1)/(2*n)
    w = 1/(12*n) + np.sum((u - cdfvals)**2)

    # avoid small negative values that can occur due to the approximation
    p = max(0, 1. - _cdf_cvm(w, n))

    return CramerVonMisesResult(statistic=w, pvalue=p)


def _get_wilcoxon_distr(n):
    """
    Distribution of counts of the Wilcoxon ranksum statistic r_plus (sum of
    ranks of positive differences).
    Returns an array with the counts/frequencies of all the possible ranks
    r = 0, ..., n*(n+1)/2
    """
    cnt = _wilcoxon_data.COUNTS.get(n)

    if cnt is None:
        raise ValueError("The exact distribution of the Wilcoxon test "
                         "statistic is not implemented for n={}".format(n))

    return np.array(cnt, dtype=int)


def _tau_b(A):
    """Calculate Kendall's tau-b and p-value from contingency table."""
    # See [2] 2.2 and 4.2

    # contingency table must be truly 2D
    if A.shape[0] == 1 or A.shape[1] == 1:
        return np.nan, np.nan

    NA = A.sum()
    PA = _P(A)
    QA = _Q(A)
    Sri2 = (A.sum(axis=1)**2).sum()
    Scj2 = (A.sum(axis=0)**2).sum()
    denominator = (NA**2 - Sri2)*(NA**2 - Scj2)

    tau = (PA-QA)/(denominator)**0.5

    numerator = 4*(_a_ij_Aij_Dij2(A) - (PA - QA)**2 / NA)
    s02_tau_b = numerator/denominator
    if s02_tau_b == 0:  # Avoid divide by zero
        return tau, 0
    Z = tau/s02_tau_b**0.5
    p = 2*norm.sf(abs(Z))  # 2-sided p-value

    return tau, p


def _somers_d(A):
    """Calculate Somers' D and p-value from contingency table."""
    # See [3] page 1740

    # contingency table must be truly 2D
    if A.shape[0] <= 1 or A.shape[1] <= 1:
        return np.nan, np.nan

    NA = A.sum()
    NA2 = NA**2
    PA = _P(A)
    QA = _Q(A)
    Sri2 = (A.sum(axis=1)**2).sum()

    d = (PA - QA)/(NA2 - Sri2)

    S = _a_ij_Aij_Dij2(A) - (PA-QA)**2/NA
    if S == 0:  # Avoid divide by zero
        return d, 0
    Z = (PA - QA)/(4*(S))**0.5
    p = 2*norm.sf(abs(Z))  # 2-sided p-value

    return d, p


SomersDResult = make_dataclass("SomersDResult",
                               ("statistic", "pvalue", "table"))


def somersd(x, y=None):
    r"""Calculates Somers' D, an asymmetric measure of ordinal association.

    Like Kendall's :math:`\tau`, Somers' :math:`D` is a measure of the
    correspondence between two rankings. Both statistics consider the
    difference between the number of concordant and discordant pairs in two
    rankings :math:`X` and :math:`Y`, and both are normalized such that values
    close  to 1 indicate strong agreement and values close to -1 indicate
    strong disagreement. They differ in how they are normalized. To show the
    relationship, Somers' :math:`D` can be defined in terms of Kendall's
    :math:`\tau_a`:

    .. math::
        D(Y|X) = \frac{\tau_a(X, Y)}{\tau_a(X, X)}

    Suppose the first ranking :math:`X` has :math:`r` distinct ranks and the
    second ranking :math:`Y` has :math:`s` distinct ranks. These two lists of
    :math:`n` rankings can also be viewed as an :math:`r \times s` contingency
    table in which element :math:`i, j` is the number of rank pairs with rank
    :math:`i` in ranking :math:`X` and rank :math:`j` in ranking :math:`Y`.
    Accordingly, `somersd` also allows the input data to be supplied as a
    single, 2D contingency table instead of as two separate, 1D rankings.

    Note that the definition of Somers' :math:`D` is asymmetric: in general,
    :math:`D(Y|X) \neq D(X|Y)`. ``somersd(x, y)`` calculates Somers'
    :math:`D(Y|X)`: the "row" variable :math:`X` is treated as an independent
    variable, and the "column" variable :math:`Y` is dependent. For Somers'
    :math:`D(X|Y)`, swap the input lists or transpose the input table.

    Parameters
    ----------
    x: array_like
        1D array of rankings, treated as the (row) independent variable.
        Alternatively, a 2D contingency table.
    y: array_like
        If `x` is a 1D array of rankings, `y` is a 1D array of rankings of the
        same length, treated as the (column) dependent variable.
        If `x` is 2D, `y` is ignored.

    Returns
    -------
    res : SomersDResult
        A `SomersDResult` object with the following fields:

            correlation : float
               The Somers' :math:`D` statistic.
            pvalue : float
               The two-sided p-value for a hypothesis test whose null
               hypothesis is an absence of association, :math:`D=0`.
               See notes for more information.
            table : 2D array
               The contingency table formed from rankings `x` and `y` (or the
               provided contingency table, if `x` is a 2D array)

    See Also
    --------
    kendalltau : Calculates Kendall's tau, another correlation measure.
    weightedtau : Computes a weighted version of Kendall's tau.
    spearmanr : Calculates a Spearman rank-order correlation coefficient.
    pearsonr : Calculates a Pearson correlation coefficient.

    Notes
    -----
    This function follows the contingency table approach of [2]_ and
    [3]_. *p*-values are computed based on an asymptotic approximation of
    the test statistic distribution under the null hypothesis :math:`D=0`.

    Theoretically, hypothesis tests based on Kendall's :math:`tau` and Somers'
    :math:`D` should be identical.
    However, the *p*-values returned by `kendalltau` are based
    on the null hypothesis of *independence* between :math:`X` and :math:`Y`
    (i.e. the population from which pairs in :math:`X` and :math:`Y` are
    sampled contains equal numbers of all possible pairs), which is more
    specific than the null hypothesis :math:`D=0` used here. If the null
    hypothesis of independence is desired, it is acceptable to use the
    *p*-value returned by `kendalltau` with the statistic returned by
    `somersd` and vice versa. For more information, see [2]_.

    Contingency tables are formatted according to the convention used by
    SAS and R: the first ranking supplied (``x``) is the "row" variable, and
    the second ranking supplied (``y``) is the "column" variable. This is
    opposite the convention of Somers' original paper [1]_.

    References
    ----------
    .. [1] Robert H. Somers, "A New Asymmetric Measure of Association for
           Ordinal Variables", *American Sociological Review*, Vol. 27, No. 6,
           pp. 799--811, 1962.

    .. [2] Morton B. Brown and Jacqueline K. Benedetti, "Sampling Behavior of
           Tests for Correlation in Two-Way Contingency Tables", *Journal of
           the American Statistical Association* Vol. 72, No. 358, pp.
           309--315, 1977.

    .. [3] SAS Institute, Inc., "The FREQ Procedure (Book Excerpt)",
           *SAS/STAT 9.2 User's Guide, Second Edition*, SAS Publishing, 2009.

    .. [4] Laerd Statistics, "Somers' d using SPSS Statistics", *SPSS
           Statistics Tutorials and Statistical Guides*,
           https://statistics.laerd.com/spss-tutorials/somers-d-using-spss-statistics.php,
           Accessed July 31, 2020.

    Examples
    --------
    We calculate Somers' D for the example given in [4]_, in which a hotel
    chain owner seeks to determine the association between hotel room
    cleanliness and customer satisfaction. The independent variable, hotel
    room cleanliness, is ranked on an ordinal scale: "below average (1)",
    "average (2)", or "above average (3)". The dependent variable, customer
    satisfaction, is ranked on a second scale: "very dissatisfied (1)",
    "moderately dissatisfied (2)", "neither dissatisfied nor satisfied (3)",
    "moderately satisfied (4)", or "very satisfied (5)". 189 customers
    respond to the survey, and the results are cast into a contingency table
    with the hotel room cleanliness as the "row" variable and customer
    satisfaction as the "column" variable.

    +-----+-----+-----+-----+-----+-----+
    |     | (1) | (2) | (3) | (4) | (5) |
    +=====+=====+=====+=====+=====+=====+
    | (1) | 27  | 25  | 14  | 7   | 0   |
    +-----+-----+-----+-----+-----+-----+
    | (2) | 7   | 14  | 18  | 35  | 12  |
    +-----+-----+-----+-----+-----+-----+
    | (3) | 1   | 3   | 2   | 7   | 17  |
    +-----+-----+-----+-----+-----+-----+

    For example, 27 customers assigned their room a cleanliness ranking of
    "below average (1)" and a corresponding satisfaction of "very
    dissatisfied (1)". We perform the analysis as follows.

    >>> from scipy.stats import somersd
    >>> table = [[27, 25, 14, 7, 0], [7, 14, 18, 35, 12], [1, 3, 2, 7, 17]]
    >>> res = somersd(table)
    >>> res.statistic
    0.6032766111513396
    >>> res.pvalue
    1.0007091191074533e-27

    The value of the Somers' D statistic is approximately 0.6, indicating
    a positive correlation between room cleanliness and customer satisfaction
    in the sample.
    The *p*-value is very small, indicating a very small probability of
    observing such an extreme value of the statistic under the null
    hypothesis that the statistic of the entire population (from which
    our sample of 189 customers is drawn) is zero. This supports the
    alternative hypothesis that the true value of Somers' D for the population
    is nonzero.

    """
    x, y = np.array(x), np.array(y)
    if x.ndim == 1:
        if x.size != y.size:
            raise ValueError("Rankings must be of equal length.")
        table = scipy.stats.contingency.crosstab(x, y)[1]
    elif x.ndim == 2:
        if np.any(x < 0):
            raise ValueError("All elements of the contingency table must be "
                             "non-negative.")
        if np.any(x != x.astype(int)):
            raise ValueError("All elements of the contingency table must be "
                             "integer.")
        if x.nonzero()[0].size < 2:
            raise ValueError("At least two elements of the contingency table "
                             "must be nonzero.")
        table = x
    else:
        raise ValueError("x must be either a 1D or 2D array")
    d, p = _somers_d(table)
    return SomersDResult(d, p, table)


def _all_partitions(nx, ny):
    """
    Partition a set of indices into two fixed-length sets in all possible ways

    Partition a set of indices 0 ... nx + ny - 1 into two sets of length nx and
    ny in all possible ways (ignoring order of elements).
    """
    z = np.arange(nx+ny)
    for c in combinations(z, nx):
        x = np.array(c)
        mask = np.ones(nx+ny, bool)
        mask[x] = False
        y = z[mask]
        yield x, y


def _compute_log_combinations(n):
    """Compute all log combination of C(n, k)."""
    gammaln_arr = gammaln(np.arange(n + 1) + 1)
    return gammaln(n + 1) - gammaln_arr - gammaln_arr[::-1]


BarnardExactResult = make_dataclass(
    "BarnardExactResult", [("statistic", float), ("pvalue", float)]
)


def barnard_exact(table, alternative="two-sided", pooled=True, n=32):
    r"""Perform a Barnard exact test on a 2x2 contingency table.

    Parameters
    ----------
    table : array_like of ints
        A 2x2 contingency table.  Elements should be non-negative integers.

    alternative : {'two-sided', 'less', 'greater'}, optional
        Defines the null and alternative hypotheses. Default is 'two-sided'.
        Please see explanations in the Notes section below.

    pooled : bool, optional
        Whether to compute score statistic with pooled variance (as in
        Student's t-test, for example) or unpooled variance (as in Welch's
        t-test). Default is ``True``.

    n : int, optional
        Number of sampling points used in the construction of the sampling
        method. Note that this argument will automatically be converted to
        the next higher power of 2 since `scipy.stats.qmc.Sobol` is used to
        select sample points. Default is 32. Must be positive. In most cases,
        32 points is enough to reach good precision. More points comes at
        performance cost.

    Returns
    -------
    ber : BarnardExactResult
        A result object with the following attributes.

        statistic : float
            The Wald statistic with pooled or unpooled variance, depending
            on the user choice of `pooled`.

        pvalue : float
            P-value, the probability of obtaining a distribution at least as
            extreme as the one that was actually observed, assuming that the
            null hypothesis is true.

    See Also
    --------
    chi2_contingency : Chi-square test of independence of variables in a
        contingency table.
    fisher_exact : Fisher exact test on a 2x2 contingency table.
    boschloo_exact : Boschloo's exact test on a 2x2 contingency table,
        which is an uniformly more powerful alternative to Fisher's exact test.

    Notes
    -----
    Barnard's test is an exact test used in the analysis of contingency
    tables. It examines the association of two categorical variables, and
    is a more powerful alternative than Fisher's exact test
    for 2x2 contingency tables.

    Let's define :math:`X_0` a 2x2 matrix representing the observed sample,
    where each column stores the binomial experiment, as in the example
    below. Let's also define :math:`p_1, p_2` the theoretical binomial
    probabilities for  :math:`x_{11}` and :math:`x_{12}`. When using
    Barnard exact test, we can assert three different null hypotheses :

    - :math:`H_0 : p_1 \geq p_2` versus :math:`H_1 : p_1 < p_2`,
      with `alternative` = "less"

    - :math:`H_0 : p_1 \leq p_2` versus :math:`H_1 : p_1 > p_2`,
      with `alternative` = "greater"

    - :math:`H_0 : p_1 = p_2` versus :math:`H_1 : p_1 \neq p_2`,
      with `alternative` = "two-sided" (default one)

    In order to compute Barnard's exact test, we are using the Wald
    statistic [3]_ with pooled or unpooled variance.
    Under the default assumption that both variances are equal
    (``pooled = True``), the statistic is computed as:

    .. math::

        T(X) = \frac{
            \hat{p}_1 - \hat{p}_2
        }{
            \sqrt{
                \hat{p}(1 - \hat{p})
                (\frac{1}{c_1} +
                \frac{1}{c_2})
            }
        }

    with :math:`\hat{p}_1, \hat{p}_2` and :math:`\hat{p}` the estimator of
    :math:`p_1, p_2` and :math:`p`, the latter being the combined probability,
    given the assumption that :math:`p_1 = p_2`.

    If this assumption is invalid (``pooled = False``), the statistic is:

    .. math::

        T(X) = \frac{
            \hat{p}_1 - \hat{p}_2
        }{
            \sqrt{
                \frac{\hat{p}_1 (1 - \hat{p}_1)}{c_1} +
                \frac{\hat{p}_2 (1 - \hat{p}_2)}{c_2}
            }
        }

    The p-value is then computed as:

    .. math::

        \sum
            \binom{c_1}{x_{11}}
            \binom{c_2}{x_{12}}
            \pi^{x_{11} + x_{12}}
            (1 - \pi)^{t - x_{11} - x_{12}}

    where the sum is over all  2x2 contingency tables :math:`X` such that:
    * :math:`T(X) \leq T(X_0)` when `alternative` = "less",
    * :math:`T(X) \geq T(X_0)` when `alternative` = "greater", or
    * :math:`T(X) \geq |T(X_0)|` when `alternative` = "two-sided".
    Above, :math:`c_1, c_2` are the sum of the columns 1 and 2,
    and :math:`t` the total (sum of the 4 sample's element).

    The returned p-value is the maximum p-value taken over the nuisance
    parameter :math:`\pi`, where :math:`0 \leq \pi \leq 1`.

    This function's complexity is :math:`O(n c_1 c_2)`, where `n` is the
    number of sample points.

    References
    ----------
    .. [1] Barnard, G. A. "Significance Tests for 2x2 Tables". *Biometrika*.
           34.1/2 (1947): 123-138. :doi:`dpgkg3`

    .. [2] Mehta, Cyrus R., and Pralay Senchaudhuri. "Conditional versus
           unconditional exact tests for comparing two binomials."
           *Cytel Software Corporation* 675 (2003): 1-5.

    .. [3] "Wald Test". *Wikipedia*. https://en.wikipedia.org/wiki/Wald_test

    Examples
    --------
    An example use of Barnard's test is presented in [2]_.

        Consider the following example of a vaccine efficacy study
        (Chan, 1998). In a randomized clinical trial of 30 subjects, 15 were
        inoculated with a recombinant DNA influenza vaccine and the 15 were
        inoculated with a placebo. Twelve of the 15 subjects in the placebo
        group (80%) eventually became infected with influenza whereas for the
        vaccine group, only 7 of the 15 subjects (47%) became infected. The
        data are tabulated as a 2 x 2 table::

                Vaccine  Placebo
            Yes     7        12
            No      8        3

    When working with statistical hypothesis testing, we usually use a
    threshold probability or significance level upon which we decide
    to reject the null hypothesis :math:`H_0`. Suppose we choose the common
    significance level of 5%.

    Our alternative hypothesis is that the vaccine will lower the chance of
    becoming infected with the virus; that is, the probability :math:`p_1` of
    catching the virus with the vaccine will be *less than* the probability
    :math:`p_2` of catching the virus without the vaccine.  Therefore, we call
    `barnard_exact` with the ``alternative="less"`` option:

    >>> import scipy.stats as stats
    >>> res = stats.barnard_exact([[7, 12], [8, 3]], alternative="less")
    >>> res.statistic
    -1.894...
    >>> res.pvalue
    0.03407...

    Under the null hypothesis that the vaccine will not lower the chance of
    becoming infected, the probability of obtaining test results at least as
    extreme as the observed data is approximately 3.4%. Since this p-value is
    less than our chosen significance level, we have evidence to reject
    :math:`H_0` in favor of the alternative.

    Suppose we had used Fisher's exact test instead:

    >>> _, pvalue = stats.fisher_exact([[7, 12], [8, 3]], alternative="less")
    >>> pvalue
    0.0640...

    With the same threshold significance of 5%, we would not have been able
    to reject the null hypothesis in favor of the alternative. As stated in
    [2]_, Barnard's test is uniformly more powerful than Fisher's exact test
    because Barnard's test does not condition on any margin. Fisher's test
    should only be used when both sets of marginals are fixed.

    """
    if n <= 0:
        raise ValueError(
            "Number of points `n` must be strictly positive, "
            f"found {n!r}"
        )

    table = np.asarray(table, dtype=np.int64)

    if not table.shape == (2, 2):
        raise ValueError("The input `table` must be of shape (2, 2).")

    if np.any(table < 0):
        raise ValueError("All values in `table` must be nonnegative.")

    if 0 in table.sum(axis=0):
        # If both values in column are zero, the p-value is 1 and
        # the score's statistic is NaN.
        return BarnardExactResult(np.nan, 1.0)

    total_col_1, total_col_2 = table.sum(axis=0)

    x1 = np.arange(total_col_1 + 1, dtype=np.int64).reshape(-1, 1)
    x2 = np.arange(total_col_2 + 1, dtype=np.int64).reshape(1, -1)

    # We need to calculate the wald statistics for each combination of x1 and
    # x2.
    p1, p2 = x1 / total_col_1, x2 / total_col_2

    if pooled:
        p = (x1 + x2) / (total_col_1 + total_col_2)
        variances = p * (1 - p) * (1 / total_col_1 + 1 / total_col_2)
    else:
        variances = p1 * (1 - p1) / total_col_1 + p2 * (1 - p2) / total_col_2

    # To avoid warning when dividing by 0
    with np.errstate(divide="ignore", invalid="ignore"):
        wald_statistic = np.divide((p1 - p2), np.sqrt(variances))

    wald_statistic[p1 == p2] = 0  # Removing NaN values

    wald_stat_obs = wald_statistic[table[0, 0], table[0, 1]]

    if alternative == "two-sided":
        index_arr = np.abs(wald_statistic) >= abs(wald_stat_obs)
    elif alternative == "less":
        index_arr = wald_statistic <= wald_stat_obs
    elif alternative == "greater":
        index_arr = wald_statistic >= wald_stat_obs
    else:
        msg = (
            "`alternative` should be one of {'two-sided', 'less', 'greater'},"
            f" found {alternative!r}"
        )
        raise ValueError(msg)

    x1_sum_x2 = x1 + x2

    x1_log_comb = _compute_log_combinations(total_col_1)
    x2_log_comb = _compute_log_combinations(total_col_2)
    x1_sum_x2_log_comb = x1_log_comb[x1] + x2_log_comb[x2]

    result = shgo(
        _get_binomial_log_p_value_with_nuisance_param,
        args=(x1_sum_x2, x1_sum_x2_log_comb, index_arr),
        bounds=((0, 1),),
        n=n,
        sampling_method="sobol",
    )

    # result.fun is the negative log pvalue and therefore needs to be
    # changed before return
    p_value = np.clip(np.exp(-result.fun), a_min=0, a_max=1)
    return BarnardExactResult(wald_stat_obs, p_value)


BoschlooExactResult = make_dataclass(
    "BoschlooExactResult", [("statistic", float), ("pvalue", float)]
)


def boschloo_exact(table, alternative="two-sided", n=32):
    r"""Perform Boschloo's exact test on a 2x2 contingency table.

    Parameters
    ----------
    table : array_like of ints
        A 2x2 contingency table.  Elements should be non-negative integers.

    alternative : {'two-sided', 'less', 'greater'}, optional
        Defines the null and alternative hypotheses. Default is 'two-sided'.
        Please see explanations in the Notes section below.

    n : int, optional
        Number of sampling points used in the construction of the sampling
        method. Note that this argument will automatically be converted to
        the next higher power of 2 since `scipy.stats.qmc.Sobol` is used to
        select sample points. Default is 32. Must be positive. In most cases,
        32 points is enough to reach good precision. More points comes at
        performance cost.

    Returns
    -------
    ber : BoschlooExactResult
        A result object with the following attributes.

        statistic : float
            The statistic used in Boschloo's test; that is, the p-value
            from Fisher's exact test.

        pvalue : float
            P-value, the probability of obtaining a distribution at least as
            extreme as the one that was actually observed, assuming that the
            null hypothesis is true.

    See Also
    --------
    chi2_contingency : Chi-square test of independence of variables in a
        contingency table.
    fisher_exact : Fisher exact test on a 2x2 contingency table.
    barnard_exact : Barnard's exact test, which is a more powerful alternative
        than Fisher's exact test for 2x2 contingency tables.

    Notes
    -----
    Boschloo's test is an exact test used in the analysis of contingency
    tables. It examines the association of two categorical variables, and
    is a uniformly more powerful alternative to Fisher's exact test
    for 2x2 contingency tables.

    Let's define :math:`X_0` a 2x2 matrix representing the observed sample,
    where each column stores the binomial experiment, as in the example
    below. Let's also define :math:`p_1, p_2` the theoretical binomial
    probabilities for  :math:`x_{11}` and :math:`x_{12}`. When using
    Boschloo exact test, we can assert three different null hypotheses :

    - :math:`H_0 : p_1=p_2` versus :math:`H_1 : p_1 < p_2`,
      with `alternative` = "less"

    - :math:`H_0 : p_1=p_2` versus :math:`H_1 : p_1 > p_2`,
      with `alternative` = "greater"

    - :math:`H_0 : p_1=p_2` versus :math:`H_1 : p_1 \neq p_2`,
      with `alternative` = "two-sided" (default one)

    Boschloo's exact test uses the p-value of Fisher's exact test as a
    statistic, and Boschloo's p-value is the probability under the null
    hypothesis of observing such an extreme value of this statistic.

    Boschloo's and Barnard's are both more powerful than Fisher's exact
    test.

    .. versionadded:: 1.7.0

    References
    ----------
    .. [1] R.D. Boschloo. "Raised conditional level of significance for the
       2 x 2-table when testing the equality of two probabilities",
       Statistica Neerlandica, 24(1), 1970

    .. [2] "Boschloo's test", Wikipedia,
       https://en.wikipedia.org/wiki/Boschloo%27s_test

    .. [3] Lise M. Saari et al. "Employee attitudes and job satisfaction",
       Human Resource Management, 43(4), 395-407, 2004,
       :doi:`10.1002/hrm.20032`.

    Examples
    --------
    In the following example, we consider the article "Employee
    attitudes and job satisfaction" [3]_
    which reports the results of a survey from 63 scientists and 117 college
    professors. Of the 63 scientists, 31 said they were very satisfied with
    their jobs, whereas 74 of the college professors were very satisfied
    with their work. Is this significant evidence that college
    professors are happier with their work than scientists?
    The following table summarizes the data mentioned above::

                         college professors   scientists
        Very Satisfied   74                     31
        Dissatisfied     43                     32

    When working with statistical hypothesis testing, we usually use a
    threshold probability or significance level upon which we decide
    to reject the null hypothesis :math:`H_0`. Suppose we choose the common
    significance level of 5%.

    Our alternative hypothesis is that college professors are truly more
    satisfied with their work than scientists. Therefore, we expect
    :math:`p_1` the proportion of very satisfied college professors to be
    greater than :math:`p_2`, the proportion of very satisfied scientists.
    We thus call `boschloo_exact` with the ``alternative="greater"`` option:

    >>> import scipy.stats as stats
    >>> res = stats.boschloo_exact([[74, 31], [43, 32]], alternative="greater")
    >>> res.statistic
    0.0483...
    >>> res.pvalue
    0.0355...

    Under the null hypothesis that scientists are happier in their work than
    college professors, the probability of obtaining test
    results at least as extreme as the observed data is approximately 3.55%.
    Since this p-value is less than our chosen significance level, we have
    evidence to reject :math:`H_0` in favor of the alternative hypothesis.

    """
    hypergeom = distributions.hypergeom

    if n <= 0:
        raise ValueError(
            "Number of points `n` must be strictly positive,"
            f" found {n!r}"
        )

    table = np.asarray(table, dtype=np.int64)

    if not table.shape == (2, 2):
        raise ValueError("The input `table` must be of shape (2, 2).")

    if np.any(table < 0):
        raise ValueError("All values in `table` must be nonnegative.")

    if 0 in table.sum(axis=0):
        # If both values in column are zero, the p-value is 1 and
        # the score's statistic is NaN.
        return BoschlooExactResult(np.nan, np.nan)

    total_col_1, total_col_2 = table.sum(axis=0)
    total = total_col_1 + total_col_2
    x1 = np.arange(total_col_1 + 1, dtype=np.int64).reshape(1, -1)
    x2 = np.arange(total_col_2 + 1, dtype=np.int64).reshape(-1, 1)
    x1_sum_x2 = x1 + x2

    if alternative == 'less':
        pvalues = hypergeom.cdf(x1, total, x1_sum_x2, total_col_1).T
    elif alternative == 'greater':
        # Same formula as the 'less' case, but with the second column.
        pvalues = hypergeom.cdf(x2, total, x1_sum_x2, total_col_2).T
    elif alternative == 'two-sided':
        boschloo_less = boschloo_exact(table, alternative="less", n=n)
        boschloo_greater = boschloo_exact(table, alternative="greater", n=n)

        res = (
            boschloo_less if boschloo_less.pvalue < boschloo_greater.pvalue
            else boschloo_greater
        )

        # Two-sided p-value is defined as twice the minimum of the one-sided
        # p-values
        pvalue = 2 * res.pvalue
        return BoschlooExactResult(res.statistic, pvalue)
    else:
        msg = (
            f"`alternative` should be one of {'two-sided', 'less', 'greater'},"
            f" found {alternative!r}"
        )
        raise ValueError(msg)

    fisher_stat = pvalues[table[0, 0], table[0, 1]]

    # fisher_stat * (1+1e-13) guards us from small numerical error. It is
    # equivalent to np.isclose with relative tol of 1e-13 and absolute tol of 0
    # For more throughout explanations, see gh-14178
    index_arr = pvalues <= fisher_stat * (1+1e-13)

    x1, x2, x1_sum_x2 = x1.T, x2.T, x1_sum_x2.T
    x1_log_comb = _compute_log_combinations(total_col_1)
    x2_log_comb = _compute_log_combinations(total_col_2)
    x1_sum_x2_log_comb = x1_log_comb[x1] + x2_log_comb[x2]

    result = shgo(
        _get_binomial_log_p_value_with_nuisance_param,
        args=(x1_sum_x2, x1_sum_x2_log_comb, index_arr),
        bounds=((0, 1),),
        n=n,
        sampling_method="sobol",
    )

    # result.fun is the negative log pvalue and therefore needs to be
    # changed before return
    p_value = np.clip(np.exp(-result.fun), a_min=0, a_max=1)
    return BoschlooExactResult(fisher_stat, p_value)


def _get_binomial_log_p_value_with_nuisance_param(
    nuisance_param, x1_sum_x2, x1_sum_x2_log_comb, index_arr
):
    r"""
    Compute the log pvalue in respect of a nuisance parameter considering
    a 2x2 sample space.

    Parameters
    ----------
    nuisance_param : float
        nuisance parameter used in the computation of the maximisation of
        the p-value. Must be between 0 and 1

    x1_sum_x2 : ndarray
        Sum of x1 and x2 inside barnard_exact

    x1_sum_x2_log_comb : ndarray
        sum of the log combination of x1 and x2

    index_arr : ndarray of boolean

    Returns
    -------
    p_value : float
        Return the maximum p-value considering every nuisance paramater
        between 0 and 1

    Notes
    -----

    Both Barnard's test and Boschloo's test iterate over a nuisance parameter
    :math:`\pi \in [0, 1]` to find the maximum p-value. To search this
    maxima, this function return the negative log pvalue with respect to the
    nuisance parameter passed in params. This negative log p-value is then
    used in `shgo` to find the minimum negative pvalue which is our maximum
    pvalue.

    Also, to compute the different combination used in the
    p-values' computation formula, this function uses `gammaln` which is
    more tolerant for large value than `scipy.special.comb`. `gammaln` gives
    a log combination. For the little precision loss, performances are
    improved a lot.
    """
    t1, t2 = x1_sum_x2.shape
    n = t1 + t2 - 2
    with np.errstate(divide="ignore", invalid="ignore"):
        log_nuisance = np.log(
            nuisance_param,
            out=np.zeros_like(nuisance_param),
            where=nuisance_param >= 0,
        )
        log_1_minus_nuisance = np.log(
            1 - nuisance_param,
            out=np.zeros_like(nuisance_param),
            where=1 - nuisance_param >= 0,
        )

        nuisance_power_x1_x2 = log_nuisance * x1_sum_x2
        nuisance_power_x1_x2[(x1_sum_x2 == 0)[:, :]] = 0

        nuisance_power_n_minus_x1_x2 = log_1_minus_nuisance * (n - x1_sum_x2)
        nuisance_power_n_minus_x1_x2[(x1_sum_x2 == n)[:, :]] = 0

        tmp_log_values_arr = (
            x1_sum_x2_log_comb
            + nuisance_power_x1_x2
            + nuisance_power_n_minus_x1_x2
        )

    tmp_values_from_index = tmp_log_values_arr[index_arr]

    # To avoid dividing by zero in log function and getting inf value,
    # values are centered according to the max
    max_value = tmp_values_from_index.max()

    # To have better result's precision, the log pvalue is taken here.
    # Indeed, pvalue is included inside [0, 1] interval. Passing the
    # pvalue to log makes the interval a lot bigger ([-inf, 0]), and thus
    # help us to achieve better precision
    with np.errstate(divide="ignore", invalid="ignore"):
        log_probs = np.exp(tmp_values_from_index - max_value).sum()
        log_pvalue = max_value + np.log(
            log_probs,
            out=np.full_like(log_probs, -np.inf),
            where=log_probs > 0,
        )

    # Since shgo find the minima, minus log pvalue is returned
    return -log_pvalue


def _pval_cvm_2samp_exact(s, nx, ny):
    """
    Compute the exact p-value of the Cramer-von Mises two-sample test
    for a given value s (float) of the test statistic by enumerating
    all possible combinations. nx and ny are the sizes of the samples.
    """
    rangex = np.arange(nx)
    rangey = np.arange(ny)

    us = []

    # x and y are all possible partitions of ranks from 0 to nx + ny - 1
    # into two sets of length nx and ny
    # Here, ranks are from 0 to nx + ny - 1 instead of 1 to nx + ny, but
    # this does not change the value of the statistic.
    for x, y in _all_partitions(nx, ny):
        # compute the statistic
        u = nx * np.sum((x - rangex)**2)
        u += ny * np.sum((y - rangey)**2)
        us.append(u)

    # compute the values of u and the frequencies
    u, cnt = np.unique(us, return_counts=True)
    return np.sum(cnt[u >= s]) / np.sum(cnt)


@_vectorize_hypotest_factory(
        CramerVonMisesResult, n_samples=2, too_small=1,
        result_unpacker=np.vectorize(lambda res: (res.statistic, res.pvalue)))
def cramervonmises_2samp(x, y, method='auto'):
    """Perform the two-sample Cramér-von Mises test for goodness of fit.

    This is the two-sample version of the Cramér-von Mises test ([1]_):
    for two independent samples :math:`X_1, ..., X_n` and
    :math:`Y_1, ..., Y_m`, the null hypothesis is that the samples
    come from the same (unspecified) continuous distribution.

    Parameters
    ----------
    x : array_like
        A 1-D array of observed values of the random variables :math:`X_i`.
    y : array_like
        A 1-D array of observed values of the random variables :math:`Y_i`.
    method : {'auto', 'asymptotic', 'exact'}, optional
        The method used to compute the p-value, see Notes for details.
        The default is 'auto'.

    Returns
    -------
    res : object with attributes
        statistic : float
            Cramér-von Mises statistic.
        pvalue : float
            The p-value.

    See Also
    --------
    cramervonmises, anderson_ksamp, epps_singleton_2samp, ks_2samp

    Notes
    -----
    .. versionadded:: 1.7.0

    The statistic is computed according to equation 9 in [2]_. The
    calculation of the p-value depends on the keyword `method`:

    - ``asymptotic``: The p-value is approximated by using the limiting
      distribution of the test statistic.
    - ``exact``: The exact p-value is computed by enumerating all
      possible combinations of the test statistic, see [2]_.

    The exact calculation will be very slow even for moderate sample
    sizes as the number of combinations increases rapidly with the
    size of the samples. If ``method=='auto'``, the exact approach
    is used if both samples contain less than 10 observations,
    otherwise the asymptotic distribution is used.

    If the underlying distribution is not continuous, the p-value is likely to
    be conservative (Section 6.2 in [3]_). When ranking the data to compute
    the test statistic, midranks are used if there are ties.

    References
    ----------
    .. [1] https://en.wikipedia.org/wiki/Cramer-von_Mises_criterion
    .. [2] Anderson, T.W. (1962). On the distribution of the two-sample
           Cramer-von-Mises criterion. The Annals of Mathematical
           Statistics, pp. 1148-1159.
    .. [3] Conover, W.J., Practical Nonparametric Statistics, 1971.

    Examples
    --------

    Suppose we wish to test whether two samples generated by
    ``scipy.stats.norm.rvs`` have the same distribution. We choose a
    significance level of alpha=0.05.

    >>> from scipy import stats
    >>> rng = np.random.default_rng()
    >>> x = stats.norm.rvs(size=100, random_state=rng)
    >>> y = stats.norm.rvs(size=70, random_state=rng)
    >>> res = stats.cramervonmises_2samp(x, y)
    >>> res.statistic, res.pvalue
    (0.29376470588235293, 0.1412873014573014)

    The p-value exceeds our chosen significance level, so we do not
    reject the null hypothesis that the observed samples are drawn from the
    same distribution.

    For small sample sizes, one can compute the exact p-values:

    >>> x = stats.norm.rvs(size=7, random_state=rng)
    >>> y = stats.t.rvs(df=2, size=6, random_state=rng)
    >>> res = stats.cramervonmises_2samp(x, y, method='exact')
    >>> res.statistic, res.pvalue
    (0.197802197802198, 0.31643356643356646)

    The p-value based on the asymptotic distribution is a good approximation
    even though the sample size is small.

    >>> res = stats.cramervonmises_2samp(x, y, method='asymptotic')
    >>> res.statistic, res.pvalue
    (0.197802197802198, 0.2966041181527128)

    Independent of the method, one would not reject the null hypothesis at the
    chosen significance level in this example.

    """
    xa = np.sort(np.asarray(x))
    ya = np.sort(np.asarray(y))

    if xa.size <= 1 or ya.size <= 1:
        raise ValueError('x and y must contain at least two observations.')
    if xa.ndim > 1 or ya.ndim > 1:
        raise ValueError('The samples must be one-dimensional.')
    if method not in ['auto', 'exact', 'asymptotic']:
        raise ValueError('method must be either auto, exact or asymptotic.')

    nx = len(xa)
    ny = len(ya)

    if method == 'auto':
        if max(nx, ny) > 10:
            method = 'asymptotic'
        else:
            method = 'exact'

    # get ranks of x and y in the pooled sample
    z = np.concatenate([xa, ya])
    # in case of ties, use midrank (see [1])
    r = scipy.stats.rankdata(z, method='average')
    rx = r[:nx]
    ry = r[nx:]

    # compute U (eq. 10 in [2])
    u = nx * np.sum((rx - np.arange(1, nx+1))**2)
    u += ny * np.sum((ry - np.arange(1, ny+1))**2)

    # compute T (eq. 9 in [2])
    k, N = nx*ny, nx + ny
    t = u / (k*N) - (4*k - 1)/(6*N)

    if method == 'exact':
        p = _pval_cvm_2samp_exact(u, nx, ny)
    else:
        # compute expected value and variance of T (eq. 11 and 14 in [2])
        et = (1 + 1/N)/6
        vt = (N+1) * (4*k*N - 3*(nx**2 + ny**2) - 2*k)
        vt = vt / (45 * N**2 * 4 * k)

        # computed the normalized statistic (eq. 15 in [2])
        tn = 1/6 + (t - et) / np.sqrt(45 * vt)

        # approximate distribution of tn with limiting distribution
        # of the one-sample test statistic
        # if tn < 0.003, the _cdf_cvm_inf(tn) < 1.28*1e-18, return 1.0 directly
        if tn < 0.003:
            p = 1.0
        else:
            p = max(0, 1. - _cdf_cvm_inf(tn))

    return CramerVonMisesResult(statistic=t, pvalue=p)
