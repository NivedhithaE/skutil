from __future__ import division, print_function, absolute_import
import abc
import warnings
import numpy as np
import pandas as pd
from h2o.frame import H2OFrame
from numpy.random import choice
from sklearn.externals import six
from sklearn.neighbors import NearestNeighbors
from skutil.base import overrides, BaseSkutil
from ..utils.fixes import dict_keys
from ..utils import *

__all__ = [
    'OversamplingClassBalancer',
    'SamplingWarning',
    'SMOTEClassBalancer',
    'UndersamplingClassBalancer'
]


class SamplingWarning(UserWarning):
    """Custom warning used to notify the user that sub-optimal sampling behavior
    has occurred. For instance, performing oversampling on a minority class with only
    one instance will cause this warning to be thrown.
    """


def _validate_ratio(ratio):
    # validate ratio, if the current ratio is >= the ratio, it's "balanced enough"
    if not isinstance(ratio, (float, np.float)) or ratio <= 0 or ratio > 1:
        raise ValueError('ratio should be a float between 0.0 and 1.0, but got %s' % str(ratio))
    return ratio


def _validate_target(y):
    if (not y) or (not isinstance(y, six.string_types)):
        raise ValueError('y must be a column name')
    return str(y)  # force string


def _validate_num_classes(cts):
    mc, n_classes = BalancerMixin._max_classes, cts.shape[0]
    if n_classes > mc:
        raise ValueError('class balancing can only handle <= %i classes, but got %i' % (mc, n_classes))
    elif n_classes < 2:
        raise ValueError('class balancing requires at least 2 classes')
    return n_classes


def _validate_x_y_ratio(X, y, ratio):
    """Validates the following, given that X is
    already a validated pandas DataFrame:

    1. That y is a string
    2. That the number of classes does not exceed _max_classes
       as defined by the BalancerMixin class
    3. That the number of classes is at least 2
    4. That ratio is a float that falls between 0.0 (exclusive) and
       1.0 (inclusive)

    Parameters
    ----------

    X : Pandas ``DataFrame`` or ``H2OFrame``, shape=(n_samples, n_features)
        The frame from which to sample

    y : str
        The name of the column that is the response class.
        This is the column on which ``value_counts`` will be
        executed to determine imbalance.

    ratio : float
        The ratio at which the balancing operation will 
        be performed. Used to determine whether balancing is
        required.

    Returns
    -------

    out_tup : tuple, shape=(3,)
        a length-3 tuple with the following args:
            [0] - cts (pd.Series), the ascending sorted ``value_counts`` 
                  of the class, where the index is the class label.
            [1] - n_classes (int), the number of unique classes
            [2] - needs_balancing (bool), whether the least populated class
                  is represented at a rate lower than the demanded ratio.
    """
    ratio = _validate_ratio(ratio)
    y = _validate_target(y)  # force to string
    is_factor = X.dtypes[y] == 'object'

    # validate is < max classes
    cts = X[y].value_counts().sort_values(ascending=True)
    n_classes = _validate_num_classes(cts)
    needs_balancing = (cts.values[0] / cts.values[-1]) < ratio

    index = cts.index if not is_factor else cts.index.astype('str')
    out_tup = (dict(zip(index.values, cts.values)),  # cts
               index,  # labels sorted ascending by commonality
               X[y].values if not is_factor else X[y].astype('str').values,  # the target
               n_classes,
               needs_balancing)
    return out_tup


class BalancerMixin:
    """Mixin class for balancers that provides interface for ``balance``
    and the constant ``_max_classes`` (default=20). Used in h2o module as well.
    """

    # the max classes handled by class balancers
    _max_classes = 20
    _def_ratio = 0.2

    def balance(self, X):
        """This method must be overridden by
        a subclass. This does nothing right now.

        Parameters
        ----------

        X : Pandas ``DataFrame`` or ``H2OFrame``, shape=(n_samples, n_features)
            The frame from which to balance
        """
        raise NotImplementedError('this method must be implemented by a subclass')


def _default_indices(length, shuffle):
    x = np.arange(length)
    return x.tolist() if not shuffle else np.random.permutation(x).tolist()


class _BaseBalancePartitioner(six.with_metaclass(abc.ABCMeta, object)):
    """Base class for sample partitioners. The partitioner class is
    responsible for implementing the `_get_sample_indices` method, which
    implements the specific logic for which rows to sample. The `get_indices`
    method will return the indices that should be sampled (if using with H2O,
    these should be sorted).

    Parameters
    ----------

    X : Pandas ``DataFrame`` or ``H2OFrame``, shape=(n_samples, n_features)
        The frame from which to sample

    y_name : str
        The name of the column that is the response class

    ratio : float
        The ratio at which to sample

    validation_function : callable, optional (default=_validate_x_y_ratio)
        The function that will validate X, y and the ratio. This function
        differs for H2OFrames.
    """

    @abc.abstractmethod
    def __init__(self, X, y_name, ratio, validation_function=_validate_x_y_ratio):
        self.X = X
        self.y = y_name
        self.ratio = ratio

        # perform validation_function
        cts, index, target_col, n_classes, needs_balancing = validation_function(X, y_name, ratio)
        self.cts = cts
        self.index = index
        self.target_col = target_col
        self.needs_balancing = needs_balancing

    def get_indices(self, shuffle):
        return self._get_sample_indices(shuffle)

    @abc.abstractmethod
    def _get_sample_indices(self, shuffle):
        """To be overridden"""
        raise NotImplementedError('must be overridden by subclass!')


class _OversamplingBalancePartitioner(_BaseBalancePartitioner):
    """Balance partitioner for oversampling the minority classes."""

    def __init__(self, X, y_name, ratio, validation_function=_validate_x_y_ratio):
        super(_OversamplingBalancePartitioner, self).__init__(
            X, y_name, ratio, validation_function)

    @overrides(_BaseBalancePartitioner)
    def _get_sample_indices(self, shuffle):
        # if we don't need balancing, then just return the indices as is
        if not self.needs_balancing:
            return _default_indices(self.X.shape[0], shuffle)

        cts = self.cts
        ratio = self.ratio
        X, y = self.X, self.y

        # get the maj class
        majority = self.index[-1]
        n_required = np.maximum(1, int(ratio * cts[majority]))

        # target_col needs to be np array
        target_col = self.target_col  # already computed and in a NP array
        all_indices = np.arange(X.shape[0])

        sample_indices = []
        for minority in self.index:
            # since it's sorted, it means we've hit the end
            if minority == majority:
                break

            min_ct = cts[minority]
            if min_ct == 1:
                warnings.warn('class %s only has one observation' % str(minority), SamplingWarning)

            current_ratio = min_ct / cts[majority]
            if current_ratio >= ratio:
                continue  # if ratio is already met, continue

            n_samples = n_required - min_ct  # the difference in the current present and the number we need
            if n_samples <= 0:  # the np maximum can cause weirdness
                continue  # move onto next class

            minority_recs = all_indices[target_col == minority]
            idcs = choice(minority_recs, n_samples, replace=True)
            sample_indices.extend(list(idcs))

        # make list
        all_indices = list(all_indices)
        all_indices.extend(sample_indices)

        # sorted because h2o doesn't play nicely with random indexing
        out = sorted(all_indices) if not shuffle else [j for j in np.random.permutation(all_indices)]
        return out


class _UndersamplingBalancePartitioner(_BaseBalancePartitioner):
    """Balance partitioner for undersampling the minority class"""

    def __init__(self, X, y_name, ratio, validation_function=_validate_x_y_ratio):
        super(_UndersamplingBalancePartitioner, self).__init__(
            X, y_name, ratio, validation_function)

    @overrides(_BaseBalancePartitioner)
    def _get_sample_indices(self, shuffle):
        # if we don't need balancing, then just return the indices as is
        if not self.needs_balancing:
            return _default_indices(self.X.shape[0], shuffle)

        cts = self.cts
        ratio = self.ratio
        X, y = self.X, self.y

        # get the maj class
        majority = self.index[-1]
        next_most = self.index[-2]  # the next-most-populous class label - we know there are at least two! (validation)
        n_required = int((1 / ratio) * cts[next_most])  # i.e., if ratio == 0.5 and next_most == 30, n_required = 60
        all_indices = np.arange(X.shape[0])

        # check the exit condition (that majority class <= n_required)
        if cts[majority] <= n_required:
            return sorted(list(all_indices))

        # if not returned early, drop some indices
        target_col = self.target_col
        majority_recs = all_indices[target_col == majority]
        idcs = choice(majority_recs, n_required, replace=False)

        # get all the "minority" observation idcs, append the sampled
        # majority idcs, then sort and return
        minorities = list(all_indices[target_col != majority])
        minorities.extend(idcs)

        out = sorted(minorities) if not shuffle else [j for j in np.random.permutation(minorities)]
        return out


class _BaseBalancer(six.with_metaclass(abc.ABCMeta, BaseSkutil, BalancerMixin)):
    """A super class for all balancer classes. Balancers are not like TransformerMixins
    or BaseEstimators, and do not implement fit or predict. This is because Balancers
    are ONLY applied to training data.

    Parameters
    ----------

    y : str
        The name of the response column. The response column must be
        biclass, no more or less.

    ratio : float, optional (default=0.2)
        The target ratio of the minority records to the majority records. If the
        existing ratio is >= the provided ratio, the return value will merely be
        a copy of the input matrix, otherwise SMOTE will impute records until the
        target ratio is reached.

    shuffle : bool, optional (default=True)
        Whether or not to shuffle rows on return

    as_df : bool, optional (default=True)
        Whether to return a Pandas ``DataFrame`` in the ``transform``
        method. If False, will return a Numpy ``ndarray`` instead. 
        Since most skutil transformers depend on explicitly-named
        ``DataFrame`` features, the ``as_df`` parameter is True by default.
    """

    def __init__(self, y, ratio=BalancerMixin._def_ratio, shuffle=True, as_df=True):
        self.y_ = y
        self.ratio = ratio
        self.shuffle = shuffle
        self.as_df = as_df


def _over_under_balance(X, y, ratio, as_df, shuffle, partitioner_class):
    # check on state of X
    X, _ = validate_is_pd(X, None)  # there are no cols, and we don't want warnings

    # since we rely on indexing X, we need to reset indices
    # in case X is the result of a slice and they're out of order.
    X.index = np.arange(X.shape[0])
    partitioner = partitioner_class(X, y, ratio)

    # the balancing is handled in the partitioner
    balanced = X.iloc[partitioner.get_indices(shuffle)]

    # we need to re-index...
    balanced.index = np.arange(balanced.shape[0])

    # return the combined frame
    return balanced if as_df else balanced.as_matrix()


class OversamplingClassBalancer(_BaseBalancer):
    """Oversample all of the minority classes until they are 
    represented at the target proportion to the majority class.

    Parameters
    ----------

    y : str
        The name of the response column. The response column must be
        biclass, no more or less.

    ratio : float, optional (default=0.2)
        The target ratio of the minority records to the majority records. If the
        existing ratio is >= the provided ratio, the return value will merely be
        a copy of the input matrix

    shuffle : bool, optional (default=True)
        Whether or not to shuffle rows on return

    as_df : bool, optional (default=True)
        Whether to return a Pandas ``DataFrame`` in the ``transform``
        method. If False, will return a Numpy ``ndarray`` instead. 
        Since most skutil transformers depend on explicitly-named
        ``DataFrame`` features, the ``as_df`` parameter is True by default.


    Examples
    --------
    
    Consider the following example: with a ``ratio`` of 0.5, the 
    minority classes (1, 2) will be oversampled until they are represented 
    at a ratio of at least 0.5 * the prevalence of the majority class (0)

        >>> import pandas as pd
        >>> import numpy as np
        >>> 
        >>> # 100 zeros, 30 ones and 25 twos
        >>> X = pd.DataFrame(np.concatenate([np.zeros(100), np.ones(30), np.ones(25)*2]), columns=['A'])
        >>> sampler = OversamplingClassBalancer(y="A", ratio=0.5)
        >>>
        >>> X_balanced = sampler.balance(X)
        >>> X_balanced['A'].value_counts().sort_index()
        0.0    100
        1.0     50
        2.0     50
        Name: A, dtype: int64
    """

    def __init__(self, y, ratio=BalancerMixin._def_ratio, shuffle=True, as_df=True):
        super(OversamplingClassBalancer, self).__init__(ratio=ratio, y=y,
                                                        shuffle=shuffle,
                                                        as_df=as_df)

    @overrides(BalancerMixin)
    def balance(self, X):
        """Apply the oversampling balance operation. Oversamples
        the minority class to the provided ratio of minority
        class : majority class

        Parameters
        ----------

        X : Pandas ``DataFrame``, shape=(n_samples, n_features)
            The data to balance.

        Returns
        -------

        blnc : pandas ``DataFrame``, shape=(n_samples, n_features)
            The balanced dataframe. The dataframe will be
            explicitly shuffled if ``self.shuffle`` is True however,
            if ``self.shuffle`` is False, preservation of original, 
            natural ordering is not guaranteed.
        """
        blnc = _over_under_balance(X=X, y=self.y_, ratio=self.ratio,
                                   shuffle=self.shuffle, as_df=self.as_df,
                                   partitioner_class=_OversamplingBalancePartitioner)
        return blnc


class SMOTEClassBalancer(_BaseBalancer):
    """Balance a matrix with the SMOTE (Synthetic Minority Oversampling TEchnique)
    method. This will generate synthetic samples for the minority class(es) using
    K-nearest neighbors

    Parameters
    ----------

    y : str
        The name of the response column. The response column must be
        biclass, no more or less.

    ratio : float, optional (default=0.2)
        The target ratio of the minority records to the majority records. If the
        existing ratio is >= the provided ratio, the return value will merely be
        a copy of the input matrix, otherwise SMOTE will impute records until the
        target ratio is reached.

    shuffle : bool, optional (default=True)
        Whether or not to shuffle rows on return

    k : int, def 3
        The number of neighbors to use in the nearest neighbors model

    as_df : bool, optional (default=True)
        Whether to return a Pandas ``DataFrame`` in the ``transform``
        method. If False, will return a Numpy ``ndarray`` instead. 
        Since most skutil transformers depend on explicitly-named
        ``DataFrame`` features, the ``as_df`` parameter is True by default.


    Examples
    --------

    Consider the following example: with a ``ratio`` of 0.5, the 
    minority classes (1, 2) will be oversampled until they are represented 
    at a ratio of at least 0.5 * the prevalence of the majority class (0)

        >>> import pandas as pd
        >>> import numpy as np
        >>> from numpy.random import RandomState
        >>>
        >>> # establish a random state
        >>> prng = RandomState(42)
        >>>
        >>> # 100 zeros, 30 ones and 25 twos
        >>> X = pd.DataFrame(np.asarray([prng.rand(155), 
        ...                              np.concatenate([np.zeros(100), np.ones(30), np.ones(25)*2])]).T,
        ...                              columns=['x', 'y'])
        >>> sampler = SMOTEClassBalancer(y="y", ratio=0.5)
        >>>
        >>> X_balanced = sampler.balance(X)
        >>> X_balanced['y'].value_counts().sort_index()
        0.0    100
        1.0     50
        2.0     50
        Name: y, dtype: int64
    """

    def __init__(self, y, ratio=BalancerMixin._def_ratio, shuffle=True, k=3, as_df=True):
        super(SMOTEClassBalancer, self).__init__(ratio=ratio, y=y,
                                                 shuffle=shuffle,
                                                 as_df=as_df)
        self.k = k

    @overrides(BalancerMixin)
    def balance(self, X):
        """Apply the SMOTE balancing operation. Oversamples
        the minority class to the provided ratio of minority
        class : majority class by interpolating points between
        each sampled point's k-nearest neighbors.

        Parameters
        ----------

        X : Pandas ``DataFrame``, shape=(n_samples, n_features)
            The data to balance.

        Returns
        -------

        X : pandas ``DataFrame``, shape=(n_samples, n_features)
            The balanced dataframe. The dataframe will be
            explicitly shuffled if ``self.shuffle`` is True however,
            if ``self.shuffle`` is False, preservation of original, 
            natural ordering is not guaranteed.
        """
        # check on state of X
        X, _ = validate_is_pd(X, None, assert_all_finite=True)  # there are no cols, and we don't want warnings

        # since we rely on indexing X, we need to reset indices
        # in case X is the result of a slice and they're out of order.
        X.index = np.arange(0, X.shape[0])
        ratio = self.ratio
        cts, index, target_col, n_classes, needs_balancing = _validate_x_y_ratio(X, self.y_, ratio)

        # if we don't need balancing, then just return the indices as is
        if not needs_balancing:
            return X if not self.shuffle else shuffle_dataframe(X)

        # get the maj class
        majority = index[-1]
        n_required = np.maximum(1, int(ratio * cts[majority]))
        for minority in index:
            if minority == majority:
                break

            min_ct = cts[minority]
            if min_ct == 1:
                raise ValueError('cannot perform SMOTE on only one observation (class=%s)' % str(minority))

            current_ratio = min_ct / cts[majority]
            if current_ratio >= ratio:
                continue  # if ratio is already met, continue

            n_samples = n_required - min_ct  # the difference in the current present and the number we need
            # the np maximum can cause weirdness
            if n_samples <= 0:
                continue  # move onto next class

            # don't need to validate K, neighbors will
            # randomly select n_samples points from the minority records
            minority_recs = X[X[self.y_] == minority]
            replace = n_samples > minority_recs.shape[0]  # may have to replace if required num > num available
            idcs = choice(minority_recs.index, n_samples, replace=replace)
            pts = X.iloc[idcs].drop([self.y_], axis=1)

            # Fit the neighbors model on the random points
            nn = NearestNeighbors(n_neighbors=self.k).fit(pts)

            # do imputation
            synthetics_pts = []
            for neighbors in nn.kneighbors()[1]:  # go over indices
                mn = pts.iloc[neighbors].mean()

                # add the minority target, and the mean record
                synthetics_pts.append(mn.tolist())

            # append the minority target to the frame
            syn_frame = pd.DataFrame.from_records(data=synthetics_pts, columns=pts.columns)
            syn_frame[self.y_] = np.array([minority] * syn_frame.shape[0])

            # reorder the columns
            syn_frame = syn_frame[X.columns]

            # append to X
            X = pd.concat([X, syn_frame])

        # reset index
        X.index = np.arange(X.shape[0])

        # shuffle if necessary
        X = X if not self.shuffle else shuffle_dataframe(X)

        # return the combined frame
        return X if self.as_df else X.as_matrix()


class UndersamplingClassBalancer(_BaseBalancer):
    """Undersample the majority class until it is represented
    at the target proportion to the most-represented minority class 
    (i.e., the second-most populous class).

    Parameters
    ----------

    y : str
        The name of the response column. The response column must be
        biclass, no more or less.

    ratio : float, optional (default=0.2)
        The target ratio of the minority records to the majority records. If the
        existing ratio is >= the provided ratio, the return value will merely be
        a copy of the input matrix

    shuffle : bool, optional (default=True)
        Whether or not to shuffle rows on return

    as_df : bool, optional (default=True)
        Whether to return a Pandas ``DataFrame`` in the ``transform``
        method. If False, will return a Numpy ``ndarray`` instead. 
        Since most skutil transformers depend on explicitly-named
        ``DataFrame`` features, the ``as_df`` parameter is True by default.


    Examples
    --------

    Consider the following example: with a ``ratio`` of 0.5, the 
    majority class (0) will be undersampled until the second most-populous 
    class (1) is represented at a ratio of 0.5.

        >>> import pandas as pd
        >>> import numpy as np
        >>> 
        >>> # 150 zeros, 30 ones and 10 twos
        >>> X = pd.DataFrame(np.concatenate([np.zeros(150), np.ones(30), np.ones(10)*2]), columns=['A'])
        >>> sampler = UndersamplingClassBalancer(y="A", ratio=0.5)
        >>>
        >>> X_balanced = sampler.balance(X)
        >>> X_balanced['A'].value_counts().sort_index()
        0.0    60
        1.0    30
        2.0    10
        Name: A, dtype: int64
    """

    def __init__(self, y, ratio=0.2, shuffle=True, as_df=True):
        super(UndersamplingClassBalancer, self).__init__(ratio=ratio, y=y,
                                                         shuffle=shuffle,
                                                         as_df=as_df)

    def balance(self, X):
        """Apply the undersampling balance operation. Undersamples
        the majority class to the provided ratio over the second-most-
        populous class label.

        Parameters
        ----------

        X : Pandas ``DataFrame``, shape=(n_samples, n_features)
            The data to balance.

        Returns
        -------

        blnc : pandas ``DataFrame``, shape=(n_samples, n_features)
            The balanced dataframe. The dataframe will be
            explicitly shuffled if ``self.shuffle`` is True however,
            if ``self.shuffle`` is False, preservation of original, 
            natural ordering is not guaranteed.
        """
        blnc = _over_under_balance(X=X, y=self.y_, ratio=self.ratio,
                                   shuffle=self.shuffle, as_df=self.as_df,
                                   partitioner_class=_UndersamplingBalancePartitioner)
        return blnc
