from __future__ import annotations
import sys
import os
import inspect
import queue
import string
import time
import requests
import fnmatch
import threading
import json
import typing
from collections import deque
from typing import Optional, Tuple, List, get_origin
from IPython.display import display
# from ..md_logger import logger
from ..md_logger import LoggingType, LoggingBase
from ..stomp_ws.client import WSClient
# from ...url import AutoRefresh
from .quotedata_subscription_rule import  subscription_rules
from .quotedata import QuoteData
from .quote_status import QuotationCode, QuotationError, QuoteEventCode
from ..locale_manager import LocaleManager
#----------------------------------------------------------------------------
BASE62_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
BASE = len(BASE62_ALPHABET)
#----------------------------------------------------------------------------
def encode_base62(num: int) -> str:
    if num == 0:
        return BASE62_ALPHABET[0]
    chars = []
    while num > 0:
        num, rem = divmod(num, BASE)
        chars.append(BASE62_ALPHABET[rem])
    return ''.join(reversed(chars))
#----------------------------------------------------------------------------
def generate_unique_id() -> str:
    ns = time.time_ns()
    return encode_base62(ns)
#----------------------------------------------------------------------------
# === WebSocket reconnect config ===
MAX_WS_RECONNECT_RETRIES = 20
WS_RECONNECT_DELAY_BASE_SEC = 5

# === Permission control ===
PERMISSION_EXACT_PATHS = set()
PERMISSION_WILDCARDS = []
DENY_WILDCARD_PATHS = set()

# === Logging & queue ===
SHOW_DROPPED_WARNING = False  # Set to True to show dropped items warning
DEFAULT_QUEUE_SIZE = 1000
#----------------------------------------------------------------------------
def set_ws_reconnect_delay(base_sec: int):
    global WS_RECONNECT_DELAY_BASE_SEC
    if base_sec <= 0:
        raise ValueError("Reconnect delay must be a positive integer.")
    WS_RECONNECT_DELAY_BASE_SEC = base_sec
#----------------------------------------------------------------------------
def set_ws_max_reconnect_retries(max_retries: int):
    global MAX_WS_RECONNECT_RETRIES
    if max_retries <= 0:
        raise ValueError("Max reconnect retries must be a positive integer.")
    MAX_WS_RECONNECT_RETRIES = max_retries
#----------------------------------------------------------------------------
def has_permission(path: str) -> bool:
    return True
    # def is_probable_variable_part(s: str) -> bool:
    #     return bool(s) and all(c.isalnum() or c in ('.', '-') for c in s)

    # if path in PERMISSION_EXACT_PATHS:
    #     return True

    # if path in DENY_WILDCARD_PATHS:
    #     return False

    # for prefix, suffix, _ in PERMISSION_WILDCARDS:
    #     if not (path.startswith(prefix) and path.endswith(suffix)):
    #         continue

    #     middle = path[len(prefix):-len(suffix) if suffix else None]

    #     reconstructed = prefix + middle + suffix
    #     if reconstructed != path:
    #         continue

    #     if not suffix and middle.endswith(".Odd"):
    #         odd_path = prefix + middle
    #         has_odd_permission = any(
    #             odd_path.startswith(pre) and odd_path.endswith(".Odd")
    #             for pre, suf, _ in PERMISSION_WILDCARDS if suf == ".Odd"
    #         )
    #         if not has_odd_permission:
    #             return False

    #     if prefix.endswith("futures.") and middle.startswith("current.") and path not in PERMISSION_EXACT_PATHS:
    #         return False

    #     if is_probable_variable_part(middle):
    #         return True

    # return False
#----------------------------------------------------------------------------
def is_symbol_allowed(market_type: str, symbol: str) -> bool:
    rules = subscription_rules.get(market_type)
    if rules is None:
        return False

    if market_type == "TWFuture":
        contract_type = "sub_contract" if len(symbol) <= 3 else "full_contract"
        rules = rules.get(contract_type)
        if not rules:
            return False

    mode = rules.get("mode")
    pattern_list = rules.get(mode, [])

    matched = any(fnmatch.fnmatch(symbol, pattern) for pattern in pattern_list)

    if mode == "whitelist":
        return matched
    elif mode == "blacklist":
        return not matched
    return False
#----------------------------------------------------------------------------
class FastDropQueue:
    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE):
        self._queue = deque(maxlen=maxsize)
        self._cond = threading.Condition()
        self._dropped_count = 0

    def put(self, item):
        with self._cond:
            if len(self._queue) == self._queue.maxlen:
                self._queue.popleft()  # Drop the oldest
                if SHOW_DROPPED_WARNING is True:
                    self._dropped_count += 1
            self._queue.append(item)
            self._cond.notify()

    def get(self, timeout: float = None):
        with self._cond:
            end = time.perf_counter() + timeout if timeout is not None else None
            while not self._queue:
                if timeout is None:
                    self._cond.wait()
                else:
                    remaining = end - time.perf_counter()
                    if remaining <= 0:
                        return None
                    self._cond.wait(timeout=remaining)
            return self._queue.popleft()

    def clear(self):
        with self._cond:
            self._queue.clear()
            self._cond.notify_all()

    def get_nowait(self):
        with self._cond:
            return self._queue.popleft() if self._queue else None

    def get_dropped_count(self):
        with self._cond:
            return self._dropped_count

    def reset_dropped_count(self):
        with self._cond:
            self._dropped_count = 0
#----------------------------------------------------------------------------
class HighSpeedQueueWorker(LoggingBase):
    def __init__(self, queue: FastDropQueue, handler_fn, api_id:str = "", show_api_id_in_log: bool = False):
        super().__init__(api_id=api_id, show_api_id_in_log=show_api_id_in_log)
        self._queue = queue
        self._handler = handler_fn
        self._stop_event = threading.Event()
        self._thread = None
        
    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._queue.clear()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            with self._queue._cond:
                self._queue._cond.notify_all()
            self._thread.join(timeout=1.0)
            self._thread = None

    def _run(self):
        while not self._stop_event.is_set():
            item = self._queue.get(timeout=0.01)
            if item is not None:
                self._process_item(item)
                
            if SHOW_DROPPED_WARNING:
                dropped = self._queue.get_dropped_count()
                if dropped > 0:
                    # logger.warning(f" Dropped {dropped} items due to queue overflow.")
                    self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_QUEUE_OVERFLOW_DROPPED.value}, Detail: Dropped {dropped} items due to queue overflow ")
                    self._queue.reset_dropped_count()

    def _process_item(self, item):
        try:
            subscription, content, timestamp = item
            self._handler(subscription, content, timestamp)
        except Exception as e:
            pass

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass
#----------------------------------------------------------------------------
class StaticDataClient(LoggingBase):
    timer: threading.Timer
    #__auth: AutoRefresh
    #auth: AutoRefresh
    _BasicQuote: BasicQuote
    def __init__(self, market_type: QuoteData.MarketType, auth, base_url: str, kbar_topic:str,  tick_topic:str, interval=5, use_ssl_verify:bool = True, api_id:str = "", show_api_id_in_log: bool = False):
        super().__init__(api_id=api_id, show_api_id_in_log=show_api_id_in_log)
        self.__auth = auth
        self.__headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.__auth.token}"}
        self._use_ssl_verify = use_ssl_verify
        self._base_url = base_url
        self._kbar_topic = kbar_topic
        self._tick_topic = tick_topic
        self.interval = interval
        self.market_type = market_type
        self.is_running = False
        self.timer_lock = threading.Lock()
        self.lock = threading.Lock()
        self.subscriptions = {}
        self.kbar_record = {}
        self.__OWL5_KBAR_SUPPORTED_MINUTES_LIST = []
        self.__OWL5_KBAR_SUBSCRIPTION_HANDLERS = {}

    def _set_kbar_minute_list(self, minutes_list: list):
        self.__OWL5_KBAR_SUPPORTED_MINUTES_LIST = minutes_list
    
    def _set_kbar_handlers(self, handler: dict):
        self.__OWL5_KBAR_SUBSCRIPTION_HANDLERS = handler

    def get_subscriptions(self):
        with self.lock:
            return list(self.subscriptions.values())
        
    def _get_table_list(self, data_id:str, data_url:str):
        try:
            response = requests.get(data_url, headers=self.__headers, verify=self._use_ssl_verify)
            if response.status_code == 200:
                data = response.json()
                if data:
                    return data
            else:
                #logger.error(f"Failed to GET data for {data_id}:", response.status_code)
                self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_HTTP_STATUS_NOT_200.value}, Detail: Failed to GET data for {data_id}:", response.status_code )
        except Exception as e:
            #logger.error(f"Error occurred during initial GET for {data_id}:", e)
            self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_PERIODIC_FETCH_EXCEPTION.value}, Detail: Error occurred during initial GET for {data_id}:", e)
        
    def get_day_tick_list(self, symbol:str):
        url = f"{self._base_url}{self._tick_topic}{symbol}"
        try:
            response = requests.get(url, headers=self.__headers, verify=self._use_ssl_verify)
            if response.status_code == 200:
                data = response.json()
                if data:
                    return data
            else:
                # logger.error(f"Failed to GET data for {symbol}:", response.status_code)
                self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_HTTP_STATUS_NOT_200.value}, Detail: Failed to GET data for {symbol}:", response.status_code )
                
        except Exception as e:
            #logger.error(f"Error occurred during initial GET for {symbol}:", e)
            self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_PERIODIC_FETCH_EXCEPTION.value}, Detail: Error occurred during initial GET for {symbol}:", e)
        
    def get_day_kbar_list(self, symbol: str, minute: int):
        url = f"{self._base_url}{self._kbar_topic}{symbol}"
        try:
            response = requests.get(url, headers=self.__headers, verify=self._use_ssl_verify)
            if response.status_code == 200:
                data = response.json()
                if data:
                    return data
            else:
                #logger.error(f"Failed to GET data for {symbol} ({minute}m):", response.status_code)
                self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_HTTP_STATUS_NOT_200.value}, Detail: Failed to GET data for {symbol} ({minute}m):", response.status_code)
                return response.json()
        except Exception as e:
            #logger.error(f"Error occurred during initial GET for {symbol} ({minute}m):", e)
            self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_PERIODIC_FETCH_EXCEPTION.value}, Detail: Error occurred during initial GET for {symbol} ({minute}m):", e)
    
    def receive_kbar_data(self, url, minute, symbol):
        try:
            response = requests.get(url, headers=self.__headers, verify=self._use_ssl_verify)
            
            if response.status_code == 200:
                data = response.json()
                if data:
                    last_record = data[-1]
                    key = (minute, symbol)
                    is_updated = False

                    with self.lock:
                        if key not in self.kbar_record or last_record != self.kbar_record[key]:
                            self.kbar_record[key] = last_record
                            is_updated = True

                        if key not in self.subscriptions:
                            sub_id = QuoteData.QuoteType.qtKBar.value + QuoteData.QuoteVersion.v0.value + '.' + str(minute) + '.' + self.market_type.value + '.' + symbol 
                            self.subscriptions[key] = sub_id
                            #self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.SUCCESSFUL_OPERATION.value}, Detail: Successfully subscribed to {symbol} ({minute}m).")
                            self._BasicQuote._log_and_trigger_event_with_locate(
                                LoggingType.INFO,
                                QuoteEventCode.SUBSCRIBE_OK_KBAR,
                                QuotationCode.SUCCESSFUL_OPERATION,
                                {
                                    "market": self.market_type.value,
                                    "minute": str(minute),
                                    "symbol": symbol,
                                },
                                symbol=symbol,
                                minute=str(minute),
                                )
                            
                    if is_updated:
                        for key_type, handler_name in self.__OWL5_KBAR_SUBSCRIPTION_HANDLERS.items():
                            if key_type == QuoteData.QuoteType.qtKBar.value:
                                handler = getattr(self, handler_name, None)
                                if handler:
                                    handler(symbol, minute, last_record)
                                break
                else:
                    #self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_EMPTY_DATA.value}, Detail: No data received for {symbol} ({minute}m).")
                    self._BasicQuote._log_and_trigger_event_with_locate(
                        LoggingType.WARNING,
                        QuoteEventCode.SUBSCRIBE_FAIL,
                        QuotationCode.QUOTE_STATICDATA_EMPTY_DATA,
                        {
                            "market": self.market_type.value,
                            "minute": str(minute),
                            "symbol": symbol,
                        },
                        symbol=symbol,
                        minute=str(minute)
                        )
                    self._unsubscribe_kbar(self.market_type, symbol, minute)
            else:
                #self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_HTTP_STATUS_NOT_200.value}, Detail: Failed to GET data for {symbol} ({minute}m): {response.status_code}")
                self._BasicQuote._log_and_trigger_event_with_locate(
                    LoggingType.WARNING,
                    QuoteEventCode.SUBSCRIBE_FAIL,
                    QuotationCode.QUOTE_STATICDATA_HTTP_STATUS_NOT_200,
                    {
                        "market": self.market_type.value,
                        "minute": str(minute),
                        "symbol": symbol,
                        "http_status_code": response.status_code,
                    },
                    symbol=symbol,
                    minute=str(minute),
                    http_status_code=response.status_code,
                    )
                if response.status_code == 403:
                    self._unsubscribe_kbar(self.market_type, symbol, minute)

        except Exception as e:
            #self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_PRODUCT_FILE_PARSE_ERROR.value}, Detail: Error occurred during GET for {symbol} ({minute}m): {e}")
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.WARNING,
                QuoteEventCode.SUBSCRIBE_FAIL,
                QuotationCode.QUOTE_STATICDATA_PRODUCT_FILE_PARSE_ERROR,
                {
                    "market": self.market_type.value,
                    "minute": str(minute),
                    "symbol": symbol,
                },
                symbol=symbol,
                minute=str(minute),
                error=str(e)
                )

    def _subscribe_kbar(self, market_type: QuoteData.MarketType, symbol: str, minute: int = None, version: QuoteData.QuoteVersion = QuoteData.QuoteVersion.v0):
        if minute not in self.__OWL5_KBAR_SUPPORTED_MINUTES_LIST:
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.WARNING, 
                QuoteEventCode.SUBSCRIBE_FAIL,
                QuotationCode.QUOTE_STATICDATA_KBAR_MINUTE_UNSUPPORTED,
                {
                    "market": market_type.value,
                    "minute": str(minute),
                    "symbol": symbol,
                },
                symbol=symbol,
                minute=str(minute),
                allowed_minutes=self.__OWL5_KBAR_SUPPORTED_MINUTES_LIST,
                )

            return
        
        should_start_timer = False
        with self.lock:
            key = (minute, symbol)
            if key in self.subscriptions:
                #self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_KBAR_DUPLICATE_SUBSCRIPTION.value}, Detail: Already subscribed to symbol[{symbol}] minute[({minute})].")
                self._BasicQuote._log_and_trigger_event_with_locate(
                    LoggingType.WARNING, 
                    QuoteEventCode.SUBSCRIBE_FAIL,
                    QuotationCode.QUOTE_STATICDATA_KBAR_DUPLICATE_SUBSCRIPTION,
                    {
                        "market": market_type.value,
                        "minute": str(minute),
                        "symbol": symbol,
                    },
                    minute=str(minute),
                    symbol=symbol,
                    )
                return
            
            sub_id = QuoteData.QuoteType.qtKBar.value + version.value + '.' + str(minute) + '.' + market_type.value + '.' + symbol 
            self.subscriptions[key] = sub_id 
            if len(self.subscriptions) == 1:
                should_start_timer = True

        if market_type == QuoteData.MarketType.USStock:
            url = f"{self._base_url}{self._kbar_topic}/{symbol}"
        else:    
            if minute is None:
                url = f"{self._base_url}{self._kbar_topic}/{symbol}"
            else:
                url = f"{self._base_url}{self._kbar_topic}{minute}/{symbol}"
        
        self.receive_kbar_data(url, minute, symbol)

        if should_start_timer:
            self.start()
    
    def _unsubscribe_kbar(self, market_type: QuoteData.MarketType, symbol: str, minute: int = None):
        if minute not in self.__OWL5_KBAR_SUPPORTED_MINUTES_LIST:
            # self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID.value}, Detail: Unsubscribe KBar failed. Unsupported minute value: {minute}. Allowed values are: {self.__OWL5_KBAR_SUPPORTED_MINUTES_LIST}")
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.WARNING, 
                QuoteEventCode.UNSUBSCRIBE_FAIL,
                QuotationCode.QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID,
                {
                    "market": market_type.value,
                    "minute": str(minute),
                    "symbol": symbol,
                },
                log_key="UNSUBSCRIBE_FAIL_KBAR.QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID",
                reason=f"Unsupported minute value: {minute}. Allowed values are: {self.__OWL5_KBAR_SUPPORTED_MINUTES_LIST}",)
            return
        
        should_stop_timer = False
        with self.lock:
            key = (minute, symbol)
            if key not in self.subscriptions:
                #self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_KBAR_KEY_NOT_FOUND.value}, Detail: Not subscribed to symbol[{symbol}] minute[({minute})].")
                self._BasicQuote._log_and_trigger_event_with_locate(
                    LoggingType.ERROR,
                    QuoteEventCode.UNSUBSCRIBE_FAIL,
                    QuotationCode.QUOTE_STATICDATA_KBAR_KEY_NOT_FOUND,
                    {
                        "market": market_type.value,
                        "minute": str(minute),
                        "symbol": symbol,
                    },
                    symbol=symbol,
                    minute=str(minute),
                    )
                return

            del self.subscriptions[key]
            if key in self.kbar_record:
                del self.kbar_record[key]
            
            if not self.subscriptions:
                should_stop_timer = True
            #self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.SUCCESSFUL_OPERATION.value}, Detail: Successfully unsubscribed from {symbol} ({minute})")
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.INFO,
                QuoteEventCode.UNSUBSCRIBE_OK_KBAR,
                QuotationCode.SUCCESSFUL_OPERATION,
                {
                    "market": market_type.value,
                    "minute": str(minute),
                    "symbol": symbol,
                },
                symbol=symbol,
                minute=str(minute),
                )

        if should_stop_timer:
            self.stop_timer()

    def _unsubscribe_kbar_by_id(self, sub_id: str):
        parts = sub_id.split('.')
        if len(parts) != 4:
            #self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID.value}, Detail: Invalid format({sub_id}). The input must contain exactly 4 parts")
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.ERROR,
                QuoteEventCode.UNSUBSCRIBE_FAIL,
                QuotationCode.QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID,
                {
                    "market": self.market_type.value,
                    "sub_id": sub_id,
                },
                log_key=QuotationCode.QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID.name,
                sub_id=sub_id,
                )
            raise ValueError("Invalid format: The input must contain exactly 4 parts.")
        
        minute = int(parts[1])
        symbol = parts[3]
        key = (minute, symbol)
        should_stop_timer = False
        with self.lock:
            if key in self.subscriptions and self.subscriptions[key] == sub_id:
                del self.subscriptions[key]
                # self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.SUCCESSFUL_OPERATION.value}, Detail: Successfully unsubscribed from {sub_id}")
                self._BasicQuote._log_and_trigger_event_with_locate(
                        LoggingType.INFO,
                        QuoteEventCode.UNSUBSCRIBE_OK,
                        QuotationCode.SUCCESSFUL_OPERATION,
                        {
                            "market": self.market_type.value,
                            "sub_id": sub_id,
                        },
                        sub_id=sub_id,)
                
            else:
                #self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID.value}, Detail: No such subscription{sub_id}")
                self._BasicQuote._log_and_trigger_event_with_locate(
                    LoggingType.ERROR,
                    QuoteEventCode.UNSUBSCRIBE_FAIL,
                    QuotationCode.QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID,
                    {
                        "market": self.market_type.value,
                        "sub_id": sub_id,
                    },
                    log_key="UNSUBSCRIBE_FAIL_KBAR.QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID",
                    reason=f"No such subscription: {sub_id}",)
                raise QuotationError("No Such Subscription", QuotationCode.INVALID_SUB_ID)
            
            if not self.subscriptions:
                should_stop_timer = True

        if should_stop_timer:
            self.stop_timer()
            
    def _unsubscribeAll(self):
        with self.lock:
            if not self.subscriptions:
                # logger.info("No active subscriptions to unsubscribe.")
                return

            unsubscribed_values = list(self.subscriptions.values())

            self.subscriptions.clear()
            self.kbar_record.clear()

            for sub_id in unsubscribed_values:
                #self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.SUCCESSFUL_OPERATION.value}, Detail: Successfully Unsubscribed From {sub_id} ")            
                self._BasicQuote._log_and_trigger_event_with_locate(
                    LoggingType.INFO,
                    QuoteEventCode.UNSUBSCRIBE_OK,
                    QuotationCode.SUCCESSFUL_OPERATION,
                    {
                        "market": self.market_type.value,
                        "sub_id": sub_id,
                    },
                    sub_id=sub_id,)
        
        self._log_message(LoggingType.INFO, "All subscriptions have been successfully unsubscribed.")
        self.stop_timer()
        

    def fetch_kbar_data_periodically(self):
        with self.lock:
            if not self.subscriptions:
                return
            subscriptions = list(self.subscriptions.keys())

        for minute, symbol in subscriptions:
            # #US Stock Kbar currently supports only 1-minute intervals.
            if self.market_type == QuoteData.MarketType.USStock:
                url = f"{self._base_url}{self._kbar_topic}/{symbol}"
            else:    
                if minute is None:
                    url = f"{self._base_url}{self._kbar_topic}/{symbol}"
                else:
                    url = f"{self._base_url}{self._kbar_topic}{minute}/{symbol}"
            try:
                self.receive_kbar_data(url, minute, symbol)
            except Exception as e:
                #self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_STATICDATA_PERIODIC_FETCH_EXCEPTION.value}, Detail: Error occurred for {symbol} ({minute}m): {e}")
                self._BasicQuote._log_and_trigger_event_with_locate(
                    LoggingType.ERROR,
                    QuoteEventCode.DATA_ERROR,
                    QuotationCode.QUOTE_STATICDATA_PERIODIC_FETCH_EXCEPTION,
                    {
                        "market": self.market_type.value,
                        "minute": str(minute),
                        "symbol": symbol,
                    },
                    symbol=symbol,
                    minute=str(minute),
                    error=str(e),
                    )
                
                
        #time.sleep(self.interval)

    def stop(self):
        self._unsubscribeAll()
    
    def stop_timer(self):
        with self.timer_lock:
            if self.timer and self.timer.is_alive():
                self.timer.cancel()
                self.is_running = False
                self._log_message(LoggingType.INFO, f"{self.market_type.value} StaticDataClient has been stopped.")

    def start(self):
        with self.timer_lock:
            if not self.is_running:
                self.is_running = True
                self._schedule_next_run()

    def _schedule_next_run(self):
        if not self.is_running:
            return

        self.timer = threading.Timer(self.interval, self._periodic_task_wrapper)
        self.timer.start()

    def _periodic_task_wrapper(self):
        try:
            self.fetch_kbar_data_periodically()
        finally:
            self._schedule_next_run()

    def _log_and_trigger_event_with_locate(self, log_level: LoggingType, event_code: QuoteEventCode, respond_code: QuotationCode,
                event_info: dict, log_file_only: bool = False, log_key:str = "", **kwargs ) -> None:
        self._BasicQuote._log_and_trigger_event_with_locate(log_level, event_code, respond_code, event_info, log_file_only, log_key, **kwargs)
#----------------------------------------------------------------------------
class MarketDataClient(LoggingBase):
    __wsclient: WSClient
    _BasicQuote: BasicQuote
    #auth: AutoRefresh
    def __init__(self, name: str, url:str, auth, api_id:str, use_async_handler: bool = True, max_queue_size: int = DEFAULT_QUEUE_SIZE, auto_reconnect_when_disconnected: bool = True, show_api_id_in_log: bool = False):
        super().__init__(api_id=api_id, show_api_id_in_log=show_api_id_in_log)
        self._name = name
        self._use_async_handler = use_async_handler
        self._data_queue = FastDropQueue(max_queue_size)
        self._auto_reconnect_when_disconnected = auto_reconnect_when_disconnected
        self._worker = None
        # self.__token = token
        self.__auth = auth
        self.__wsclient = WSClient(url=url, api_id=api_id, show_api_id_in_log=show_api_id_in_log)
        self.__wsclient.token = self.__auth.token
        self.__wsclient.set_connect_callback(self.__on_ws_connect)
        self.__wsclient.set_error_callback(self.__on_ws_error)
        self.__wsclient.set_STOMP_error_callback(self.__on_STOMP_error)
        self.__wsclient.set_close_callback(self.__on_ws_close)
        self.__OWL5_SUBSCRIPTION_HANDLERS = {}
        self._sub_id_to_destination = {}
        self._BasicQuote = None
        self._has_init_work = False
        self._reconnecting = False
        self._reconnect_lock = threading.Lock()
        self._manual_disconnect = False
        self._manual_disconnect_lock = threading.Lock()
        self._subscription_lock = threading.RLock()
        self._no_permission_subscriptions_flag = False

    def _initialize_worker_if_needed(self):
        if self._use_async_handler:
            if self._worker is None:
                self._worker = HighSpeedQueueWorker(self._data_queue, self.__call_handler_directly)
            self._worker.start()
            # logger.info(f"Using asynchronous handler for MarketDataClient. Data will be processed in a separate thread.")
        else:
            if self._has_init_work is False:
                self._has_init_work = True
                self._log_message(LoggingType.INFO, "Using synchronous handler for MarketDataClient. This may lead to performance issues if the data rate is high.")

    def set_subscription_handlers(self, handlers: dict):
        self.__OWL5_SUBSCRIPTION_HANDLERS = handlers

    def connect(self)-> bool: #websocket
        return self.__ensure_ws_connected(auto_reconnect=self._auto_reconnect_when_disconnected)

    def __ensure_ws_connected(self, timeout: float = 3.0, auto_reconnect: bool = False)-> bool:
        with self._manual_disconnect_lock:
            if auto_reconnect == True and self._manual_disconnect == True:
                return False
            
            if auto_reconnect == False:
                self._manual_disconnect = False

        if self.__wsclient.connected:
            return True
        
        try:
            self.__wsclient.stompconnect(self.__auth.token)
            #self.__wsclient.connect()

            start_time = time.time()
            while not self.__wsclient.connected:
                if time.time() - start_time > timeout:
                    #raise TimeoutError("WebSocket connection timed out.")
                    self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_WS_TIMEOUT.value}, Detail: WebSocket connection timed out.")
                    return False 
                
                time.sleep(0.1)

        except Exception as e:
            #raise ConnectionError(f"Failed to connect websocket: {e}") from e
            self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_WS_TIMEOUT.value}, Detail: Failed to connect websocket: {e}") #TODO
        
        return True
        
    def _subscribe_websocket(self, destination:str, sub_id: str, auto_subscribe: bool= False) -> bool:
        if has_permission(destination) is False:
            #logger.error(f"Permission denied for weboscket destination: {destination}")
            self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_WS_SUBSCRIPTION_NO_PERMISSION.value}, Detail: Permission denied for weboscket destination: {destination}")
            return False
        
        with self._subscription_lock:
            if self._no_permission_subscriptions_flag is True:
                self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_WS_SUBSCRIPTION_REJECTED.value}, Detail: Subscription ID {sub_id} rejected due to previous permission errors.")
                return False
            if sub_id in self.__wsclient.subscriptions:
                #self._log_message(LoggingType.WARNING, f"Subscription ID {sub_id} already exists. Skipping duplicate subscription.")
                self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_WS_SUBSCRIPTION_DUPLICATE_ID.value}, Detail: Subscription ID {sub_id} already exists. Skipping duplicate subscription.")
                self._BasicQuote._log_and_trigger_event_with_locate(
                    LoggingType.WARNING,
                    QuoteEventCode.SUBSCRIBE_FAIL,
                    QuotationCode.QUOTE_WS_SUBSCRIPTION_DUPLICATE_ID,
                    {
                        "market": self._BasicQuote._market.value,
                        "sub_id": sub_id,
                    },
                    sub_id=sub_id,)
                return False
        
        try:            
            if self.__ensure_ws_connected(auto_reconnect=auto_subscribe) == False:
                return False
            
            self._initialize_worker_if_needed()

            def wrapped_callback(frame):
                try:
                    subscription = frame.headers.get('subscription')
                    if 'content' in frame.body:
                        contents = json.loads(frame.body)
                        self.__process_subscription_contents(subscription, contents)
                    else:
                        self.__process_subscription_contents_nocontents(subscription, frame.body)
                    #------------------
                except json.JSONDecodeError as e:
                    #logger.error(f"JSON decoding failed: {e}")
                    self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_WS_DATA_JSON_DECODE_ERROR.value}, Detail: Permission denied for weboscket destination: {destination}")
                except Exception as e:
                    #raise(QuotationError(f'An error occurred during callback from websocket: {e}',code=QuotationCode.WEB_SOCKET_ERROR))
                    #logger.error(f"StatusCode: {QuotationCode.QUOTE_CALLBACK_EXCEPTION.value}, Detail: An error occurred during callback from websocket: {e}",code=QuotationCode.WEB_SOCKET_ERROR)
                    self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_EXCEPTION.value}, "f"Detail: Exception during websocket callback for {destination}: {e}")
                
            self.__wsclient.subscribe(
                destination=destination,
                callback=wrapped_callback,
                headers={"id": sub_id},
            )

            
            self._sub_id_to_destination[sub_id] = destination
                
        except QuotationError as e:
            self._log_message(LoggingType.ERROR, e)

        return True

    def __process_subscription_contents(self, subscription, contents):
        if isinstance(contents, list):
            for content_obj in contents:
                content = content_obj.get('content')
                parsed_content = json.loads(content)
                self.__enqueue_or_handle(subscription, parsed_content)
        else:   
            if 'content' in contents:
                content = contents['content']
                parsed_content = json.loads(content)
                self.__enqueue_or_handle(subscription, parsed_content)
            else:
                self._log_message(LoggingType.ERROR, "No valid contents found in frame body.")

    def __process_subscription_contents_nocontents(self, subscription, body):
        if isinstance(body, list):
            parsed_content = json.loads(body)    
            self.__enqueue_or_handle(subscription, parsed_content)
        else:
            parsed_content = json.loads(body)
            self.__enqueue_or_handle(subscription, parsed_content)

    def __enqueue_or_handle(self, subscription, parsed_content):
        timestamp = time.perf_counter()
        if self._use_async_handler:
            self._data_queue.put((subscription, parsed_content, timestamp))
        else:
            self.__call_handler_directly(subscription, parsed_content, timestamp)

    def __call_handler_directly(self, subscription, parsed_content, timestamp):
        for key, handler_name in self.__OWL5_SUBSCRIPTION_HANDLERS.items():
            if key in subscription:
                parts = subscription.split('.')
                sub_quote = parts[0]
                sub_exchange = parts[1]
                sub_symbol = parts[2]
                handler = getattr(self, handler_name, None)
                
                if handler:
                    handler(sub_quote, sub_exchange, sub_symbol, parsed_content, timestamp)
                break
        else:
            self._log_message(LoggingType.WARNING, "Subscription type not recognized.")

    def _unsubscribeAll(self):
        sub_id_list = self.get_subscriptions()
        for sub_id in sub_id_list:
            self._unsubscribe_by_id(sub_id)

    def _unsubscribe_by_id(self, sub_id)-> bool:
        if self.__wsclient is None:
            #self._log_message(LoggingType.WARNING, "Unsubscribe request ignored: WebSocket client is not initialized.")
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.WARNING,
                QuoteEventCode.UNSUBSCRIBE_FAIL,
                QuotationCode.QUOTE_WS_SUBSCRIPTION_REJECTED,
                {
                    "market": self._BasicQuote._market.value,
                    "sub_id": sub_id,
                },
                reason="WebSocket client is not initialized.",)
            return False
        
        flag = False
        remaining = 0
        
        with self._subscription_lock:
            if self._no_permission_subscriptions_flag is True:
                #self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_WS_SUBSCRIPTION_REJECTED.value}", f"Detail: ") #TODO
                self._BasicQuote._log_and_trigger_event_with_locate(
                    LoggingType.WARNING,
                    QuoteEventCode.UNSUBSCRIBE_FAIL,
                    QuotationCode.QUOTE_WS_SUBSCRIPTION_REJECTED,
                    {
                        "market": self._BasicQuote._market.value,
                        "sub_id": sub_id,
                    },
                    reason=f"Unsubscribe request for subscription ID {sub_id} rejected due to previous permission errors.",)
                return False

            if sub_id in self.__wsclient.subscriptions:
                self.__wsclient.unsubscribe(sub_id)
                flag = True

            if sub_id in self._sub_id_to_destination:
                del self._sub_id_to_destination[sub_id]

            self._BasicQuote._remove_record(sub_id)
            

        #self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.SUCCESSFUL_OPERATION.value}, "f"Detail: Successfully unsubscribed from {sub_id}")
        if flag is True:
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.INFO,
                QuoteEventCode.UNSUBSCRIBE_OK,
                QuotationCode.SUCCESSFUL_OPERATION,
                {
                    "market": self._BasicQuote._market.value,
                    "sub_id": sub_id,
                },
                sub_id=sub_id,)
        else:
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.WARNING,
                QuoteEventCode.UNSUBSCRIBE_FAIL,
                QuotationCode.QUOTE_WS_SUBSCRIPTION_INVALID_SUB_ID,
                {
                    "market": self._BasicQuote._market.value,
                    "sub_id": sub_id,
                },
                sub_id=sub_id)
            
        remaining_sub_ids = list(self.__wsclient.subscriptions.keys())
        remaining = len(remaining_sub_ids)
        if remaining == 0 or (remaining == 1 and remaining_sub_ids[0].startswith("error-")):
            self._log_message(LoggingType.INFO, f"[{self._name}] No active subscriptions remaining. WebSocket client will be disconnected.")
            with self._subscription_lock:
                self._sub_id_to_destination.clear()
                self.__wsclient.subscriptions.clear()
            # self._BasicQuote._log_and_trigger_event(
            #     LoggingType.INFO,
            #     f"[{self._name}] No active subscriptions remaining after unsubscribing from {sub_id}. WebSocket client will be disconnected.",
            #     QuoteEventCode.UNSUBSCRIBE_OK,
            #     QuotationCode.SUCCESSFUL_OPERATION,
            #     {
            #         "market": self._BasicQuote._market.value,
            #         "sub_id": sub_id,
            #     })
            with self._manual_disconnect_lock:
                # If there are no active subscriptions, consider this a manual disconnect to prevent auto-reconnect logic from kicking in.
                self._manual_disconnect = True

            if self.__wsclient.connected:
                self.disconnect()

        return True            

    def __on_ws_connect(self, ws_client, frame):
        self.__subscribe_error_topic()
        self._BasicQuote._clear_records()
        #self._log_message(LoggingType.INFO, f"[{self._name}] WebSocket connection established.")
        self._BasicQuote._log_and_trigger_event_with_locate(
            LoggingType.INFO,
            QuoteEventCode.CONNECTED,
            QuotationCode.SUCCESSFUL_OPERATION,
            {
                "quote_name": self._name,
                "market": self._BasicQuote._market.value,
            },
            quote_name=self._name,
            )
            
    def __subscribe_error_topic(self):
        destination = "/user/queue/errors"
        sub_id = f"error-{self._name}"
        
        def wrapped_error_callback(frame):
            try:
                self.__on_owls_error(frame=frame, destination=destination)
            except Exception as e:
                # self._log_message(
                #     LoggingType.ERROR,
                #     f"StatusCode: {QuotationCode.QUOTE_CALLBACK_EXCEPTION.value}, "
                #     f"Detail: Exception during websocket callback for {destination}: {e}"
                # )
                self._BasicQuote._log_and_trigger_event_with_locate(
                    LoggingType.ERROR,
                    QuoteEventCode.DATA_ERROR,
                    QuotationCode.QUOTE_CALLBACK_EXCEPTION,
                    {
                        "quote_name": self._name,
                        "market": self._BasicQuote._market.value,
                        "destination": destination,
                        "error": str(e),
                    },
                    destination=destination,
                    error=str(e),)
                
        self.__wsclient.subscribe(
            destination=destination,
            callback=wrapped_error_callback,
            headers={"id": sub_id},
        )

    def __on_owls_error(self, frame, destination: str):
        subscription, code, msg, subscription_id, raw = self.__parse_owl_error_frame(frame)

        if code is None:
            # self._log_message(
            #     LoggingType.ERROR,
            #     f"StatusCode: {QuotationCode.QUOTE_WS_DATA_JSON_DECODE_ERROR.value}, "
            #     f"Detail: Invalid error payload; destination={destination}; subscription={subscription}; subscription_id={subscription_id}; body={raw!r}"
            # )
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.ERROR,
                QuoteEventCode.DATA_ERROR,
                QuotationCode.QUOTE_WS_DATA_JSON_DECODE_ERROR,
                {
                    "quote_name": self._name,
                    "market": self._BasicQuote._market.value,
                    "destination": destination,
                    "subscription": subscription,
                    "subscription_id": subscription_id,
                    "raw_body": raw,
                },
                destination=destination,)
            return
        
        self._log_message(
            LoggingType.WARNING,
            f"on_owls_error: subscription[{subscription}], Code[{code}], Message[{msg}], subscription_id[{subscription_id}]",
            True
        )
        event_error_code = QuotationCode.QUOTE_SERVER_UNKNOWN_ERROR
        if code == "01":
            event_error_code = QuotationCode.QUOTE_SERVER_TOO_MANY_CONNECTIONS
            with self._subscription_lock:
                self._no_permission_subscriptions_flag = True
                #self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.QUOTE_SERVER_TOO_MANY_CONNECTIONS.value}, Detail: Too many connections error received. WebSocket will be closed later.")
                self._BasicQuote._log_and_trigger_event_with_locate(
                    LoggingType.INFO,
                    QuoteEventCode.SUBSCRIBE_FAIL,
                    QuotationCode.QUOTE_SERVER_TOO_MANY_CONNECTIONS,
                    {
                        "quote_name": self._name,
                        "market": self._BasicQuote._market.value,
                        "code": code,
                        "msg": msg,
                    })

        elif code == "02":
            event_error_code = QuotationCode.QUOTE_SERVER_TOO_MANY_SUBSCRIPTIONS
            self._no_permission_subscriptions_flag = True
            with self._subscription_lock:
                self._no_permission_subscriptions_flag = True
                if subscription_id in self.__wsclient.subscriptions:
                    del self.__wsclient.subscriptions[subscription_id]
                if subscription_id in self._sub_id_to_destination:
                    del self._sub_id_to_destination[subscription_id]
                    #self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.QUOTE_SERVER_TOO_MANY_SUBSCRIPTIONS.value}, Detail: Subscription ID [{subscription_id}] removed due to too many subscriptions error.")
                    self._BasicQuote._log_and_trigger_event_with_locate(
                        LoggingType.INFO,
                        QuoteEventCode.SUBSCRIBE_FAIL,
                        QuotationCode.QUOTE_SERVER_TOO_MANY_SUBSCRIPTIONS,
                        {
                            "quote_name": self._name,
                            "market": self._BasicQuote._market.value,
                            "code": code,
                            "msg": msg,
                            "subscription_id": subscription_id,
                        },
                        subscription_id=subscription_id)
        elif code == "03":
            event_error_code = QuotationCode.QUOTE_SERVER_NO_PERMISSION_SUBSCRIPTION
            with self._subscription_lock:
                self._no_permission_subscriptions_flag = True
                if subscription_id in self.__wsclient.subscriptions:
                    del self.__wsclient.subscriptions[subscription_id]
                if subscription_id in self._sub_id_to_destination:
                    del self._sub_id_to_destination[subscription_id]
                    #self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.QUOTE_SERVER_NO_PERMISSION_SUBSCRIPTION.value}, Detail: Subscription ID [{subscription_id}] removed due to no permission error.")
                    self._BasicQuote._log_and_trigger_event_with_locate(
                        LoggingType.INFO,
                        QuoteEventCode.SUBSCRIBE_FAIL,
                        QuotationCode.QUOTE_SERVER_NO_PERMISSION_SUBSCRIPTION,
                        {
                            "quote_name": self._name,
                            "market": self._BasicQuote._market.value,
                            "code": code,
                            "msg": msg,
                            "sub_id": subscription_id,
                        },
                        subscription_id=subscription_id,)
        elif code == "04":
            event_error_code = QuotationCode.QUOTE_SERVER_INVALID_SYMBOL_SUBSCRIPTION
            # TODO invalid symbol
            #warning only, do not close websocket, as this can be caused by user input error and may not require reconnecting
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.WARNING,
                QuoteEventCode.SUBSCRIBE_FAIL,
                QuotationCode.QUOTE_SERVER_INVALID_SYMBOL_SUBSCRIPTION,
                {
                    "quote_name": self._name,
                    "market": self._BasicQuote._market.value,
                    "code": code,
                    "msg": msg,
                    "sub_id": subscription_id,
                },
                subscription_id=subscription_id)
        elif code == "05":
            event_error_code = QuotationCode.QUOTE_SERVER_INVALID_TOKEN
            with self._subscription_lock:
                self._no_permission_subscriptions_flag = True
                if subscription_id in self.__wsclient.subscriptions:
                    del self.__wsclient.subscriptions[subscription_id]
                if subscription_id in self._sub_id_to_destination:
                    del self._sub_id_to_destination[subscription_id]
                    #self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.QUOTE_SERVER_INVALID_TOKEN.value}, Detail: Subscription ID [{subscription_id}] removed due to invalid token error.")
                    self._BasicQuote._log_and_trigger_event_with_locate(
                        LoggingType.INFO,
                        QuoteEventCode.SUBSCRIBE_FAIL,
                        QuotationCode.QUOTE_SERVER_INVALID_TOKEN,
                        {
                            "quote_name": self._name,
                            "market": self._BasicQuote._market.value,
                            "code": code,
                            "msg": msg,
                            "sub_id": subscription_id,
                        },
                        subscription_id=subscription_id)
                        
        elif code == "06":
            event_error_code = QuotationCode.QUOTE_SERVER_SUBSCRIPTION_ID_DUPLICATED
            #self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_SERVER_SUBSCRIPTION_ID_DUPLICATED.value}, Detail: Subscription ID [{subscription_id}] is duplicated.")
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.WARNING,
                QuoteEventCode.SUBSCRIBE_FAIL,
                QuotationCode.QUOTE_SERVER_SUBSCRIPTION_ID_DUPLICATED,
                {
                    "quote_name": self._name,
                    "market": self._BasicQuote._market.value,
                    "code": code,
                    "msg": msg,
                    "sub_id": subscription_id,
                },
                subscription_id=subscription_id)
        else:
            #self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.QUOTE_SERVER_UNKNOWN_ERROR.value}, Detail: Unknown error code received: {code}")
            self._BasicQuote._log_and_trigger_event_with_locate(
                LoggingType.INFO,
                QuoteEventCode.SUBSCRIBE_FAIL,
                QuotationCode.QUOTE_SERVER_UNKNOWN_ERROR,
                {
                    "quote_name": self._name,
                    "market": self._BasicQuote._market.value,
                    "code": code,
                    "msg": msg,
                    "subscription_id": subscription_id,
                },
                code=event_error_code,
                msg=msg)
        # self._log_message(
        #     LoggingType.ERROR,
        #     f"Error destination={destination}, subscription={subscription}, Code={code}, Message={msg}, subscription_id={subscription_id}"
        # )

    def __on_ws_error(self, ws_client, error):
        self._log_message(LoggingType.ERROR, f"WebSocket error occurred: {error}")
        self._BasicQuote._log_and_trigger_event_with_locate(
            LoggingType.ERROR,
            QuoteEventCode.WS_ERROR,
            QuotationCode.QUOTE_WS_ERROR,
            {
                "quote_name": self._name,
                "market": self._BasicQuote._market.value,
                "error": str(error),
            },
            error=str(error))
        # Depending on the nature of the error, you might want to trigger a reconnect here.
        # For example, if the error indicates a lost connection, you could call self.__on_ws_close() to handle it.
        # self.__on_ws_close()

    def __on_STOMP_error(self, ws_client, frame):
        self._log_message(LoggingType.ERROR, f"STOMP error occurred: {frame}")
        # Depending on the nature of the error, you might want to trigger a reconnect here.
        # For example, if the error indicates a lost connection, you could call self.__on_ws_close() to handle it.
        # self.__on_ws_close()

    def __parse_owl_error_frame(self, frame):
        headers = getattr(frame, "headers", None) or {}
        subscription = headers.get("subscription", "N/A")

        body = getattr(frame, "body", "")
        if body is None:
            body = ""

        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", errors="replace")

        raw_body = body

        try:
            obj = json.loads(body) if body else None
        except Exception:
            return subscription, None, "", raw_body

        if not isinstance(obj, dict):
            return subscription, None, "", raw_body

        code = obj.get("code")
        msg = obj.get("msg", "")
        subscription_id = obj.get("subscriptionId", "")

        if code is None:
            return subscription, None, msg, raw_body, subscription_id

        return subscription, str(code), str(msg), str(subscription_id), raw_body


    def __on_ws_close(self, *args, **kwargs):
        no_need_reconnect = self._no_permission_subscriptions_flag
        with self._subscription_lock:
            if self._no_permission_subscriptions_flag is True:
                self._sub_id_to_destination.clear()
                self.__wsclient.subscriptions.clear()
            self._no_permission_subscriptions_flag = False

        #self._log_message(LoggingType.WARNING, f"[{self._name}] WebSocket connection closed.")
        self._BasicQuote._log_and_trigger_event_with_locate(
            LoggingType.WARNING,
            QuoteEventCode.DISCONNECTED,
            QuotationCode.QUOTE_WS_DISCONNECTED,
            {
                "quote_name": self._name,
                "market": self._BasicQuote._market.value,
            },
            quote_name=self._name
            )
        
        with self._manual_disconnect_lock:
            manual = self._manual_disconnect
            if manual == True:
                self._log_message(LoggingType.INFO, f"{self._name} Manual disconnect detected. Skipping auto-reconnect.")
                self._reconnecting = False
                return
        
        if self._auto_reconnect_when_disconnected:
            if no_need_reconnect is True:
                self._reconnecting = False
                self._log_message(LoggingType.INFO, f"[{self._name}] No need to reconnect due to subscription permission issues. Please check the error logs for details.")
                return
            with self._reconnect_lock:
                if not self._reconnecting:
                    # self.__wsclient.disconnect()
                    self._reconnecting = True
                    threading.Thread(target = self.reconnect_worker).start()
                else:
                    self._log_message(LoggingType.WARNING, "Reconnect already in progress. Skipping duplicate trigger.")
        else:
            self.disconnect()

    def reconnect_worker(self):
        with self._manual_disconnect_lock:
            if self._manual_disconnect == True:
                self._log_message(LoggingType.INFO, f"[Reconnect:{self._name}] Manual disconnect detected. Skipping auto-reconnect.")
                return
        
        self._log_message(LoggingType.INFO, f"[Reconnect:{self._name}] WebSocket connection closed. Starting auto-reconnect sequence...")
        try:
            for attempt in range(1, MAX_WS_RECONNECT_RETRIES + 1):
                try:
                    self._log_message(LoggingType.INFO, f"[Reconnect:{self._name}] Attempt {attempt}/{MAX_WS_RECONNECT_RETRIES} - Reconnecting WebSocket...")
                    self._BasicQuote._trigger_event_cb(
                        QuoteEventCode.RECONNECTING,
                        QuotationCode.SUCCESSFUL_OPERATION,
                        {
                            "market": self._BasicQuote._market.value,
                            "transport": "websocket",
                            "attempt": attempt
                        },
                        f"{self._BasicQuote._market.value} WebSocket client is reconnecting... (Attempt {attempt}/{MAX_WS_RECONNECT_RETRIES})"
                    )
                    self.__wsclient.reconnect(self.__auth.token)
                    time.sleep(1)
                    if self.__wsclient.connected:
                        self._log_message(LoggingType.INFO, f"[Reconnect:{self._name}] WebSocket reconnected successfully.")
                        self._BasicQuote._trigger_event_cb(
                            QuoteEventCode.RECONNECTED,
                            QuotationCode.SUCCESSFUL_OPERATION,
                            {
                                "market": self._BasicQuote._market.value,
                                "transport": "websocket",
                                "attempt": attempt
                            },
                            f"{self._BasicQuote._market.value} WebSocket client has been reconnected. (Attempt {attempt}/{MAX_WS_RECONNECT_RETRIES})"
                        )

                        sub_index = 0

                        with self._subscription_lock:
                            sub_total = len(self._sub_id_to_destination)
                            items = list(self._sub_id_to_destination.items())

                        for sub_id, destination in items:
                            try:
                                sub_index += 1
                                #logger.info(f"[Reconnect:{self._name}] Resubscribe {sub_index}/{sub_total}: {destination} (sub_id={sub_id})")
                                self._log_message(LoggingType.INFO, f"[Reconnect:{self._name}] Resubscribe {sub_index}/{sub_total}: (sub_id={sub_id})")
                                success = self._subscribe_websocket(destination, sub_id, True)
                                if not success:
                                    self._log_message(LoggingType.WARNING, f"[Reconnect:{self._name}] Resubscription to {destination} failed.")
                            except Exception as e:
                                self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_WS_RECONNECT_RESUBSCRIBE_EXCEPTION.value}, Detail:[Reconnect:{self._name}] Exception during resubscription to {destination} (sub_id={sub_id}): {e}")
                        break
                except Exception as e:
                    self._log_message(LoggingType.ERROR, f"StatusCode: {QuotationCode.QUOTE_WS_RECONNECT_ATTEMPT_FAILED.value}, Detail: [Reconnect:{self._name}] Reconnection attempt {attempt} failed with error: {e}")
                time.sleep(WS_RECONNECT_DELAY_BASE_SEC)
            else:
                self._log_message(LoggingType.CRITICAL, f"StatusCode: {QuotationCode.QUOTE_WS_RECONNECT_MAX_REACHED.value}, Detail:[Reconnect:{self._name}] Max reconnection attempts ({MAX_WS_RECONNECT_RETRIES}) reached. Giving up reconnection.")
        finally:
            with self._reconnect_lock:
                self._reconnecting = False

    def get_subscriptions(self)-> List[str]:
        if self.__wsclient is None:
            return []
        with self._subscription_lock:
            # return list(self.__wsclient.subscriptions.keys())
            # Filter out error topic subscriptions
            return [
                k for k in self.__wsclient.subscriptions.keys()
                if not k.startswith("error-")
            ]
        
    
    def get_subscriptions_count(self) -> int:
        if self.__wsclient is None:
            return 0
        
        with self._subscription_lock:
            #return len(self.__wsclient.subscriptions)
            return len(self.get_subscriptions())
    
    def disconnect(self):
        with self._manual_disconnect_lock:
            self._manual_disconnect = True

        if self.__wsclient is None: 
            return
        
        self._unsubscribeAll()

        with self._subscription_lock:
            self._sub_id_to_destination.clear()

        if self.__wsclient.connected:
            self.__wsclient.disconnect()
            if self._BasicQuote is not None:
                self._BasicQuote._trigger_event_cb(
                    QuoteEventCode.DISCONNECTED,
                    QuotationCode.SUCCESSFUL_OPERATION,
                    {
                        "market": self._BasicQuote._market.value,
                        "transport": "websocket"
                    },
                    f"{self._BasicQuote._market.value} WebSocket client has been disconnected."
                )
                self._log_message(LoggingType.INFO, f"{self._BasicQuote._market.value} WebSocket client has been disconnected.")
            else:
                self._log_message(LoggingType.INFO, f"WebSocket client has been disconnected.")

        if self._use_async_handler and self._worker is not None:
            self._worker.stop()
            self._data_queue.clear()

    def __del__(self):
        if self._worker:
            self._worker.stop()

    def _log_and_trigger_event_with_locate(self, log_level: LoggingType, event_code: QuoteEventCode, respond_code: QuotationCode,
                event_info: dict, log_file_only: bool = False, log_key:str = "", **kwargs ) -> None:
        self._BasicQuote._log_and_trigger_event_with_locate(log_level, event_code, respond_code, event_info, log_file_only, log_key, **kwargs)
#----------------------------------------------------------------------------
class BasicQuote(LoggingBase):
    _market: QuoteData.MarketType
    _marketdata_client: MarketDataClient
    _static_data_client: StaticDataClient
    __cb_tick_version_map: dict
    __cb_bidask_version_map: dict
    __cb_all_version_map: dict
    __cb_index_version_map: dict
    __cb_kbar_version_map: dict
    
    def __init__(self, api_id: str, show_api_id_in_log: bool = False, locale:str ="en" ):
        super().__init__(api_id=api_id, show_api_id_in_log=show_api_id_in_log)
        # user callback for market data
        self._on_cb_Tick_v0 = self.__on_default_cb
        self._on_cb_Tick_v1 = self.__on_default_cb
        self._on_cb_BidAsk_v0 = self.__on_default_cb
        self._on_cb_BidAsk_v1 = self.__on_default_cb
        self._on_cb_All_v0 = self.__on_default_cb
        self._on_cb_All_v1 = self.__on_default_cb
        self._on_cb_KBar_v0 = self.__on_default_cb
        self._on_cb_KBar_v1 = self.__on_default_cb
        self._on_cb_Index_v0 = self.__on_default_cb
        ### user callback for events
        self._on_cb_Event = self.__on_default_event_cb
        self._event_cb_lock = threading.Lock()
        self._event_queue = queue.Queue()
        threading.Thread(target=self._run_events, daemon=True).start()
        self._locale = LocaleManager(
            locale, locales_dir=os.path.join(os.path.dirname(__file__), "locales")
        )

        self._market = None
        self._marketdata_client= None
        self._static_data_client = None    
        self.tick_v0_records = {}
        self.tick_v1_records = {}
        self.bidask_v0_records = {}
        self.bidask_v1_records = {}
        self.all_v0_records = {}
        self.all_v1_records = {}
        self._record_map = {
            "qtTickv0": self.tick_v0_records,
            "qtTickv1": self.tick_v1_records,
            "qtBidAskv0": self.bidask_v0_records,
            "qtBidAskv1": self.bidask_v1_records,
            "qtAllv0": self.all_v0_records,
            "qtAllv1": self.all_v1_records,
        }

    def _clear_records(self):
        self.tick_v0_records.clear()
        self.tick_v1_records.clear()
        self.bidask_v0_records.clear()
        self.bidask_v1_records.clear()
        self.all_v0_records.clear()
        self.all_v1_records.clear()

    def _remove_record(self, sub_id:str):
        parts = sub_id.split('.')
        if len(parts) < 3:
            return
        
        sub_quote = parts[0]
        sub_exchange = parts[1]
        if "Odd" in sub_id:
            sub_symbol = parts[2] + ".Odd"
        else:
            sub_symbol = parts[2]

        record = self._record_map.get(sub_quote)
        if record is None:
            return

        record.pop(sub_symbol, None)

    def __on_default_cb(self, data):
        if self._show_api_id_in_log == True:
            display(f"[{self._api_id}] {data}")
        else:
            display(data)

    #def __on_default_event_cb(self, api_id:str, event_code:QuoteEventCode, respond_code:QuotationCode, info, event_msg: str):
    def __on_default_event_cb(self, api_id:str, event_code:QuoteEventCode, respond_code:QuotationCode, info:dict, event_msg: str):
        self._log_message(LoggingType.INFO, f"Event received - Event Code: {event_code.value}, Respond Code: {respond_code.value}, Info: {info}, Message: {event_msg}")

    def _run_events(self):
        while True:
            try:
                item = self._event_queue.get()
                if item is None:
                    break
                event_code, respond_code, info, event_msg = item
                with self._event_cb_lock:
                    cb = self._on_cb_Event
                if cb:
                    try:
                        cb(self._api_id, event_code, respond_code, info, event_msg)
                    except Exception as e:
                        self._log_message(LoggingType.ERROR, f"Detail: Exception occurred in event callback: {e}", True)
            except Exception as e:
                self._log_message(LoggingType.ERROR, f"Detail: Unexpected error in event worker: {e}", True)

    def _trigger_event_cb(self, event_code:QuoteEventCode, respond_code:QuotationCode, info:dict, event_msg: str):
        if not isinstance(info, dict):
            info = {}
        self._event_queue.put((event_code, respond_code, info, event_msg))

    def _log_and_trigger_event_with_locate(self, log_level: LoggingType, event_code: QuoteEventCode, respond_code: QuotationCode,
                event_info: dict, log_file_only: bool = False, log_key:str = "", **kwargs ) -> None:
        message = self._locale.build_message(event_code, respond_code, **kwargs)
        self._log_message(log_level, message, log_file_only)
        self._trigger_event_cb(event_code, respond_code, event_info, message)

    # def _log_and_trigger_event(self, log_level: LoggingType, message: str, event_code: QuoteEventCode, 
    #     respond_code: QuotationCode, event_info, log_file_only:bool = False ) -> None:
    #     self._log_message(log_level, message, log_file_only)
    #     self._trigger_event_cb(event_code, respond_code, event_info, message)

    def _trigger_subscribed_event_cb(self, sub_quote: str, symbol:str, version:str):
        event_code = QuoteEventCode.SUBSCRIBE_OK
        respond_code = QuotationCode.SUCCESSFUL_OPERATION
        sub_id = sub_quote + "." + symbol + "." + version
        info = {
            "market": self._market.value,
            "sub_id": sub_id,
        }
        event_msg = f"Subscription successful for {sub_id}."
        self._trigger_event_cb(event_code, respond_code, info, event_msg)

    def __set_and_check_callback_function__(self, cb_name:str, func, expected_type, callback_attr, version: QuoteData.QuoteVersion):
        if not isinstance(func, typing.Callable) or func.__annotations__.get("return") not in (None, type(None)):
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_NAME_INVALID.value}, Detail: The provided function does not match the required signature.")
            raise TypeError("The provided function does not match the required signature.")
        
        if len(func.__annotations__) > 2:
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_PARAM_COUNT_INVALID.value}, Detail: The provided function must only accept self and one additional parameter.")
            raise TypeError("The provided function must only accept self and one additional parameter.")
        
        param_types = list(func.__annotations__.values())
        first_param_type = param_types[1] if len(param_types) > 1 else None

        if first_param_type is not None and first_param_type != expected_type:
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_VERSION_MISMATCH.value}, Detail: The provided function does not match the required signature for version {version.value}.")
            raise TypeError(f"The provided function does not match the required signature for version {version.value}.")
        
        setattr(self, callback_attr, func)
        self._log_message(LoggingType.INFO, f"StatusCode: {QuotationCode.SUCCESSFUL_OPERATION.value}, Detail: Callback for [{self._market.value}] [{cb_name}] version[{version.value}] set successfully.")
    
    def set_cb_tick(
        self,
        func: typing.Callable,
        version: QuoteData.QuoteVersion = QuoteData.QuoteVersion.v0,
        bind: bool = False
    ) -> None:
        
        if self.__cb_tick_version_map is None:
            raise ValueError("Subscription is not supported for Tick.")
        
        if version not in self.__cb_tick_version_map:
            raise ValueError("Unsupported version specified")
        expected_type, callback_attr = self.__cb_tick_version_map[version]
        self.__set_and_check_callback_function__("tick", func, expected_type, callback_attr, version)

    def set_cb_bidask(
        self,
        func: typing.Callable,
        version: QuoteData.QuoteVersion = QuoteData.QuoteVersion.v0,
        bind: bool = False
    ) -> None:
        
        if self.__cb_bidask_version_map is None:
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_SUBSCRIPTION_TYPE_UNSUPPORTED.value}, Detail: Subscription not supported for BidAsk.")
            raise ValueError("Subscription not supported for BidAsk.")
        
        if version not in self.__cb_bidask_version_map:
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_VERSION_UNSUPPORTED.value}, Detail: Unsupported version specified for BidAsk.")
            raise ValueError("Unsupported version specified for BidAsk")
        
        expected_type, callback_attr = self.__cb_bidask_version_map[version]
        
        self.__set_and_check_callback_function__("BidAsk", func, expected_type, callback_attr, version)
    
    def set_cb_all(
        self,
        func: typing.Callable,
        version: QuoteData.QuoteVersion = QuoteData.QuoteVersion.v0,
        bind: bool = False
    ) -> None:
        if self.__cb_all_version_map is None:
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_SUBSCRIPTION_TYPE_UNSUPPORTED.value}, Detail: Subscription not supported for BidAsk.")
            raise ValueError("Subscription not supported for All.")
        
        if version not in self.__cb_all_version_map:
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_VERSION_UNSUPPORTED.value}, Detail: Unsupported version specified for All.")
            raise ValueError("Unsupported version specified for All")
        expected_type, callback_attr = self.__cb_all_version_map[version]
        
        self.__set_and_check_callback_function__("All", func, expected_type, callback_attr, version)

    def set_cb_index(
        self,
        func: typing.Callable,
        version: QuoteData.QuoteVersion = QuoteData.QuoteVersion.v0,
        bind: bool = False
    ) -> None:
        if self.__cb_index_version_map is None:
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_SUBSCRIPTION_TYPE_UNSUPPORTED.value}, Detail: Subscription not supported for Index.")
            raise ValueError("Subscription not supported for Index.")
        
        if version not in self.__cb_index_version_map:
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_VERSION_UNSUPPORTED.value}, Detail: Unsupported version specified for Index.")
            raise ValueError("Unsupported version specified for Index")
        expected_type, callback_attr = self.__cb_index_version_map[version]
        
        self.__set_and_check_callback_function__("Index", func, expected_type, callback_attr, version)

    def set_cb_kbar(
            
        self,
        func: typing.Callable,
        version: QuoteData.QuoteVersion = QuoteData.QuoteVersion.v0,
        bind: bool = False
    ) -> None:
        
        if self.__cb_kbar_version_map is None:
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_SUBSCRIPTION_TYPE_UNSUPPORTED.value}, Detail: Subscription not supported for KBar.")
            raise ValueError("Subscription not supported for KBar.")
    
        if version not in self.__cb_kbar_version_map:
            self._log_message(LoggingType.WARNING, f"StatusCode: {QuotationCode.QUOTE_CALLBACK_VERSION_UNSUPPORTED.value}, Detail: Unsupported version specified for KBar.")
            raise ValueError("Unsupported version specified for KBar")
        expected_type, callback_attr = self.__cb_kbar_version_map[version]
        
        self.__set_and_check_callback_function__("KBar", func, expected_type, callback_attr, version)
    
    # def set_cb_event(
    #     self,
    #     func: typing.Callable,
    # ) -> None:
    #     if not callable(func):
    #         raise QuotationError("Event callback must be callable.", QuotationCode.QUOTE_CALLBACK_NAME_INVALID)
    #     sig = inspect.signature(func)
    #     params = list(sig.parameters.values())
        
    #     if len(params) != 5:
    #         raise QuotationError("Event callback must have 5 parameters: ""(api_id, event_code, respond_code, info, event_msg).", QuotationCode.QUOTE_CALLBACK_PARAM_COUNT_INVALID)

    #     for p in params:
    #         ann = p.annotation
    #         if ann is inspect._empty:
    #             raise QuotationError(f"Event callback parameter '{p.name}' must have type annotation 'str'.", QuotationCode.QUOTE_CALLBACK_VERSION_MISMATCH)
    #         if ann not in (str, "str"):
    #             raise QuotationError(f"Event callback parameter '{p.name}' must be 'str', got '{ann}'.", QuotationCode.QUOTE_CALLBACK_VERSION_MISMATCH)
            
    #     with self._event_cb_lock:
    #         self._on_cb_Event = func

    def set_cb_event(
        self,
        func: typing.Callable,
    ) -> None:
        if not callable(func):
            raise QuotationError(
                "Event callback must be callable.",
                QuotationCode.QUOTE_CALLBACK_NAME_INVALID
            )

        sig = inspect.signature(func)
        params = list(sig.parameters.values())

        if len(params) != 5:
            raise QuotationError(
                "Event callback must have 5 parameters: "
                "(api_id, event_code, respond_code, info, event_msg).",
                QuotationCode.QUOTE_CALLBACK_PARAM_COUNT_INVALID
            )

        expected = [
            ("api_id", str),
            ("event_code", QuoteEventCode),
            ("respond_code", QuotationCode),
            ("info", dict),
            ("event_msg", str),
        ]

        for p, (expected_name, expected_type) in zip(params, expected):
            ann = p.annotation

            if ann is inspect._empty:
                raise QuotationError(
                    f"Event callback parameter '{p.name}' must have type annotation.",
                    QuotationCode.QUOTE_CALLBACK_VERSION_MISMATCH
                )

            if p.name != expected_name:
                raise QuotationError(
                    f"Event callback parameter name must be '{expected_name}', got '{p.name}'.",
                    QuotationCode.QUOTE_CALLBACK_VERSION_MISMATCH
                )

            if expected_type is dict:
                origin = get_origin(ann)
                if ann not in (dict, "dict") and origin is not dict:
                    raise QuotationError(
                        f"Event callback parameter '{p.name}' must be 'dict', got '{ann}'.",
                        QuotationCode.QUOTE_CALLBACK_VERSION_MISMATCH
                    )
            else:
                if ann not in (expected_type, expected_type.__name__):
                    raise QuotationError(
                        f"Event callback parameter '{p.name}' must be '{expected_type.__name__}', got '{ann}'.",
                        QuotationCode.QUOTE_CALLBACK_VERSION_MISMATCH
                    )

        with self._event_cb_lock:
            self._on_cb_Event = func

    def get_subscriptions(self)-> List[str]:
        return self._static_data_client.get_subscriptions() + self._marketdata_client.get_subscriptions()
    
    def unsubscribe_by_id(self, id:str):
        if "qtKBar" in id:
            self._static_data_client._unsubscribe_kbar_by_id(id)
        else:
            self._marketdata_client._unsubscribe_by_id(id)

    # def connect(self):
    #     if self._marketdata_client is not None:
    #         self._marketdata_client.connect()

    def stop(self):
        self._event_queue.put(None)

        if self._marketdata_client is not None:
            self._marketdata_client.disconnect()

        if self._static_data_client is not None:
            self._static_data_client.stop()