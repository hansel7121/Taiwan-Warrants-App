from enum import Enum

class ContractType(str, Enum):
    Index = "IND"
    Stock = "STK"
    Future = "FUT"
    Option = "OPT"
    ForeignStock = "FSTK"
    ForeignFuture = "FFUT"

class OptionCallPut(str, Enum):
    Call = "C"
    Put = "P"

class Contract:
    contract_type: str
    market: str
    exchange: str
    symbol: str
    name : str
    category: str
    update_date: str

class Stock(Contract):
    contract_type = ContractType.Stock
    limit_up: float
    limit_down: float
    reference: float
    yesterday_total_match_qty: int
    yesterday_today_ref_price: float
    pre_close_orice: float
    update_date: str
    pre_total_trading_amount: float
    margin_trading_balance: int
    short_selling_balance: int
    margin_trading_rate: int
    short_selling_rate: int
    day_trade: bool

class Index(Contract):
    contract_type = ContractType.Index

class ForeignStock(Contract):
    contract_type = ContractType.ForeignStock
    reference: float
    yesterday_total_match_qty: int
    yesterday_today_ref_price: float
    pre_close_orice: float
    update_date: str
    pre_total_trading_amount: float
    margin_trading_balance: int
    short_selling_balance: int
    margin_trading_rate: int
    short_selling_rate: int

class Future(Contract):
    contract_type = ContractType.Future
    contract_month: str
    contract_date: str 
    limit_up: float
    limit_down: float
    underlying_kind: str
    reference: float

class Option(Contract):
    contract_type = ContractType.Option
    contract_month: str
    contract_date: str 
    strike_preice: str
    call_put: str
    limit_up: float
    limit_down: float
    underlying_kind: str
    reference: float