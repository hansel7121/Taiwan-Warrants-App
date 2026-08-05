import enum
from enum import Enum 
from dataclasses import dataclass , field
from typing import Optional, List
import pandas as pd

class Action(Enum):
    Buy = "B"
    Sell = "S"

@dataclass(frozen=True)
class Symbol:
    action :Action
    com_id: Optional[str] = None
    cp: Optional[str] = None
    com_ym :Optional[str] = None
    strike_price: Optional[float] = None

class TimeInForce(Enum):
    ROD =  0
    IOC =  1
    FOK =  2

class PriceType(Enum):
    LMT= 0
    MKT= '1' 
    LimitDown = 1
    LimitUp = 9
    Reference = 5

    StopLossMarket ='2'
    StopLossLimit ='3'
    RangeMarket='4'

    def __repr__(self):
        return f"<{self.__class__.__name__}.{self.name}>"
    
class CP(Enum):
    CallOption ='C'
    PutOption ='P'
    Futures ='F'

    def __repr__(self):
        return f"<{self.__class__.__name__}.{self.name}>"

class OddLot(Enum):
    Common = 0            #整股
    Fixing =2             #盤後定價整股
    Odd_AfterMarket =1    #盤後零股
    Odd = 4               #零股

class OrderCond(Enum):
    CASH =0
    MARGIN =3
    SHORT_SELLING =4
    Lend_SELLING = 6
    MARGIN_DayTrade = 7
    SHORT_DayTrade = 8
    CASH_SELLING = 9 

class Category(Enum):
    FUTURE = 0
    OPTION = 1

class TradeHour(Enum):
    REGULAR = 'R'
    POSTMARKET  = 'P'

class Market(Enum):
    HK = 0
    US = 1
    SH = 2
    JP = 3

class Currency(Enum):
    TWD =0
    MUT =1

class Task(Enum):
    NewOrder = "NewOrder"
    CancelOrder = "CancelOrder"
    UpdatePrice = "UpdatePrice"
    UpdateQty = "UpdateQty"
    UpdateOrder = "UpdateOrder"
    
    Deal ='Deal'
    PendingNewOrder = "Pending_NewOrder"
    PendingCancelOrder = "Pending_CancelOrder"
    PendingUpdateOrder = "Pending_UpdateOrder"
    
    def __repr__(self):
        return f"<{self.__class__.__name__}.{self.name}>"
    
class Status( Enum):
    Pending = "Pending"
    Success ="Success"
    Failed = "Failed"

    PendingSubmitted = "Pending_Submitted"
    Submitted = "Submitted"
    Filled = "Filled"
    Cancelled = "Cancelled"
    PartFilled = "PartFilled"
    PartFilled_Cancelled = "PartFilled_Cancelled"

    def __repr__(self):
        return f"<{self.__class__.__name__}.{self.name}>"

@dataclass
class BaseEven:
    task : any =None
    status : any = None
    org_seqnum :str =None
    order_id:str=None
    seqno :str =None
    status:any=None
    action:Action=None
    category : str = None
    market:any =None
    symbol:str =None
    symbol2:str =None
    quantity: int =None
    price: float =None
    price_type: PriceType =None
    stop_price :any =None
    order_cond:OrderCond=None
    time_in_force :TimeInForce=None
    odd_lot:OddLot=None
    trade_hour :any =None
    ts: str =None
    reportseq:any = None
    msg : str =None

@dataclass
class NewOrder(BaseEven):
    def __repr__(self):
        ordered_keys = list(self.__annotations__.keys())
        fields = [
            f"{k}={repr(getattr(self, k))}"
            for k in ordered_keys
            if getattr(self, k) is not None
        ]
        return f"{self.__class__.__name__}({', '.join(fields)})"
    
@dataclass
class UpdateOrder(BaseEven):
    def __repr__(self):
        ordered_keys = list(self.__annotations__.keys())
        fields = [
            f"{k}={repr(getattr(self, k))}"
            for k in ordered_keys
            if getattr(self, k) is not None
        ]
        return f"{self.__class__.__name__}({', '.join(fields)})"
    
@dataclass
class CancelOrder(BaseEven):
    def __repr__(self):
        ordered_keys = list(self.__annotations__.keys())
        fields = [
            f"{k}={repr(getattr(self, k))}"
            for k in ordered_keys
            if getattr(self, k) is not None
        ]
        return f"{self.__class__.__name__}({', '.join(fields)})"

@dataclass
class Deal(BaseEven):
    def __repr__(self):
        ordered_keys = list(self.__annotations__.keys())
        fields = [
            f"{k}={repr(getattr(self, k))}"
            for k in ordered_keys
            if getattr(self, k) is not None
        ]
        return f"{self.__class__.__name__}({', '.join(fields)})"
    
@dataclass
class Order():
    """
    委託單資訊（Order）

    描述一筆使用者送出的原始委託單內容。此類別為交易核心資料結構之一，包含所有下單時必要參數，
    在委託成功或失敗時不變。

    常由用戶下單輸入或交易系統自動產生，用於追蹤原始交易意圖。

    屬性說明：
    ----------
    org_seqnum : str
        原始委託序號，可用於與外部系統對應或追溯來源。

    order_id : str
        訂單代碼，由券商或交易所系統回傳，用於後續查詢、改單、取消。

    action : Action
        委託方向，為買（Buy）或賣（Sell）。

    category : Category
        商品類型，例如期貨或選擇權。

    market : Market
        市場代號，例如美股、港股、台股（未定義詳細格式但預期為 Market Enum）。

    symbol : str
        商品代碼，例如股票代碼（如 '2330'）或期貨商品碼。

    quantity : int
        委託數量（股數、口數等），實際下單之數量。

    price : float | PriceType
        委託價格，可為浮點數（限價）或特殊價格型態（如市價、漲停等）。

    order_cond : OrderCond
        委託條件，例如現股、融資、融券等。

    time_in_force : TimeInForce
        委託的有效時間，例如 ROD（當日有效）、IOC（立即成交否則取消）。

    price_type : PriceType
        價格型態，例如 LMT（限價單）、MKT（市價單）等。

    odd_lot : OddLot
        零股類型，分為整股、盤後零股、定價盤後等。

    trade_hour : str
        委託時段，例如盤中、盤後，配合交易所時段分類。
    """
    org_seqnum :str =None
    order_id: str =None
    action: Action =None   
    category:any =None
    market:any =None
    symbol: str =None
    symbol2:str =None
    quantity: int =0 
    price: any = 0.0   
    price_type :PriceType=None
    stop_price :any =None
    order_type :int =None
    order_cond: any = None
    time_in_force :TimeInForce =None
    odd_lot: any =None
    trade_hour:str=None
    currency:any =None
    name :str=None

    def __repr__(self):
        # 按定義順序列出欄位
        ordered_keys = list(self.__annotations__.keys())
        fields = [
            f"{k}={repr(getattr(self, k))}"
            for k in ordered_keys
            if k != "order_type" and getattr(self, k) is not None
        ]
        return f"{self.__class__.__name__}({', '.join(fields)})"

@dataclass
class OrderStatus:
    """
    委託單狀態資訊（OrderStatus）

    記錄目前委託單的最新狀態，會隨成交、取消、失敗等事件更新，用來反映委託處理結果與成交情形。

    屬性說明：
    ----------
    nid : int
        回報交易序號，用來對應交易所或券商送出的回報順序。

    status : Status
        委託目前狀態，如 Submitted（已送出）、Filled（已成交）、PartFilled（部分成交）等。

    modified_time : str
        最後異動時間（格式為 HHMMSS），對應最新成交或狀態變更的發生時刻。

    modified_price : float
        最後成交或委託修改後的價格。

    modified_quantity : int
        最新的剩餘未成交量或已成交量。

    deals : List[Deal]
        所有成交記錄（可能多筆），每筆為一筆 Deal 回報，包含成交數量、成交價格與時間。
    """
    nid: int =None  
    status: any = None                 
    modified_time: str = ''
    modified_quantity: int = None
    modified_price: float = None
    deals: List[Deal] = field(default_factory=list)  

@dataclass
class Event(BaseEven):
    """
    訂單事件資訊 (Event)

    用於記錄交易系統中某次操作的事件資訊，例如：下單、成交、委託失敗等。

    屬性：
    ----------
    task : Task
        任務類型，例如下單、新單、成交等 (e.g., Task.NewOrder, Task.Deal)
    
    status : Optional[Status]
        執行狀態，例如成功、失敗，部分事件如成交不一定有 (e.g., Status.Success, Status.Failed)

    order_id : str
        訂單代碼 (Order ID)

    action : Action
        委託動作，買入或賣出 (e.g., Action.Buy, Action.Sell)

    symbol : str
        股票代碼 (Stock ID)

    quantity : int
        委託數量或成交數量

    price : float | PriceType
        價格，可為市價單 (PriceType.MKT) 或限價浮點數

    order_cond : Optional[OrderCond]
        委託條件，例如現股、融資、融券，僅委託回報會有

    time_in_force : Optional[TimeInForce]
        委託有效時間，例如 ROD、IOC 等，僅委託回報會有

    odd_lot : OddLot
        零股註記 (0 = 整股, 1 = 零股)

    ts : Optional[str]
        事件時間，格式為 HHMMSS 的字串（例如 '112116' 表示上午 11 點 21 分 16 秒）。

    msg : Optional[str]
        錯誤訊息（僅在委託失敗時出現）
    """
    def __repr__(self):
        ordered_keys = list(self.__annotations__.keys())
        fields = [
            f"{k}={repr(getattr(self, k))}"
            for k in ordered_keys
            if getattr(self, k) is not None
        ]
        return f"{self.__class__.__name__}({', '.join(fields)})"

@dataclass
class Operation():
    """
    操作紀錄（Operation）

    用於紀錄使用者或系統對委託單所進行的操作，包含送單、改單、取消等行為，以及對應的處理狀態。

    每筆操作代表一個具體的下單行為或回報歷程。

    屬性說明：
    ----------
    nid : any
        操作或回報序號，可為券商系統提供的唯一流水號。

    task : Task
        此操作的類型，例如 Task.NewOrder（新單）、Task.CancelOrder（取消）、Task.UpdateOrder（改單）等。

    status : Status
        執行結果狀態，成功（Success）、失敗（Failed）、尚未處理（Pending）等。

    op_time : str
        操作發生時間，格式為 HHMMSS 的字串。

    status_code : str
        錯誤或狀態碼，便於與交易所或券商訊息對應。

    msg : str
        回報訊息內容，通常為錯誤說明或狀態補充文字（如：'MAT0024: 價格超出漲跌範圍'）。
    """
    nid: any = None
    task: Task =Task.NewOrder
    status: any = None
    op_time: str = None
    status_code: str = None
    msg: str = None

    def __repr__(self):
        # 按定義順序列出欄位
        ordered_keys = list(self.__annotations__.keys())
        fields = [
            f"{k}={repr(getattr(self, k))}"
            for k in ordered_keys
            if getattr(self, k) is not None
        ]
        return f"{self.__class__.__name__}({', '.join(fields)})"

@dataclass
class Trade():
    """
    表示一筆完整的交易流程，包含委託資訊、狀態回報與操作記錄。

    屬性：
    - order：Order 物件，代表使用者送出的原始委託單。
    - order_status：OrderStatus 物件，紀錄該委託單的成交結果，例如最新待成交數量、成交價格、狀態等。
    - operations：List[Operation]，追蹤該交易的所有操作紀錄（如送單、改價、改量、取消），每筆操作會包含時間戳與操作類型。

    用途：
    - 可作為交易狀態管理的核心資料單位。
    - 支援追蹤每筆訂單的歷史操作與成交情況。
    """
    order: any = None
    order_status: any = None
    operations: any = None

    def __str__(self):
        from pprint import pformat
        return pformat({
            "order": self.order,
            "order_status": self.order_status,
            "operations": self.operations
        }, indent=2, width=80)

@dataclass
class Trade():
    """
    表示一筆完整的交易流程，包含委託資訊、狀態回報與操作記錄。
    """
    order: any = None
    order_status: any = None
    operations: any = None

    def __repr__(self):
        # 輔助函式：將文字每一行前面補上 4 個空格（用於階層縮進）
        def indent_lines(text, spaces=4):
            return "\n".join(f"{' ' * spaces}{line}" for line in text.splitlines()).strip()

        # 輔助函式：動態拆解 Symbol 物件，過濾 None
        def repr_symbol(sym):
            if not sym or isinstance(sym, str): return repr(sym)
            sym_keys = list(sym.__annotations__.keys())
            sym_fields = [
                f"{k}={repr(getattr(sym, k))}"
                for k in sym_keys
                if getattr(sym, k) is not None
            ]
            return f"Symbol({', '.join(sym_fields)})"

        # 輔助函式：處理陣列型態（operations）的漂亮縮進
        def repr_list(lst, spaces=8):
            if not lst: return "[]"
            lines = [f"{' ' * spaces}{repr(item)}," for item in lst]
            lines[-1] = lines[-1][:-1] # 拔掉最後一行的逗號
            return "[\n" + "\n".join(lines) + f"\n{' ' * (spaces-4)}]"

        # 1. 動態深度拆解 order (不顯示 None)
        if self.order:
            o = self.order
            ordered_keys = list(o.__annotations__.keys())
            order_fields = []
            
            for k in ordered_keys:
                if k != "order_type" and getattr(o, k) is not None:
                    val = getattr(o, k)
                    if k in ('symbol', 'symbol2') and hasattr(val, '__annotations__'):
                        order_fields.append(f"    {k}={repr_symbol(val)}")
                    else:
                        order_fields.append(f"    {k}={repr(val)}")
            
            order_str = "Order(\n" + ",\n".join(order_fields) + ")"
        else:
            order_str = "None"

        # 2. 深度拆解 order_status (動態過濾 Deal 內部的 None 值)
        if self.order_status:
            os = self.order_status
            
            formatted_deals = []
            for d in os.deals:
                # 動態撈取 Deal 物件當前非 None 的所有屬性
                d_keys = list(d.__annotations__.keys()) if hasattr(d, '__annotations__') else d.__dict__.keys()
                deal_fields = []
                
                for dk in d_keys:
                    d_val = getattr(d, dk, None)
                    if d_val is not None:
                        # 如果 Deal 裡面有 symbol 且不為 None，走特殊美化
                        if dk == 'symbol' and hasattr(d_val, '__annotations__'):
                            deal_fields.append(f"symbol={repr_symbol(d_val)}")
                        else:
                            deal_fields.append(f"{dk}={repr(d_val)}")
                
                # 組裝單一筆 Deal 字串
                deal_str = f"Deal({', '.join(deal_fields)})"
                formatted_deals.append(deal_str)
                
            if formatted_deals:
                deals_lines = [f"            {d}," for d in formatted_deals]
                deals_lines[-1] = deals_lines[-1][:-1] # 拔掉最後一個逗號
                deals_str = "[\n" + "\n".join(deals_lines) + "\n        ]"
            else:
                deals_str = "[]"

            status_str = (
                f"OrderStatus(\n"
                f"    nid={repr(os.nid)},\n"
                f"    status={repr(os.status)},\n"
                f"    modified_time={repr(os.modified_time)},\n"
                f"    modified_quantity={repr(os.modified_quantity)},\n"
                f"    modified_price={repr(os.modified_price)},\n"
                f"    deals={deals_str})"
            )
        else:
            status_str = "None"

        # 3. 處理 operations 陣列
        ops_str = repr_list(self.operations, spaces=8) if self.operations else "[]"

        # 最終結構化輸出
        return (
            f"Trade(\n"
            f"    order={indent_lines(order_str, 4)},\n"
            f"    order_status={indent_lines(status_str, 4)},\n"
            f"    operations={ops_str})"
        )