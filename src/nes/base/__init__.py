import inspect
import os
import pickle
from functools import wraps
from typing import TypeVar, Dict, Any

import ruamel.yaml.constructor

from ..helper import set_logger, MemoryCache, time_profile, yaml

_tb = TypeVar('T', bound='TrainableBase')


class TrainableType(type):
    default_property = {
        'is_trained': False,
        'batch_size': None,
    }

    def __new__(meta, *args, **kwargs):
        cls = super().__new__(meta, *args, **kwargs)
        cls.__init__ = meta._store_init_kwargs(cls.__init__)
        if os.environ.get('NES_PROFILING', False):
            for f_name in ['train', 'encode', 'add', 'query']:
                if getattr(cls, f_name, None):
                    setattr(cls, f_name, time_profile(getattr(cls, f_name)))

        if getattr(cls, 'train', None):
            setattr(cls, 'train', meta._as_train_func(getattr(cls, 'train')))

        yaml.register_class(cls)
        return cls

    def __call__(cls, *args, **kwargs):
        obj = type.__call__(cls, *args, **kwargs)

        for k, v in TrainableType.default_property.items():
            if not hasattr(obj, k):
                setattr(obj, k, v)
        return obj

    @staticmethod
    def _as_train_func(func):
        @wraps(func)
        def arg_wrapper(self, *args, **kwargs):
            if self.is_trained:
                self.logger.warning('"%s" has been trained already, '
                                    'training it again will override the previous training' % self.__class__.__name__)
            f = func(self, *args, **kwargs)
            self.is_trained = True
            return f

        return arg_wrapper

    @staticmethod
    def _store_init_kwargs(func):
        @wraps(func)
        def arg_wrapper(self, *args, **kwargs):
            taboo = {'self', 'args', 'kwargs'}
            all_pars = inspect.signature(func).parameters
            tmp = {k: v.default for k, v in all_pars.items() if k not in taboo}
            tmp_list = [k for k in all_pars.keys() if k not in taboo]
            # set args by aligning tmp_list with arg values
            for k, v in zip(tmp_list, args):
                tmp[k] = v
            # set kwargs
            for k, v in kwargs.items():
                if k in tmp:
                    tmp[k] = v

            if self.store_args_kwargs:
                if args: tmp['args'] = args
                if kwargs: tmp['kwargs'] = kwargs

            if getattr(self, '_init_kwargs_dict', None):
                self._init_kwargs_dict.update(tmp)
            else:
                self._init_kwargs_dict = tmp
            f = func(self, *args, **kwargs)
            return f

        return arg_wrapper


class TrainableBase(metaclass=TrainableType):
    _timeit = time_profile
    store_args_kwargs = False

    def __init__(self, *args, **kwargs):
        self.is_trained = False
        self.verbose = 'verbose' in kwargs and kwargs['verbose']
        self.logger = set_logger(self.__class__.__name__, self.verbose)
        self.memcached = MemoryCache(cache_path='.nes_cache')

    def __getstate__(self):
        d = dict(self.__dict__)
        del d['logger']
        del d['memcached']
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self.logger = set_logger(self.__class__.__name__, self.verbose)
        self.memcached = MemoryCache(cache_path='.nes_cache')

    @staticmethod
    def _train_required(func):
        @wraps(func)
        def arg_wrapper(self, *args, **kwargs):
            if self.is_trained:
                return func(self, *args, **kwargs)
            else:
                raise RuntimeError('training is required before calling "%s"' % func.__name__)

        return arg_wrapper

    def train(self, *args, **kwargs):
        pass

    @_timeit
    def dump(self, filename: str) -> None:
        with open(filename, 'wb') as fp:
            pickle.dump(self, fp)

    @_timeit
    def dump_yaml(self, filename: str) -> None:
        with open(filename, 'w') as fp:
            yaml.dump(self, fp)

    @classmethod
    def load_yaml(cls, filename: str) -> _tb:
        with open(filename) as fp:
            return yaml.load(fp)

    @staticmethod
    @_timeit
    def load(filename: str) -> _tb:
        with open(filename, 'rb') as fp:
            return pickle.load(fp)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @classmethod
    def to_yaml(cls, representer, data):
        tmp = data._dump_instance_to_yaml(data)
        return representer.represent_mapping('!' + cls.__name__, tmp)

    @classmethod
    def from_yaml(cls, constructor, node):
        return cls._get_instance_from_yaml(constructor, node)[0]

    @classmethod
    def _get_instance_from_yaml(cls, constructor, node):
        data = ruamel.yaml.constructor.SafeConstructor.construct_mapping(
            constructor, node, deep=True)
        cls.init_from_yaml = True

        if cls.store_args_kwargs:
            p = data.get('parameter', {})  # type: Dict[str, Any]
            a = p.pop('args') if 'args' in p else ()
            k = p.pop('kwargs') if 'kwargs' in p else {}
            # maybe there are some hanging kwargs in "parameter"
            obj = cls(*a, **{**k, **p})
        else:
            obj = cls(**data.get('parameter', {}))

        for k, v in data.get('property', {}).items():
            setattr(obj, k, v)

        cls.init_from_yaml = False

        return obj, data

    @staticmethod
    def _dump_instance_to_yaml(data):
        # note: we only dump non-default property for the sake of clarity
        p = {k: getattr(data, k) for k, v in TrainableType.default_property.items() if getattr(data, k) != v}
        a = {k: v for k, v in data._init_kwargs_dict.items()}

        r = {}
        if a:
            r['parameter'] = a
        if p:
            r['property'] = p
        return r
