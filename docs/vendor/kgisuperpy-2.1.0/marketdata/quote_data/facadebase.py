from html import escape
from ..md_logger import LoggingType, write_log
from .quote_status import QuotationError, QuotationCode
#--------------------------------------------------------------
class FacadeMeta(type):
    def __dir__(cls):
        if hasattr(cls, "_facade_props"):
            return cls._facade_props()
        return super().__dir__()
#--------------------------------------------------------------
class FacadeBase(metaclass=FacadeMeta):
    __slots__ = ("_obj",)
    _facade_cache = {}
    _facade_props_cache = None

    def __init__(self, internal_obj):
        self._obj = internal_obj

    def __getattr__(self, name):
        if name in self.__dir__():
            write_log(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_DATA_FACADE_NOT_FOUND.value}, Detail: Expected property getter '{name}' in {self.__internal_class__.__name__}, but it was not properly defined.")
            raise AttributeError(f"Expected property getter '{name}' in {self.__internal_class__().__name__}, but it was not properly defined.")
        #logger.warning(f"StatusCode: {QuotationCode.QUOTE_DATA_FACADE_NOT_FOUND.value}, Detail: Access to attribute '{name}' is not allowed in '{self.__internal_class__.__name__}'.")
        raise AttributeError(f"Access to attribute '{name}' is not allowed in '{self.__internal_class__().__name__}'.")

    def __dir__(self):
        return type(self)._facade_props()

        # return type(self).__dir__()
    
    # def __dir__(self):
    #     return [
    #         key for key in dir(self.__internal_class__())
    #         if isinstance(getattr(self.__internal_class__(), key, None), property)
    #     ]

    def __getitem__(self, key):
        props = self.__dir__()

        if key in props:
            try:
                return getattr(self, key)
            except Exception as e:
                write_log(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_DATA_FACADE_NOT_FOUND.value}, Detail: Expected property getter '{key}' in {type(self).__name__}, but it was not properly executed: {e}")
                raise AttributeError( f"Expected property getter '{key}' in {type(self).__name__}, but it was not properly executed: {e}" ) from e

        write_log(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_DATA_FACADE_NOT_FOUND.value}, Detail: Access to key '{key}' is not allowed in '{type(self).__name__}'.")
        raise KeyError(f"Access to key '{key}' is not allowed in '{type(self).__name__}'.")
    
    # @property
    # def __class__(self):
    #     return self._obj.__class__
    
    def __internal_class__(self):
        return self._obj.__class__

    # @classmethod
    # def facade_props(cls):
    #     return [name for name, v in cls.__dict__.items() if isinstance(v, property)]

    # @classmethod
    # def _facade_props(cls):
    #     props = cls._facade_props_cache
    #     if props is None:
    #         cache = set()
    #         for base in cls.__mro__:
    #             for name, attr in base.__dict__.items():
    #                 if isinstance(attr, property) and attr.fget:
    #                     cache.add(name)
    #         props = cache
    #         # props = sorted(cache)
    #         cls._facade_props_cache = props
    #     return props
    
    @classmethod
    def _facade_props(cls):
        props = cls._facade_props_cache
        if props is None:
            seen = set()
            ordered = []
            for base in cls.__mro__:
                for name, attr in base.__dict__.items():
                    if (
                        isinstance(attr, property)
                        and attr.fget
                        and name not in seen
                    ):
                        seen.add(name)
                        ordered.append(name)

            props = ordered
            cls._facade_props_cache = props
        return props
    
    @classmethod
    def _collect_facade_props(cls):
        props = set()
        for base in cls.__mro__:
            for name, attr in base.__dict__.items():
                if isinstance(attr, property) and attr.fget:
                    props.add(name)
        return sorted(props)
    
    @staticmethod
    def facade_class(facade_module, class_name: str):
        return getattr(facade_module, class_name, lambda x: x)
    
    @staticmethod
    def facade_callback(facade_module, facade_name: str, data_obj, callback_fn):
        try:
            facade_class = FacadeBase._facade_cache.get(facade_name)
            if facade_class is None:
                facade_class = getattr(facade_module, facade_name, None)
                FacadeBase._facade_cache[facade_name] = facade_class

            if facade_class is None:
                # logger.warning(f"StatusCode: {QuotationCode.QUOTE_DATA_FACADE_NOT_FOUND.value}, Detail: Facade is not founded: {facade_name}")
                write_log(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_DATA_FACADE_NOT_FOUND.value}, Detail: Facade is not founded: {facade_name}")
                return

            callback_fn(facade_class(data_obj))

        except Exception as e:
            #logger.warning(f"StatusCode: {QuotationCode.QUOTE_DATA_FACADE_CALLBACK_ERROR.value}, Detail: {facade_name}: {e}")
            write_log(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_DATA_FACADE_CALLBACK_ERROR.value}, Detail: {facade_name}: {e}")

    # def __str__(self):
    #     cls = self.__internal_class__
    #     seen = set()
    #     pairs = []

    #     for base in cls.__mro__:
    #         for name, prop in base.__dict__.items():
    #             if name in seen:
    #                 continue
    #             seen.add(name)
    #             if isinstance(prop, property) and prop.fget:
    #                 try:
    #                     val = getattr(self, name)
    #                     pairs.append(f"{name}={val!r}")
    #                 except Exception:
    #                     # print(f"Error accessing property '{name}' in {cls.__name__}")
    #                     pass
    #                     # pairs.append(f"{name}=<error>")
                        

    #     original_type = type(self._obj).__name__ 
    #     if not pairs:
    #         return f"{original_type}(<no accessible properties>)"

    #     return f"{original_type}({', '.join(pairs)})"
    def __str__(self):
        props = type(self)._facade_props()

        pairs = []
        for name in props:
            try:
                val = getattr(self, name)
            except Exception:
                continue
            pairs.append(f"{name}={val!r}")

        original_type = type(self._obj).__name__
        if not pairs:
            return f"{original_type}(<no accessible properties>)"
        return f"{original_type}({', '.join(pairs)})"

    # def __str__(self):
    #     seen = set()
    #     pairs = []
    #     for base in type(self).__mro__:
    #         for name, prop in base.__dict__.items():
    #             if name in seen:
    #                 continue
    #             if isinstance(prop, property) and prop.fget:
    #                 seen.add(name)
    #                 try:
    #                     val = getattr(self, name)
    #                     pairs.append(f"{name}={val!r}")
    #                 except Exception:
    #                     pass

    #     original_type = type(self._obj).__name__
    #     if not pairs:
    #         return f"{original_type}(<no accessible properties>)"
    #     return f"{original_type}({', '.join(pairs)})"

        # for name in self.__dir__():
        #     if name in seen:
        #         continue
        #     seen.add(name)
        #     try:
        #         val = getattr(self, name)
        #         pairs.append(f"{name}={val!r}")
        #     except Exception:
        #         pass

        # original_type = type(self._obj).__name__
        # if not pairs:
        #     return f"{original_type}(<no accessible properties>)"
        # return f"{original_type}({', '.join(pairs)})"
        
    def __repr__(self):
        return self.__str__()
    
    # def _repr_mimebundle_(self, include=None, exclude=None):
    #     return {'text/plain': self.__repr__()}, {}

    def _repr_pretty_(self, p, cycle):
        # For IPython display (text/plain)
        p.text(self.__repr__())
        # print(f"Pretty repr: {self.__repr__()}")

    def _repr_html_(self):
        # For Jupyter display (text/html)
        return f"<div>{escape(repr(self))}</div>"
#--------------------------------------------------------------