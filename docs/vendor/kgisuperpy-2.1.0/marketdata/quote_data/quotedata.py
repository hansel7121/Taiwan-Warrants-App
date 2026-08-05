from enum import Enum
import inspect
import operator
from .quote_status import QuotationCode, QuotationError, QuoteEventCode
#-----------------------------------------------------
class BasicQuote:
    def __repr__(self):
        cls = self.__class__
        seen = set()
        pairs = []

        for base in cls.__mro__:
            for name, prop in base.__dict__.items():
                if name in seen:
                    continue
                seen.add(name)
                if isinstance(prop, property) and prop.fget:
                    try:
                        val = getattr(self, name)
                        pairs.append(f"{name}={val!r}")
                    except Exception:
                        pairs.append(f"{name}=<error>")

        return f"{cls.__name__}({', '.join(pairs)})"

#-----------------------------------------------------
def convert_tw_stock_exchange_from_code(code:str)-> str:
    if code == "1":
        return "TWSE"
    elif code == "2":
        return "OTC"
    elif code == "3":
        return "ES"
    else:
        return "Unknown"
#----------------------Type Enum----------------------
class MarketType(str, Enum):
    TWStock = "TWStock"
    TWStockOdd = "TWStockOdd"
    TWFuture = "TWFuture"
    USStock = "USStock"

class QuoteType(Enum):
    qtTick = "qtTick"
    qtBidAsk = "qtBidAsk"
    qtAll = "qtAll"
    qtKBar = "qtKBar"
    qtIndex = "qtIndex"

class QuoteVersion(str, Enum):
    v0 = "v0"
    v1 = "v1"
#----------------------Contract----------------------    
class _BasicContract:
    _market: MarketType
    _symbol: str
    _name: str
    
    def __init__(self, symbol:str, market_type:MarketType):
        self._symbol = symbol
        self._market = market_type
    

    def __repr__(self):
        cls = self.__class__
        seen = set()
        pairs = []

        for base in cls.__mro__:
            for name, prop in base.__dict__.items():
                if name in seen:
                    continue
                seen.add(name)
                if isinstance(prop, property) and prop.fget:
                    try:
                        val = getattr(self, name)
                        pairs.append(f"{name}={val!r}")
                    except Exception:
                        pass

        return f"{cls.__name__}({', '.join(pairs)})"

    # def __repr__(self):
    #     cls = self._class__
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
    #                     pairs.append(f"{name}={val}")
    #                 except Exception:
    #                     pass

    #     return f"{cls.__name__}({', '.join(pairs)})"
        # # 只獲取public variable（不以 "_" 開頭的variable）
        # attributes = {key: value for key, value in vars(self).items() if not key.startswith("_")}
        # attributes_str = ', '.join(f"{key}={value}" for key, value in attributes.items())
        # return f"{self._class__.__name__}({attributes_str})"
    
    @property
    def market(self):
        return self._market
    
    @property
    def symbol(self):
        return self._symbol
    
    @symbol.setter
    def symbol(self, value):
        self._symbol = value
    
    @property
    def name(self):
        return self._name
    
    @name.setter
    def name(self, value):
        self._name = value
#----------------------TW Stock----------------------
class TWStockContract(_BasicContract):
    _exchange: str
    _category: str
    _limit_up: float
    _limit_down: float
    _reference_price: float
    _update_date: str
    _margin_trading_balance: float
    _short_selling_balance: float
    # margin_trading_rate: int
    # short_selling_rate: int
    _day_trade: str

    def __init__(self, symbol:str):
        super().__init__(symbol, MarketType.TWStock)
    
    @property
    def exchange(self):
        return self._exchange
    
    @exchange.setter
    def exchange(self, value:str):
        self._exchange = value

    @property
    def category(self):
        return self._category
    
    @category.setter
    def category(self, value:str):
        self._category = value

    @property
    def limit_up(self):
        return self._limit_up
    
    @limit_up.setter
    def limit_up(self, value:float):
        self._limit_up = value

    @property
    def limit_down(self):
        return self._limit_down
    
    @limit_down.setter
    def limit_down(self, value:float):
        self._limit_down = value

    @property
    def reference(self):
        return self._reference_price
    
    @reference.setter
    def reference(self, value:float):
        self._reference_price = value

    @property
    def update_date(self):
        return self._update_date
    
    @update_date.setter
    def update_date(self, value:str):
        self._update_date = value

    @property
    def margin_trading_balance(self):
        return self._margin_trading_balance
    
    @margin_trading_balance.setter
    def margin_trading_balance(self, value:float):
        self._margin_trading_balance = value

    @property
    def short_selling_balance(self):
        return self._short_selling_balance
    
    @short_selling_balance.setter
    def short_selling_balance(self, value:float):
        self._short_selling_balance = value

    @property
    def day_trade(self):
        return self._day_trade
    
    @day_trade.setter
    def day_trade(self, value:str):
        self._day_trade = value
#----------------------US Stock----------------------
class USStockContract(_BasicContract):     
    _name_tw: str
    _name_cn: str
    _category: str
    _category_tw: str
    _category_cn: str
    _sub_category: str
    _sub_category_tw: str
    _sub_category_cn: str
    _business_overview: str
    _business_overview_tw: str
    _business_overview_cn: str
    _industry_position: str    
    _industry_position_tw: str
    _industry_position_cn: str
    _is_etf: bool
    _market_cap: float
    _outstanding_shares: float
    _employees: int
    _shareholders: int

    def __init__(self, symbol:str):
        super().__init__(symbol, MarketType.USStock)

    @property
    def name(self):
        return self._name
    
    @name.setter
    def name(self, value:str):
        self._name = value

    @property
    def name_tw(self):
        return self._name_tw
    
    @name_tw.setter
    def name_tw(self, value:str):
        self._name_tw = value

    @property
    def name_cn(self):
        return self._name_cn
    
    @name_cn.setter
    def name_cn(self, value:str):
        self._name_cn = value

    @property
    def category(self):
        return self._category
    
    @category.setter
    def category(self, value:str):
        self._category = value

    @property
    def category_tw(self):
        return self._category_tw
    
    @category_tw.setter
    def category_tw(self, value:str):
        self._category_tw = value
    
    @property
    def category_cn(self):
        return self._category_cn
    
    @category_cn.setter
    def category_cn(self, value:str):
        self._category_cn = value

    @property
    def sub_category(self):
        return self._sub_category
    
    @sub_category.setter
    def sub_category(self, value:str):
        self._sub_category = value

    @property
    def sub_category_tw(self):
        return self._sub_category_tw
    
    @sub_category_tw.setter
    def sub_category_tw(self, value:str):
        self._sub_category_tw = value

    @property
    def sub_category_cn(self):
        return self._sub_category_cn
    
    @sub_category_cn.setter
    def sub_category_cn(self, value:str):
        self._sub_category_cn = value

    @property
    def business_overview(self):
        return self._business_overview
    
    @business_overview.setter
    def business_overview(self, value:str):
        self._business_overview = value

    @property
    def business_overview_tw(self):
        return self._business_overview_tw
    
    @business_overview_tw.setter
    def business_overview_tw(self, value:str):
        self._business_overview_tw = value

    @property
    def business_overview_cn(self):
        return self._business_overview_cn
    
    @business_overview_cn.setter
    def business_overview_cn(self, value:str):
        self._business_overview_cn = value

    @property
    def industry_position(self):
        return self._industry_position
    @industry_position.setter
    def industry_position(self, value:str):
        self._industry_position = value

    @property
    def industry_position_tw(self):
        return self._industry_position_tw
    
    @industry_position_tw.setter
    def industry_position_tw(self, value:str):
        self._industry_position_tw = value

    @property
    def industry_position_cn(self):
        return self._industry_position_cn
    @industry_position_cn.setter

    def industry_position_cn(self, value:str):
        self._industry_position_cn = value

    @property
    def is_etf(self):
        return self._is_etf
    
    @is_etf.setter
    def is_etf(self, value:bool):
        self._is_etf = value

    @property
    def market_cap(self):
        return self._market_cap
    
    @market_cap.setter
    def market_cap(self, value:float):
        self._market_cap = value

    @property
    def outstanding_shares(self):
        return self._outstanding_shares
    
    @outstanding_shares.setter
    def outstanding_shares(self, value:float):
        self._outstanding_shares = value

    @property
    def employees(self):
        return self._employees
    
    @employees.setter
    def employees(self, value:int):
        self._employees = value

    @property
    def shareholders(self):
        return self._shareholders
    
    @shareholders.setter
    def shareholders(self, value:int):
        self._shareholders = value
#----------------------TW Future----------------------
class TWFutureContract(_BasicContract):
    _exchange: str
    _underlying_symbol: str
    _underlying_name: str
    _settlement_margin: float
    _maintenance_margin: float
    _initial_margin: float
    # _limit_up: float
    # _limit_down: float
    # _reference: float
    # _update_date: str

    def __init__(self, symbol:str):
        self._exchange = "TAIFEX"
        super().__init__(symbol, MarketType.TWFuture)

    @property
    def exchange(self):
        return self._exchange
    
    @exchange.setter
    def exchange(self, value):
        self._exchange = value

    @property
    def name(self):
        return self._name
    
    @name.setter
    def name(self, value):
        self._name = value

    @property
    def underlying_symbol(self):
        return self._underlying_symbol
    
    @underlying_symbol.setter
    def underlying_symbol(self, value):
        self._underlying_symbol = value

    @property
    def underlying_name(self):
        return self._underlying_name
    
    @underlying_name.setter
    def underlying_name(self, value):
        self._underlying_name = value


    @property
    def settlement_margin(self):
        return self._settlement_margin
    
    @settlement_margin.setter
    def settlement_margin(self, value:float):
        self._settlement_margin = value
    
    @property
    def maintenance_margin(self):
        return self._maintenance_margin
    
    @maintenance_margin.setter
    def maintenance_margin(self, value:float):
        self._maintenance_margin = value
    
    @property
    def initial_margin(self):
        return self._initial_margin
    
    @initial_margin.setter
    def initial_margin(self, value:float):
        self._initial_margin = value

    
    # @property
    # def category(self):
    #     return self._category
    
    # @category.setter
    # def category(self, value):
    #     self._category = value

    # @property
    # def limit_up(self):
    #     return self._limit_up
    
    # @limit_up.setter
    # def limit_up(self, value):
    #     self._limit_up = float(value)

    # @property
    # def limit_down(self):
    #     return self._limit_down
    
    # @limit_down.setter
    # def limit_down(self, value):
    #     self._limit_down = float(value)

    # @property
    # def reference_price(self):
    #     return self._reference
    
    # @reference_price.setter
    # def reference_price(self, value):
    #     self._reference = float(value)

    # @property
    # def update_date(self):
    #     return self._update_date
    
    # @update_date.setter
    # def update_date(self, value):
    #     self._update_date = value
#----------------------Market Msg----------------------
class _BasicMsg():
    _symbol: str
    _datetime: str
    _delay_time: float
    
    def __init__(self):
        self._symbol = None
        self._datetime =  None
        self._delay_time = 0.0

    def __repr__(self):
        cls = self.__class__
        seen = set()
        pairs = []

        for base in cls.__mro__:
            for name, prop in base.__dict__.items():
                if name in seen:
                    continue
                seen.add(name)
                if isinstance(prop, property) and prop.fget:
                    try:
                        val = getattr(self, name)
                        pairs.append(f"{name}={val}")
                    except Exception:
                        pass

        return f"{cls.__name__}({', '.join(pairs)})"
        attributes = {key: value for key, value in vars(self).items() if not key.startswith("_")}
        attributes_str = ', '.join(f"{key}={value}" for key, value in attributes.items())
        return f"{self._class__.__name__}({attributes_str})"

    def __dir__(self):
        result = set()
        for base in self.__class__.__mro__:
            for name, prop in base.__dict__.items():
                if isinstance(prop, property) and prop.fget:
                    result.add(name)
        return sorted(result)

    @property
    def symbol(self):
        return self._symbol
    
    @symbol.setter
    def symbol(self, value:str):
        self._symbol = value

    @property
    def datetime(self):
        return self._datetime
    
    @datetime.setter
    def datetime(self, value:str):
        self._datetime = value

    @property
    def delay_time(self):
        return self._delay_time
    
    @delay_time.setter
    def delay_time(self, value:float):
        self._delay_time = round(value, 3)
#----------------------KBar----------------------
class _KBarMsg(_BasicMsg):
    _timeframe: int
    _open: float
    _high: float
    _low: float
    _close: float
    _volume: float
    _average: float

    def __init__(self):
        super().__init__()
        self._timeframe = 0
        self._open = 0
        self._high = 0
        self._low = 0
        self._close = 0
        self._volume = 0
        self._average = 0

    @property
    def timeframe(self):
        return self._timeframe
    
    @timeframe.setter
    def timeframe(self, value:int):
        self._timeframe = value

    @property
    def open(self):
        return self._open
    
    @open.setter
    def open(self, value:float):
        self._open = value

    @property
    def high(self):
        return self._high
    
    @high.setter
    def high(self, value:float):
        self._high = value
    
    @property
    def low(self):
        return self._low
    
    @low.setter
    def low(self, value:float):
        self._low = value

    @property
    def close(self):
        return self._close
    
    @close.setter
    def close(self, value:float):
        self._close = value

    @property
    def volume(self):
        return self._volume
    
    @volume.setter
    def volume(self, value:float):
        self._volume = value

    @property
    def average(self):
        return self._average
    
    @average.setter
    def average(self, value:float):
        self._average = value

#----------------------Tick----------------------
class _TickData(_BasicMsg):
    _open: float
    _high: float
    _low: float
    _close: float
    _volume: float
    _total_volume: int

    def __init__(self):
        super().__init__()
        self._open = 0
        self._high = 0
        self._low = 0
        self._close = 0
        self._volume = 0
        self._total_volume = 0

    @property
    def open(self):
        return self._open
    
    @open.setter
    def open(self, value:float):
        self._open = value

    @property
    def high(self):
        return self._high
    
    @high.setter
    def high(self, value:float):
        self._high = value

    @property
    def low(self):
        return self._low
    
    @low.setter
    def low(self, value:float):
        self._low = value

    @property
    def close(self):
        return self._close
    
    @close.setter
    def close(self, value:float):
        self._close = value
    
    @property
    def volume(self):
        return self._volume
    
    @volume.setter
    def volume(self, value:float):
        self._volume = value
    
    @property
    def total_volume(self):
        return self._total_volume
    
    @total_volume.setter
    def total_volume(self, value:int):
        self._total_volume = value

#----------------------BidAsk----------------------
class _BidAskData(_BasicMsg):
    _bid_prices: list[float]
    _bid_volumes: list[int]
    _ask_prices: list[float]
    _ask_volumes: list[int]

    def __init__(self):
        super().__init__()
        self._bid_prices = [0.0] * 5
        self._bid_volumes = [0] * 5
        self._ask_prices = [0.0] * 5
        self._ask_volumes = [0] * 5

    @property
    def bid_prices(self):
        return self._bid_prices
    
    @bid_prices.setter
    def bid_prices(self, value:list[float]):
        self._bid_prices = value

    @property
    def bid_volumes(self):
        return self._bid_volumes
    
    @bid_volumes.setter
    def bid_volumes(self, value:list[int]):
        self._bid_volumes = value

    @property
    def ask_prices(self):
        return self._ask_prices
    
    @ask_prices.setter
    def ask_prices(self, value:list[float]):
        self._ask_prices = value

    @property
    def ask_volumes(self):
        return self._ask_volumes
    
    @ask_volumes.setter
    def ask_volumes(self, value:list[int]):
        self._ask_volumes = value
        
#----------------------TW Stock----------------------
class _TWStockMsg(_BasicMsg):
    _exchange: str
    _odd_lot: bool
    _simtrade: int
    _suspend: int

    def __init__(self):
        super().__init__()
        self._exchange = None
        self._odd_lot = False
        self._simtrade = 0
        self._suspend = 0

    @property
    def exchange(self):
        return self._exchange
    
    @exchange.setter
    def exchange(self, value:str):
        self._exchange = value

    @property
    def odd_lot(self):
        return self._odd_lot
    
    @odd_lot.setter
    def odd_lot(self, value:bool):
        self._odd_lot = value
    
    @property
    def simtrade(self):
        return self._simtrade
    
    @simtrade.setter
    def simtrade(self, value:int):
        self._simtrade = value

    @property
    def suspend(self):
        return self._suspend
    
    @suspend.setter
    def suspend(self, value:int):
        self._suspend = value

class Tick_Stock_v0(_TWStockMsg, _TickData):
    def __init__(self):
        super().__init__()

class Tick_Stock_v1(Tick_Stock_v0):
    _avg_price: float
    _amount: float
    _total_amount: float
    _chg_type: int
    _price_chg: float
    _pct_chg: float
    _ref_price: float
    _bull_price: float
    _bear_price: float

    def __init__(self):
        super().__init__()
        self._avg_price = 0
        self._amount = 0
        self._total_amount = 0
        self._chg_type = 0
        self._price_chg = 0
        self._pct_chg = 0
        self._ref_price = 0
        self._bull_price = 0
        self._bear_price = 0

    def _caculate(self):
        self._amount = round(self._close * self._volume, 4)
        self._price_chg = round(self._close - self._ref_price, 4)
        if self._ref_price == 0:
            self._pct_chg = 0
        else:
            self._pct_chg = round(self._price_chg / self._ref_price * 100, 2)
        #TODO
        #self.total_amount = 
        #self.avg_price = 

    @property
    def avg_price(self):
        return self._avg_price
    
    @avg_price.setter
    def avg_price(self, value:float):
        self._avg_price = value

    @property
    def amount(self):
        return self._amount
    
    @amount.setter
    def amount(self, value:float):
        self._amount = value

    @property
    def total_amount(self):
        return self._total_amount
    
    @total_amount.setter
    def total_amount(self, value:float):
        self._total_amount = value

    @property
    def chg_type(self):
        return self._chg_type
    
    @chg_type.setter
    def chg_type(self, value:int):
        self._chg_type = value

    @property
    def price_chg(self):
        return self._price_chg
    
    @price_chg.setter
    def price_chg(self, value:float):
        self._price_chg = value

    @property
    def pct_chg(self):
        return self._pct_chg
    
    @pct_chg.setter
    def pct_chg(self, value:float):
        self._pct_chg = value

    @property
    def ref_price(self):
        return self._ref_price
    
    @ref_price.setter
    def ref_price(self, value:float):
        self._ref_price = value
    
    @property
    def bull_price(self):
        return self._bull_price
    
    @bull_price.setter
    def bull_price(self, value:float):
        self._bull_price = value

    @property
    def bear_price(self):
        return self._bear_price
    
    @bear_price.setter
    def bear_price(self, value:float):
        self._bear_price = value
    
        
class BidAsk_Stock_v0(_TWStockMsg, _BidAskData):
    def __init__(self):
        super().__init__()

class BidAsk_Stock_v1(BidAsk_Stock_v0):
    _diff_bid_vol: list[int]
    _diff_ask_vol: list[int]
    __pre_bid_volumes: list[int]
    __pre_ask_volumes: list[int]

    def __init__(self):
        super().__init__()
        self._diff_bid_vol = [0] * 5
        self._diff_ask_vol = [0] * 5
        self.__pre_bid_volumes = [0] * 5
        self.__pre_ask_volumes = [0] * 5

    def _caculate(self):
        self.diff_bid_vol =  list(map(operator.sub, self.bid_volumes, self.__pre_bid_volumes))
        self.diff_ask_vol =  list(map(operator.sub, self.ask_volumes, self.__pre_ask_volumes))
        self.__pre_bid_volumes = self.bid_volumes
        self.__pre_ask_volumes = self.ask_volumes
        pass
    
    @property
    def diff_bid_vol(self):
        return self._diff_bid_vol
    
    @diff_bid_vol.setter
    def diff_bid_vol(self, value:list[int]):
        self._diff_bid_vol = value

    @property
    def diff_ask_vol(self):
        return self._diff_ask_vol
    
    @diff_ask_vol.setter
    def diff_ask_vol(self, value:list[int]):
        self._diff_ask_vol = value
    
class All_Stock_v0(Tick_Stock_v0, BidAsk_Stock_v0):
    def __init__(self):
        super().__init__()
    
class All_Stock_v1(Tick_Stock_v1, BidAsk_Stock_v1):
    def __init__(self):
        super().__init__()

    def _caculate_Tick(self):
        Tick_Stock_v1._caculate(self)

    def _caculate_BidAsk(self):
        BidAsk_Stock_v1._caculate(self)

class KBar_Stock_v0(_KBarMsg):
    __exchange: str
    _avg_price: float
    _total_amount: float
    def __init__(self):
        super().__init__()
        self._avg_price = 0
        self._total_amount = 0

    @property
    def exchange(self):
        return self._exchange
    
    @exchange.setter
    def exchange(self, value:str):
        self._exchange = value

    @property
    def avg_price(self):
        return self._avg_price
    
    @avg_price.setter
    def avg_price(self, value:float):
        self._avg_price = value

    @property
    def total_amount(self):
        return self._total_amount
    
    @total_amount.setter
    def total_amount(self, value:float):
        self._total_amount = value
#----------------------Index Stock----------------------
class Index_Stock_v0(_BasicMsg):
    _exchange: str
    _index_value: float
    _total_qty: int
    _total_count: int
    _total_amount: int
    def __init__(self):
        super().__init__()
        self._index_value = 0
        self._total_qty = 0
        self._total_count = 0
        self._total_amount = 0

    @property
    def exchange(self):
        return self._exchange
    
    @exchange.setter
    def exchange(self, value:str):
        self._exchange = value

    @property
    def index_value(self):
        return self._index_value
    
    @index_value.setter
    def index_value(self, value:float):
        self._index_value = value

    @property
    def total_qty(self):
        return self._total_qty
    
    @total_qty.setter
    def total_qty(self, value:int):
        self._total_qty = value

    @property
    def total_count(self):
        return self._total_count
    
    @total_count.setter
    def total_count(self, value:int):
        self._total_count = value

    @property
    def total_amount(self):
        return self._total_amount
    
    @total_amount.setter
    def total_amount(self, value:int):
        self._total_amount = value
#----------------------TW Future----------------------
class _TWFutureMsg(_BasicMsg):
    exchange: str
    #simtrade: int
    
    def __init__(self):
        super().__init__()
        self.exchange = 'TAIFEX'
        #self.simtrade = 0

    @property
    def exchange(self):
        return self._exchange
    
    @exchange.setter
    def exchange(self, value:str):
        self._exchange = value

class Tick_Future_v0(_TWFutureMsg, _TickData):
    def __init__(self):
        super().__init__()
    
class Tick_Future_v1(Tick_Future_v0):
    _ref_price: float
    _bull_price: float
    _bear_price: float
#  avg_price: float
#     amount: float
#     total_amount: float
#     chg_type: int
#     price_chg: float
#     pct_chg: float
    def __init__(self):
        super().__init__()
        self._ref_price = 0
        self._bull_price = 0
        self._bear_price = 0


class BidAsk_Future_v0(_TWFutureMsg, _BidAskData):
    def __init__(self):
        super().__init__()

class BidAsk_Future_v1(BidAsk_Future_v0):
    _diff_bid_vol: list[int]
    _diff_ask_vol: list[int]
    __pre_bid_volumes: list[int]
    __pre_ask_volumes: list[int]

    def __init__(self):
        super().__init__()
        self._diff_bid_vol = [0] * 5
        self._diff_ask_vol = [0] * 5
        self.__pre_bid_volumes = [0] * 5
        self.__pre_ask_volumes = [0] * 5

    def _caculate(self):
        self._diff_bid_vol =  list(map(operator.sub, self._bid_volumes, self.__pre_bid_volumes))
        self._diff_ask_vol =  list(map(operator.sub, self._ask_volumes, self.__pre_ask_volumes))
        self.__pre_bid_volumes = self._bid_volumes
        self.__pre_ask_volumes = self._ask_volumes

    @property
    def diff_bid_vol(self):
        return self._diff_bid_vol
    
    @diff_bid_vol.setter
    def diff_bid_vol(self, value:list[int]):
        self._diff_bid_vol = value

    @property
    def diff_ask_vol(self):
        return self._diff_ask_vol
    
    @diff_ask_vol.setter
    def diff_ask_vol(self, value:list[int]):
        self._diff_ask_vol = value

class All_Future_v0(Tick_Future_v0, BidAsk_Future_v0):
    def __init__(self):
        super().__init__()

class All_Future_v1(Tick_Future_v1, BidAsk_Future_v1):
    def __init__(self):
        super().__init__()

class KBar_Future_v0(_KBarMsg):
    def __init__(self):
        super().__init__()
#----------------------TW Future----------------------
class _USStockMsg(_BasicMsg):
    _suspend: int
    _trading_session: int
    def __init__(self):
        super().__init__()
        self._suspend = 0
        self._trading_session = 0

    @property
    def suspend(self):
        return self._suspend
    
    @suspend.setter
    def suspend(self, value:int):
        self._suspend = value

    @property
    def trading_session(self):
        return self._trading_session
    
    @trading_session.setter
    def trading_session(self, value:int):
        self._trading_session = value
    
class Tick_USStock_v0(_USStockMsg, _TickData):
    def __init__(self):
        super().__init__()
    
class Tick_USStock_v1(Tick_USStock_v0):
    _avg_price: float
    _amount: float
    _total_amount: float
    _price_chg: float
    _pct_chg: float

    def __init__(self):
        super().__init__()
        self._avg_price = 0
        self._amount = 0
        self._total_amount = 0
        self._price_chg = 0
        self._pct_chg = 0

    def _caculate(self):
        pass

    @property
    def amount(self):
        return self._amount
    
    @amount.setter
    def amount(self, value:float):
        self._amount = value

    @property
    def price_chg(self):
        return self._price_chg
    
    @price_chg.setter
    def price_chg(self, value:float):
        self._price_chg = value

    @property
    def pct_chg(self):
        return self._pct_chg
    
    @pct_chg.setter
    def pct_chg(self, value:float):
        self._pct_chg = value

    @property
    def total_amount(self):
        return self._total_amount
    
    @total_amount.setter
    def total_amount(self, value:float):
        self._total_amount = value

    @property
    def avg_price(self):
        return self._avg_price
    
    @avg_price.setter
    def avg_price(self, value:float):
        self._avg_price = value    
    
class BidAsk_USStock_v0(_USStockMsg):
    _best_bid_price: float
    _best_ask_price: float
    _best_bid_volume: int
    _best_ask_volume: int

    def __init__(self):
        super().__init__()
        self._best_bid_price = 0
        self._best_ask_price = 0
        self._best_bid_volume = 0
        self._best_ask_volume = 0
    
    def _caculate(self):
        pass

    @property
    def best_bid_price(self):
        return self._best_bid_price
    
    @best_bid_price.setter
    def best_bid_price(self, value:float):
        self._best_bid_price = value

    @property
    def best_ask_price(self):
        return self._best_ask_price
    
    @best_ask_price.setter
    def best_ask_price(self, value:float):
        self._best_ask_price = value

    @property
    def best_bid_volume(self):
        return self._best_bid_volume
    
    @best_bid_volume.setter
    def best_bid_volume(self, value:int):
        self._best_bid_volume = value

    @property
    def best_ask_volume(self):
        return self._best_ask_volume
    
    @best_ask_volume.setter
    def best_ask_volume(self, value:int):
        self._best_ask_volume = value

#目前沒有Bid/Ask加強版
# class _BidAsk_USStock_v1(BidAsk_USStock_v0):
#     def __init__(self):
#         super().__init__()

class All_USStock_v0(Tick_USStock_v0, BidAsk_USStock_v0):
    def __init__(self):
        super().__init__()

class All_USStock_v1(Tick_USStock_v1, BidAsk_USStock_v0):
    def __init__(self):
        super().__init__()

    def _caculate_Tick(self):
        Tick_USStock_v1._caculate(self)

    def _caculate_BidAsk(self):
        #BidAsk_USStock_v1.__caculate__(self)
        pass

class KBar_USStock_v0(_KBarMsg):
    _type: str
    _market: str

    def __init__(self):
        super().__init__()

    @property
    def type(self):
        return self._type
    
    @type.setter
    def type(self, value:str):
        self._type = value

    @property
    def market(self):
        return self._market
    
    @market.setter
    def market(self, value:str):
        self._market = value


#-------------------------------------------------
class QuoteData():
    MarketType = MarketType
    QuoteType = QuoteType
    QuoteVersion = QuoteVersion

    QuotationCode = QuotationCode
    QuotationError = QuotationError
    QuoteEventCode = QuoteEventCode

    TWStockContract = TWStockContract
    USStockContract = USStockContract
    TWFutureContract = TWFutureContract

    Tick_Stock_v0 = Tick_Stock_v0
    Tick_Stock_v1 = Tick_Stock_v1
    BidAsk_Stock_v0 = BidAsk_Stock_v0
    BidAsk_Stock_v1 = BidAsk_Stock_v1
    All_Stock_v0 = All_Stock_v0
    All_Stock_v1 = All_Stock_v1
    Index_Stock_v0 = Index_Stock_v0
    KBar_Stock_v0 = KBar_Stock_v0

    Tick_Future_v0 = Tick_Future_v0
    Tick_Future_v1 = Tick_Future_v1
    BidAsk_Future_v0 = BidAsk_Future_v0
    BidAsk_Future_v1 = BidAsk_Future_v1
    All_Future_v0 = All_Future_v0
    All_Future_v1 = All_Future_v1
    KBar_Future_v0 = KBar_Future_v0

    Tick_USStock_v0 = Tick_USStock_v0
    Tick_USStock_v1 = Tick_USStock_v1
    BidAsk_USStock_v0 = BidAsk_USStock_v0
    #BidAsk_USStock_v1 = BidAsk_USStock_v1
    All_USStock_v0 = All_USStock_v0
    All_USStock_v1 = All_USStock_v1
    KBar_USStock_v0 = KBar_USStock_v0
    