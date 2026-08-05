import ctypes
import os
from enum import IntEnum
from typing import Optional

class MsgType(IntEnum):
    Recover                                   = 0
    BasicInfo                                 = 1
    Trade                                     = 2
    TotalTrade                                = 3
    DayHighLow                                = 4
    OpeningInfo                               = 5
    SumOfOrderInfo                            = 6
    News                                      = 7
    UnderlyingIndexInfo                       = 8
    ClosingMarketData                         = 9
    ClosingMarketDataWithSettlementPrice      = 10
    ClosingMarketDataWithSettlementPriceAndOI = 11
    CombinationProductClosingMarketData       = 12
    OrderBookData                             = 13
    QuoteRequest                              = 14
    FullSnapshot                              = 15
    SystemMsg                                 = 16
    PreNClosePx                               = 17
    ResetForOpen                              = 18
    ErrorMsg                                  = 19
    BrokerQueue                               = 20
    SuspensionIndicator                       = 21
    PreClosingPrice                           = 22
    OddOrderBookData                          = 97
    BuyBrokerQueueData                        = 98
    SellBrokerQueueData                       = 99


class SubscribeType(IntEnum):
    Snapshot           = 0
    Subscribe          = 1
    SnapshotWithUpdate = 2

class MarketData(ctypes.Structure):
    _fields_ = [
        ("Symbol",    ctypes.c_char * 32),
        ("MsgTime",   ctypes.c_char * 32),
        ("MatchTime", ctypes.c_char * 16),
        ("TestMatchTime", ctypes.c_char * 16),
        # order book prices
        ("BidPrice1", ctypes.c_double),
        ("BidPrice2", ctypes.c_double),
        ("BidPrice3", ctypes.c_double),
        ("BidPrice4", ctypes.c_double),
        ("BidPrice5", ctypes.c_double),
        ("AskPrice1", ctypes.c_double),
        ("AskPrice2", ctypes.c_double),
        ("AskPrice3", ctypes.c_double),
        ("AskPrice4", ctypes.c_double),
        ("AskPrice5", ctypes.c_double),
        ("DerivedBidPrice", ctypes.c_double),
        ("DerivedAskPrice", ctypes.c_double),
        # match
        ("MatchPrice",   ctypes.c_double),
        ("TestMatchPrice", ctypes.c_double),
        # day stats
        ("OpeningPrice", ctypes.c_double),
        ("HighPrice",    ctypes.c_double),
        ("LowPrice",     ctypes.c_double),
        ("ClosingPrice", ctypes.c_double),
        # settlement
        ("SettlementPrice", ctypes.c_double),
        ("PreClosePrice",   ctypes.c_double),
        # flags / counts (int section)
        ("Market",         ctypes.c_int),
        ("MsgType",        ctypes.c_int),
        ("Sequence",       ctypes.c_int),
        ("IsTestMatch",    ctypes.c_int),
        ("TradingSession", ctypes.c_int),
        ("StatusRemarks",    ctypes.c_int),
        ("RaiseFallRemarks", ctypes.c_int),
        # order book quantities
        ("BidQty1", ctypes.c_int),
        ("BidQty2", ctypes.c_int),
        ("BidQty3", ctypes.c_int),
        ("BidQty4", ctypes.c_int),
        ("BidQty5", ctypes.c_int),
        ("AskQty1", ctypes.c_int),
        ("AskQty2", ctypes.c_int),
        ("AskQty3", ctypes.c_int),
        ("AskQty4", ctypes.c_int),
        ("AskQty5", ctypes.c_int),
        ("DerivedFlag",   ctypes.c_int),
        ("DerivedBidQty", ctypes.c_int),
        ("DerivedAskQty", ctypes.c_int),
        # match / totals
        ("MatchQty",     ctypes.c_int),
        ("TestMatchQty", ctypes.c_int),
        ("TotalMatchQty", ctypes.c_int),
        ("OpeningQty",   ctypes.c_int),
        ("OpenInterest", ctypes.c_int),
        # index
        ("BidTotalQty",   ctypes.c_int),
        ("AskTotalQty",   ctypes.c_int),
        ("BidTotalCount", ctypes.c_int),
        ("AskTotalCount", ctypes.c_int),
        ("Range",               ctypes.c_int),
        ("SuspensionIndicator", ctypes.c_int),
    ]


class Contract(ctypes.Structure):
    _fields_ = [
        ("Exchange",   ctypes.c_char * 32),
        ("Symbol",     ctypes.c_char * 32),
        ("ProductID",  ctypes.c_char * 32),
        ("BullPrice",          ctypes.c_double),
        ("BearPrice",          ctypes.c_double),
        ("RefPrice",           ctypes.c_double),
        ("StrikePrice",        ctypes.c_double),
        ("ContractMultiplier", ctypes.c_double),
        ("Market",               ctypes.c_int),
        ("TradeUnit",            ctypes.c_int),
        ("DayTrade",             ctypes.c_int),
        ("WarningStock",         ctypes.c_int),
        ("DecimalLocator",       ctypes.c_int),
        ("StrikeDecimalLocator", ctypes.c_int),
        ("TradeFlag",  ctypes.c_bool),
        ("IsWarrant",  ctypes.c_bool),
        ("CallPut",    ctypes.c_char),
        ("SettleMonth", ctypes.c_char * 16),
        ("EndDate",     ctypes.c_char * 16),
    ]


# ─── Internal callback types (cdecl) ─────────────────────────────────────────

_CB_Connected    = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
_CB_Disconnected = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
_CB_Logon        = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool, ctypes.c_char_p)
_CB_MarketData   = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,                    # user_defined
    ctypes.c_int,                       # MsgType
    ctypes.c_char_p,                    # Exchange
    ctypes.c_char_p,                    # Symbol
    ctypes.c_char_p,                    # MsgTime
    ctypes.POINTER(MarketData),         # MarketData*
)
_CB_Contract = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,                    # user_defined
    ctypes.c_char_p,                    # Exchange
    ctypes.c_char_p,                    # Symbol
    ctypes.c_char_p,                    # Name
    ctypes.c_char_p,                    # MaturityDate
    ctypes.c_char_p,                    # Category
    ctypes.POINTER(Contract),           # Contract*
)
_CB_ContractDownloadComplete = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int)


# ─── StarWaveAPI ──────────────────────────────────────────────────────────────

class StarWaveAPI:
    """
    Base class for StarWave market data API.

    Usage:
        class MyApp(StarWaveAPI):
            def on_market_data(self, msg_type, exchange, symbol, msg_time, data):
                print(f"{exchange}.{symbol}  bid={data.BidPrice1}  ask={data.AskPrice1}")

        app = MyApp()
        app.logon("192.168.1.1", 8080, "user", "pass")
        app.subscribe("TAIFEX", "TXFB5")
    """

    def __init__(self, dll_path: str = "StarWaveCAPI_64.dll", version: Optional[str] = None,
                 instance_name: Optional[str] = None):
        self._lib = ctypes.CDLL(dll_path)
        self._setup_prototypes()
        if instance_name:
            ver = (version or "").encode()
            self._adapter = self._lib.swQuoteAdapter_NewWithVersionAndName(ver, instance_name.encode())
        elif version:
            self._adapter = self._lib.swQuoteAdapter_NewWithVersion(version.encode())
        else:
            self._adapter = self._lib.swQuoteAdapter_New()
        self._register_callbacks()

    # ── DLL function signatures ───────────────────────────────────────────────

    def _setup_prototypes(self):
        lib = self._lib
        lib.swQuoteAdapter_New.restype  = ctypes.c_void_p
        lib.swQuoteAdapter_New.argtypes = []

        lib.swQuoteAdapter_NewWithVersion.restype  = ctypes.c_void_p
        lib.swQuoteAdapter_NewWithVersion.argtypes = [ctypes.c_char_p]

        lib.swQuoteAdapter_NewWithVersionAndName.restype  = ctypes.c_void_p
        lib.swQuoteAdapter_NewWithVersionAndName.argtypes = [ctypes.c_char_p, ctypes.c_char_p]

        lib.swQuoteAdapter_Delete.argtypes = [ctypes.c_void_p]

        lib.swQuoteAdapter_GetBuildDate.restype  = ctypes.c_char_p
        lib.swQuoteAdapter_GetBuildDate.argtypes = []

        lib.swQuoteAdapter_IsConnected.argtypes = [ctypes.c_void_p]
        lib.swQuoteAdapter_IsConnected.restype  = ctypes.c_bool

        lib.swQuoteAdapter_Disconnect.argtypes = [ctypes.c_void_p]

        lib.swQuoteAdapter_Logon.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_bool,
        ]
        lib.swQuoteAdapter_Logon.restype = ctypes.c_bool

        lib.swQuoteAdapter_RequestSnapshot.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.swQuoteAdapter_RequestSnapshot.restype  = ctypes.c_bool

        lib.swQuoteAdapter_Subscribe.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        lib.swQuoteAdapter_Subscribe.restype  = ctypes.c_bool

        lib.swQuoteAdapter_IsSubscribeLimited.argtypes = [ctypes.c_void_p]
        lib.swQuoteAdapter_IsSubscribeLimited.restype  = ctypes.c_bool

        lib.swQuoteAdapter_HasSubscribeLimit.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.swQuoteAdapter_HasSubscribeLimit.restype  = ctypes.c_bool

        lib.swQuoteAdapter_GetSubscribeLimit.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.swQuoteAdapter_GetSubscribeLimit.restype  = ctypes.c_int

        lib.swQuoteAdapter_CanSubscribeAll.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.swQuoteAdapter_CanSubscribeAll.restype  = ctypes.c_bool

        lib.swQuoteAdapter_Unsubscribe.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.swQuoteAdapter_Unsubscribe.restype  = ctypes.c_bool

        lib.swQuoteAdapter_UnsubscribeAll.argtypes   = [ctypes.c_void_p]
        lib.swQuoteAdapter_DownloadContracts.argtypes = [ctypes.c_void_p]

        lib.swQuoteAdapter_GetLastErrorMessage.argtypes = [ctypes.c_void_p]
        lib.swQuoteAdapter_GetLastErrorMessage.restype  = ctypes.c_char_p

        lib.swQuoteAdapter_SetAppVersion.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

        lib.swQuoteAdapter_RegEvents.argtypes = [
            ctypes.c_void_p,              # adapter
            ctypes.c_void_p,              # user_defined
            _CB_Connected,
            _CB_Disconnected,
            _CB_Logon,
            _CB_Contract,
            _CB_ContractDownloadComplete,
            _CB_MarketData,
        ]

    # ── Callback wiring ───────────────────────────────────────────────────────

    def _register_callbacks(self):
        self_ref = self  # closure capture

        @_CB_Connected
        def _on_connected(ud):
            self_ref.on_connected()

        @_CB_Disconnected
        def _on_disconnected(ud):
            self_ref.on_disconnected()

        @_CB_Logon
        def _on_logon(ud, ok, msg):
            self_ref.on_logon_response(bool(ok), msg.decode() if msg else "")

        @_CB_MarketData
        def _on_market_data(ud, msg_type, exchange, symbol, msg_time, data_ptr):
            self_ref.on_market_data(
                MsgType(msg_type),
                exchange.decode() if exchange else "",
                symbol.decode()   if symbol   else "",
                msg_time.decode() if msg_time else "",
                data_ptr.contents,
            )

        @_CB_Contract
        def _on_contract(ud, exchange, symbol, name, maturity, category, contract_ptr):
            self_ref.on_contract(
                exchange.decode() if exchange else "",
                symbol.decode()   if symbol   else "",
                name.decode()     if name     else "",
                maturity.decode() if maturity else "",
                category.decode() if category else "",
                contract_ptr.contents,
            )

        @_CB_ContractDownloadComplete
        def _on_complete(ud, total):
            self_ref.on_contract_download_complete(total)

        # Must keep references alive — GC would collect them otherwise
        self._cb_connected   = _on_connected
        self._cb_disconnected = _on_disconnected
        self._cb_logon       = _on_logon
        self._cb_market_data = _on_market_data
        self._cb_contract    = _on_contract
        self._cb_complete    = _on_complete

        self._lib.swQuoteAdapter_RegEvents(
            self._adapter, None,
            self._cb_connected,
            self._cb_disconnected,
            self._cb_logon,
            self._cb_contract,
            self._cb_complete,
            self._cb_market_data,
        )

    def __del__(self):
        if hasattr(self, "_adapter") and self._adapter:
            self._lib.swQuoteAdapter_Delete(self._adapter)
            self._adapter = None

    # ── Public API ────────────────────────────────────────────────────────────

    def set_app_version(self, version: str):
        self._lib.swQuoteAdapter_SetAppVersion(self._adapter, version.encode())

    def logon(self, ip: str, port: int, user_id: str, password: str,
              download_contracts: bool = False, version: Optional[str] = None) -> bool:
        if version:
            self.set_app_version(version)
        return self._lib.swQuoteAdapter_Logon(
            self._adapter,
            ip.encode(), port, user_id.encode(), password.encode(), download_contracts,
        )

    def get_build_date(self) -> str:
        """Return the native DLL build date (compiler __DATE__, e.g. 'Jun 25 2026')."""
        raw = self._lib.swQuoteAdapter_GetBuildDate()
        return raw.decode() if raw else ""

    def is_connected(self) -> bool:
        return self._lib.swQuoteAdapter_IsConnected(self._adapter)

    def disconnect(self):
        """Disconnect from the market data server."""
        self._lib.swQuoteAdapter_Disconnect(self._adapter)

    def is_subscribe_limited(self) -> bool:
        """Return whether this account has any subscription quantity limit."""
        return self._lib.swQuoteAdapter_IsSubscribeLimited(self._adapter)

    def has_subscribe_limit(self, exchange: str) -> bool:
        """Return whether the specified exchange has a subscription quantity limit."""
        return self._lib.swQuoteAdapter_HasSubscribeLimit(self._adapter, exchange.encode())

    def get_subscribe_limit(self, exchange: str) -> int:
        """Return the exchange subscription limit, or -1 when unlimited."""
        return self._lib.swQuoteAdapter_GetSubscribeLimit(self._adapter, exchange.encode())

    def can_subscribe_all(self, exchange: str) -> bool:
        """Return whether all products on the specified exchange can be subscribed."""
        return self._lib.swQuoteAdapter_CanSubscribeAll(self._adapter, exchange.encode())

    def subscribe(self, exchange: str, symbol: str,
                  sub_type: SubscribeType = SubscribeType.SnapshotWithUpdate) -> bool:
        return self._lib.swQuoteAdapter_Subscribe(
            self._adapter, exchange.encode(), symbol.encode(), int(sub_type),
        )

    def request_snapshot(self, exchange: str, symbol: str) -> bool:
        return self._lib.swQuoteAdapter_RequestSnapshot(
            self._adapter, exchange.encode(), symbol.encode(),
        )

    def unsubscribe(self, exchange: str, symbol: str) -> bool:
        return self._lib.swQuoteAdapter_Unsubscribe(
            self._adapter, exchange.encode(), symbol.encode(),
        )

    def unsubscribe_all(self):
        self._lib.swQuoteAdapter_UnsubscribeAll(self._adapter)

    def download_contracts(self):
        self._lib.swQuoteAdapter_DownloadContracts(self._adapter)

    def get_last_error(self) -> str:
        msg = self._lib.swQuoteAdapter_GetLastErrorMessage(self._adapter)
        return msg.decode() if msg else ""

    # ── Override these ────────────────────────────────────────────────────────

    def on_connected(self):
        pass

    def on_disconnected(self):
        pass

    def on_logon_response(self, success: bool, message: str):
        pass

    def on_market_data(self, msg_type: MsgType, exchange: str, symbol: str,
                       msg_time: str, data: MarketData):
        pass

    def on_contract(self, exchange: str, symbol: str, name: str,
                    maturity: str, category: str, contract: Contract):
        pass

    def on_contract_download_complete(self, total_contracts: int):
        pass
