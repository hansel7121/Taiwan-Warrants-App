from typing import Generic, List, TypeVar, TYPE_CHECKING, Any
from .marketdata.quote_data.quotedata import QuoteVersion
from .marketdata.quote import PRoboQuoteAPI
from IPython.display import display
import json

T = TypeVar('T')  # T will be _Quote, _USQuote, etc.
QuoteData = PRoboQuoteAPI.QuoteData

from .marketdata.quote_data.quote_status import QuoteEventCode ,QuotationCode
from dataclasses import dataclass, asdict , field

class QuoteEventCodeNamespace:
    CONNECTING            = QuoteEventCode.CONNECTING.value
    CONNECTED             = QuoteEventCode.CONNECTED.value
    MANUAL_DISCONNECT     = QuoteEventCode.MANUAL_DISCONNECT.value
    DISCONNECTED          = QuoteEventCode.DISCONNECTED.value
    RECONNECTING          = QuoteEventCode.RECONNECTING.value
    RECONNECTED           = QuoteEventCode.RECONNECTED.value
    RECONNECT_FAILED      = QuoteEventCode.RECONNECT_FAILED.value
    RECONNECT_MAX_REACHED = QuoteEventCode.RECONNECT_MAX_REACHED.value
    HEARTBEAT_TIMEOUT     = QuoteEventCode.HEARTBEAT_TIMEOUT.value
    WS_TIMEOUT            = QuoteEventCode.WS_TIMEOUT.value
    WS_ERROR              = QuoteEventCode.WS_ERROR.value
    SUBSCRIBE_REQUEST     = QuoteEventCode.SUBSCRIBE_REQUEST.value
    SUBSCRIBE_OK_KBAR     = QuoteEventCode.SUBSCRIBE_OK_KBAR.value
    SUBSCRIBE_OK          = QuoteEventCode.SUBSCRIBE_OK.value
    SUBSCRIBE_FAIL        = QuoteEventCode.SUBSCRIBE_FAIL.value
    UNSUBSCRIBE_REQUEST   = QuoteEventCode.UNSUBSCRIBE_REQUEST.value
    UNSUBSCRIBE_OK_KBAR   = QuoteEventCode.UNSUBSCRIBE_OK_KBAR.value
    UNSUBSCRIBE_OK        = QuoteEventCode.UNSUBSCRIBE_OK.value
    UNSUBSCRIBE_FAIL      = QuoteEventCode.UNSUBSCRIBE_FAIL.value
    UNSUBSCRIBE_ALL       = QuoteEventCode.UNSUBSCRIBE_ALL.value
    MO_SUBSCRIPTIONS      = QuoteEventCode.MO_SUBSCRIPTIONS.value
    DATA_ERROR            = QuoteEventCode.DATA_ERROR.value
    NO_DATA_TIMEOUT       = QuoteEventCode.NO_DATA_TIMEOUT.value
    BACKPRESSURE          = QuoteEventCode.BACKPRESSURE.value
    CALLBACK_SLOW         = QuoteEventCode.CALLBACK_SLOW.value
    DROPPED_MESSAGE       = QuoteEventCode.DROPPED_MESSAGE.value
    CALLBACK_REGISTERED   = QuoteEventCode.CALLBACK_REGISTERED.value
    CALLBACK_UNREGISTERED = QuoteEventCode.CALLBACK_UNREGISTERED.value
    CALLBACK_EXCEPTION    = QuoteEventCode.CALLBACK_EXCEPTION.value
    INTERNAL_ERROR        = QuoteEventCode.INTERNAL_ERROR.value
    USER_ERROR            = QuoteEventCode.USER_ERROR.value

    def __get__(self, instance, owner):
        if instance is None:
            return self
        # 100% 執行你指定的防禦邏輯，去 dataclass 的 __dict__ 抓轉接完的字串
        val = instance.__dict__.get('event_code', getattr(instance, '_event_code_raw', ''))
        StrWithMagic = type('StrWithMagic', (str,), dict(QuoteEventCodeNamespace.__dict__))
        return StrWithMagic(val)

class QuotationCodeNamespace:
    QUOTE_SERVER_UNKNOWN_ERROR              = QuotationCode.QUOTE_SERVER_UNKNOWN_ERROR.value
    QUOTE_SERVER_TOO_MANY_CONNECTIONS       = QuotationCode.QUOTE_SERVER_TOO_MANY_CONNECTIONS.value
    QUOTE_SERVER_INVALID_TOKEN              = QuotationCode.QUOTE_SERVER_INVALID_TOKEN.value
    QUOTE_SERVER_TOO_MANY_SUBSCRIPTIONS     = QuotationCode.QUOTE_SERVER_TOO_MANY_SUBSCRIPTIONS.value
    QUOTE_SERVER_NO_PERMISSION_SUBSCRIPTION = QuotationCode.QUOTE_SERVER_NO_PERMISSION_SUBSCRIPTION.value
    QUOTE_SERVER_INVALID_SYMBOL_SUBSCRIPTION= QuotationCode.QUOTE_SERVER_INVALID_SYMBOL_SUBSCRIPTION.value
    QUOTE_SERVER_SUBSCRIPTION_ID_DUPLICATED = QuotationCode.QUOTE_SERVER_SUBSCRIPTION_ID_DUPLICATED.value
    SUCCESSFUL_OPERATION                    = QuotationCode.SUCCESSFUL_OPERATION.value
    QUOTE_UNKNOWN_ERROR                     = QuotationCode.QUOTE_UNKNOWN_ERROR.value
    QUOTE_SUBSCRIPTION_PARAM_QUOTE_TYPE_NONE= QuotationCode.QUOTE_SUBSCRIPTION_PARAM_QUOTE_TYPE_NONE.value
    QUOTE_SUBSCRIPTION_PARAM_SYMBOL_EMPTY   = QuotationCode.QUOTE_SUBSCRIPTION_PARAM_SYMBOL_EMPTY.value
    QUOTE_SUBSCRIPTION_PARAM_SESSION_NOT_INT= QuotationCode.QUOTE_SUBSCRIPTION_PARAM_SESSION_NOT_INT.value
    QUOTE_SUBSCRIPTION_PARAM_VERSION_NONE   = QuotationCode.QUOTE_SUBSCRIPTION_PARAM_VERSION_NONE.value
    QUOTE_SUBSCRIPTION_TYPE_UNKNOWN         = QuotationCode.QUOTE_SUBSCRIPTION_TYPE_UNKNOWN.value
    QUOTE_WS_PERMISSION_FAILED_FETCH        = QuotationCode.QUOTE_WS_PERMISSION_FAILED_FETCH.value
    QUOTE_WS_PERMISSION_FAILED_HTTP         = QuotationCode.QUOTE_WS_PERMISSION_FAILED_HTTP.value
    QUOTE_WS_PERMISSION_FAILED_JSON_PARSE   = QuotationCode.QUOTE_WS_PERMISSION_FAILED_JSON_PARSE.value
    QUOTE_WS_PERMISSION_NOT_ALLOWED_RULE    = QuotationCode.QUOTE_WS_PERMISSION_NOT_ALLOWED_RULE.value
    QUOTE_WS_SUBSCRIPTION_REJECTED          = QuotationCode.QUOTE_WS_SUBSCRIPTION_REJECTED.value
    QUOTE_WS_SUBSCRIPTION_NO_PERMISSION     = QuotationCode.QUOTE_WS_SUBSCRIPTION_NO_PERMISSION.value
    QUOTE_WS_SUBSCRIPTION_LIMIT_EXCEEDED    = QuotationCode.QUOTE_WS_SUBSCRIPTION_LIMIT_EXCEEDED.value
    QUOTE_WS_SUBSCRIPTION_DUPLICATE_ID      = QuotationCode.QUOTE_WS_SUBSCRIPTION_DUPLICATE_ID.value
    QUOTE_WS_SUBSCRIPTION_INVALID_QUOTE_TYPE= QuotationCode.QUOTE_WS_SUBSCRIPTION_INVALID_QUOTE_TYPE.value
    QUOTE_WS_SUBSCRIPTION_INVALID_SUB_ID    = QuotationCode.QUOTE_WS_SUBSCRIPTION_INVALID_SUB_ID.value
    QUOTE_WS_SUBSCRIPTION_INVALID_PARAMETER = QuotationCode.QUOTE_WS_SUBSCRIPTION_INVALID_PARAMETER.value
    QUOTE_WS_TIMEOUT                        = QuotationCode.QUOTE_WS_TIMEOUT.value
    QUOTE_WS_DATA_JSON_DECODE_ERROR         = QuotationCode.QUOTE_WS_DATA_JSON_DECODE_ERROR.value
    QUOTE_WS_RECONNECT_ATTEMPT_FAILED       = QuotationCode.QUOTE_WS_RECONNECT_ATTEMPT_FAILED.value
    QUOTE_WS_RECONNECT_RESUBSCRIBE_EXCEPTION= QuotationCode.QUOTE_WS_RECONNECT_RESUBSCRIBE_EXCEPTION.value
    QUOTE_WS_RECONNECT_MAX_REACHED          = QuotationCode.QUOTE_WS_RECONNECT_MAX_REACHED.value
    QUOTE_CALLBACK_EXCEPTION                = QuotationCode.QUOTE_CALLBACK_EXCEPTION.value
    QUOTE_CALLBACK_NAME_INVALID             = QuotationCode.QUOTE_CALLBACK_NAME_INVALID.value
    QUOTE_CALLBACK_PARAM_COUNT_INVALID      = QuotationCode.QUOTE_CALLBACK_PARAM_COUNT_INVALID.value
    QUOTE_CALLBACK_VERSION_MISMATCH         = QuotationCode.QUOTE_CALLBACK_VERSION_MISMATCH.value
    QUOTE_CALLBACK_SUBSCRIPTION_TYPE_UNSUPPORTED = QuotationCode.QUOTE_CALLBACK_SUBSCRIPTION_TYPE_UNSUPPORTED.value
    QUOTE_CALLBACK_VERSION_UNSUPPORTED      = QuotationCode.QUOTE_CALLBACK_VERSION_UNSUPPORTED.value
    QUOTE_DATA_INVALID_MARKET_TYPE          = QuotationCode.QUOTE_DATA_INVALID_MARKET_TYPE.value
    QUOTE_DATA_FACADE_NOT_FOUND             = QuotationCode.QUOTE_DATA_FACADE_NOT_FOUND.value
    QUOTE_DATA_FACADE_CALLBACK_ERROR        = QuotationCode.QUOTE_DATA_FACADE_CALLBACK_ERROR.value
    QUOTE_DATA_FACADE_NAME_NOT_FOUND        = QuotationCode.QUOTE_DATA_FACADE_NAME_NOT_FOUND.value
    QUOTE_DATA_GETTER_MISSING               = QuotationCode.QUOTE_DATA_GETTER_MISSING.value
    QUOTE_DATA_ACCESS_DENIED                = QuotationCode.QUOTE_DATA_ACCESS_DENIED.value
    QUOTE_STATICDATA_HTTP_STATUS_NOT_200    = QuotationCode.QUOTE_STATICDATA_HTTP_STATUS_NOT_200.value
    QUOTE_STATICDATA_FETCH_PARSE_EXCEPTION  = QuotationCode.QUOTE_STATICDATA_FETCH_PARSE_EXCEPTION.value
    QUOTE_STATICDATA_EMPTY_DATA             = QuotationCode.QUOTE_STATICDATA_EMPTY_DATA.value
    QUOTE_STATICDATA_PERIODIC_FETCH_EXCEPTION= QuotationCode.QUOTE_STATICDATA_PERIODIC_FETCH_EXCEPTION.value
    QUOTE_STATICDATA_PRODUCT_FILE_PARSE_ERROR= QuotationCode.QUOTE_STATICDATA_PRODUCT_FILE_PARSE_ERROR.value
    QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID = QuotationCode.QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID.value
    QUOTE_STATICDATA_KBAR_MINUTE_UNSUPPORTED= QuotationCode.QUOTE_STATICDATA_KBAR_MINUTE_UNSUPPORTED.value
    QUOTE_STATICDATA_KBAR_DUPLICATE_SUBSCRIPTION   = QuotationCode.QUOTE_STATICDATA_KBAR_DUPLICATE_SUBSCRIPTION.value
    QUOTE_STATICDATA_KBAR_KEY_NOT_FOUND     = QuotationCode.QUOTE_STATICDATA_KBAR_KEY_NOT_FOUND.value
    QUOTE_STATICDATA_KBAR_INVALID_SUB_ID    = QuotationCode.QUOTE_STATICDATA_KBAR_INVALID_SUB_ID.value
    QUOTE_WS_ERROR                          = QuotationCode.QUOTE_WS_ERROR.value
    QUOTE_WS_DISCONNECTED                   = QuotationCode.QUOTE_WS_DISCONNECTED.value

    def __get__(self, instance, owner):
        if instance is None:
            return self
        # 100% 執行你指定的防禦邏輯，去 dataclass 的 __dict__ 抓轉接完的字串
        val = instance.__dict__.get('respond_code', getattr(instance, '_respond_code_raw', ''))
        StrWithMagic = type('StrWithMagic', (str,), dict(QuotationCodeNamespace.__dict__))
        return StrWithMagic(val)
    
@dataclass
class Event:
    api_id: str = ""
    event_code: Any = QuoteEventCodeNamespace()
    respond_code: Any = QuotationCodeNamespace()
    info: dict = field(default_factory=dict)
    event_msg: str = ""

def _verify_odd(odd):
    if odd == True or str(odd) == '1':
        return 1
    if odd == False or str(odd) == '0':
        return 0
    else:
        raise ValueError("Invalid input for odd. Acceptable values are True, False, '1', or '0'.")

def _verify_version(version):
    if version == 'v1' or version == QuoteVersion.v1:
        return QuoteVersion.v1
    elif version == 'v0' or version == QuoteVersion.v0:
        return QuoteVersion.v0
    else:
        raise ValueError('Invalid version provided. Only "v1" or "v0" are acceptable.') 
    
def default_cb(data):
    pass
    
class _Quote:
    def __init__(self, api):
        self._api = api.Quote
        self._list = list(self._api.Contracts.keys())
        self.set_cb_event(default_cb)

    def set_cb_event(self, callback):
        # 這裡定義轉接邏輯
        def _wrapper(api_id:str , event_code:QuoteEventCode ,  respond_code:QuotationCode, info:dict , event_msg: str):
            # 1. 將參數打包成 DataClass 物件
            cb_data = Event(
                api_id=api_id,
                event_code=event_code,
                respond_code=respond_code,
                info= info,
                event_msg=event_msg
            )
            if callback:
                callback(cb_data)
        
        self._api.set_cb_event(_wrapper)

    def subscribe_tick(self, symbol, odd_lot=False, version=QuoteVersion.v1):
        _odd = _verify_odd(odd_lot)
        _version = _verify_version(version)
        if  symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        self._api.subscribe_tick(symbol=symbol, is_odd=_odd, version=_version)

    def set_cb_tick(self ,callback ,version=QuoteVersion.v1):
        _version = _verify_version(version)
        self._api.set_cb_tick( func= callback ,version= _version)

    def subscribe_bidask(self, symbol, odd_lot=False, version=QuoteVersion.v1):
        _odd = _verify_odd(odd_lot)
        _version = _verify_version(version)
        if  symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        self._api.subscribe_bidask(symbol=symbol, is_odd=_odd, version=_version)

    def set_cb_bidask(self ,callback ,version=QuoteVersion.v1):
        _version = _verify_version(version)
        self._api.set_cb_bidask( func= callback ,version= _version)

    def subscribe_all(self, symbol, odd_lot=False, version=QuoteVersion.v1):
        _odd = _verify_odd(odd_lot)
        _version = _verify_version(version)
        if  symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        self._api.subscribe_all(symbol=symbol, is_odd=_odd, version=_version)

    def set_cb_all(self ,callback ,version=QuoteVersion.v1):
        _version = _verify_version(version)
        self._api.set_cb_all( func= callback ,version= _version)

    def subscribe_kbar(self ,symbol ,minute=1):
        """
        minute: 1,3,5,15,30,60
        """
        if  symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        self._api.subscribe_kbar(symbol=symbol, minute=minute)

    def set_cb_kbar(self ,callback):
        self._api.set_cb_kbar( func= callback )

    # def subscribe_index(self, symbol):
    #     """
    #     '001':'TWSE','101':'OTC'
    #     """
    #     if  symbol not in ['001' ,'101']:
    #         print(f"[系統訊息] 股票代號 {symbol} 無效。")
    #         return 
    #     self._api.subscribe_index(symbol=symbol)

    # def set_cb_index(self,callback ):
    #     self._api.set_cb_index( func= callback)

    def get_subscriptions(self):
        return self._api.get_subscriptions()

    def unsubscribe(self ,label ):
        if 'qtIndex'in label:
            self._api.unsubscribe_index(label[-3:])
        else:
            self._api.unsubscribe_by_id(label)

    def unsubscribe_all(self):
        ls = self.get_subscriptions()
        for label in ls:
            self.unsubscribe(label)

class _USQuote:
    def __init__(self, api):
        self._api = api.USQuote
        self._list = list(self._api.Contracts.keys())
        self.set_cb_event(default_cb)

    def set_cb_event(self, callback):
        # 這裡定義轉接邏輯
        def _wrapper(api_id:str , event_code:QuoteEventCode ,  respond_code:QuotationCode, info:dict , event_msg: str):
            # 1. 將參數打包成 DataClass 物件
            cb_data = Event(
                api_id=api_id,
                event_code=event_code,
                respond_code=respond_code,
                info= info,
                event_msg=event_msg
            )
            if callback:
                callback(cb_data)
        
        self._api.set_cb_event(_wrapper)

    def subscribe_tick(self, symbol, version=QuoteVersion.v1):
        if  symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        _version = _verify_version(version)
        self._api.subscribe_tick(symbol = symbol, version =_version)

    def set_cb_tick(self ,callback ,version=QuoteVersion.v1):
        _version = _verify_version(version)
        self._api.set_cb_tick( func= callback ,version= _version)
        # wrapped = wrap_callback(callback)
        # self._api.set_cb_tick( func= wrapped ,version= _version)

    def subscribe_bidask(self, symbol ):
        if  symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        _version=QuoteVersion.v0
        self._api.subscribe_bidask(symbol=symbol ,version=_version)

    def set_cb_bidask(self ,callback):
        _version=QuoteVersion.v0
        self._api.set_cb_bidask( func= callback ,version= _version)

    def subscribe_all(self, symbol ,version=QuoteVersion.v1):
        if  symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        _version = _verify_version(version)
        self._api.subscribe_all(symbol=symbol ,version=_version)

    def set_cb_all(self ,callback ,version=QuoteVersion.v1):
        _version = _verify_version(version)
        self._api.set_cb_all( func= callback ,version= _version)

    def subscribe_kbar(self ,symbol ,minute=1):
        if  symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        self._api.subscribe_kbar(symbol=symbol, minute=minute)

    def set_cb_kbar(self ,callback):
        self._api.set_cb_kbar( func= callback )

    def get_subscriptions(self):
        return self._api.get_subscriptions()
    
    def unsubscribe(self ,label ):
        self._api.unsubscribe_by_id(label)

    def unsubscribe_all(self):
        ls = self.get_subscriptions()
        for label in ls:
            self.unsubscribe(label)

class _FutQuote:
    def __init__(self, api):
        self._api = api.FutQuote
        self._list = list(self._api.Contracts.keys())
        self.set_cb_event(default_cb)

    def set_cb_event(self, callback):
        # 這裡定義轉接邏輯
        def _wrapper(api_id:str , event_code:QuoteEventCode ,  respond_code:QuotationCode, info:dict , event_msg: str):
            # 1. 將參數打包成 DataClass 物件
            cb_data = Event(
                api_id=api_id,
                event_code=event_code,
                respond_code=respond_code,
                info= info,
                event_msg=event_msg
            )
            if callback:
                callback(cb_data)
        
        self._api.set_cb_event(_wrapper)

    def subscribe_tick(self, symbol ):
        _symbol = symbol[:3]
        if  _symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        version=QuoteVersion.v0
        self._api.subscribe_tick(symbol=symbol ,version=version)

    def set_cb_tick(self ,callback):
        version=QuoteVersion.v0
        self._api.set_cb_tick( func= callback ,version= version)

    def subscribe_bidask(self, symbol ):
        _symbol = symbol[:3]
        if  _symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        version=QuoteVersion.v0
        self._api.subscribe_bidask(symbol=symbol ,version=version)

    def set_cb_bidask(self ,callback):
        version=QuoteVersion.v0
        self._api.set_cb_bidask( func= callback ,version= version)

    def subscribe_all(self, symbol):
        _symbol = symbol[:3]
        if  _symbol not in self._list  and len(self._list) !=0 :
            print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
            return 
        version=QuoteVersion.v0
        self._api.subscribe_all(symbol=symbol ,version=version)

    def set_cb_all(self ,callback):
        version=QuoteVersion.v0
        self._api.set_cb_all( func= callback ,version= version)

    def get_subscriptions(self):
        return self._api.get_subscriptions()

    def unsubscribe(self ,label ):
        self._api.unsubscribe_by_id(label)

    def unsubscribe_all(self):
        ls = self.get_subscriptions()
        for label in ls:
            self.unsubscribe(label)

class Subscribe:
    def __init__(self, AUTH, count=10):
        self.AUTH = AUTH
        self.count = count

        # 保存 PRoboQuoteAPI 實例的容器（初始化時只建第一條）
        self.__api_list: List[PRoboQuoteAPI | None] = [None] * count

        # 代理容器，用於 _Quote, _USQuote, _FutQuote
        self.Quote = LazyQuoteList(_Quote, self.__api_list, AUTH)
        self.USQuote = LazyQuoteList(_USQuote, self.__api_list, AUTH)
        self.FutQuote = LazyQuoteList(_FutQuote, self.__api_list, AUTH)

    def stop(self):
        # 停止已初始化的 API
        for api in self.__api_list:
            if api is not None:
                api.stop()

# ===== LazyQuoteList =====
class LazyQuoteList:
    def __init__(self, quote_cls, api_list, AUTH):
        self._quote_cls = quote_cls
        self._api_list = api_list
        self._AUTH = AUTH
        self._list: List[Any | None] = [None] * len(api_list)

    def __getitem__(self, index):
        if self._list[index] is None:
            print("[系統訊息] 開始訂閱服務初始化。")
            # 延遲初始化 PRoboQuoteAPI
            if self._api_list[index] is None:
                self._api_list[index] = PRoboQuoteAPI(
                    auth=self._AUTH,
                    max_queue_size=10000,
                    auto_reconnect_when_disconnected=False,
                    show_env_log_info=False,
                    need_record_log=True,
                    locale ='zh-KGI'
                )
            self._list[index] = self._quote_cls(self._api_list[index])
        return self._list[index]

    def __len__(self):
        return len(self._api_list)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

class _BaseQuoteManager(Generic[T]):
    def __init__(self, list):
        self.__list = list
        self.__default: T | None = None  # 不要現在拿

    def _get_default(self) -> T:
        if self.__default is None:
            self.__default = self.__list[0]  # 真的用到才拿
        return self.__default

    def __getitem__(self, index: int) -> T:
        return self.__list[index]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get_default(), name)

    def __dir__(self):
        return dir(self._get_default())

    def __len__(self):
        return len(self.__list)

class _STKQuoteManager(_Quote, _BaseQuoteManager[_Quote]):
    def __init__(self, list: List[_Quote]):
        _BaseQuoteManager.__init__(self, list)

class _USQuoteManager(_USQuote, _BaseQuoteManager[_USQuote]):
    def __init__(self, list: List[_USQuote]):
        _BaseQuoteManager.__init__(self, list)

class _FutQuoteManager(_FutQuote, _BaseQuoteManager[_FutQuote]):
    def __init__(self, list: List[_FutQuote]):
        _BaseQuoteManager.__init__(self, list)
