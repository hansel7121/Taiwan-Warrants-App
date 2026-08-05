import json ,copy
import pandas as pd
import numpy as np
from ._trade_base import *
import time ,datetime
from typing import Union


_TIF={'R':TimeInForce.ROD,
      'F':TimeInForce.FOK,
      'I':TimeInForce.IOC}

_PriceType={'1':PriceType.MKT,
            '2':PriceType.LMT,}

def _str_to_num(s):
    try:
        number = float(s)
        if number == int(number):
            number = int(number)
    except:
        number = s

    return number

def event(data):
    print(data)

def _Odd(trade):
    BS= trade.order.action.value
    if trade.order.odd_lot in [OddLot.Odd_AfterMarket ,OddLot.Odd]:
        symbol = trade.order.symbol+'.Odd_'+BS
    else:
        symbol = trade.order.symbol+'_'+BS
    return symbol

class OrderAPI:
    def __init__(self , ObjOrder ,broker_id , account):
        self._Order = ObjOrder
        self._broker_id = broker_id
        self._account = account
        self._trades = {'無效單':[]}#{}
        self._deals ={}
        self._rid ={} #rid :Trade()
        self._cnt={}  #cnt :rid
        self._name={}
        self._p=[]
        self._px= []
        self._rp =[]
        self._rpx=[]
        self._1d =[]
        self._3d=[]
        self._s =[]
        self._Credit=[]
        self._order_report=[]
        self._exec_report=[]
        self.set_event(event)

    def create_order(self ,
                     action :Action ,
                     symbol :str ,
                     qty :int,
                     price : Union[float ,PriceType] =None ,
                     time_in_force :TimeInForce =TimeInForce.ROD ,
                     order_cond :OrderCond= OrderCond.CASH ,
                     odd_lot: Union[bool, OddLot] = False,
                     name:str=''):
        """

        建立一筆委託單（市價單或限價單，現股、融資、融券、零股等皆可）。

        Parameters
        ----------
        action : kgi.Action 
            買進或賣出動作
            -Action.Buy  :買
            -Action.Sell :賣
            
        symbol : str
            股票代碼（如 "2330"）
            
        qty : int
            委託張數 or 股數
            
        price : float or kgi.PriceType
            委託價格，可為數值（限價單）或使用特定價格類型 PriceType
            預設為 None，方向多為PriceType.LimitUp、方向空為PriceType.LimitDown
            -PriceType.MKT  市價
            -PriceType.Reference  平盤價
            -PriceType.LimitUp  漲停價
            -PriceType.LimitDown  跌停價
            
        time_in_force : kgi.TimeInForce
            委託有效時間，預設為 TimeInForce.ROD
            -TimeInForce.IOC  立刻成交否則取消
            -TimeInForce.ROD  當天有效
            -TimeInForce.FOK  立刻成交，并且全部成交，否則取消

        order_cond : kgi.OrderCond
            委託條件，預設為 OrderCond.CASH
            -OrderCond.CASH  現股
            -OrderCond.CASH_SELLING 現股先賣
            -OrderCond.MARGIN  融資
            -OrderCond.MARGIN_DayTrade  當沖融資
            -OrderCond.SHORT_SELLING  融券
            -OrderCond.SHORT_SELLING  當沖融券
            -OrderCond.Lend_SELLING  借券

        odd_lot : bool or kgi.OddLot 
            是否為零股單，傳入bool時 會根據當前時段自動轉換為OddLot ,也可直接指定OddLot
            -bool：True 代表零股、False 代表整股     預設為 False（整股）
            -OddLot.Common  整股
            -OddLot.Fixing  盤後整股
            -OddLot.Odd  零股
            -OddLot.Odd_AfterMarket  盤後零股

        Returns 
        ----------
            Trade物件
            
        Notes
        ----------
            OrderResponse 回傳委託結果資料，常見代碼有 pending 6002、order 4010 、deal 4011。
            此類回傳訊息發生於下單委託時（NewOrder）及發生成交時（Deal）。
        """
        
        if price == PriceType.LMT:
            raise ValueError("如欲設定限價，請直接輸入價格") 
        if price in [PriceType.StopLossMarket ,PriceType.RangeMarket ]:
            raise ValueError("現股PriceType僅支持 MKT、LimitUp、LimitDown、Reference，或直接輸入價格 ") 
        if price is None:
            if action == Action.Buy : price = PriceType.LimitUp
            if action == Action.Sell : price = PriceType.LimitDown
        if order_cond==OrderCond.CASH_SELLING and action==Action.Buy:
            raise ValueError("action 和order_cond發生衝突") 

        BrokerId = self._broker_id
        Account = self._account
        Symbol = symbol
        Side = action.value
        Qty = qty
        OrderNo = ''
        Tif = time_in_force
        OrderClass = order_cond
        
        if price == PriceType.MKT:
            Price =0
            OrderType = 2
            pricetype = PriceType.MKT
        elif price in [PriceType.LimitDown ,PriceType.LimitUp ,PriceType.Reference]:
            Price =0
            OrderType = price.value
            pricetype = price
        elif isinstance(price, (float, int)):
            Price = price
            OrderType = PriceType.LMT.value
            pricetype = PriceType.LMT
        if Price == int(Price): 
            Price = int(Price)    

        now = datetime.datetime.utcnow()+ datetime.timedelta(hours=8)
        if odd_lot==False :
            if  datetime.time(14, 00) < datetime.time(now.hour, now.minute) and \
                datetime.time(now.hour, now.minute) < datetime.time(14, 30) :
                odd_lot= OddLot.Fixing
            else:
                odd_lot = OddLot.Common
        elif odd_lot==True:
            if datetime.time(13, 40) < datetime.time(now.hour, now.minute) and \
                datetime.time(now.hour, now.minute) < datetime.time(14, 30):
                odd_lot = OddLot.Odd_AfterMarket
            else:
                odd_lot = OddLot.Odd

        RequestId = self._Order.RetriveRequestID()
        order = Order(
            action = action ,
            symbol = symbol ,
            quantity= int(Qty),
            price= float(Price),
            order_cond= OrderClass ,
            time_in_force = Tif ,
            price_type = pricetype ,
            odd_lot= odd_lot ,
            name = None if name=='' else name )
        order.order_type  =OrderType
        order_status = OrderStatus()
        operation = Operation(
            nid =RequestId ,
            task=Task.NewOrder,
            status= Status.Pending,
            op_time= datetime.datetime.now().strftime('%H%M%S') )
        
        trade = Trade()
        trade.order = order
        trade.order_status= order_status
        trade.operations = [operation]

        self._rid[str(RequestId)] = trade

        self._Order.SetStrategyName(name)
        rt = self._Order.SecurityOrder(RequestId,
                                    int(0),
                                    int(odd_lot.value),
                                    int(OrderClass.value),
                                    BrokerId,
                                    Account,
                                    Symbol,
                                    Side,
                                    int(Qty),
                                    str(Price),
                                    int(OrderType),
                                    '',
                                    '',
                                    OrderNo,
                                    int(Tif.value))
        
        if rt != 0:
            rt = rt -2**64
            print(f'SecurityOrder return {rt}, {self._Order.GetOrderErrMsg(rt)}')
        else:
            return trade
        
    def update_order(self ,order_id ,price=None, qty=None):
        """

        修改一筆委託單的 數量或價格 ,需注意僅能填寫一種。

        Parameters
        ----------
        order_id : str
            委托書號
        price : float or int
            新價格
        qty : int
            預減少的數量

        Returns 
        ----------
            預想要改單的Trade物件
            
        Notes
        ----------
            OrderResponse回傳委託結果資料，6002、4010
            此類回傳訊息發生於改單委託更新時（UpdateOrder）。
        """
        if isinstance(order_id, Trade):
            order_id = order_id.order.order_id
        if order_id in self.get_trades().keys():
            trade = self._trades[order_id]
        else:
            raise ValueError("查無此order_id，或已失效")   

        if (price != None)&(qty != None):
            raise ValueError("price和qty僅能輸入一個")     
        if (price == None)&(qty == None):
            raise ValueError("price和qty需輸入一個")  
        if price is not None and not isinstance(price, (float, int)):
            raise ValueError("價格需為數字且不得為零")
        if trade.order.price_type == PriceType.MKT and price is not None:
            raise ValueError("市價單不得改價")

        if price is None: #改量
            Price = 0
            Action = 2
        elif qty is None: #改價
            Price = price
            qty= 1
            Action = 3 
            if Price == int(Price): 
                Price = int(Price) 

        BrokerId = self._broker_id
        Account = self._account
        OrderLot = trade.order.odd_lot.value
        OrderClass = trade.order.order_cond.value
        Symbol = trade.order.symbol
        Side = trade.order.action.value
        pricetype = trade.order.price_type
        OrderType = 2 if pricetype==PriceType.MKT else pricetype.value
        Qty = qty
        OrderNo = order_id
        Tif = trade.order.time_in_force.value

        RequestId = self._Order.RetriveRequestID()
        if Action==2: #改量
            value= trade.order_status.modified_quantity - int(Qty)
            task = Task.UpdateQty
            msg = f"NewQty_{value}"

        elif Action==3: #改價
            value = Price
            task = Task.UpdatePrice
            msg= f"NewPrice_{value}"

        operation = Operation(
            nid =RequestId ,
            task = task ,
            status= Status.Pending ,
            msg = msg,
            op_time= datetime.datetime.now().strftime('%H%M%S') )
        trade.operations.append(operation)
        self._rid[str(RequestId)] = trade

        rt = self._Order.SecurityOrder(RequestId,
                                    int(Action),
                                    int(OrderLot),
                                    int(OrderClass),
                                    BrokerId,
                                    Account,
                                    Symbol,
                                    Side,
                                    int(Qty),
                                    str(Price),
                                    int(OrderType),
                                    '',
                                    '',
                                    OrderNo,
                                    int(Tif))
        
        if rt != 0:
            rt = rt -2**64
            print(f'SecurityOrder return {rt}, {self._Order.GetOrderErrMsg(rt)}')
        else:
            return trade
        
    def cancel_order(self ,order_id):
        """

        刪除一筆還未失效的委託單。

        Parameters
        ----------
        order_id : str
            委托書號

        Returns 
        ----------
            預想刪單的Trade物件
            
        Notes
        ----------
            OrderResponse回傳委託結果資料，6002、4010
            此類回傳訊息發生於刪單委託更新時（UpdateOrder）。
        """
        if isinstance(order_id, Trade):
            order_id = order_id.order.order_id
        if order_id in self.get_trades().keys():
            trade = self._trades[order_id]
        else:
            raise ValueError("查無此order_id，或已失效") 
        
        trade = self._trades[order_id]

        BrokerId = self._broker_id
        Account = self._account
        Action = 1
        OrderLot = trade.order.odd_lot.value
        OrderClass = trade.order.order_cond.value
        Symbol = trade.order.symbol
        Side = trade.order.action.value
        Qty = trade.order.quantity
        Price = trade.order.price
        pricetype = trade.order.price_type
        OrderType = 2 if pricetype==PriceType.MKT else pricetype.value
        OrderNo = order_id

        Tif = trade.order.time_in_force.value
        if Price == int(Price): 
            Price = int(Price)  

        RequestId = self._Order.RetriveRequestID()
        operation = Operation(
            nid =RequestId ,
            task =Task.CancelOrder ,
            status= Status.Pending,
            op_time= datetime.datetime.now().strftime('%H%M%S') )
        trade.operations.append(operation)
        self._rid[str(RequestId)] = trade

        rt = self._Order.SecurityOrder(RequestId,
                                    int(Action),
                                    int(OrderLot),
                                    int(OrderClass),
                                    BrokerId,
                                    Account,
                                    Symbol,
                                    Side,
                                    int(Qty),
                                    str(Price),
                                    int(OrderType),
                                    '',
                                    '',
                                    OrderNo,
                                    int(Tif))
        
        if rt != 0:
            rt = rt -2**64
            print(f'SecurityOrder return {rt}, {self._Order.GetOrderErrMsg(rt)}')
        else:
            return trade
        
    def cancel_order_all(self):
        """

        取消所有尚未失效的委託單。

        此方法會自動取得目前所有委託單，並逐一呼叫 `cancel_order` 進行取消。

        Returns
        -------
        None
            無回傳值。
        """
        T_dic = self.get_trades()
        sub_oid = list(T_dic.keys())

        for oid in sub_oid:
            self.cancel_order(oid)
        return 

    def set_event(self, event):
        """

        設定事件回調函數（callback）。

        Parameters
        ----------
        event : callable
            接收一個資料物件（如委託或成交回報）的函數。
            若未設定，預設為以下函數：

            >>> def event(data):
            ...     print(data)

            `data` 可能是以下五種類型的事件資料物件：

            NewOrder 、UpdatePrice 、UpdateQty 、CancelOrder 、 Deal
            這五種事件均包含以下欄位：
            - seqno
            - action
            - symbol
            - quantity
            - price
            - order_cond
            - time_in_force
            - odd_lot
            - ts

        Returns
        -------
        None
        """
        self._event = event 

    def _OnOrderPending(self,data):
        #6002 4002
        rid = data['RequestId']
        if rid not in self._rid.keys():
            print(f'[系統訊息] 查無源頭 Order: {data}')
            return 
        
        self._cnt[data['CNT']] =rid

        #拿檔  ==============================================================
        trade = self._rid[rid] 

        operation = next((x for x in trade.operations if str(x.nid).strip() == str(rid).strip()), None)
        if operation is None:
            operation = next((x for x in trade.operations if str(x.nid).strip() == str(data['CNT']).strip()), None)
        if operation is None:
            operation = Operation()
            trade.operations.append(operation)

        #data  ==============================================================
        ts = datetime.datetime.now().strftime('%H%M%S')
        msg = operation.msg
        
        operation.nid = data['CNT']
        operation.ts = ts

        #event & operation  ==================================================
        event= Event( 
            task = operation.task ,
            status = Status.Pending ,
            order_id= trade.order.order_id,
            seqno = data['CNT'],
            action= trade.order.action,
            symbol =trade.order.symbol,
            order_cond =trade.order.order_cond,
            time_in_force =trade.order.time_in_force,
            odd_lot =trade.order.odd_lot,
            ts =ts )
        
        if operation.task ==Task.NewOrder:
            event.price = trade.order.price
            event.quantity =trade.order.quantity

        elif operation.task ==Task.UpdateQty:
            value = operation.msg.split("_")[1]
            event.quantity = _str_to_num(value)
            event.price = trade.order_status.modified_price or trade.order.price

        elif operation.task ==Task.UpdatePrice:
            value = operation.msg.split("_")[1]
            event.quantity = trade.order_status.modified_quantity or trade.order.quantity
            event.price = _str_to_num(value)

        elif operation.task==Task.CancelOrder:
            event.quantity = 0
            event.price = trade.order_status.modified_price or trade.order.price

        if data['ErrorCode'] != '0':
            num = "".join(filter(str.isdigit, data['ErrorCode']))
            mg = self._Order.errorMap.get(num, "")
            msg = mg if not msg else f"{msg}|{mg}"
            event.status =Status.Failed
            operation.status = Status.Failed

        event.msg =msg
        operation.msg =msg
        
        if event.price==0:
            order_type = trade.order.order_type
            if order_type ==1:
                event.price = '跌停價'
            elif order_type ==9:
                event.price = '漲停價'     
            elif order_type ==5:
                event.price = '平盤價'    
            else:
                event.price = '市價'   

        self._event(event)
        
    def _OnOrderName(self,data):
        if data['Market']=='S' and data['Account']==self._account and data['BrokerID']==self._broker_id:
            self._name[data['Cnt']] =data['ExeName']

    def _OnOrderReport(self,data):
        # 4010
        self._order_report.append(data)

        #《拿檔》  
        rid=0
        if data['CNTN'] in self._cnt.keys():
            rid = self._cnt[data['CNTN']]
            trade = self._rid[rid] 
        elif data['OrderNo'] in self._trades.keys():
            trade = self._trades[data['OrderNo']]
        elif data['OrderFunc']=='I': 
            trade= Trade(order=Order(),order_status=OrderStatus(),operations=[])
        else:
            print(f'[系統訊息] 查無源頭 Order: {data}')
            return 
        
        if data['CNT'] in self._name.keys():
            name =self._name[data['CNT']]
        else:
            name =None

        if data['OrderNo']!='' and data['OrderNo'] not in self._trades.keys():
            if data['ErrCode'] != '0':
                ls = self._trades['無效單']
                ls.append(trade)
            else:
                self._trades[data['OrderNo']] =trade

        operation = next((x for x in trade.operations if str(x.nid).strip() == str(rid).strip()), None)
        if operation is None:
            operation = next((x for x in trade.operations if str(x.nid).strip() == str(data['CNTN']).strip()), None)
        if operation is None:
            operation = next((x for x in trade.operations if str(x.nid).strip() == str(data['CNT']).strip()), None)
        if operation is None:
            operation = Operation()
            trade.operations.append(operation)

        operation.nid = data['CNT']
        operation.op_time=data['ReportTime']

        #《data》  
        side = data['Side']
        if side in {a.value for a in Action}:
            side = Action(side)

        afterqty = _str_to_num(data['AfterQty'])

        price_type = data['PriceFlagN']
        if price_type in _PriceType.keys():
            price_type = _PriceType[ price_type]

        price = 0 if price_type is PriceType.MKT  else data['Price']
        price = _str_to_num(price)

        order_cond  =  _str_to_num(data['OrdClass'])
        if order_cond in {a.value for a in OrderCond}:
            order_cond = OrderCond(order_cond)

        time_in_force = data['TimeInForceN']
        if time_in_force in _TIF.keys():
            time_in_force = _TIF[time_in_force]

        odd_lot = _str_to_num(data['OrdLot'])
        if odd_lot in {a.value for a in OddLot}:
            odd_lot = OddLot(odd_lot)

        msg =None
        if data['OrderFunc']=='C':
            msg = f"NewQty_{afterqty}"

        elif data['OrderFunc']=='M' :
            msg = f"NewPrice_{price}"
            
        if data['ErrCode'] != '0':
            num = "".join(filter(str.isdigit, data['ErrCode']))
            mg = self._Order.errorMap.get(num, "") + f"|{data['ErrMsg']}"
            msg = mg if not msg else f"{msg}|{mg}"

        #《order》
        if data['OrderFunc']=='I': 
            order = Order(
                order_id =data['OrderNo'],
                action = side ,
                symbol = data['StockID'] ,
                quantity= afterqty ,
                price= price  ,
                order_cond= order_cond ,
                time_in_force = time_in_force,
                price_type = price_type ,
                odd_lot= odd_lot ,
                name =name )
            trade.order =order

        #《order_status》
        if data['ErrCode'] != '0':
            pass
        
        elif data['OrderFunc']=='I':
            order_status = OrderStatus(
                nid=data['CNT'],
                status=Status.Submitted ,
                modified_quantity= afterqty ,
                modified_price=  price ,
                modified_time= data['ReportTime'] )
            trade.order_status = order_status

        elif data['OrderFunc']=='D':
            trade.order_status.modified_quantity =0
            trade.order_status.nid = data['CNT']
            trade.order_status.modified_time= data['ReportTime']

        elif data['OrderFunc']=='C' :#改量
            trade.order_status.modified_quantity = afterqty
            trade.order_status.nid = data['CNT']
            trade.order_status.modified_time= data['ReportTime']

        elif data['OrderFunc']=='M' : #改價
            trade.order_status.modified_price = price
            trade.order_status.nid = data['CNT']
            trade.order_status.modified_time= data['ReportTime']

        if trade.order_status.modified_quantity ==0 :
            if trade.order_status.status in [Status.Filled ,Status.PartFilled_Cancelled]:
                pass
            elif trade.order_status.status == Status.PartFilled:
                trade.order_status.status = Status.PartFilled_Cancelled
            else:
                trade.order_status.status = Status.Cancelled

        if trade.order_status.modified_price==0:
            trade.order_status.modified_price ='市價'

        #《operation》
        operation.msg = msg
        operation.status = Status.Success

        if data['OrderFunc']=='I': 
            operation.task = Task.NewOrder

        elif data['OrderFunc']=='D':
            operation.task=Task.CancelOrder

        elif data['OrderFunc']=='C' : #改量
            operation.task=Task.UpdateQty

        elif data['OrderFunc']=='M' : #改價
            operation.task=Task.UpdatePrice

        if data['ErrCode'] != '0':
            operation.status = Status.Failed

        #《event》
        event = Event(
                status = operation.status,
                task = operation.task,
                order_id = data['OrderNo'],
                seqno = data['CNT'] ,
                action = trade.order.action ,
                symbol = trade.order.symbol ,
                quantity= afterqty ,
                price= price,
                order_cond = trade.order.order_cond ,
                time_in_force= trade.order.time_in_force ,
                odd_lot= trade.order.odd_lot ,
                msg= msg ,
                ts = data['ReportTime'] ,)
        
        if event.price==0:
            event.price = '市價'   

        if self._show is True:
            self._event(event)

    def _OnExecReport(self,data):
        self._exec_report.append(data)

        #data  ==============================================================
        symbol = data['StockID']

        FillQty = _str_to_num(data['DealQty'])

        price = _str_to_num(data['Price'])

        side = data['Side']
        if side in {a.value for a in Action}:
            side = Action(side)

        time_in_force = data['TimeInForceN']
        if time_in_force in _TIF.keys():
            time_in_force = _TIF[time_in_force]

        ts = data['ReportTime']

        order_cond  =  _str_to_num(data['OrdClass'])
        if order_cond in {a.value for a in OrderCond}:
            order_cond = OrderCond(order_cond)

        odd_lot = _str_to_num(data['OrdLot'])
        if odd_lot in {a.value for a in OddLot}:
            odd_lot = OddLot(odd_lot)

        #deal  ==============================================================
        trade = self._trades.get(data['OrderNo'])

        if trade is None:
            print(f'[系統訊息] 查無源頭 Order: {data}')

            event =Event(
                task = Task.Deal ,
                order_id =data['OrderNo'],
                seqno = data['CNT'],
                action= side ,
                symbol = symbol ,
                quantity = FillQty ,
                price = price,
                time_in_force = time_in_force ,
                order_cond = order_cond ,
                odd_lot = odd_lot,
                ts = ts
                )
        else:
            deal =Deal( 
                quantity=FillQty,
                price=price ,
                ts= ts)
                #reportseq=data['CNT'])
            trade.order_status.deals.append(deal)
            
            order_qty =  trade.order_status.modified_quantity
            if FillQty < order_qty:
                trade.order_status.status = Status.PartFilled
            else:
                trade.order_status.status = Status.Filled
            trade.order_status.modified_quantity = order_qty -FillQty

            event =Event(
                task = Task.Deal ,
                order_id =data['OrderNo'],
                seqno = data['CNT'],
                action= trade.order.action,
                symbol = trade.order.symbol,
                quantity = FillQty ,
                price = price ,
                time_in_force =trade.order.time_in_force ,
                order_cond = trade.order.order_cond ,
                odd_lot = trade.order.odd_lot,
                ts = ts
                )

        if symbol in self._deals:
            ls=self._deals[symbol]
            ls.append(event)
        else:
            self._deals[symbol] =[event]

        if self._show is True:
            self._event(event)

    def OrderReport(self):
        """
        取得所有委託資料。

        Returns
        -------
        pd.DataFrame
            含欄位排序的委託資料表。如果無資料，則回傳欄位結構完整但內容為空的 DataFrame。

        Notes
        -----
            欄位內容來源自證券 OnQueryResult 4010 回傳資料。
        """
        df = pd.DataFrame(self._order_report)
        col = [
            # 1. 帳戶與身份類
            'Account', 'SubAccount', 'OmniAccount', 'BrokerId', 'AgentId',
            # 2. 商品與市場資訊
            'StockID', 'Market', 'Side',
            # 3. 交易方向與價格數量
            'Price', 'Qty', 'OrdLot',
            # 4. 下單屬性與方式
            'OrderNo', 'OrderFunc', 'OrdClass', 'Channel', 'TimeInForceN',
            # 5. 時間相關欄位
            'ClientOrderTime', 'ReportTime', 'TradeDate',
            # 6. 回報與狀態
            'BeforeQty', 'AfterQty', 'CNT',
            # 7. 錯誤與訊息
            'ErrCode', 'ErrMsg',
            # 8. 含 N 的統計或補充欄位（可選擇收尾用）
            'BeforeQtyN', 'AfterQtyN', 'CNTN',
            'ClientOrderTimeN', 'ReportTimeN',
            'PriceN', 'PriceFlagN', 'ChannelN'
        ]
        if df.empty ==True:
            df = pd.DataFrame(columns=col)
        else:
            df = df[[c for c in col if c in df.columns] + [c for c in df.columns if c not in col]]


        df.loc[(df['PriceFlagN'] == '1') & (df['Price'].astype(float).astype(int) == 0), 'Price'] = '市價'

        return df

    def ExecReport(self):
        """
        取得所有成交資料。

        Returns
        -------
        pd.DataFrame
            含欄位排序的成交資料表。如果無資料，則回傳欄位結構完整但內容為空的 DataFrame。

        Notes
        -----
        欄位內容來源自證券 OnQueryResult 4011 回傳資料。
        """
        df = pd.DataFrame(self._exec_report)
        col = [
            # 1. 帳戶與身份類
            'Account', 'SubAccount', 'OmniAccount', 'BrokerId', 'AgentID',
            # 2. 商品與市場資訊
            'StockID', 'Market', 'MarketNo',
            # 3. 交易方向與價格數量
            'Side', 'Price', 'DealQty',
            # 4. 委託與撮合屬性
            'OrdClass', 'OrdLot', 'OrderFunc', 'OrderNo', 'Channel',
            # 5. 成交與回報數據
            'AvgPriceN', 'CNT', 'SumQtyN',
            # 6. 時間資訊
            'ReportTime', 'TradeDate',
            # 7. 補充或描述欄位（字尾 N 為主）
            'DealQtyN', 'PriceN', 'PriceFlagN',
            'MarketNoN', 'ChannelN', 'TimeInForceN',
            'CNTN', 'ReportTimeN'
        ]
        if df.empty ==True:
            df = pd.DataFrame(columns=col)
        else:
            df = df[[c for c in col if c in df.columns] + [c for c in df.columns if c not in col]]
            
        return df

    def get_deals(self):
        """

        取得所有成交資料。

        Returns
        -------
        dict[str, list[Deal]]
            一個字典，鍵為股票代碼（symbol），
            值為該股票對應的 Deal 物件列表。

        """
        return self._deals
    
    def get_trades(self ,full=False):
        """

        查看委託單。

        Parameters
        ----------
        full : bool
            若為 True，則顯示所有委託單；預設為 False，僅顯示當前委託。

        Returns
        -------
        dict
            一個字典，鍵為 order_id，值為 Trade 物件。
        """
        if full==True:
            return self._trades
        T_dic={}
        for key, value in self._trades.items():
            if key =='無效單':
                continue
            if value.order_status.status not in [Status.Cancelled ,Status.Failed ,Status.Filled ,Status.PartFilled_Cancelled]:
                T_dic[key] =value
        return  T_dic
   
    def _update(self):
        self._show = False
        self._Order.ReceiveSReport(self._broker_id ,self._account)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._order_report) > N:
                N =len(self._order_report)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1) 
        self._show =True

#=====================================================================================  
    def InventorySum(self, FType ='A'):
        """
        證券庫存彙總查詢

        此函數查詢所有持股資訊，包括現股、融資、融券、零股與借券等，並以表格形式回傳。
        可選擇顯示單位為「張」或「股」。

        Parameters
        ----------
        FType : str, default='A'
            顯示的數量單位：
            - 'A'：以「張」為單位（1 張 = 1000 股）
            - 'B'：以「股」為單位

        Returns
        -------
        DataFrame
            回傳一個 Pandas DataFrame，包含以下類別欄位：

            - 基本資訊：
              `Symbol`, `SymbolName`, `CURRENCY`, `CURRNAME`, `EXPDATE`,
              `ASSET`, `ASSET_REAL`, `AVG_PRICE0`, `AVG_PRICE4`,
              `NETPL`, `NETPL_TWD`, `PRICE_RATE`, `RLPRICE`

            - 現股（整股）：
              `NETQTY0`, `R_QTY0`, `I_MATQTY0`, `O_MATQTY0`,
              `SALEQTY0`, `AORDQTY0`, `I_ORDQTY0`, `O_ORDQTY0`,
              `ORI_PRICE0`, `AVG_PRICE0`, `DTQTY0`, `DTCHECK0`,
              `I_RQTY0`, `O_RQTY0`, `S_RQTY0`, `I_CTLQTY0`, `O_CTLQTY0`

            - 融資：
              `NETQTY3`, `R_QTY3`, `I_ORDQTY3`, `I_MATQTY3`,
              `O_ORDQTY3`, `O_MATQTY3`, `SALEQTY3`, `AORDQTY3`,
              `I_CTLQTY3`, `O_CTLQTY3`, `ORI_PRICE3`, `AVG_PRICE3`

            - 融券：
              `NETQTY4`, `R_QTY4`, `I_ORDQTY4`, `I_MATQTY4`,
              `O_ORDQTY4`, `O_MATQTY4`, `SALEQTY4`, `AORDQTY4`,
              `I_CTLQTY4`, `O_CTLQTY4`, `ORI_PRICE4`, `AVG_PRICE4`

            - 零股：
              `NETQTY9`, `R_QTY9`, `I_ORDQTY9`, `I_MATQTY9`,
              `O_ORDQTY9`, `O_MATQTY9`, `SALEQTY9`, `AORDQTY9`

            - 借券與其他項目：
              `NETQTY5`, `R_QTY5`, `B_RQTY`, `O_ORDQTY5`, `O_MATQTY5`,
              `SALEQTY5`, `AORDQTY5`, `LSML04001`, `LSML04003`, `LSSL0400`,
              `EDNQTY`

        Notes
        -----
        資料來源為證券 OnQueryResult 4310 回傳結果。
        """

        if FType not in ['A', 'B']:
            print("[系統訊息] FType 需為 'A' 或 'B'")
            return
        
        BrokerID = self._broker_id
        Account = self._account
        Symbol = ''
        self._p=[]
        self._Order.RetrieveWsInventorySum( FType ,BrokerID ,Account, Symbol)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._p) > N:
                N =len(self._p)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1) 

        if self._p ==[]:
            print('[系統訊息] 未收到響應')
            return
        
        if self._p[0]['Code']!='MS0000':
            msg=self._p[0]['CodeDesc']
            print( f"[系統訊息] 發生錯誤: {msg}")
            return

        col = [
            # 基本欄位
            'Symbol', 'SymbolName', 'BrokerID', 'Account', 'MKTYPE',
            'RLPRICE', 'ASSET', 'ASSET_REAL', 'NETPL', 'NETPL_TWD',
            'CURRENCY', 'CURR_NAME', 'EXRATE_M', 'EXPDATE', 'QUNIT',
            'PRICE_RATE', 'EDNQTY',

            # 現股
            'R_QTY0', 'I_ORDQTY0', 'I_MATQTY0', 'O_ORDQTY0', 'O_MATQTY0',
            'SALEQTY0', 'AORDQTY0', 'NETQTY0',
            'I_RQTY0', 'O_RQTY0', 'S_RQTY0',
            'I_CTLQTY0', 'O_CTLQTY0',
            'ORI_PRICE0', 'AVG_PRICE0',
            'DTQTY0', 'DTCHECK0',

            # 融資
            'R_QTY3', 'I_ORDQTY3', 'I_MATQTY3', 'O_ORDQTY3', 'O_MATQTY3',
            'SALEQTY3', 'AORDQTY3', 'NETQTY3','DTRQTY',
            'O_CTLQTY3', 'I_CTLQTY3',
            'ORI_PRICE3', 'AVG_PRICE3',

            # 融券
            'R_QTY4', 'I_ORDQTY4', 'I_MATQTY4', 'O_ORDQTY4', 'O_MATQTY4',
            'SALEQTY4', 'AORDQTY4', 'NETQTY4',
            'O_CTLQTY4', 'I_CTLQTY4',
            'ORI_PRICE4', 'AVG_PRICE4',

            # 零股
            'R_QTY9', 'I_ORDQTY9', 'I_MATQTY9', 'O_ORDQTY9', 'O_MATQTY9',
            'SALEQTY9', 'AORDQTY9', 'NETQTY9',

            # 借券
            'B_RQTY', 'O_ORDQTY5', 'O_MATQTY5', 'SALEQTY5', 'AORDQTY5',
            'R_QTY5', 'NETQTY5', 'LSML04001', 'LSML04003', 'LSSL0400'
        ]

        if self._p[0]['Detail'] == []:
            return pd.DataFrame(columns=col)

        dfs = [pd.DataFrame(data['Detail']) for data in self._p]
        dfs = pd.concat(dfs)
        dfs =dfs.rename(columns={'ORI_GPRICE0':'ORI_PRICE0', 'ORI_GPRICE4':'ORI_PRICE4'})

        # for name in dfs.columns:
        #     if name in ['EXPDATE','CURRENCY' ,'CURRNAME' ,'DTCHECK0' ,'Symbol' ,'SymbolName']:
        #         continue
        #     converted = pd.to_numeric(dfs[name], errors='coerce')
        #     dfs[name] = converted.where(converted.notna(), dfs[name])

        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs
        
    def _profit(self,js_string):
        data = json.loads(js_string)
        self._p.append(data)

#===================================================================================== 
    def Inventory(self ,FType ='SS'):
        """
        庫存損益及即時維持率試算查詢

        Parameters
        ----------
        FType : str
            指定回傳資料格式，預設為 'SS'。
            - S:  帳號彙總 (換算成台幣彙總)  (現資券合併)
            - SS: 帳號彙總成多筆 (客戶+幣別)  (現資券合併)
            - M: 股票+交易類別小計 (現資券合併) 
            - M1: 股票+交易類別小計 (資券分開，無現股)
            - D: 明細 (現資券合併) 
            - D1: 明細 (資券分開，無現股)

        Returns
        -------
        DataFrame


        Notes
        -----
            欄位內容來源自證券OnQueryResult 4316回傳資料
        """

        if FType not in ['S', 'SS','M','M1','D','D1']:
            print("[系統訊息] FType 需為 'S', 'SS','M','M1','D','D1'")
            return
        BrokerID = self._broker_id
        Account = self._account
        Symbol=''
        DType = 'A'
        self._px= []
        self._Order.RetrieveWsInventory(FType,BrokerID,Account,Symbol,DType)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._px) > N:
                N =len(self._px)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._px ==[]:
            print('[系統訊息] 未收到響應')
            return
        
        if self._px[0]['Code']!='MS0000':
            msg =self._px[0]['CodeDesc']
            print( f"[系統訊息] 發生錯誤: {msg}")
            return
        
        col=[
            'STOCK', 'SNAME', 'ADDAMTCODE',
            'TDATE', 'EDATE', 'TERMSEQNO', 'SEQ',
            'CURRENCY', 'CURR_NAME', 'EXRATE_B',
            'PRICE', 'PRICE_RATE', 'ADJPRICE', 'ORI_PRICE', 'RLPRICE', 'SF_PRICE', 'SW_SETPRICE',
            'QTY', 'QTY0', 'QUNIT', 'RQTY',
            'ASSET', 'ASSETREAL', 'ASSET_ADJ', 'BFINT', 'COST', 'CRDBAMT0', 'CRSFAMT0', 'OTYPE', 'RTAMT', 'SALENET', 'SF_MARK', 'SUTRATE', 'SUTRATE_A', 'SUTRATE_B',
            'CRDBAMT', 'CRDBINT', 'CRSFAMT',
            'INSU_FEE', 'FEERATE', 'DLFEE_RATE', 'TAXRATE',
            'NETPL', 'NETPL_TWD', 'SF_PNL', 'PNL_RATE'        ]

        df1 = [pd.DataFrame(data['Detail1']) for data in self._px]
        df1 = pd.concat(df1)
        df2 = [pd.DataFrame(data['Detail2']) for data in self._px]
        df2 = pd.concat(df2)
        df3 = [pd.DataFrame(data['Detail3']) for data in self._px]
        df3 = pd.concat(df3)
        dfs = pd.concat([df1,df2,df3] )
        if dfs.empty:
            return pd.DataFrame(columns=col)
        
        # for name in dfs.columns:
        #     if name in ['STOCK','SNAME','ADDAMTCODE','TDATE','EDATE','TERMSEQNO','SEQ','CURRENCY','CURR_NAME','EXRATE_B','SW_SETPRICE','SF_MARK']:
        #         continue
        #     converted = pd.to_numeric(dfs[name], errors='coerce')
        #     dfs[name] = converted.where(converted.notna(), dfs[name])

        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs

    def _profit_realize(self,js_string):
        data = json.loads(js_string)
        self._px.append(data)

#===================================================================================== 
    def SettleAmtTrial(self ,FType ='D' ):
        """
        當日交割金額試算查詢

        此函數查詢當日交割金額相關資訊，支援回傳明細或總覽內容。

        Parameters
        ----------
        FType : str, optional
            指定回傳資料格式，預設為 'D'。
            - 'M': 當日交割金額匯總 (Detail1) 及盤中當沖沖銷資料 (Detail2)。
            - 'D': 盤中非當沖沖銷明細 +盤中沖銷-幣別彙總 (Detail1) 及盤中當沖沖銷資料 (Detail2)。
            - 'S': 回傳台幣彙總 (不含外幣) 的盤中沖銷資料 (Detail1)。
            - 'SS': 回傳客戶+幣別彙總的盤中沖銷-幣別彙總資料 (Detail1)。

        Returns
        -------
        dict
            包含以下鍵值的字典：
            - 'Detail1': list of dict, 根據 FType 回傳對應的明細或彙總資料。
            - 'Detail2': list of dict, 根據 FType 回傳當沖沖銷資料（若適用）。

        Notes
        -----
        欄位內容來源自證券 OnQueryResult 4302 回傳資料。

        Examples
        --------
        >>> df = api.Account.SettleAmtTrial('D')
        >>> pd.DataFrame(df['Detail1'])  # 盤中非當沖沖銷明細
        >>> pd.DataFrame(df['Detail2'])  # 盤中當沖沖銷資料

        Notes
        -----
            欄位內容來源自證券 OnQueryResult 4302 回傳資料。 

        """
        if FType not in ['M', 'D' ,'S' ,'SS']:
            print("[系統訊息] FType 需為 'M' ,'D' ,'S' 或 'SS' ")
            return

        BrokerID = self._broker_id
        Account = self._account
        Symbol = ''
        self._rp=[]
        self._Order.RetrieveWsSettleAmtTrial(FType,BrokerID,Account,Symbol)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._rp) > N:
                N =len(self._rp)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._rp ==[]:
            print('[系統訊息] 未收到響應')
            return
        
        if self._rp[0]['Code']!='MS0000':
            msg = self._rp[0]['CodeDesc']
            print( f"[系統訊息] 發生錯誤: {msg}")
            return

        col_d1 = [
            'BrokerId','Account','OTYPE','BS','TYPENAME','Symbol', 'SymbolName',
            'TERMSEQNO','QTY', 'PRICE','AMT','FEE','TAX',
            'CRDBAMT','CRSFAMT','GTAMT', 'DNAMT','CRDBINT', 'INSUFEE',
            'DBDLFEE','BNFEE','RECAMT','PAYAMT','NETAMT','NETPL',
            'SOURCE' ,'MKTYPE','BONDINT','DIFFQTY','CURRENCY', 'CURRNAME',
            'NETPLTWD', 'EXRATEB']

        col_d2 =  [
            'BrokerId','Account','Symbol','OTYPE','SymbolName','QTY',
            'BAVGPRICE' ,'BAMT','BFEE', 'BTOTAL',
            'SAVGPRICE','SAMT' ,'SFEE','STAX' ,'SDLFEE' ,'STOTAL',
            'INSUFEE','PNLX','PNLNX','QUNIT','CURRENCY', 'CURRNAME',
            'NETPLTWDX', 'NETPLTWDNX','EXRATEB' ]

        df1 = pd.concat([pd.DataFrame(data['Detail1']) for data in self._rp])
        df2 = pd.concat([pd.DataFrame(data['Detail2']) for data in self._rp])
        df3 = pd.concat([pd.DataFrame(data['PB4302_3']) for data in self._rp])
        df4 = pd.concat([pd.DataFrame(data['PB4302_4']) for data in self._rp])

        if df1.empty:
            df1 = pd.DataFrame(columns=col_d1)
        else:
            df1 = df1[[c for c in col_d1 if c in df1.columns] + [c for c in df1.columns if c not in col_d1]]
            df1 = df1.reset_index(drop=True)
        if df2.empty:
            df2 = pd.DataFrame(columns=col_d2)
        else:
            df2 = df2[[c for c in col_d2 if c in df2.columns] + [c for c in df2.columns if c not in col_d2]]
            df2 = df2.reset_index(drop=True)
        if df3.empty:
            df3 = pd.DataFrame(columns=col_d1)
        else:
            df3 = df3[[c for c in col_d1 if c in df3.columns] + [c for c in df3.columns if c not in col_d1]]
            df3 = df3.reset_index(drop=True)
        if df4.empty:
            df4 = pd.DataFrame(columns=col_d1)
        else:
            df4 = df4[[c for c in col_d1 if c in df4.columns] + [c for c in df4.columns if c not in col_d1]]
            df4 = df4.reset_index(drop=True)

        if FType =='M':
            dic={'Detail1': df1,'Detail2':df2 }
        elif FType =='D':
            dfx = pd.concat([df1,df3],axis=0).reset_index(drop=True)
            dic={'Detail1': dfx ,'Detail2':df2 }
        elif FType =='S':
            dic={'Detail1': df3 }
        elif FType =='SS':
            dic={'Detail1': df3 }
        return dic
            
    def _realprofit(self ,js_string):
        data = json.loads(js_string)
        self._rp.append(data)  

#===================================================================================== 
    def RealizePL(self ,FType='M' ,StartDate=None ,EndDate=None):
        """
        
        帳中已實現損益查詢

        此函數查詢帳戶內已實現的損益資訊，支援匯總或明細格式。

        Parameters
        ----------
        FType : str, optional
            指定回傳資料格式，預設為 'M'。
            - 'M': 匯總資料。
            - 'D': 明細資料。
        StartDate : str, optional
            查詢起始日期，格式為 yyyymmdd (e.g., '20250618')。若為 None，則預設為 EndDate 前30日。
        EndDate : str, optional
            查詢結束日期，格式為 yyyymmdd (e.g., '20250618')。若為 None，則預設為今天。

        Notes
        -----
            欄位內容來源自證券 OnQueryResult 4314 回傳資料。 
        
        """
        if FType not in ['M', 'D']:
            print("[系統訊息] FType 需為 'M'或'D'")
            return

        if not (isinstance(StartDate, str) or StartDate is None):
            print("[系統訊息] StartDate 必須為字串或 None")
            return

        if not (isinstance(EndDate, str) or EndDate is None):
            print("[系統訊息] EndDate 必須為字串或 None")
            return

        if EndDate is None:
            EndDate = datetime.datetime.today().strftime('%Y%m%d')
        if StartDate is None:
            end_date_obj = datetime.datetime.strptime(EndDate, '%Y%m%d')
            StartDate = (end_date_obj - datetime.timedelta(days=30)).strftime('%Y%m%d')

        BrokerID = self._broker_id
        Account = self._account
        Symbol =''
        DType = 'T' 
        print(f"[系統訊息] Start Date: {StartDate:<15}  End Date: {EndDate:<15}")
        
        QDays = 0
        self._rpx=[]
        self._Order.RetrieveWsRealizePL(FType ,BrokerID ,Account ,Symbol ,DType ,StartDate ,EndDate ,QDays)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._rpx) > N:
                N =len(self._rpx)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._rpx ==[]:
            print('[系統訊息] 未收到響應')
            return

        if self._rpx[0]['Code']!='MS0000':
            msg =self._rpx[0]['CodeDesc']
            print( f"[系統訊息] 發生錯誤: {msg}")
            return

        dfs = [pd.DataFrame(data['Detail']) for data in self._rpx]
        dfs = pd.concat(dfs)

        col = [
            # 商品與交易基本資訊
            'Symbol', 'SymbolName', 'Descript', 'CURRENCY', 'CURRNAME', 'TradeDate', 'ordClass', 'TERMSEQNO',
            # 交易方向與屬性
            'BS', 'DayTrade',
            # 價格與數量資訊
            'QTY', 'PRICE', 'ORIPRICE', 'COST', 'SFCOST', 'SFMARK',
            # 手續費與稅金
            'FEE', 'TAX', 'DBDLFEE', 'INSUFEE', 'BONDINT',
            # 損益資訊
            'AMT', 'GTAMT', 'DNAMT', 'NETAMT', 'NETPL', 'SFPNL',
            # 信用交易資訊
            'CRDBAMT', 'CRDBINT', 'CRSFAMT',
            # 損益比率與價格變化
            'PNLRATE', 'PRICERATE', 'SFPNLRATE' ]

        if dfs.empty ==True:
            dfs = pd.DataFrame(columns=col)

        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs

    def _realprofit_realize(self ,js_string):
        data = json.loads(js_string)
        self._rpx.append(data)  

#===================================================================================== 
    def SettleAmtDetail(self):
        """
        
        當日交割金額_沖銷明細(非當沖)查詢


        Notes
        -----
            欄位內容來源自證券 OnQueryResult 4304 回傳資料。 
        
        """
        FType = 'D'
        BrokerID = self._broker_id
        Account = self._account
        Symbol =''
        MthSeqno = ''
        self._1d =[]
        self._Order.RetrieveWsSettleAmtDetail(FType,BrokerID,Account,Symbol,MthSeqno)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._1d) > N:
                N =len(self._1d)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._1d ==[]:
            print('[系統訊息] 未收到響應')
            return
        
        if self._1d[0]['Code']!='MS0000':
            msg = self._1d[0]['CodeDesc']
            print( f"[系統訊息] 發生錯誤: {msg}")
            return
        
        dfs = [pd.DataFrame(data['Detail']) for data in self._1d]
        dfs = pd.concat(dfs)

        col = [
        'Account', 'BrokerId', 'Symbol', 'SymbolName', 'TERMSEQNO', 'TradeDate',
        'AMT', 'CRDBAMT', 'CRDBINT', 'CRSFAMT', 'DNAMT', 'FEE', 'GTAMT', 'TAX',
        'QTY', 'PRICE'    ]

        if dfs.empty ==True:
            dfs = pd.DataFrame(columns=col)

        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs

    def _settlement_td(self ,js_string):
        data = json.loads(js_string)
        self._1d.append(data)  

#===================================================================================== 
    def SettleAmt(self ,FType = 'SS'):
        """
        交割金額查詢(3日)查詢

        Returns
        -------
        DataFrame
            回傳一個 Pandas DataFrame，欄位包括：

            - Account : 帳號
            - BrokerId : 營業員代碼
            - CDate1 / CDate2 / CDate3 : 交割日期（第一日至第三日）
            - DealDate1 / DealDate2 / DealDate3 : 成交日（對應交割日）
            - CSRPAMT1 / CSRPAMT2 / CSRPAMT3 : 各日應收付金額（負值代表應付）
            - SettleMark1 / SettleMark2 / SettleMark3 : 是否已結算標記（Y/N）
            - CURRENCY : 幣別（如 TWD）

        Notes
        -----
            欄位內容來源自證券 OnQueryResult 4306 回傳資料。 
        """
        if FType not in ['S', 'S1', 'SS', 'SS1']:
            print("[系統訊息] FType 需為 'S', 'S1', 'SS', 'SS1'")
            return

        BrokerID = self._broker_id
        Account = self._account
        self._3d=[]
        self._Order.RetrieveWsSettleAmt(FType ,BrokerID ,Account)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._3d) > N:
                N =len(self._3d)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._3d ==[]:
            print('[系統訊息] 未收到響應')
            return
        
        if self._3d[0]['Code']!='MS0000':
            msg = self._3d[0]['CodeDesc']
            print( f"[系統訊息] 發生錯誤: {msg}")
            return
   
        dfs =[pd.DataFrame(data['Detail']) for data in self._3d]
        dfs = pd.concat(dfs)
        col = ['Account', 'BrokerId', 'CURRENCY', 'CURR_NAME',
               'DealDate1','CDate1','SettleMark1','CSRPAMT1',
               'DealDate2','CDate2','SettleMark2','CSRPAMT2',
               'DealDate3','CDate3','SettleMark3','CSRPAMT3']
        if dfs.empty==True:
            dfs = pd.DataFrame(columns=col)

        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs
    
    def _settlement_3d(self ,js_string):
        data = json.loads(js_string)
        self._3d.append(data)  

#===================================================================================== 
    def BalanceStatement(self ,FType ='M' ,StartDate=None ,EndDate=None ):
        """
        
        對賬單查詢 

        Parameters
        ----------
        FType : str, optional
            指定回傳資料格式，預設為 'M'。
            - 'M': 匯總資料。
            - 'D': 明細資料。
        StartDate : str, optional
            查詢起始日期，格式為 yyyymmdd (e.g., '20250618')。若為 None，則預設同 EndDate日期。
        EndDate : str, optional
            查詢結束日期，格式為 yyyymmdd (e.g., '20250618')。若為 None，則預設為今天。

        Notes
        -----
            欄位內容來源自證券 OnQueryResult 4312 回傳資料。 
        
        """
        if FType not in ['M', 'D']:
            print("[系統訊息] FType 需為 'M'或'D'")
            return

        if not (isinstance(StartDate, str) or StartDate is None):
            print("[系統訊息] StartDate 必須為字串或 None")
            return

        if not (isinstance(EndDate, str) or EndDate is None):
            print("[系統訊息] EndDate 必須為字串或 None")
            return

        if EndDate is None:
            EndDate = datetime.datetime.today().strftime('%Y%m%d')
        if StartDate is None:
            StartDate = EndDate

        BrokerID = self._broker_id
        Account = self._account
        Symbol=''
        DType='T'
        QDays = 0
        self._s=[]
        print(f"[系統訊息] Start Date: {StartDate:<15}  End Date: {EndDate:<15}")
        self._Order.RetrieveWsBalanceStatement(FType,BrokerID,Account,Symbol,DType,StartDate,EndDate,QDays)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._s) > N:
                N =len(self._s)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._s ==[]:
            print('[系統訊息] 未收到響應')
            return
        
        if self._s[0]['Code']!='MS0000':
            msg = self._s[0]['CodeDesc']
            print( f"[系統訊息] 發生錯誤: {msg}")
            return

        dfs = [pd.DataFrame(data['Detail']) for data in self._s]
        dfs = pd.concat(dfs)

        col=  ['BS' ,'Symbol','SymbolName','TERMSEQNO','QTY','PRICE',
                'AMT', 'FEE','TAX', 'CRDBAMT','CRSFAMT','GTAMT',
                'DNAMT','CRDBINT','BONDINT','INSUFEE','DBDLFEE', 'NETAMT',
                'NETPL','MKTYPE','CURRENCY', 'CURRNAME','CSPAYAMT','CSRECAMT',
                'DayTrade','Descript','PNLDTRADE','SOURCE','TradeDate','ordClass']
            
        if dfs.empty==True:
            dfs = pd.DataFrame(columns=col)

        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs

    def _statement(self ,js_string):
        data = json.loads(js_string)
        self._s.append(data)  

#===================================================================================== 
    def TSECreditInfo(self ,symbol):
        """

        查詢指定股票的信用交易額度資訊。

        Parameters
        ----------
        symbol : str
            股票代碼，例如 "2330"。

        Returns
        -------
        DataFrame
            包含信用額度相關資訊的查詢結果。

        Notes
        -----
        該方法內部調用格式代碼 4033（OnQueryResult）以取得信用額度資料。
        """
        brokerid = self._broker_id
        type = 1
        qno = symbol
        self._Credit=[]
        self._Order.RetrieveTSECreditInfo(brokerid ,type ,qno)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._Credit) > N:
                N =len(self._Credit)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._Credit ==[]:
            print('[系統訊息] 未收到響應')
            return
        
        dfs= pd.DataFrame([self._Credit[0]])

        col=['BrokerWarn', 'CurrName', 'Currency', 'DTCode', 'DealStatus',
        'DepositRate', 'ErrCode', 'ErrMsg', 'HPercent', 'LPercent',
        'MarginDateB', 'MarginDateE', 'MarginRate', 'MarginStatus', 'MarignQty',
        'MarkM', 'MarkT', 'Market', 'MarketControl', 'PGCL', 'ShortDateB',
        'ShortDateE', 'ShortLastDate', 'ShortQty', 'ShortRate', 'ShortStatus',
        'Source', 'StockName', 'StockNo', 'StockType', 'StopMark', 'TradeUT2',
        'TradeUnit', 'TransType']
            
        if dfs.empty==True:
            dfs = pd.DataFrame(columns=col)

        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs

    def _CreditInfo(self ,js_string):
        data = json.loads(js_string)
        self._Credit.append(data)

#===================================================================================== 
    def get_position(self):
        """
        整合性持股資訊查詢

        此函數會整合庫存資料（現股、融資、融券、零股）與損益試算資料，統一以商品代碼（Symbol）為索引，
        回傳各商品的持股資訊、買賣明細、均價、即時市價、損益等彙總結果。

        Returns
        -------
        DataFrame
            回傳一個以商品代碼（Symbol）為 index 的 Pandas DataFrame，欄位包括：

            - type : str
                固定值 'odd /cash /margin /short'，表示本報表含括的股別類型。
            
            - quantity_yd : list[int]
                昨日餘額，格式為 `[odd, cash, margin, short]`，分別對應零股、現股、融資、融券。

            - quantity_B : list[int]
                今日「買進」成交數量，格式同上 `[odd, cash, margin, short]`。

            - quantity_S : list[int]
                今日「賣出」成交數量，格式同上 `[odd, cash, margin, short]`。

            - quantity_td : list[int]
                今日持股餘額，格式同上 `[odd, cash, margin, short]`。

            - dealPrice_current|margin|short : list[float]
                均價資料，格式為 `[現股均價, 融資均價, 融券均價]`。

            - lastprice : float
                即時市價（RLPRICE 欄位平均）。

            - unrealized : float
                未實現損益（NETPL_TWD 欄位平均）。

            - realized : float
                已實現損益（由損益試算表 Detail1 中的 NETPLTWD 欄位加總而得）。

        Notes
        -----
        - 庫存資料來自 `self.InventorySum()`，損益資料來自 `self.SettleAmtTrial()['Detail1']`。
        - 欄位值皆為同商品多筆來源彙總後的結果。
        - quantity 欄位均為 4 元 list，依序為：零股、現股、融資、融券。

        """

        df = self.InventorySum()
        dic = self.SettleAmtTrial('D')
        pos1 = dic['Detail1']
        pos2 = dic['Detail2']

        # 對數值欄位進行轉換（df）
        for col in ['R_QTY9','R_QTY0','R_QTY3','R_QTY4',
                    'I_MATQTY9','I_MATQTY0','I_MATQTY3','I_MATQTY4',
                    'O_MATQTY9','O_MATQTY0','O_MATQTY3','O_MATQTY4',
                    'NETQTY9','NETQTY0','NETQTY3','NETQTY4',
                    'AVG_PRICE0','AVG_PRICE3','AVG_PRICE4',
                    'RLPRICE','NETPL_TWD']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # pos1 & pos2 數值欄位轉換
        for col in ['NETPLTWD']:
            if col in pos1.columns:
                pos1[col] = pd.to_numeric(pos1[col], errors='coerce')

        for col in ['NETPLTWDX']:
            if col in pos2.columns:
                pos2[col] = pd.to_numeric(pos2[col], errors='coerce')

        dfx = pd.DataFrame(columns=[
            'type',
            'quantity_yd',
            'quantity_B',
            'quantity_S',
            'quantity_td',
            'dealPrice_cash|margin|short',
            'lastprice',
            'unrealized',
            'realized'  # 如果你後面有加這欄建議也先列出來
        ])
        for row in df.itertuples():
            symbol=row.Symbol
            dfx.loc[symbol,'type'] = 'odd /cash /margin /short' 

            yo = df[df['Symbol'] == symbol].apply(lambda row: row['R_QTY9'], axis=1).sum().astype(int)
            yc = df[df['Symbol'] == symbol].apply(lambda row: row['R_QTY0'] , axis=1).sum().astype(int)
            ym = df[df['Symbol'] == symbol].apply(lambda row: row['R_QTY3'] , axis=1).sum().astype(int)
            ys = df[df['Symbol'] == symbol].apply(lambda row: row['R_QTY4']  ,axis=1).sum().astype(int)
            dfx.at[symbol,'quantity_yd']= [yo ,yc ,ym ,ys]

            Bo = df[df['Symbol'] == symbol].apply(lambda row: row['I_MATQTY9'], axis=1).sum().astype(int)
            Bc = df[df['Symbol'] == symbol].apply(lambda row: row['I_MATQTY0'] , axis=1).sum().astype(int)
            Bm = df[df['Symbol'] == symbol].apply(lambda row: row['I_MATQTY3'] , axis=1).sum().astype(int)
            Bs = df[df['Symbol'] == symbol].apply(lambda row: row['I_MATQTY4']  , axis=1).sum().astype(int)
            dfx.at[symbol,'quantity_B']= [Bo ,Bc ,Bm ,Bs]

            So = df[df['Symbol'] == symbol].apply(lambda row: row['O_MATQTY9'], axis=1).sum().astype(int)
            Sc = df[df['Symbol'] == symbol].apply(lambda row: row['O_MATQTY0']   , axis=1).sum().astype(int)
            Sm = df[df['Symbol'] == symbol].apply(lambda row: row['O_MATQTY3']  , axis=1).sum().astype(int)
            Ss = df[df['Symbol'] == symbol].apply(lambda row: row['O_MATQTY4']  , axis=1).sum().astype(int)
            dfx.at[symbol,'quantity_S']= [So ,Sc ,Sm ,Ss]

            to = df[df['Symbol'] == symbol].apply(lambda row: row['NETQTY9'], axis=1).sum().astype(int)
            tc = df[df['Symbol'] == symbol].apply(lambda row: row['NETQTY0'], axis=1).sum().astype(int)
            tm = df[df['Symbol'] == symbol].apply(lambda row: row['NETQTY3'], axis=1).sum().astype(int)
            ts = df[df['Symbol'] == symbol].apply(lambda row: row['NETQTY4'] , axis=1).sum().astype(int)
            dfx.at[symbol,'quantity_td']= [to ,tc ,tm ,ts]

            dl=df[df['Symbol'] == symbol]['AVG_PRICE0'].mean()
            dm=df[df['Symbol'] == symbol]['AVG_PRICE3'].mean()
            ds=df[df['Symbol'] == symbol]['AVG_PRICE4'].mean()
            dfx.at[symbol,'dealPrice_cash|margin|short'] = [dl,dm,ds]
            dfx.loc[symbol,'lastprice'] = df[df['Symbol'] == symbol]['RLPRICE'].mean()
            dfx.loc[symbol,'unrealized'] = df[df['Symbol'] == symbol]['NETPL_TWD'].mean()

            p = pos1[pos1['Symbol'] == symbol].NETPLTWD.sum() +pos2[pos2['Symbol'] == symbol].NETPLTWDX.sum()
            dfx.loc[symbol,'realized'] = p
        return dfx