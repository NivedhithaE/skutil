"""
sklearn-esque transformers for python
"""
import sys

# want these visible at module level
from .utils import log, exp

__version__ = '0.1.6'

try:
    # This variable is injected in the __builtins__ by the build
    # process. It is used to enable importing subpackages of skutil when
    # the binaries are not built
    __SKUTIL_SETUP__
except NameError:
    __SKUTIL_SETUP__ = False

if __SKUTIL_SETUP__:
    sys.stderr.write('Partial import of skutil during the build process.\n')
else:
    __all__ = [
        'decomposition',
        'feature_selection',
        'grid_search',
        'h2o',
        'metrics',
        'model_selection',
        'odr',
        'preprocessing',
        'testing',
        'utils'
    ]


def setup_module(module):
    import numpy as np
    import random

    _random_seed = int(np.random.uniform() * (2 ** 31 - 1))
    np.random.seed(_random_seed)
    random.seed(_random_seed)
