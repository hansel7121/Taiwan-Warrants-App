import json ,copy ,time ,datetime
import pandas as pd
import numpy as np
from ._trade_base import *
from io import StringIO
import copy

_BS ={'B':Action.Buy,
      'S':Action.Sell}

_Status={'0':Status.Submitted,
        '1':Status.PartFilled,
        '2':Status.Filled}

_Task ={'0':Task.NewOrder,
        '1':Task.CancelOrder,
        '2':Task.UpdateOrder}

_map={'0':'預約單', 
    '1':'委託上手中', 
    '2':'委託成功', 
    '3':'委託失敗', 
    '4':'逾期單', 
    '5':'作癈單', 
    '6':'無效單', 
    '7':'處理中', 
    '8':'已下單至交易室', 
    '9':'處理失敗', 
    '10':'上手系統處理中'}

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

class SubOrderAPI:
    def __init__(self , ObjOrder ,broker_id , account):
        self._Order = ObjOrder
        self._broker_id = broker_id
        self._account = account
        self._webid = ''
        self._deals ={}
        self._trades = {} #org_seqnum :TRADE
        self._rid={} # rid :trade
        self._seq={} #seq :rid
        self._oid={} #ordernumber : org_seqnum
        self._name={} #cnt ：name
        self._cnt={} # cnt :org_seqnum
        self._pending_update={} #org_seqnum :creatorder_args

        self._o=[]
        self._d=[]
        self._p =[]
        self._s = [] 
        self._st = []

        self._dic = ObjOrder._SubContracts._dic
        self._symbol_to_full  = ObjOrder._SubContracts._symbol_to_full
        self.pending_list=[]
        self.order_list=[]
        self.deal_list=[]
        self.set_event(event)
        self._dic()

    def create_order(self ,action ,symbol ,qty ,price ,currency :Currency =Currency.MUT ,name:str='' ,_org_seq=None):
        """
        建立一筆委託單（限價、ROD 單）。

        Parameters
        ----------
        action : Action
            買進或賣出動作，如 Action.BUY 或 Action.SELL
        symbol : str
            股票代碼，例如：
                - 美股："TSM.N", "AAPL.N"
                - 港股："0700.HK", "9988.HK"

            可呼叫 self.SubOrder.contract() 取得完整可交易清單。
        qty : int
            委託股數
        price : float 
            委託價格
            
        Returns
        -------
        Trade
            對應的 Trade 物件

        Notes
        -----
        OrderResponse 為委託與成交的回傳結果，常見狀態碼：
            - 4102：pending
            - 4110：order
            - 4111：deal

        此回傳訊息發生於下單委託（NewOrder）及成交（Deal）時。
        """
        Action = 0
        BrokerId = self._broker_id
        Account = self._account

        _contracts = self._dic(symbol)
        market = Market[_contracts.market]
        board_lot =_contracts.board_lot
        full_symbol = _contracts.full_symbol

        if market != Market.US:
            raise ValueError("目前僅支援 US 市場 (Market.US)") 
        
        if int(qty) % board_lot != 0:
            raise ValueError(
                f"qty={int(qty)} must be a multiple of board_lot={board_lot}"
            )

        Side = action.value
        Price = price
        OrderNo =''
        CNT=''
        Qty = qty
        AfterQty =0
        Currency= currency.value    #(0:台幣 1:外幣)
        RequestId = self._Order.RetriveRequestID()

        order = Order(
            action= action ,
            market= market ,
            symbol= symbol ,
            quantity= int(qty),
            price= float(Price) ,
            currency = currency ,
            name =None if name=='' else name )
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

        # 改單重下：_org_seq 有給時不立即送出，先存入 _pending_update，
        # 待原單刪除確認後再由回報流程觸發送單
        if _org_seq is not None:
            args =(RequestId,
                    int(Action),
                    BrokerId,
                    Account,
                    int(market.value),
                    full_symbol,
                    Side,
                    int(Qty),
                    str(Price),
                    int(Currency),
                    OrderNo,
                    CNT,
                    int(AfterQty),
                    name)
            if _org_seq in self._pending_update:
                print('該原始序號已存在改單紀錄，本次請求將覆寫原有紀錄')
            self._pending_update[_org_seq] = args
            return trade

        self._Order.SetStrategyName(name)
        rt = self._Order.SecuritySubOrder(RequestId,
                            int(Action),
                            BrokerId,
                            Account,
                            int(market.value),
                            full_symbol,
                            Side,
                            int(Qty),
                            str(Price),
                            int(Currency),
                            OrderNo,
                            CNT,
                            int(AfterQty))
        self._Order.SetStrategyName('')
        #測試
        if rt != 0:
            rt = rt -2**64
            print(f'SecuritySubOrder return {rt}, {self._Order.GetOrderErrMsg(rt)}')
            return 
        
        return trade

    def cancel_order(self ,org_seq):
        """
        取消一筆尚未成交或失效的委託單。

        Parameters
        ----------
        org_seq : str
            原始委託單的序號

        Returns
        -------
        Trade
            取消對應的 Trade 物件（即預期刪單的委託）

        Notes
        -----
        OrderResponse 為委託刪單的回傳結果，常見狀態碼：
            - 4102：pending
            - 4110：order

        此類回傳訊息發生於刪單請求送出時（UpdateOrder）。
        """
        if isinstance(org_seq, Trade):
            org_seq = org_seq.order.org_seqnum
        if org_seq in self.get_trades().keys():
            trade = self._trades[org_seq]
        else:
            raise ValueError("查無此org_seq，或已失效") 

        Action =1
        BrokerId = self._broker_id
        Account = self._account
        Market = trade.order.market.value
        Symbol = trade.order.symbol
        Side = trade.order.action.value
        Qty = trade.order.quantity
        Price = trade.order.price
        Currency =trade.order.currency.value
        OrderNo = trade.order.order_id
        CNT = trade.order_status.nid
        AfterQty =0

        RequestId = self._Order.RetriveRequestID()
        operation = Operation(
            nid =RequestId ,
            task=Task.CancelOrder,
            status= Status.Pending , 
            op_time= datetime.datetime.now().strftime('%H%M%S') )
        trade.operations.append(operation)

        _contracts = self._dic(Symbol)
        full_symbol = _contracts.full_symbol

        self._rid[str(RequestId)] = trade
        rt = self._Order.SecuritySubOrder(RequestId,
                            int(Action),
                            BrokerId,
                            Account,
                            int(Market),
                            full_symbol,
                            Side,
                            int(Qty),
                            str(Price),
                            int(Currency),
                            OrderNo,
                            CNT,
                            int(AfterQty))
        if rt != 0:
            rt = rt -2**64
            print(f'SecuritySubOrder return {rt}, {self._Order.GetOrderErrMsg(rt)}')
            trade.operations.pop()
            return 
    
        return trade

    def update_order(self ,org_seq ,price=None, qty=None):
        """
        修改一筆尚未成交的委託單。

        僅限香港市場支援「減量」改單；其他市場將執行「刪單後重下單」的行為。

        Parameters
        ----------
        org_seq : str
            原始委託單序號
        price : float
            修改後的新價格。若提供，系統將先取消原單再以新價格重下單。
        qty : int
            要減少的股數（僅支援港股）。港股可使用原單減量；其他市場會取消原單後重下單。

        Returns
        -------
        Trade
            修改後的委託 Trade 物件；若失敗或查無訂單則回傳 None

        Raises
        ------
        ValueError
            當 price 與 qty 同時給定，或兩者皆未給定時觸發錯誤。

        Notes
        -----
        - 若為港股市場且僅提供 `qty`，將進行減量（若減為0，則改為刪單）
        - 非港股或需修改價格時，皆會先取消原單再重新下單
        - 當減量股數大於剩餘可成交股數時，會拒絕並提示錯誤
        - 呼叫 `self._Order.SecuritySubOrder()` 送出改單請求，錯誤時會移除操作記錄並提示錯誤碼

        Examples
        --------
        >>> update_order("202507021001", qty=50)       # 港股減量
        >>> update_order("202507021003", qty=20)       # 其他情況 → 會走 cancel + create
        """
        if isinstance(org_seq, Trade):
            org_seq = org_seq.order.org_seqnum
        if org_seq in self.get_trades().keys():
            trade = self._trades[org_seq]
        else:
            raise ValueError("查無此org_seq，或已失效")

        if (price is not None) and (qty is not None):
            raise ValueError("price 和 qty 僅能輸入一個")

        # price 和 qty 不能都為 None
        if (price is None) and (qty is None):
            raise ValueError("price 和 qty 必須輸入一個")
        
        Qty = trade.order_status.modified_quantity
        BrokerId = self._broker_id
        Account = self._account
        Symbol = trade.order.symbol
        Side = trade.order.action.value
        market = trade.order.market.value
        RequestId = self._Order.RetriveRequestID()
        Currency = trade.order.currency.value
        name = trade.order.name
        name ='' if name is None else name 

        _contracts = self._dic(Symbol)
        full_symbol = _contracts.full_symbol

        if price is None:
            board_lot =_contracts.board_lot
            if int(qty) % board_lot != 0:
                raise ValueError(
                    f"qty={int(qty)} must be a multiple of board_lot={board_lot}"
                )

        if price is None and market==Market.HK.value :  #港股減量
            if qty>Qty:
                raise ValueError("減量不可多餘剩餘量")
            
            Price = 0
            Action = 2
            OrderNo = trade.order.order_id
            CNT = trade.order_status.nid
            Qty = trade.order_status.modified_quantity

            if qty==Qty:
                trade = self.cancel_order(org_seq)
                return trade

            AfterQty = Qty-qty
            operation = Operation(
                nid =RequestId ,
                task=Task.UpdateQty,
                status= Status.Pending,
                msg= f"NewQty_{AfterQty}",
                op_time= datetime.datetime.now().strftime('%H%M%S') )
            trade.operations.append(operation)
            self._rid[str(RequestId)] = trade

            rt = self._Order.SecuritySubOrder(RequestId,
                                            int(Action),
                                            BrokerId,
                                            Account,
                                            int(market),
                                            full_symbol,
                                            Side,
                                            int(Qty),
                                            str(Price),
                                            int(Currency),
                                            OrderNo,
                                            CNT,
                                            int(AfterQty))
            if rt != 0:
                rt = rt -2**64
                print(f'SecuritySubOrder return {rt}, {self._Order.GetOrderErrMsg(rt)}')
                trade.operations.pop()
                return 
            return trade

        else:
            self.cancel_order(org_seq)
            if price is not None:   #改價
                Price = price
                Qty = trade.order_status.modified_quantity
            elif qty is not None:   #改量
                Qty = trade.order_status.modified_quantity -qty
                Price = trade.order_status.modified_price

            action = trade.order.action
            currency = trade.order.currency
            trade = self.create_order( action ,Symbol ,Qty ,Price ,currency ,name ,_org_seq =org_seq)
            return trade
        
    def _OnOrderName(self,data):
        if data['Market']=='R' and data['Account']==self._account and data['BrokerID']==self._broker_id:
            self._name[data['Cnt']] =data['ExeName']

            if data['Cnt'] in self._cnt.keys():
                org_seqnum  = self._cnt[data['Cnt']]
                if org_seqnum in self._trades.keys():
                    trade = self._trades[org_seqnum]
                    trade.order.name = data['ExeName']

    def _OnOrderPending(self,data):
        #pending 不會更新
        self.pending_list.append(data)

        #拿檔  ==============================================================
        rid = data['RequsetID']
        org_seqnum =data['OrderSeqNum']

        trade1 = self._rid.get(rid)
        trade2 = self._trades.get(data['OrderSeqNum'])
        if trade1 is None and trade2 is None:
            print(f'[系統訊息] 查無源頭 Order: {data}')
            return
        elif trade1 is None:
            trade = trade2
        elif trade2 is None:
            trade = trade1
        else: #trade1 trade2都有那他們是同個trade ,但如果order先處理了 pending還要動作嗎？
            trade = trade1

        if rid in self._rid.keys():
            self._seq[data['SeqNum']] = rid

        if org_seqnum !='':
            if org_seqnum not in self._trades.keys():
                self._trades[org_seqnum] =trade 

            if data['SeqNum'] not in self._cnt.keys() and data['SeqNum'] !='':
                self._cnt[data['SeqNum']] = org_seqnum

        operation = next((x for x in trade.operations if str(x.nid).strip() == str(rid).strip()), None)
        if operation is None:
            operation = next((x for x in trade.operations if str(x.nid).strip() == str(data['SeqNum']).strip()), None)
        if operation is None:
            operation = Operation()
            trade.operations.append(operation)

        #《data》 
        org_operation = copy.deepcopy(operation)
        ts = datetime.datetime.now().strftime('%H%M%S')
        msg = operation.msg

        #《order》
        if org_seqnum !='':
            trade.order.org_seqnum = org_seqnum
            trade.order.order_id =data['OrderNo']

        if data['SeqNum'] in self._name.keys():
            trade.order.name = self._name[data['SeqNum']]

        #《event &operation》
        if data['SeqNum'] !='':
            operation.nid = data['SeqNum']
        operation.status =data['Status']
        operation.ts = ts

        event = Event(
            task = operation.task ,
            status = data['Status'],
            org_seqnum=data['OrderSeqNum'],
            seqno =  operation.nid,
            action = trade.order.action,
            market = trade.order.market , 
            symbol = trade.order.symbol ,
            ts = operation.op_time
            )

        if operation.task ==Task.NewOrder:
            event.price = trade.order.price
            event.quantity =trade.order.quantity

        elif operation.task ==Task.UpdateQty:
            value = operation.msg.split("_")[1]
            event.quantity = _str_to_num(value)
            event.price = trade.order_status.modified_price or trade.order.price
        
        elif operation.task==Task.CancelOrder:
            event.quantity = 0
            event.price = trade.order_status.modified_price or trade.order.price

        if data['ErrCode'] != '0':
            mg = data['ErrMsg'] 
            msg = mg if not msg else f"{msg}|{mg}"
            event.status =Status.Failed
            operation.status = Status.Failed

        event.msg =msg
        operation.msg =msg

        #pending 可能在order之後送到 ，保留order的狀態
        if org_operation.status != Status.Pending:
            idx = trade.operations.index(operation)
            trade.operations[idx] = org_operation

        self._event(event)

    def _OnOrderReport(self,data):
        if data['MARKET']!='US':
            return 

        #4110
        self.order_list.append(data)

        #《拿檔》  
        org_seqnum = data['ORG_SEQ_NUM']
        self._oid[data['ORDERNUM']] = org_seqnum
        
        nid=0
        add_trade =False
        if data['SEQNUM'] in self._seq.keys():
            nid =  self._seq[data['SEQNUM']]
            trade = self._rid[nid]
        elif org_seqnum in self._trades.keys():
            trade = self._trades[org_seqnum]
        else:
            add_trade =True
            trade= Trade(order=Order(),order_status=OrderStatus(),operations=[])
        
        if org_seqnum !='':
            if org_seqnum not in self._trades.keys():
                self._trades[org_seqnum] =trade 

            if data['SEQNUM'] not in self._cnt.keys():
                self._cnt[data['SEQNUM']] = org_seqnum

        operation = next((x for x in trade.operations if str(x.nid).strip() == str(nid).strip()), None)
        if operation is None:
            operation = next((x for x in trade.operations if str(x.nid).strip() == str(data['SEQNUM']).strip()), None)
        if operation is None:
            operation = Operation()
            trade.operations.append(operation)

        #《data》  
        side = data['TRADE_TYPE']
        if side in {a.value for a in Action}:
            side = Action(side)

        market = data['MARKET']
        if market in Market.__members__:
            market = Market[market]

        status = data['SALES_STATUS']
        if status in _map.keys():
            status = _map[data['SALES_STATUS']]

        qty1 = _str_to_num(data['QTY'])
        qty2 = _str_to_num(data['QTY2'])
        afterqty = qty1 -qty2
        price = _str_to_num(data['PRICE'])
        ts = data['CREATE_DATE'][8:-2]
        symbol = self._symbol_to_full(data['SYMBOL'])#.get(data['SYMBOL'],  data['SYMBOL'])
        currency = data['SETTLE_CURRENCY']
        if currency in Currency.__members__:
            currency  =Currency[currency ]
        
        msg =None
        if data['ACTION'] =='2':
            msg = f"NewQty_{afterqty}"
        if data['ORDER_ERR_CODE'] != '0':
            num = "".join(filter(str.isdigit, data['ORDER_ERR_CODE']))
            mg = self._Order.errorMap.get(num, "") + f"|{data['ORDER_ERR_MSG']}"
            msg = mg if not msg else f"{msg}|{mg}"

        #《order》
        if data['SEQNUM'] in self._name.keys():
            trade.order.name = self._name[data['SEQNUM']]

        if add_trade==True:
            trade.order.org_seqnum = org_seqnum
            trade.order.order_id= data['ORDERNUM']
            trade.order.action= side
            trade.order.market= market
            trade.order.symbol= symbol 
            trade.order.quantity= qty1
            trade.order.price= price
            trade.order.currency = currency
        
        #《order_status》
        if data['ACTION'] == '0': #new
            if data['ORDER_ERR_CODE'] == '0':
                if data['SALES_STATUS']=='2':
                    trade.order_status.nid= data['SEQNUM']
                    trade.order_status.status= Status.Submitted 
                    trade.order_status.modified_quantity= trade.order.quantity
                    trade.order_status.modified_price= trade.order.price
                    trade.order_status.modified_time =ts
                elif data['SALES_STATUS'] in ['0','8']:
                    trade.order_status.nid= data['SEQNUM']
                    trade.order_status.status =Status.PendingSubmitted 
                    trade.order_status.modified_quantity= trade.order.quantity
                    trade.order_status.modified_price= trade.order.price
                    trade.order_status.modified_time =ts
            else:
                if data['SALES_STATUS']=='3':
                    trade.order_status.status =Status.Cancelled
                    trade.order_status.modified_quantity= 0
                    trade.order_status.modified_price= trade.order.price
                    trade.order_status.modified_time =ts

        elif data['ACTION'] == '1': #刪
            if data['SALES_STATUS']=='2':
                if data['ORDER_ERR_CODE'] == '0':
                    trade.order_status.nid =data['SEQNUM'] 
                    trade.order_status.modified_quantity =0
                    trade.order_status.modified_price = trade.order.price
                    trade.order_status.modified_time =ts
            else:#旧的状态
                if self._show is False:#補單才會進
                    trade.order_status.nid= data['SEQNUM']
                    trade.order_status.status= Status.Submitted 
                    trade.order_status.modified_quantity= afterqty
                    trade.order_status.modified_price= trade.order.price
                    trade.order_status.modified_time =ts

        elif data['ACTION'] == '2': #-q
            if data['SALES_STATUS']=='2':
                if data['ORDER_ERR_CODE'] == '0':
                    trade.order_status.nid =data['SEQNUM'] 
                    trade.order_status.modified_quantity = afterqty
                    trade.order_status.modified_price = trade.order.price
                    trade.order_status.modified_time =ts
            else:#旧的状态
                if self._show is False:#補單才會進
                    trade.order_status.nid= data['SEQNUM']
                    trade.order_status.status= Status.Submitted 
                    trade.order_status.modified_quantity= afterqty
                    trade.order_status.modified_price= trade.order.price
                    trade.order_status.modified_time =ts

        if trade.order_status.modified_quantity==0:
            if trade.order_status.status in [Status.Filled ]:
                pass
            elif sum(item.quantity for item in trade.order_status.deals) ==0:
                trade.order_status.status = Status.Cancelled
            else:
                trade.order_status.status= Status.PartFilled_Cancelled
        else:
            if sum(item.quantity for item in trade.order_status.deals) !=0:
                trade.order_status.status= Status.PartFilled

        #《operation》
        operation.nid = data['SEQNUM']
        operation.op_time = ts
        operation.status = status 
        operation.msg =msg

        if data['ACTION'] == '0':
            operation.task = Task.NewOrder

        elif data['ACTION'] == '1': #刪
            operation.task = Task.CancelOrder

        elif data['ACTION'] == '2': #-q
            operation.task = Task.UpdateQty

        #《event》
        event = Event(
            status = status ,
            org_seqnum = org_seqnum,
            seqno = data['SEQNUM'] ,
            action= side ,
            market= market ,
            price = price ,
            symbol= symbol ,
            msg = msg , 
            ts = ts )

        if data['ACTION'] == '0':
            event.task= Task.NewOrder
            event.quantity = qty1

        elif data['ACTION'] == '1':
            event.task = Task.CancelOrder
            event.quantity =0

        elif data['ACTION'] == '2':
            event.task= Task.UpdateQty
            event.quantity = afterqty

        if self._show is True:
            self._event(event)

        if trade.order_status.status in [Status.Cancelled ,Status.PartFilled_Cancelled]:
            if org_seqnum in self._pending_update:
                args = self._pending_update.pop(org_seqnum)
                self._Order.SetStrategyName(args[-1])
                rt = self._Order.SecuritySubOrder(*args[:-1])
                self._Order.SetStrategyName('')
                #測試
                if rt != 0:
                    rt = rt -2**64
                    print(f'SecuritySubOrder return {rt}, {self._Order.GetOrderErrMsg(rt)}')

                
    def _OnExecReport(self,data):
        if data['MARKET']!='US':
            return 

        self.deal_list.append(data)

        #《data》
        symbol = self._symbol_to_full(data['SYMBOL'])#.get(data['SYMBOL'],  data['SYMBOL'])

        market = getattr(Market, data['MARKET'] , data['MARKET'])

        FillQty = _str_to_num(data['EXE_QTY'])

        price = _str_to_num(data['EXE_PRICE'])

        side = data['TRADE_TYPE']
        if side in {a.value for a in Action}:
            side = Action(side)

        ts = data['TRADE_DATE']

        #deal  ==============================================================
        oid = data['ORDERNUM']
        trade=None
        orq_seq =''
        if oid in self._oid.keys():
            orq_seq = self._oid[oid]
            if orq_seq in self._trades.keys():
                trade = self._trades[orq_seq]

        if trade is None:
            print(f'[系統訊息] 查無源頭 Order: {data}')
        
            event = Event(
                task = Task.Deal,
                org_seqnum = orq_seq ,
                order_id = data['ORDERNUM'],
                action = side ,
                market = market,
                symbol = symbol ,
                quantity = FillQty ,
                price = price,
                ts = ts
                )
        else:
            deal = Deal(price=price,
                        quantity=FillQty,
                        ts= data['TRADE_DATE'][8:-2])
            trade.order_status.deals.append(deal)

            event = Event(
                task = Task.Deal,
                org_seqnum = orq_seq ,
                order_id = data['ORDERNUM'],
                action = trade.order.action,
                market = trade.order.market,
                symbol = symbol  ,
                quantity = FillQty ,
                price = price,
                ts = ts
                )

            order_qty =  trade.order_status.modified_quantity

            if not isinstance(order_qty, (int, float)):
                print('發生錯誤order_qty不爲數值')
                print(trade)

            if FillQty < order_qty:
                trade.order_status.status = Status.PartFilled
            else:
                trade.order_status.status = Status.Filled
            trade.order_status.modified_quantity = order_qty -FillQty

        if symbol in self._deals:
            ls=self._deals[symbol]
            ls.append(event)
        else:
            self._deals[symbol] =[event]

        if self._show is True:
            self._event(event)

    def set_event(self, event):
        """
        設定事件回調函數（callback）。

        Parameters
        ----------
        event : callable
            接收一個資料物件（如委託或成交回報）的函數。
            預設函數為：

            >>> def event(data):
            ...     print(data)

            傳入的 `data` 物件為以下四種事件類型之一：
            - NewOrder      ：新委託建立
            - UpdateOrder   ：委託更新（如價格、口數變更）
            - CancelOrder   ：委託取消
            - Deal          ：成交通知

            這些事件資料均包含以下欄位：

            - org_seqnum : str     # 原始序號
            - order_id   : str     # 委託單號
            - status     : str     # 委託狀態
            - action     : str     # 買賣別
            - market     : str     # 市場類型
            - symbol     : str     # 商品代碼
            - quantity   : int     # 委託口數
            - price      : float   # 委託價格
            - ts         : str     # 時間戳（hhmmss）

        Returns
        -------
        None

        Examples
        --------
        設定自訂事件處理函數：

            >>> def my_event_handler(data):
            ...     print("成交商品:", data['symbol'], "價格:", data['price'])

            >>> api.SubOrder.set_event(my_event_handler)
        """
        self._event = event 

#=====================================================================================  
    def _updateO(self ):
        brokerid = self._broker_id
        account= self._account
        self._o=[]
        for market in [0,1,2,3]:
            self._Order.RetrieveRcOrderReport(brokerid,account,market)
            time.sleep(0.5)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 8:
            if len(self._o) > N:
                N =len(self._o)
                last_data_time = time.time() 
            if time.time() -last_data_time > 2 and  N>0:
                break
            time.sleep(0.1) 

        if self._o ==[]:
            raise ValueError('[系統訊息] 未收到響應')
            # print('[系統訊息] 未收到響應')
            # return  

        order_ls = [row for data in self._o if data['err_code'] == '0' for row in data['Detail']][::-1]
        return order_ls

    def _getorder(self,js_string):
        data = json.loads(js_string)
        self._o.append(data)

    def OrderReport(self):
        """
        取得最新的訂單報告資料，包含委託單與成交資訊。

        Returns
        -------
        DataFrame
            回傳一個 Pandas DataFrame，欄位包括：

            1. 客戶資訊
                - customer_id : 客戶帳號
                - customer_name : 客戶名稱
                - agent_id : 代理人編號
                - ip : 下單 IP 位址

            2. 商品與交易屬性（含系統識別）
                - symbol : 商品代碼
                - symbol_name : 商品名稱
                - market : 交易市場
                - trade_currency : 交易幣別
                - settle_currency : 交割幣別
                - system_id : 系統識別碼
                - seqnum : 交易序號

            3. 交易方向與金額
                - BuySell : 買賣方向（買或賣）
                - action : 動作
                - price : 價格
                - qty : 數量
                - replace_price : 改價價格
                - replace_qty : 改量數量

            4. 委託資訊
                - orderno : 委託單號
                - order_method : 委託方法
                - order_err_code : 委託錯誤代碼
                - order_err_msg : 委託錯誤訊息
                - orig_seqnum : 原始交易序號

            5. 成交資訊
                - exe_avg_price : 成交平均價格
                - exe_qty : 成交數量
                - exe_status : 成交狀態描述
                - exe_status_code : 成交狀態代碼

            6. 銷售與處理狀態
                - sales_status : 銷售狀態
                - sales_status_code : 銷售狀態代碼

        Notes
        -----
            欄位內容來源自證券 OnQueryResult 4113 回傳資料。 

        """
        df = pd.DataFrame(self._updateO())
        col = [
        # 1. 客戶資訊
        'create_time','trade_date','customer_id', 'customer_name', 'agent_id', 'ip',
        # 2. 商品與交易屬性（含系統識別）
        'symbol', 'symbol_name', 'market',
        'trade_currency', 'settle_currency',
        'system_id', 'seqnum',
        # 3. 交易方向與金額
        'BuySell', 'action', 'price', 'qty', 'replace_price', 'replace_qty',
        # 4. 委託資訊
        'orderno', 'order_method', 'order_err_code', 'order_err_msg', 'orig_seqnum',
        # 5. 成交資訊
        'exe_avg_price', 'exe_qty', 'exe_status', 'exe_status_code',
        # 6. 銷售與處理狀態
        'sales_status', 'sales_status_code','channel']
        if df.empty ==True:
            df = pd.DataFrame(columns=col)
            return df
        else:
            df = df[col]

        df =df[df.market=='US'].reset_index(drop=True)
        df['symbol'] = df['symbol'].replace(self._symbol_to_full())
        return df

    def _updateD(self):
        brokerid = self._broker_id
        account= self._account
        self._d=[]
        for market in [0,1,2,3]:  #成交
            self._Order.RetrieveRcMatchReportDetail(brokerid,account,market)
            time.sleep(0.5)
        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 8:
            if len(self._d) > N:
                N =len(self._d)
                last_data_time = time.time() 
            if time.time() -last_data_time > 2 and  N>0:
                break
            time.sleep(0.1) 

        if self._d ==[]:
            raise ValueError('[系統訊息] 未收到響應')
            # print('[系統訊息] 未收到響應')
            # return  

        deal_ls = [row for data in self._d if data['err_code'] == '0' for row in data['Detail']][::-1]
        return deal_ls

    def _getdeal(self,js_string):
        data = json.loads(js_string)
        self._d.append(data)

    def ExecReport(self):
        """
        取得最新成交回報資料，包含成交明細與相關委託資訊。

        Returns
        -------
        DataFrame
            回傳一個 Pandas DataFrame，欄位包括：

            客戶與來源
                - channel : 交易通道或來源
                - customer_id : 客戶帳號
                - customer_name : 客戶名稱

            商品與市場資訊
                - symbol : 商品代碼
                - symbol_name : 商品名稱
                - market : 交易市場
                - system_id : 系統識別碼

            委託相關
                - orderno : 委託單號
                - trade_type : 交易類型（買賣別）

            成交資訊
                - exe_price : 成交價格
                - exe_avg_price : 成交平均價格
                - exe_qty : 成交數量
                - trade_date : 交易日期

            幣別資訊（最後處理結算）
                - trade_currency : 交易幣別
                - settle_currency : 交割幣別

        Notes
        -----
            欄位內容來源自證券 OnQueryResult 4117 回傳資料。 

        """
        df = pd.DataFrame(self._updateD())

        col = [
            'trade_date','customer_id', 'customer_name' ,'trade_type',
            'market','symbol', 'symbol_name','trade_currency' ,'exe_qty', 'exe_avg_price','exe_price',
            'orderno','settle_currency','system_id','channel']

        if df.empty ==True:
            df = pd.DataFrame(columns=col)
            return df
        else:
            df = df[col]

        df =df[df.market=='US'].reset_index(drop=True)
        df['symbol'] = df['symbol'].replace(self._symbol_to_full())
        return df

    def _update(self):
        self._show = False

        order_ls = self._updateO()
        for o in order_ls:
            dic={}
            dic['ACTION']= o['action']
            dic['CHANNEL'] = o['channel']
            dic['CREATE_DATE'] = datetime.datetime.strptime(o['create_time'], "%Y/%m/%d %H:%M:%S").strftime("%Y%m%d%H%M%S") + "000"
            dic['CUSTOMER_ID'] = o['customer_id']
            dic['CUSTOMER_NAME'] = o['customer_name']
            dic['EXE_QTY']= o['exe_qty']
            dic['EXE_STATUS'] ={'未成交':'0' ,'部份成交': '1' ,'全部成交':'2'}.get(o['exe_status'], '')
            dic['IP'] =o['ip']
            dic['MARKET']=o['market']
            dic['ORDERNUM'] =o['orderno']
            dic['ORDER_ERR_CODE'] =o['order_err_code']
            dic['ORDER_ERR_MSG'] = o['order_err_msg']
            dic['ORDER_METHOD'] = o['sales_status_code']
            dic['ORG_SEQ_NUM'] =o['orig_seqnum']
            dic['PRICE'] = o['price']
            dic['QTY'] = o['qty']
            dic['QTY2'] =  str(int(o['qty']) - int(o['replace_qty']))
            dic['SALES_STATUS'] =o['sales_status_code']
            dic['SEQNUM'] =o['seqnum']
            dic['SETTLE_CURRENCY'] = o['settle_currency']
            dic['SYMBOL'] =o['symbol']
            dic['SYMBOL_NAME'] =o['symbol_name']
            dic['SYSTEMID'] =o['system_id']
            dic['TRADE_DATE'] = ''
            dic['TRADE_TYPE'] = o['BuySell']
            self._OnOrderReport(dic)

        deal_ls = self._updateD()
        for d in deal_ls :
            dic={}
            dic['CHANNEL']= d['channel']
            dic['CUSTOMER_ID']= d['customer_id']
            dic['CUSTOMER_NAME']= d['customer_name']
            dic['EXE_PRICE'] =d['exe_price']
            dic['EXE_QTY'] =d['exe_qty']
            dic['MARKET'] =d['market']
            dic['ORDERNUM'] =d['orderno']
            #dic['SEQNUM_MATCH_S']
            #dic['PRICE'] = d['']
            #dic['QTY'] =d['']
            dic['SYMBOL'] = d['symbol']
            dic['SYMBOL_NAME'] = d['symbol_name']
            dic['SYSTEMID'] = d['system_id']
            dic['TRADE_DATE'] = datetime.datetime.strptime(d['trade_date'] ,'%Y/%m/%d %H:%M:%S').strftime('%Y%m%d%H%M%S') + '000'
            dic['TRADE_TYPE'] =d['trade_type']
            self._OnExecReport(dic)

        self._show =True

#=====================================================================================  

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
            一個字典，鍵為 org_seqnum，值為 Trade 物件。
        """
        if full==True:
            return self._trades
        
        T_dic={}
        for key, value in self._trades.items():
            if value.order_status.status  in [Status.PartFilled ,Status.Submitted ,Status.PendingSubmitted]:
                T_dic[key] =value
            # if value.order_status.status is None:
            #     if value.operations[-1].status in ['已下單至交易室','上手系統處理中','處理中','委託上手中','委託成功']:
            #         T_dic[key] =value

        return  T_dic
    
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

#=====================================================================================   
    def StockPositionReport (self ):
        """
        股票庫存部位查詢

        Returns
        -------
        DataFrame
            回傳一個 Pandas DataFrame，欄位包括：

            - customer_id : 客戶帳號
            - symbol : 股票代碼
            - symbol_name : 股票名稱
            - market : 交易市場
            - currency : 幣別
            - settle_currency : 今日買進成交股數
            - Qty : 收盤價
            - buyqty : 今日買進成交股數
            - market_price : 最新收盤價
            - close_date : 收盤價日期

        Notes
        -----
            欄位內容來源自證券 OnQueryResult 4123 回傳資料。 
        """
        brokerid = self._broker_id
        account = self._account
        #protype=0
        self._p =[] 
        for protype in [0,1,2]:
            for market in [0,1,2,3]: #1 會崩潰
                self._Order.RetrieveRcStockPositionReport( brokerid ,account ,market ,protype)
                time.sleep(0.2)

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

        dfs =[pd.DataFrame(data['Detail']) for data in self._p]
        dfs = pd.concat(dfs)

        col = [
        # 客戶與標的資訊
        'customer_id', 'customer_name',
        'symbol', 'symbol_name',
        # 市場與幣別資訊
        'market', 'currency', 'settle_currency',
        # 數量與價格
        'Qty', 'buyqty', 'market_price',
        # 成交或關閉資訊
        'close_date' ]

        if dfs.empty ==True:
            dfs = pd.DataFrame(columns=col)
            return dfs

        #僅留美
        dfs = dfs[dfs.market=='US']

        # for name in ['Qty', 'buyqty', 'market_price']:
        #     converted = pd.to_numeric(dfs[name], errors='coerce')
        #     dfs[name] = converted.where(converted.notna(), dfs[name])

        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs['symbol'] = dfs['symbol'].replace(self._symbol_to_full())
        dfs = dfs.reset_index(drop=True)
        return dfs
    
    def _profit(self ,js_string):
        data = json.loads(js_string)
        self._p.append(data)

#=====================================================================================   
    def DeliveryReport(self):
        """
        交割明細報告查詢。

        Returns
        -------
        pandas.DataFrame
            回傳一個 Pandas DataFrame，包含交割明細資料。常見欄位包括：

        Notes
        -----
            欄位內容來源自證券 OnQueryResult 4125 回傳資料。 
        """
        BrokerID = self._broker_id
        Account= self._account
        self._s = [] 
        self._Order.RetrieveRcDeliveryReport(BrokerID, Account ,'')

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
        
        if self._s[0]['err_code']!='0':
            msg = self._s[0]['err_msg']
            print( f"[系統訊息] 發生錯誤: {msg}")
            return
        
        _col = [
            # 客戶資訊
            'CustomerId', 'BS','TradeCurrency','SettleCurrency','Symbol' ,'TotalAmt',
            'Price' ,'Qty' ,'MatchPrice','Commission','CustodyFee' ,'OtherFee','ItemRemark']
        
        dfs =[pd.DataFrame(data['Detail']) for data in self._s]
        dfs = pd.concat(dfs)
        if dfs.empty ==True:
            dfs = pd.DataFrame(columns=_col)
            return dfs
        
        _dic =self._dic()
        def parse_market(symbol_raw):
            if not isinstance(symbol_raw, str):
                return None

            # 取 <br> 前的代碼
            sym_code = symbol_raw.split('<br>')[0].strip()
            if sym_code in _dic:
                return _dic[sym_code]

            # 判斷市場
            if sym_code.endswith('.HK'):
                return 'HK'
            elif sym_code.endswith('.TW'):
                return 'TW'
            elif sym_code.endswith('.US'):
                return 'US'
            else:
                # 如果沒有 .HK/.TW/.US，就用幣別推測
                if '-HKD' in symbol_raw:
                    return 'HK'
                elif '-TWD' in symbol_raw:
                    return 'TW'
                elif '-USD' in symbol_raw:
                    return 'US'
                else:
                    return 'UNKNOWN'

        dfs['Market'] = dfs['Symbol'].apply(parse_market)
        dfs = dfs[dfs['Market']=='US'].reset_index(drop=True)
        dfs = dfs.drop(columns=['Market'])

        num_cols = ['Commission', 'CustodyFee' ,'MatchPrice' ,'OtherFee','Price', 'Qty'  ,'TotalAmt']
        for col in num_cols:
            dfs[col] = pd.to_numeric(dfs[col], errors='coerce')

        # 轉換文字欄位保持原本字串
        str_cols = ['BS', 'CustomerId', 'SettleCurrency', 'Symbol', 'TradeCurrency' ,'ItemRemark']
        for col in str_cols:
            dfs[col] = dfs[col].astype(str)
        
        dfs = dfs[[c for c in _col if c in dfs.columns] + [c for c in dfs.columns if c not in _col]]
        new = dfs['Symbol'].str.rsplit('<br>', n=1).str[0]
        back = dfs['Symbol'].str.rsplit('<br>', n=1).str[1]
        symbol = new.replace(self._symbol_to_full())
        dfs['Symbol'] = '(' + symbol + ') ' + back
        dfs = dfs.reset_index(drop=True)
        return dfs

    def _settlement(self,js_string):
        data = json.loads(js_string)
        self._s.append(data)

#=====================================================================================   
    def PositionDetailReport(self , currency='USD' ):
        """
        取得帳戶資產明細報告，包括各幣別餘額與相關資產資訊。

        Parameters
        ----------
        currency : str, optional
            幣別代碼，例如 'HKD'、'USD' 等。
            不帶參數時回傳所有幣別的資產明細（OnQueryResult 4119）。
            帶參數時只回傳該幣別的明細（OnQueryResult 4121）。

        Returns
        -------
        DataFrame
            回傳一個 Pandas DataFrame，欄位包含：

            - customer_id : 客戶帳號
            - customer_name : 客戶名稱
            - currency : 幣別（如 HKD、USD、EUR 等）
            - balance_twd : 台幣帳戶可用餘額
            - pp1 : 可借出其他幣別使用金額
            - pp2 : 可借入其他幣別使用金額
            - pp3 : 原幣別交易可用餘額
            - pp4 : 整戶交易可用餘額
            - pp5 : 原幣別可出金餘額
            - pp6_twd : 淨賣可用總額（以台幣計）
            - pp7_twd : T日圈存總額（以台幣計）
            - pp8_twd : T日圈存總額_台幣

        Notes
        -----
        - 資料來源為交易系統 OnQueryResult 4119（無參數）或 4121（帶參數）回傳資料。
        """
        BrokerID = self._broker_id
        Account= self._account

        if currency !='USD':
            raise ValueError('目前僅支援查閲"USD" ') 

        self._st = [] 
        if currency==None:
            self._Order.RetrieveRcPositionDetailReport(BrokerID, Account)

            start_time = time.time()
            last_data_time = start_time
            while time.time() - start_time < 30:
                if len(self._st) > 0:
                    break
                time.sleep(0.1) 
        else:    
            self._Order.RetrieveRcCurrencyReport(BrokerID, Account ,currency)

            start_time = time.time()
            last_data_time = start_time
            while time.time() - start_time < 5:
                if len(self._st) > 0:
                    break
                time.sleep(0.1) 

        if self._st ==[]:
            print('[系統訊息] 未收到響應')
            return
        
        if self._st[0]['err_code']!='0':
            msg = self._st[0]['err_msg']
            print( f"[系統訊息] 發生錯誤: {msg}")
            return
        
        dfs =[pd.DataFrame(data['Detail']) for data in self._st]
        dfs = pd.concat(dfs)
        
        col = [
            'customer_id', 'customer_name',
            'currency',
            'balance_twd',
            'pp1', 'pp2', 'pp3', 'pp4', 'pp5',
            'pp6_twd', 'pp7_twd', 'pp8_twd'
        ]

        if dfs.empty ==True:
            dfs = pd.DataFrame(columns=col)
            return dfs
        else:
            dfs= dfs[col]
            dfs = dfs.reset_index(drop=True)
        
        return dfs

    def _statement(self,js_string):
        data = json.loads(js_string)
        self._st.append(data)

    def _statementX(self,js_string):
        data = json.loads(js_string)
        self._st.append(data)

#=====================================================================================  
    def get_position(self):
        """
        取得目前持股部位資訊。

        本方法會整合：
        - 當日成交資料（由 `get_deals()` 提供）
        - 持股報表（由 `StockPositionReport()` 提供）
        並輸出每檔股票的市場別、今昨持股、當日買賣量、最新價格等資訊。

        Returns
        -------
        pd.DataFrame
            含以下欄位的 DataFrame，index 為連續整數，'symbol' 為欄位名稱：
                - symbol : 股票代號（如 'TSM.N', '0700.HK'）
                - market : 市場名稱（如 'HK', 'US'）
                - quantity_yd : 昨日結算後的庫存
                - quantity_td : 今日報表中的庫存（含今日成交變動後）
                - quantity_B : 當日買進總數量
                - quantity_S : 當日賣出總數量
                - market_price : 報表中的最新市價（若有提供）

        Notes
        -----
        - 若成交資料與報表皆為空，將回傳空的 DataFrame。
        """
        d = self.get_deals()
        p = self.StockPositionReport()

        if not d and (p is None or p.empty):
            return pd.DataFrame(columns=[
                'symbol', 'market', 'quantity_yd', 'quantity_B',
                'quantity_S', 'quantity_td', 'market_price'
            ])

        df = pd.DataFrame(columns=[
            'market', 'quantity_yd', 'quantity_B',
            'quantity_S', 'quantity_td', 'market_price'
        ])

        for name in ['Qty', 'buyqty', 'market_price']:
            converted = pd.to_numeric(p[name], errors='coerce')
            p[name] = converted.where(converted.notna(), p[name])

        for symbol, deals in d.items():
            if symbol not in df.index:
                df.loc[symbol] = [None] * len(df.columns)

            for item in deals:
                side = item.action.value  # 'B' or 'S'
                qty = item.quantity

                if side == 'B':
                    df.loc[symbol, 'quantity_B'] = qty + (
                        0 if pd.isna(df.get('quantity_B', {}).get(symbol, 0)) else df.loc[symbol, 'quantity_B']
                    )
                elif side == 'S':
                    df.loc[symbol, 'quantity_S'] = qty + (
                        0 if pd.isna(df.get('quantity_S', {}).get(symbol, 0)) else df.loc[symbol, 'quantity_S']
                    )

                df.loc[symbol, 'market'] = item.market.name

        if 'symbol' in p.columns and not p.empty:
            ls = list(set(p['symbol'].dropna().unique()) | set(df.index))
        else:
            ls = list(df.index)

        for symbol in ls:
            if 'symbol' in p.columns and 'Qty' in p.columns:
                qty_sum = p.loc[p['symbol'] == symbol, 'Qty'].astype(float).sum()
                df.loc[symbol, 'quantity_td'] = qty_sum
            else:
                df.loc[symbol, 'quantity_td'] = 0

        if {'symbol', 'market_price'}.issubset(p.columns):
            price_map = p.dropna(subset=['market_price']).groupby('symbol')['market_price'].last()
            for symbol, price in price_map.items():
                df.loc[symbol, 'market_price'] = price

        df['quantity_B'] = df['quantity_B'].fillna(0)
        df['quantity_S'] = df['quantity_S'].fillna(0)
        df['quantity_td'] = df['quantity_td'].fillna(0)
        df['quantity_yd'] = df['quantity_td'] + df['quantity_S'] - df['quantity_B']

        _dic =self._dic()
        df['market'] = df.index.map(   lambda s: _dic.get(s).market if s in _dic else None)
        df = df.reset_index().rename(columns={'index': 'symbol'})
        cols = ['symbol', 'market', 'quantity_yd', 'quantity_B', 'quantity_S', 'quantity_td', 'market_price']
        df = df[cols]
        return df
