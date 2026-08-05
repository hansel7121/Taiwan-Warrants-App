import json
from enum import IntEnum
#----------------------------------------------------------------------------
class SWDataType(IntEnum):
    Snapshot       = 0 
    Orderbook      = 1 
    MatchInfo      = 2 
    TotalMatchInfo = 3 
    DayHighLow     = 4 
    OpenInfo       = 5 
#----------------------------------------------------------------------------
class SWMarketData:

    def __init__(self):
        self._exchange = None
        self._symbol = None
        self._data_type = 0
        self._datetime = None
        self._open = 0.0
        self._high = 0.0
        self._low = 0.0
        self._close = 0.0
        self._volume = 0
        self._total_volume = 0
        self._bid_prices = [0.0] * 5
        self._bid_volumes = [0] * 5
        self._ask_prices = [0.0] * 5
        self._ask_volumes = [0] * 5
        self._simtrade = 0
        self._status_remarks = 0
        self._raise_fall_remarks = 0
        self._delay_time = 0.0

    def __repr__(self):
        cls = type(self)
        pairs = []
        for name, attr in cls.__dict__.items():
            if isinstance(attr, property) and attr.fget:
                try:
                    pairs.append(f"{name}={getattr(self, name)}")
                except Exception:
                    pass
        return f"{cls.__name__}({', '.join(pairs)})"

    def summary(self) -> str:
        # JSON line containing only the fields that this data_type actually carries.
        try:
            type_name = SWDataType(self._data_type).name
        except ValueError:
            type_name = self._data_type

        d = {
            "exchange": self._exchange,
            "symbol": self._symbol,
            "data_type": type_name,
            "datetime": self._datetime,
        }

        dt = self._data_type
        if dt == SWDataType.Snapshot:
            d.update({
                "open": self._open, "high": self._high, "low": self._low, "close": self._close,
                "volume": self._volume, "total_volume": self._total_volume,
                "bid_prices": self._bid_prices, "bid_volumes": self._bid_volumes,
                "ask_prices": self._ask_prices, "ask_volumes": self._ask_volumes,
            })
        elif dt == SWDataType.Orderbook:
            d.update({
                "bid_prices": self._bid_prices, "bid_volumes": self._bid_volumes,
                "ask_prices": self._ask_prices, "ask_volumes": self._ask_volumes,
            })
        elif dt == SWDataType.MatchInfo:
            d.update({
                "close": self._close, "volume": self._volume,
                "status_remarks": self._status_remarks,
                "raise_fall_remarks": self._raise_fall_remarks,
            })
        elif dt == SWDataType.TotalMatchInfo:
            d.update({
                "total_volume": self._total_volume,
                "status_remarks": self._status_remarks,
                "raise_fall_remarks": self._raise_fall_remarks,
            })
        elif dt == SWDataType.DayHighLow:
            d.update({"high": self._high, "low": self._low})
        elif dt == SWDataType.OpenInfo:
            d.update({"open": self._open})

        return json.dumps(d, ensure_ascii=False)

    @property
    def exchange(self):
        return self._exchange

    @exchange.setter
    def exchange(self, value: str):
        self._exchange = value

    @property
    def symbol(self):
        return self._symbol

    @symbol.setter
    def symbol(self, value: str):
        self._symbol = value

    @property
    def data_type(self):
        return self._data_type

    @data_type.setter
    def data_type(self, value: int):
        self._data_type = int(value)

    @property
    def datetime(self):
        return self._datetime

    @datetime.setter
    def datetime(self, value: str):
        self._datetime = value

    @property
    def open(self):
        return self._open

    @open.setter
    def open(self, value: float):
        self._open = value

    @property
    def high(self):
        return self._high

    @high.setter
    def high(self, value: float):
        self._high = value

    @property
    def low(self):
        return self._low

    @low.setter
    def low(self, value: float):
        self._low = value

    @property
    def close(self):
        return self._close

    @close.setter
    def close(self, value: float):
        self._close = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: int):
        self._volume = value

    @property
    def total_volume(self):
        return self._total_volume

    @total_volume.setter
    def total_volume(self, value: int):
        self._total_volume = value

    @property
    def bid_prices(self):
        return self._bid_prices

    @bid_prices.setter
    def bid_prices(self, value: list):
        self._bid_prices = value

    @property
    def bid_volumes(self):
        return self._bid_volumes

    @bid_volumes.setter
    def bid_volumes(self, value: list):
        self._bid_volumes = value

    @property
    def ask_prices(self):
        return self._ask_prices

    @ask_prices.setter
    def ask_prices(self, value: list):
        self._ask_prices = value

    @property
    def ask_volumes(self):
        return self._ask_volumes

    @ask_volumes.setter
    def ask_volumes(self, value: list):
        self._ask_volumes = value

    @property
    def simtrade(self):
        return self._simtrade

    @simtrade.setter
    def simtrade(self, value: int):
        self._simtrade = value

    @property
    def status_remarks(self):
        return self._status_remarks

    @status_remarks.setter
    def status_remarks(self, value: int):
        self._status_remarks = value

    @property
    def raise_fall_remarks(self):
        return self._raise_fall_remarks

    @raise_fall_remarks.setter
    def raise_fall_remarks(self, value: int):
        self._raise_fall_remarks = value

    @property
    def delay_time(self):
        return self._delay_time

    @delay_time.setter
    def delay_time(self, value: float):
        self._delay_time = round(value, 3)
#----------------------------------------------------------------------------