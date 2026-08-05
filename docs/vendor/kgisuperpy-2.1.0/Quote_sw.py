from IPython.display import display
import time
from typing import List, Any
from .marketdata.quote_starwave import StarWaveQuoteAPI ,Event
from .marketdata.starwave.sw_status import SWEventCode ,SWRespondCode

class EventCodeNamespace:
    """讓 kgi.SWEvent.event_code 可以像 Enum 一樣被點出來"""
    CONNECTED = SWEventCode.CONNECTED.value
    DISCONNECTED = SWEventCode.DISCONNECTED.value
    LOGIN_OK = SWEventCode.LOGIN_OK.value
    LOGIN_FAIL = SWEventCode.LOGIN_FAIL.value
    CONTRACTS_READY = SWEventCode.CONTRACTS_READY.value
    SUBSCRIBE_OK = SWEventCode.SUBSCRIBE_OK.value
    SUBSCRIBE_FAIL = SWEventCode.SUBSCRIBE_FAIL.value
    UNSUBSCRIBE_OK = SWEventCode.UNSUBSCRIBE_OK.value
    UNSUBSCRIBE_FAIL = SWEventCode.UNSUBSCRIBE_FAIL.value

    # 當實例呼叫時（如 event.event_code），直接回傳底層原本的字串或值
    def __get__(self, instance, owner):
        if instance is None:
            return self  # 當由類別呼叫 kgi.SWEvent.event_code 時，回傳這個命名空間物件
        # 當由實例呼叫 event.event_code 时，利用原本 C++ 綁定在實例上的機制拿值
        # 這裡用雙底線可能拿得到，若拿不到則由 getattr 處理
        return instance.__dict__.get('event_code', getattr(instance, '_event_code_raw', ''))

class RespondCodeNamespace:
    """讓 kgi.SWEvent.status_code 可以像 Enum 一樣被點出來"""
    SUCCESSFUL_OPERATION = SWRespondCode.SUCCESSFUL_OPERATION.value
    SW_DISCONNECTED      = SWRespondCode.SW_DISCONNECTED.value
    SW_LOGIN_FAILED      = SWRespondCode.SW_LOGIN_FAILED.value
    SW_SUBSCRIBE_FAILED  = SWRespondCode.SW_SUBSCRIBE_FAILED.value
    SW_SUBSCRIBE_REJECTED = SWRespondCode.SW_SUBSCRIBE_REJECTED.value
    SW_UNSUBSCRIBE_FAILED = SWRespondCode.SW_UNSUBSCRIBE_FAILED.value
    SW_SUBSCRIBE_LIMITED  = SWRespondCode.SW_SUBSCRIBE_LIMITED.value

    def __get__(self, instance, owner):
        if instance is None:
            return self  # 當由類別呼叫 kgi.SWEvent.status_code 時
        
        # 100% 複製你的邏輯！event_code 用 _event_code_raw，status_code 自然就用 _status_code_raw
        return instance.__dict__.get('status_code', getattr(instance, '_status_code_raw', ''))

SWEvent =Event
SWEvent.event_code = EventCodeNamespace()
SWEvent.status_code = RespondCodeNamespace()

def on_quote(data):
    display(data)

def on_event(event):
    pass

exchange_map = {
    '1': 'TWSE', 1: 'TWSE',
    '2': 'OTC',  2: 'OTC',
    '3': 'ES',   3: 'ES',
    '4': 'TWSE', 4: 'TWSE'
}

class SWQuote:
    def __init__(self, api):
        self._api = api
        sorted_items = sorted(self._api.StockContracts.values(), key=lambda x: x['symbol'])
        self.contracts ={item['symbol']: exchange_map.get(item['exchange'], item['exchange'])  for item in sorted_items}
        self._on_quote = on_quote
        self._on_event = on_event
        self._api.set_cb_marketdata(self._on_quote)
        self._api.set_cb_event(self._on_event)

    def set_cb(self, callback):
        self._on_quote = callback
        self._api.set_cb_marketdata(self._on_quote )

    def set_event(self ,callback):
        self._on_event =callback
        self._api.set_cb_event(self._on_event)

    def subscribe(self, symbol, odd=False):
        if not self._api.is_connected():
            self._api.connect()      
            elapsed_time = 0.0
            while elapsed_time < 3:
                if self._api.is_connected():
                    break  
                time.sleep(0.1)
                elapsed_time += 0.1

        # if not self._api.is_connected():
        #         raise TimeoutError("[連線失敗] 超過 3 秒未回應，請檢查網路或 API 狀態。")

        exchange = self.contracts.get(symbol)
        if exchange is None:
            if symbol.startswith('TXF'): # 改用 startswith 更安全
                exchange = 'TAIFEX'
                self._api.subscribe(exchange, symbol)
                return
            else:
                print(f"[系統訊息] 商品代碼不存在，請輸入正確商品代碼。")
                return

        if odd:
            exchange = exchange + 'Odd'
            symbol = symbol + '.Odd'
        self._api.subscribe(exchange, symbol)

    def get_subscriptions(self):
        return self._api.get_subscriptions()

    def unsubscribe(self, label):
        # label 格式為 "exchange.symbol"，只切第一個 '.' 以保留 symbol 中可能含有的 '.Odd'
        exchange, symbol = label.split('.', 1)
        self._api.unsubscribe(exchange, symbol)
        # 若已無任何訂閱，主動停止連線釋放資源
        ls = self.get_subscriptions()
        if len(ls) == 0:
            self._api.disconnect()

    def unsubscribe_all(self):
        self._api.disconnect()


class LazyQuoteList:
    """延遲初始化的行情通道列表，只有在真正存取時才建立連線。"""

    def __init__(self, quote_cls, api_list, AUTH):
        self._quote_cls = quote_cls
        self._api_list = api_list
        self._AUTH = AUTH
        self._list: List[Any | None] = [None] * len(api_list)

    def __getitem__(self, index):
        # 尚未初始化時才建立，避免不必要的連線開銷
        if self._list[index] is None:
            print(f"[系統訊息] 開始訂閱服務初始化 (通道 {index})。")

            if self._api_list[index] is None:
                self._api_list[index] = StarWaveQuoteAPI(self._AUTH ,
                                                        max_queue_size=10000,
                                                        )

            self._list[index] = self._quote_cls(self._api_list[index])

        return self._list[index]

    def __getattr__(self, name):
        # 將屬性存取代理至第 0 號通道
        return getattr(self[0], name)

    def __len__(self):
        return len(self._api_list)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def __dir__(self):
        # 合併自身屬性與代理目標的屬性，確保 Tab 補全可正確顯示
        own = list(self.__dict__.keys())
        proxy = dir(self[0]) if self._list[0] is not None else dir(self._quote_cls)
        return list(set(own + proxy))


class SubscribeList:
    """行情訂閱管理器，支援多通道延遲初始化，並代理至 LazyQuoteList。"""

    def __init__(self, AUTH, count=10):
        self.__api_list: List[StarWaveQuoteAPI | None] = [None] * count
        self._lazy_list = LazyQuoteList(SWQuote, self.__api_list, AUTH)

    def __getitem__(self, index):
        # 處理 api.SWQuote[n] 的呼叫，轉發給 LazyQuoteList
        return self._lazy_list[index]

    def __getattr__(self, name):
        # 處理 api.SWQuote.subscribe 等屬性呼叫，轉發給 LazyQuoteList
        return getattr(self._lazy_list, name)

    def __dir__(self):
        # 合併自身屬性與 LazyQuoteList 的屬性，確保 Tab 補全可正確顯示
        own = list(self.__dict__.keys())
        proxy = dir(self._lazy_list)
        return list(set(own + proxy))