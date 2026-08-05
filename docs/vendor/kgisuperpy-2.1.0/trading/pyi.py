from typing import Union
from ._trade_base import Action, PriceType, TimeInForce, OrderCond, OddLot ,CP

class _Order:
    def create_order(self ,
                    action :Action ,
                    symbol :str ,
                    qty :int,
                    price : Union[float ,PriceType] =None ,
                    time_in_force :TimeInForce =TimeInForce.ROD ,
                    order_cond :OrderCond= OrderCond.CASH ,
                    odd_lot: Union[bool, OddLot] = False,
                    name:str=''):
        pass

    def update_order(self ,order_id ,price=None, qty=None):
        pass

    def cancel_order(self ,order_id):
        pass
    def TSECreditInfo(self ,symbol):
        pass
    def set_event(self, event):
        pass

class _Account:
    def InventorySum(self, FType ='A'):
        pass
    def SettleAmtTrial(self , FType ='D' ):
        pass
    def Inventory(self ,FType ='SS'):
        pass
    def RealizePL(self ,FType='M' ,StartDate=None ,EndDate=None):
        pass
    def BalanceStatement(self ,FType ='M' ,StartDate=None ,EndDate=None ):
        pass

class _FutOrder:
    def create_order(self ,
                     action :Action,
                     symbol :str,
                     qty :int ,
                     price :float,
                     time_in_force :TimeInForce =TimeInForce.ROD):
        pass
    def update_order(self ,order_id ,price=None, qty=None):
        pass
    def cancel_order(self ,order_id):
        pass
    def set_event(self, event):
        pass

class _FutAccount:
    pass

class _SubOrder:
    def create_order(self ,
                     action :Action ,
                     symbol :str,
                     qty :int,
                     price :float ,
                     name:str=''):
        pass
    def update_order(self ,org_seq ,price=None, qty=None):
        pass
    def cancel_order(self ,org_seq):
        pass
    def set_event(self, event):
        pass

class _SubAccount:
    def PositionDetailReport(self , currency=None ):
        pass

class _IntFutOrder:
    def create_order(self ,
                    action:Action ,
                    exchange:str ,
                    com_id:str, 
                    cp:CP,
                    com_ym :str,
                    strike_price ='' ,
                    qty :int =None,
                    price : Union[float ,PriceType] =None ,
                    stop_price ='',
                    time_in_force=TimeInForce.ROD ):
        pass
    def cancel_order(self ,order_id):
        pass
    def update_order(self ,order_id ,price=None, qty=None):
        pass

class _IntFutAccount:
    def __init__(self):
        pass