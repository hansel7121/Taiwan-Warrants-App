from .main import login
from .trading._trade_base import (
    Action, TimeInForce, PriceType, OddLot, OrderCond, Market ,Status ,Task ,CP ,Currency ,Symbol 
)
from .marketdata.quote_starwave import SWMarketData
from .Quote_sw import SWEvent
from .Quote import QuoteData ,Event

_attrs_to_del = ['CA', 'main', 'contract', 'data', 'marketdata', 'msmp', 'usmsmp','pyTradeCom', 'stomp_ws', 'Quote', 
                 'trading','msmp','attr','pushClient','log','url','zip','backtest_plus','backtest']
for attr in _attrs_to_del:
    if attr in globals():
        del globals()[attr]
     