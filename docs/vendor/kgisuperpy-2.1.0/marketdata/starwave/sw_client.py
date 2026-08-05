import os
import re
import atexit
import queue
import weakref
import threading
import time
from collections import deque
from dataclasses import dataclass
#----------------------------------------------------------------------------
try:
    from IPython.display import display
except ImportError:
    display = print
#----------------------------------------------------------------------------
from .starwave_api import StarWaveAPI, MsgType, SubscribeType, MarketData
from .sw_quotedata import SWMarketData, SWDataType
from .sw_logger import write_log, LoggingType
from .sw_status import SWEventCode, SWRespondCode
from .sw_subscription_rule import subscription_rules
from ..locale_manager import LocaleManager
#----------------------------------------------------------------------------
DEFAULT_QUEUE_SIZE = 2000
#----------------------------------------------------------------------------
_MSGTYPE_TO_SWTYPE = {
    MsgType.Recover:          SWDataType.Snapshot,
    MsgType.OrderBookData:    SWDataType.Orderbook,
    MsgType.Trade:            SWDataType.MatchInfo,
    MsgType.TotalTrade:       SWDataType.TotalMatchInfo,
    MsgType.DayHighLow:       SWDataType.DayHighLow,
    MsgType.OpeningInfo:      SWDataType.OpenInfo,
}
#----------------------------------------------------------------------------
@dataclass
class Event:
    event_code: int
    status_code: int
    message: str
    native_code: str = None  # StarWave server's own numeric error code (e.g. "8107"), when present in message
#----------------------------------------------------------------------------
def _atexit_cleanup(client_ref):
    client = client_ref()
    if client is not None:
        try:
            client.shutdown()
        except Exception:
            pass
#----------------------------------------------------------------------------
_NATIVE_CODE_RE = re.compile(r"(?:^|\s)(\d{3,6}):")
def _extract_native_code(message: str):
    # StarWave embeds its own numeric error code in the free-text message, e.g.
    # "channel[SW001] 8107:此帳號登入連線數量已超過系統設定". Pull it out so callers
    # can branch on the code (e.g. skip reconnect on 8107) without parsing the string.
    m = _NATIVE_CODE_RE.search(message)
    return m.group(1) if m else None
#----------------------------------------------------------------------------
def _cstr(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.split(b"\x00", 1)[0].decode("utf-8", "ignore")
    return "" if value is None else str(value)
#----------------------------------------------------------------------------
class _SWDropQueue:
    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE):
        self._dq = deque(maxlen=maxsize)
        self._cond = threading.Condition()

    def put(self, item):
        with self._cond:
            self._dq.append(item)
            self._cond.notify()

    def get(self, timeout=None):
        with self._cond:
            if not self._dq:
                self._cond.wait(timeout)
            return self._dq.popleft() if self._dq else None

    def clear(self):
        with self._cond:
            self._dq.clear()
            self._cond.notify_all()
#----------------------------------------------------------------------------
class SWMarketDataClient(StarWaveAPI):
    def __init__(self, dll_path: str, api_id:str, version:str, max_queue_size: int = DEFAULT_QUEUE_SIZE, locale: str = "en", auth=None, show_api_id_in_log: bool = False):
        self.api_id = api_id
        self._show_api_id_in_log = show_api_id_in_log
        super().__init__(dll_path, version=version, instance_name=api_id)

        # Auth object (e.g. PRoboAuth) kept for permission / limit checks by callers.
        self._auth = auth

        # Localized event messages (StarWave keeps its own locales dir)
        self._locale = LocaleManager(
            locale, locales_dir=os.path.join(os.path.dirname(__file__), "locales")
        )

        self._records = {}
        self._records_lock = threading.Lock()
        self.contracts = {}    

        self._subscriptions = set()
        self._sub_lock = threading.Lock()

        self._logon_event = threading.Event()
        self._logon_ok = False
        self._logon_msg = ""
        self._disconnected_fired = False  # reset by on_connected; set by first on_disconnected

        self._current_channel = None

        # user callback
        self._on_cb_SWMarketData = self._default_data_cb
        self._on_cb_Event = self._default_event_cb

        self._queue = _SWDropQueue(max_queue_size)
        self._event_queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker = None
        self._event_worker = None
        self._worker_lock = threading.Lock()
        self._shutdown_done = False
        self.ensure_worker()

        atexit.register(_atexit_cleanup, weakref.ref(self))

    def _default_data_cb(self, data: SWMarketData):
        display(data.summary())

    def _default_event_cb(self, event: Event):
        pass

    def _log_and_trigger_event(self, level: LoggingType, event_code: SWEventCode, respond_code: SWRespondCode, **kwargs):
        message = self._locale.build_message(event_code, respond_code, **kwargs)
        if self._show_api_id_in_log:
            message = f"[{self.api_id}] {message}"
        write_log(level, message)
        self._event_queue.put(Event(
            event_code=event_code.value,
            status_code=respond_code.value,
            message=message,
            native_code=_extract_native_code(message),
        ))

    def _run_events(self):
        while not self._stop_event.is_set():
            try:
                event = self._event_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if event is None:
                continue
            try:
                self._on_cb_Event(event)
            except Exception as e:
                write_log(LoggingType.ERROR, f"event callback error: {e}")

    def on_connected(self):
        self._disconnected_fired = False
        self._log_and_trigger_event(LoggingType.INFO, SWEventCode.CONNECTED,
                                    SWRespondCode.SUCCESSFUL_OPERATION,
                                    channel=self._current_channel or "-")

    def on_disconnected(self):
        if self._disconnected_fired:
            return  # DLL fires on_disconnected twice on login failure; ignore the duplicate
        self._disconnected_fired = True
        self._logon_ok = False
        self._logon_event.clear()
        with self._sub_lock:
            self._subscriptions.clear()
        with self._records_lock:
            self._records.clear()
        self._queue.clear()
        self._log_and_trigger_event(LoggingType.WARNING, SWEventCode.DISCONNECTED,
                                    SWRespondCode.SW_DISCONNECTED)

    def on_logon_response(self, success: bool, message: str):
        if self._current_channel:
            message = f"channel[{self._current_channel}] {message}".strip()
        self._logon_ok = success
        self._logon_msg = message
        self._logon_event.set()
        if success:
            self._log_and_trigger_event(LoggingType.INFO, SWEventCode.LOGIN_OK, SWRespondCode.SUCCESSFUL_OPERATION, message=message)
        else:
            self._log_and_trigger_event(LoggingType.ERROR, SWEventCode.LOGIN_FAIL,SWRespondCode.SW_LOGIN_FAILED, message=message)

    def notify_login_failed(self, message: str = "",
                             event_code: SWEventCode = SWEventCode.LOGIN_FAIL,
                             respond_code: SWRespondCode = SWRespondCode.SW_LOGIN_FAILED):
        self._logon_ok = False
        self._logon_msg = message
        self._logon_event.set()
        self._log_and_trigger_event(LoggingType.ERROR, event_code, respond_code, message=message)

    def get_logon_native_code(self):
        # StarWave's own numeric error code (e.g. "8107") from the most recent
        # logon attempt's message, if any. Only meaningful right after a failed
        # logon/wait_logon/__try_login call.
        return _extract_native_code(self._logon_msg)

    def on_contract(self, exchange, symbol, name, maturity, category, contract):
        self.contracts[symbol] = {
            "exchange": exchange,
            "symbol": symbol,
            "name": name,
            "maturity": maturity,
            "category": category,
            "ref_price": contract.RefPrice,
            "bull_price": contract.BullPrice,
            "bear_price": contract.BearPrice,
            "market": contract.Market,
        }

    def on_contract_download_complete(self, total_contracts: int):
        self._log_and_trigger_event(LoggingType.INFO, SWEventCode.CONTRACTS_READY,
                                    SWRespondCode.SUCCESSFUL_OPERATION, total=total_contracts)

    def on_market_data(self, msg_type: MsgType, exchange, symbol, msg_time, data: MarketData):
        ts = time.perf_counter()
        try:
            data_copy = MarketData.from_buffer_copy(data)
        except Exception as e:
            write_log(LoggingType.ERROR, f"on_market_data copy failed: {e}")
            return
        self._queue.put((msg_type, exchange, symbol, msg_time, data_copy, ts))

    def ensure_worker(self):
        with self._worker_lock:
            need_data = self._worker is None or not self._worker.is_alive()
            need_event = self._event_worker is None or not self._event_worker.is_alive()
            if not need_data and not need_event:
                return
            self._stop_event.clear()
            self._shutdown_done = False
            if need_data:
                self._worker = threading.Thread(target=self._run, daemon=True)
                self._worker.start()
            if need_event:
                self._event_worker = threading.Thread(target=self._run_events, daemon=True)
                self._event_worker.start()

    def _run(self):
        while not self._stop_event.is_set():
            item = self._queue.get(timeout=0.1)
            if item is not None:
                try:
                    self._process(item)
                except Exception as e:
                    write_log(LoggingType.ERROR, f"process error: {e}")

    def _process(self, item):
        msg_type, exchange, symbol, msg_time, data, ts = item
        key = (exchange, symbol)
        with self._records_lock:
            rec = self._records.get(key)
            if rec is None:
                rec = SWMarketData()
                rec.exchange = exchange
                rec.symbol = symbol
                self._records[key] = rec
        self._fill(rec, msg_type, msg_time, data, ts)
        try:
            self._on_cb_SWMarketData(rec)
        except Exception as e:
            write_log(LoggingType.ERROR, f"data callback error: {e}")

    def _fill(self, rec: SWMarketData, msg_type, msg_time, data: MarketData, ts: float):
        rec.delay_time = time.perf_counter() - ts
        rec.data_type = int(_MSGTYPE_TO_SWTYPE.get(msg_type, SWDataType.Snapshot))
        if msg_time:
            rec.datetime = msg_time
        if msg_type == MsgType.Trade or msg_type == MsgType.OrderBookData:
            rec.status_remarks = data.StatusRemarks
            rec.raise_fall_remarks = data.RaiseFallRemarks

        snapshot = msg_type == MsgType.Recover
        # MatchInfo
        if snapshot or msg_type == MsgType.Trade:
            if data.MatchPrice > 0:
                rec.close = data.MatchPrice
                rec.volume = data.MatchQty
                match_time = _cstr(data.MatchTime)
            else:
                rec.close = data.TestMatchPrice
                rec.volume = data.TestMatchQty
                match_time = _cstr(data.TestMatchTime)
            rec.simtrade = int(bool(data.IsTestMatch))
            if match_time:
                rec.datetime = match_time

        # TotalTrade
        if snapshot or msg_type == MsgType.TotalTrade:
            rec.simtrade = int(bool(data.IsTestMatch))
            rec.total_volume = data.TotalMatchQty

        # DayHighLow
        if snapshot or msg_type == MsgType.DayHighLow:
            rec.high = data.HighPrice
            rec.low = data.LowPrice

        # OpenInfo
        if snapshot or msg_type == MsgType.OpeningInfo:
            rec.open = data.OpeningPrice

        # Orderbook
        if snapshot or msg_type in (MsgType.OrderBookData, MsgType.OddOrderBookData):
            rec.simtrade = int(bool(data.IsTestMatch))
            rec.bid_prices = [data.BidPrice1, data.BidPrice2, data.BidPrice3, data.BidPrice4, data.BidPrice5]
            rec.bid_volumes = [data.BidQty1, data.BidQty2, data.BidQty3, data.BidQty4, data.BidQty5]
            rec.ask_prices = [data.AskPrice1, data.AskPrice2, data.AskPrice3, data.AskPrice4, data.AskPrice5]
            rec.ask_volumes = [data.AskQty1, data.AskQty2, data.AskQty3, data.AskQty4, data.AskQty5]

    def logon(self, ip, port, user_id, password, download_contracts=False, version=None) -> bool:
        self._logon_ok = False
        self._logon_event.clear()
        return super().logon(ip, port, user_id, password, download_contracts, version)

    def wait_logon(self, timeout: float = 10.0) -> bool:
        if not self._logon_event.wait(timeout):
            return False
        return self._logon_ok

    def is_logged_in(self) -> bool:
        return self._logon_ok and self.is_connected()

    # Substrings in the DLL's last-error that mark a *transient* "still (re)logging in"
    # state. The auth table is repopulated on the network thread, so a subscribe that
    # lands during a (re)login can be rejected even though login will be ready a moment
    # later. These are worth one short retry rather than surfacing as a hard failure;
    # genuine failures (not authorized / quota exhausted) are NOT retried.
    _TRANSIENT_SUBSCRIBE_ERRORS = ("logon not applied", "not ready", "logon reply not received")

    def _subscribe_once_with_retry(self, exchange, symbol, sub_type,
                                   retries: int = 1, delay: float = 0.3) -> bool:
        ok = super().subscribe(exchange, symbol, sub_type)
        while not ok and retries > 0:
            err = (self.get_last_error() or "").lower()
            if not any(s in err for s in self._TRANSIENT_SUBSCRIBE_ERRORS):
                break  # genuine failure — don't retry
            time.sleep(delay)
            if not self.is_logged_in():
                break  # connection dropped meanwhile — let caller see the failure
            ok = super().subscribe(exchange, symbol, sub_type)
            retries -= 1
        return ok

    def subscribe(self, exchange: str, symbol: str, sub_type: SubscribeType = SubscribeType.SnapshotWithUpdate) -> bool:
        if not self.is_logged_in():
            self._log_and_trigger_event(LoggingType.WARNING, SWEventCode.SUBSCRIBE_FAIL,
                                        SWRespondCode.SW_SUBSCRIBE_FAILED,
                                        exchange=exchange, symbol=symbol,
                                        error="not logged in; call connect() first")
            return False
        key = (exchange, symbol)
        with self._sub_lock:
            if key in self._subscriptions:
                return True  # already subscribed — treat as success, skip the DLL
            ok = self._subscribe_once_with_retry(exchange, symbol, sub_type)
        if ok:
            self._subscriptions.add(key)
            self._log_and_trigger_event(LoggingType.INFO, SWEventCode.SUBSCRIBE_OK, SWRespondCode.SUCCESSFUL_OPERATION, exchange=exchange, symbol=symbol)
        else:
            self._log_and_trigger_event(LoggingType.WARNING, SWEventCode.SUBSCRIBE_FAIL,
                                        SWRespondCode.SW_SUBSCRIBE_FAILED,
                                        exchange=exchange, symbol=symbol, error=self.get_last_error())
        return ok

    def unsubscribe(self, exchange: str, symbol: str) -> bool:
        key = (exchange, symbol)
        with self._sub_lock:
            if key not in self._subscriptions:
                return True  # not subscribed — treat as success, skip the DLL
            ok = super().unsubscribe(exchange, symbol)
            if ok:
                self._subscriptions.discard(key)
        if ok:
            with self._records_lock:
                self._records.pop(key, None)
            self._log_and_trigger_event(LoggingType.INFO, SWEventCode.UNSUBSCRIBE_OK,
                                        SWRespondCode.SUCCESSFUL_OPERATION,
                                        exchange=exchange, symbol=symbol)
        else:
            self._log_and_trigger_event(LoggingType.WARNING, SWEventCode.UNSUBSCRIBE_FAIL,
                                        SWRespondCode.SW_UNSUBSCRIBE_FAILED,
                                        exchange=exchange, symbol=symbol, error=self.get_last_error())
        return ok

    def unsubscribe_all(self):
        with self._sub_lock:
            removed = [f"{ex}.{sym}" for (ex, sym) in self._subscriptions]
            if removed:
                super().unsubscribe_all()
                self._subscriptions.clear()
        if removed:
            with self._records_lock:
                self._records.clear()
            self._log_and_trigger_event(LoggingType.INFO, SWEventCode.UNSUBSCRIBE_OK,
                                        SWRespondCode.SUCCESSFUL_OPERATION,
                                        log_key="UNSUBSCRIBE_OK.ALL",
                                        count=len(removed), subscriptions=", ".join(removed))
        else:
            self._log_and_trigger_event(LoggingType.INFO, SWEventCode.UNSUBSCRIBE_OK,
                                        SWRespondCode.SUCCESSFUL_OPERATION,
                                        log_key="UNSUBSCRIBE_OK.ALL_EMPTY")

    def get_subscriptions(self):
        with self._sub_lock:
            return [f"{ex}.{sym}" for (ex, sym) in self._subscriptions]

    def get_subscriptions_count(self) -> int:
        with self._sub_lock:
            return len(self._subscriptions)

    def get_record(self, exchange: str, symbol: str):
        with self._records_lock:
            return self._records.get((exchange, symbol))
        
    def disconnect(self):
        try:
            self.unsubscribe_all()
        except Exception:
            pass
        self._queue.clear()
        super().disconnect()

    def shutdown(self):
        with self._worker_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        try:
            self.unsubscribe_all()
        except Exception:
            pass
        self._stop_event.set()
        self._queue.clear()
        current = threading.current_thread()
        for w in (self._worker, self._event_worker):
            if w and w.is_alive() and w is not current:
                w.join(timeout=1.0)
        super().disconnect()
#----------------------------------------------------------------------------