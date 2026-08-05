import json ,time ,copy ,datetime
import pandas as pd
import numpy as np
from ._trade_base import *
from decimal import Decimal

class TradeHour(Enum):
    REGULAR = 'R'
    POSTMARKET  = 'P'

_BS ={'B':Action.Buy,
      'S':Action.Sell}

_TIF={'R':TimeInForce.ROD,
      'F':TimeInForce.FOK,
      'I':TimeInForce.IOC}

_PriceType={'1':PriceType.MKT,
            '2':PriceType.LMT,
            '3':PriceType.StopLossMarket,
            '0':PriceType.RangeMarket}

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

def compose_symbol(row):
    prefix = str(row.get('ComID', '')).strip()
    ym_str = str(row.get('ComYM', '')).strip()

    if len(ym_str) < 6:
        return None  # 不合法年月

    year = int(ym_str[:4])
    month = int(ym_str[-2:])
    year_code = str(year)[-1]
    cp = str(row.get('CP', '')).upper().strip()

    if cp == '':
        # 期貨
        month_code = chr(ord('A') + month - 1)
        return f"{prefix}{month_code}{year_code}"
    else:
        # 選擇權
        strike_raw = row.get('StrikePrice', 0)
        if pd.isna(strike_raw) or strike_raw == '':
            strike = 0
        else:
            strike = int(round(float(strike_raw)))
        strike_str = f"{strike:05d}"

        if cp == 'C':
            month_code = chr(ord('A') + month - 1)
        elif cp == 'P':
            month_code = chr(ord('M') + month - 1)
        else:
            month_code = '?'

        return f"{prefix}{strike_str}{month_code}{year_code}"

class FutOrderAPI:
    def __init__(self , ObjOrder ,broker_id , account):
        self._Order = ObjOrder
        self._broker_id = broker_id
        self._account = account
        self._trades = {'無效單':[]}
        self._deals ={}
        self._webid={}   # oid :webid
        self.order_list=[]
        self.deal_list=[]
        self._p=[]
        self._pd=[]
        self._rp=[]
        self._rpd=[]
        self._bal=[]
        self._balX =[]
        self._rid = {} #裏面存rid:trade 
        self._cnt ={} #裏面存cnt:rid
        self.set_event(event)

    def create_order(self ,action ,symbol ,qty ,price ,time_in_force =TimeInForce.ROD):
        """
        建立一筆期貨或選擇權的委託單。

        Parameters
        ----------
        action : Action
            買進或賣出動作，使用 enum Action，例如：
                - Action.Buy：買進
                - Action.Sell：賣出

        symbol : str
            商品代碼。依商品不同長度會不同，例如：
                - "TXFG5"（期貨）
                - "TXO23100L4"（選擇權）

        qty : int
            委託口數（張數）

        price : float or PriceType
            價格或價格類型：
                - 若為數值（float/int），表示限價
                - 若為 PriceType.MKT 表示市價
                - 若為 PriceType.StopLossMarket 表示觸價市價單
                - 不支援 PriceType.LimitUp / LimitDown / Reference

        time_in_force : TimeInForce, optional
            委託條件，預設為 ROD。常見選項：
                - TimeInForce.ROD：當日有效
                - TimeInForce.IOC：立即成交剩撤
                - TimeInForce.FOK：立即成交全額否則撤單

        Returns
        -------
        Trade
            初步建立的 Trade 物件（尚未回報成交），可透過事件追蹤後續狀態。
        """
        #TXO23100L4  CDFA5 TXFA5

        Action = 0
        Side = action.value
        Symbol = symbol
        Market = 1 if len(Symbol)== 10 else 0
        BrokerId =self._broker_id 
        Account = self._account
        Qty = qty

        if price == PriceType.LMT:
            raise ValueError("如欲設定限價，請直接輸入價格") 
        
        if isinstance(price, (float, int)):
            Price = price
            PriceFlag = PriceType.LMT
        elif price in [PriceType.MKT ,PriceType.RangeMarket]:
            Price = 0
            PriceFlag = price
        else:
            raise ValueError("期貨PriceType僅支持 MKT及RangeMarket ，或直接輸入價格 ") 
                      
        PE = 3
        WebID = ''
        CNT = ''
        OrderNo= ''
        RequestId = self._Order.RetriveRequestID()

        order = Order(
            category= Category.OPTION if len(symbol)== 10 else Category.FUTURE,
            action= action,
            symbol= symbol,
            quantity= qty ,
            price= Price ,
            time_in_force = time_in_force,
            price_type = PriceFlag )
        order_status =OrderStatus()
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

        rt = self._Order.Order(int(Action),  
                                int(Market),
                                RequestId,  
                                BrokerId,
                                Account,
                                '',
                                Symbol,
                                Side,
                                int(PriceFlag.value),
                                str(Price),
                                int(time_in_force.value),
                                int(Qty),
                                int(PE),
                                0,
                                WebID,
                                CNT,
                                OrderNo)
        if rt != 0:
            rt = rt -2**64
            print(f'SecurityOrder return {rt}, {self._Order.GetOrderErrMsg(rt)}')
            return 

        return trade

    def cancel_order(self ,order_id):
        """
        取消一筆已送出的期貨或選擇權委託單。

        Parameters
        ----------
        order_id : str
            原委託單號，必須為已存在的 Trade 物件。

        Returns
        -------
        Trade or None
            更新後的 Trade 物件（含取消操作記錄），
            若查無此委託單則回傳 None。

        Notes
        -----
        若指定的 `order_id` 不存在，將顯示警告訊息。
        此情況可能發生於：
            - 委託已成交完畢並清除
            - 指定的 order_id 打錯
        """
        if isinstance(order_id, Trade):
            order_id = order_id.order.order_id
        if order_id in self.get_trades().keys():
            trade = self._trades[order_id]
        else:
            raise ValueError("查無此order_id，或已失效")   
        
        Action =1
        Symbol = trade.order.symbol
        Market = trade.order.category.value
        BrokerId =self._broker_id 
        Account = self._account
        Side = trade.order.action.value
        PriceFlag = trade.order.price_type.value
        Price = trade.order.price
        if Price == int(Price): 
            Price = int(Price)    
        Tif = trade.order.time_in_force.value
        Qty = trade.order.quantity
        PE  = 3
        WebID = self._webid[order_id]
        CNT = trade.order_status.nid
        OrderNo = order_id
        RequestId = self._Order.RetriveRequestID()

        operation = Operation(
            nid =RequestId ,
            task=Task.CancelOrder,
            status=Status.Pending,
            op_time= datetime.datetime.now().strftime('%H%M%S') )
        trade.operations.append(operation)
        self._rid[str(RequestId)] = trade

        rt = self._Order.Order(int(Action),
                            int(Market),
                            RequestId,
                            BrokerId,
                            Account,
                            '',
                            Symbol,
                            Side,
                            int(PriceFlag),
                            str(Price),
                            int(Tif),
                            int(Qty),
                            int(PE),
                            0,
                            WebID,
                            CNT,
                            OrderNo)
        if rt != 0:
            rt = rt -2**64
            print(f'Order return {rt}, {self._Order.GetOrderErrMsg(rt)}')  
            trade.operations.pop()
            return 
        
        return trade

    def update_order(self ,order_id ,price=None, qty=None):
        """
        修改既有的期貨或選擇權委託單（限改價或改量擇一）。

        Parameters
        ----------
        order_id : str
            原委託單號，需為已存在的 Trade 物件

        price : float 
            修改後的價格。可輸入數值（限價）或價格型態（僅支援 PriceType.LMT 或 PriceType.StopLossMarket）
            注意：不可使用 PriceType.MKT

        qty : int, optional
            要減少的口數，僅支援減量（不可增加數量）
            欲修改為的新剩餘數量為：原數量 - qty

        Returns
        -------
        Trade
            更新後的 Trade 物件（含追加的 Operation 記錄）

        Raises
        ------
        ValueError
            - 若 `price` 與 `qty` 同時輸入或皆未輸入
            - 若 `price` 為 MKT 類型
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
        if trade.order.price_type != PriceType.LMT and price is not None:
                raise ValueError("只有限價單可以改價")

        if price is None: #改量
            Action = 2
            Price = 0
            PriceFlag = PriceType.LMT.value

        if qty is None:#改價
            Action = 3
            Price = price
            PriceFlag = PriceType.LMT.value
            qty=0

        Symbol = trade.order.symbol
        Market = 1 if len(Symbol)== 10 else 0
        BrokerId =self._broker_id 
        Account = self._account
        Side = trade.order.action.value
        Tif = trade.order.time_in_force.value
        Qty = qty
        PE  = 3
        WebID = self._webid[order_id]
        CNT = trade.order_status.nid
        OrderNo = order_id
        RequestId = self._Order.RetriveRequestID()

        if Action==3: #改價
            task=Task.UpdatePrice
            msg= f"NewPrice_{Price}"
            newqty =trade.order_status.modified_quantity
            newprice = Price
        elif Action==2: #改量
            task=Task.UpdateQty
            AfterQty= trade.order_status.modified_quantity -int(Qty)
            if AfterQty <0 :
                raise ValueError("減量不可多餘剩餘量")

            msg= f"NewQty_{AfterQty}"
            newqty = AfterQty
            newprice =trade.order_status.modified_price

        operation = Operation(
            nid =RequestId ,
            task=task,
            status= Status.Pending,
            msg= msg,
            op_time= datetime.datetime.now().strftime('%H%M%S') )
        trade.operations.append(operation)
        self._rid[str(RequestId)] = trade

        rt = self._Order.Order(int(Action),
                            int(Market),
                            int(RequestId),
                            BrokerId,
                            Account,
                            '',
                            Symbol,
                            Side,
                            int(PriceFlag),
                            str(Price),
                            int(Tif),
                            int(Qty),
                            int(PE),
                            0,
                            WebID,
                            CNT,
                            OrderNo)
        if rt != 0:
            rt = rt -2**64
            print(f'SecuritySubOrder return {rt}, {self._Order.GetOrderErrMsg(rt)}')
            trade.operations.pop()
            return 
        
        return trade
    
    def _OnOrderPending(self,data):
        rid = data['RequestID']

        #《拿檔》  
        if rid in self._rid.keys():
            trade = self._rid[rid]
            self._cnt[data['CNT']] = rid
        elif data['OrderNo'] in self._trades.keys():
            trade = self._trades[data['OrderNo']]
        else:
            print(f'[系統訊息] 查無源頭 Order: {data}')
            return 
        
        if data['OrderNo']!='' and data['OrderNo'] not in self._trades.keys():
            if data['Error']!= '0':
                ls = self._trades['無效單']
                ls.append(trade)
            else:
                self._trades[data['OrderNo']] = trade
                self._webid[data['OrderNo']] = data['WEBID']
        
        operation = next((x for x in trade.operations if str(x.nid).strip() == str(rid).strip()), None)
        if operation is None:
            operation = next((x for x in trade.operations if str(x.nid).strip() == str(data['CNT']).strip()), None)
        if operation is None:
            operation =Operation()
            trade.operations.append(operation)

        #《data》  
        ts = datetime.datetime.now().strftime('%H%M%S')
        msg = operation.msg

        status =  Status.Pending
        if data['Error'] != '0':
            num = "".join(filter(str.isdigit, data['Error']))
            mg = self._Order.errorMap.get(num, "") 
            msg = mg if not msg else f"{msg}|{mg}"
            status =  Status.Failed  

        #《operation》  
        operation.nid = data['CNT']
        operation.ts = ts
        operation.msg = msg
        operation.status =status

        #《event》
        event = Event(
            task = operation.task,
            status = operation.status ,
            order_id =data['OrderNo'] ,
            seqno = operation.nid,
            action = trade.order.action ,
            category = trade.order.category ,
            symbol = trade.order.symbol ,
            time_in_force = trade.order.time_in_force,
            trade_hour= trade.order.trade_hour ,
            ts = ts ,
            msg =msg
            )

        if event.task ==Task.NewOrder:
            event.quantity = trade.order.quantity
            event.price=  trade.order.price

        elif event.task ==Task.UpdatePrice:
            value = operation.msg.split("_")[1]
            event.quantity = trade.order_status.modified_quantity or trade.order.quantity
            event.price= value

        elif event.task ==Task.UpdateQty:
            value = operation.msg.split("_")[1]
            event.quantity = value
            event.price = trade.order_status.modified_price or trade.order.price

        elif event.task==Task.CancelOrder:
            event.quantity = 0
            event.price= trade.order_status.modified_price or trade.order.price

        if event.price==0:
            event.price = '市價'

        self._event(event)

    def _OnOrderReport(self,data):
        self.order_list.append(data)

        #《拿檔》
        rid=0
        if data['CNT'] in self._cnt:
            rid = self._cnt[data['CNT']]
            trade = self._rid[rid] 
        elif data['OrderNo'] in self._trades.keys():
            trade = self._trades[data['OrderNo']]
        elif data['OrderFunc']=='I': 
            trade= Trade(order=Order(),order_status=OrderStatus(),operations=[])
        else:
            print(f'[系統訊息] 查無源頭 Order: {data}')
            return 
        
        if  data['OrderNo'] not in self._trades.keys():
            if data['ErrCode'] != '0':
                ls = self._trades['無效單']
                ls.append(trade)
            else:
                self._trades[data['OrderNo']] = trade
                self._webid[data['OrderNo']] = data['WEBID']

        operation = next((x for x in trade.operations if str(x.nid).strip() == str(rid).strip()), None)
        if operation is None:
            operation = next((x for x in trade.operations if str(x.nid).strip() == str(data['CNT']).strip()), None)
        if operation is None:
            operation = Operation()
            trade.operations.append(operation)

        #《data》
        category= Category.OPTION if len(data['Symbol'])== 10 else Category.FUTURE

        ts = data['ReportTime'][:-3]

        side = data['BS']
        if side in {a.value for a in Action}:
            side = Action(side)

        qty = _str_to_num(data['AfterQty'])

        price_type = data['PriceFlag']
        if price_type in _PriceType.keys():
            price_type = _PriceType[ price_type]

        price = 0 if price_type is PriceType.MKT  else data['Price']
        price = _str_to_num(price)

        time_in_force = data['TimeInForce']
        if time_in_force in _TIF.keys():
            time_in_force = _TIF[time_in_force]

        tradehour = data['TradeHour']
        if tradehour in {a.value for a in TradeHour}:
            tradehour = TradeHour(tradehour)

        msg= operation.msg
        if data['OrderFunc']=='C':
            mg = f"NewQty_{qty}"
            msg = mg if not msg else f"{msg}|{mg}"
        elif data['OrderFunc']=='R':
            mg = f"NewPrice_{price}"
            msg = mg if not msg else f"{msg}|{mg}"
        if data['ErrCode'] != '0' or data['TaiCode'] in ['47','48']:
            mg =data['ErrMsg']
            msg = mg if not msg else f"{msg}|{mg}"

        ex_status = 'kill' if trade.order_status.status in [Status.Cancelled ,Status.PartFilled_Cancelled] else 'alive'

        #《order》
        if data['OrderFunc']=='I':
            order = Order(
                order_id= data['OrderNo'],
                category= category ,
                action= side ,
                symbol= data['Symbol'],
                quantity= qty ,
                price= price ,
                time_in_force = time_in_force ,
                price_type = price_type ,
                trade_hour = tradehour )
            trade.order = order

        #《order_status》
        if data['ErrCode'] != '0' or ex_status=='kill':
            pass
        else:
            trade.order_status.nid=data['CNT']
            trade.order_status.status=Status.Submitted
            trade.order_status.modified_time = ts
            trade.order_status.modified_quantity = qty
            trade.order_status.modified_price = price
        
            if sum(item.quantity for item in trade.order_status.deals) !=0:
                trade.order_status.status = Status.PartFilled

            if qty==0:
                if sum(item.quantity for item in trade.order_status.deals) ==0:
                    trade.order_status.status = Status.Cancelled
                else:
                    trade.order_status.status= Status.PartFilled_Cancelled

            if trade.order_status.modified_price ==0:
                trade.order_status.modified_price = '市價'

        #《operation》
        operation.nid =  data['CNT']
        operation.op_time= ts
        operation.msg = msg

        if data['OrderFunc']=='I':
            operation.task=Task.NewOrder
            operation.status = Status.Success
        elif data['OrderFunc']=='D':#刪單
            operation.task = Task.CancelOrder
            operation.status = Status.Success
        elif data['OrderFunc']=='C':
            operation.task = Task.UpdateQty
            operation.status = Status.Success
        elif data['OrderFunc']=='R':#改價
            operation.task = Task.UpdatePrice
            operation.status = Status.Success

        if data['ErrCode'] != '0' or ex_status=='kill':
            operation.status =Status.Failed
        
        #《event》
        event = Event(
            task = operation.task,
            status = operation.status ,
            order_id =data['OrderNo'] ,
            seqno = operation.nid,
            action = trade.order.action ,
            category = trade.order.category ,
            symbol = trade.order.symbol ,
            quantity = qty ,
            price= price ,
            time_in_force = trade.order.time_in_force,
            trade_hour= trade.order.trade_hour ,
            ts = operation.op_time ,
            msg = operation.msg 
            )
        
        if event.price==0:
            event.price = '市價'

        if self._show is True:
            self._event(event)

    def _OnExecReport(self,data):
        self.deal_list.append(data)

        #《data》
        symbol = data['Symbol']

        category= Category.OPTION if len(data['Symbol'])== 10 else Category.FUTURE

        side = data['BS']
        if side in {a.value for a in Action}:
            side = Action(side)

        FillQty = _str_to_num(data['DealQty'])

        price = _str_to_num(data['DealPrice'])

        tradehour = data['TradeHour']
        if tradehour in {a.value for a in TradeHour}:
            tradehour = TradeHour(tradehour)

        ts = data['ReportTime']

        #《deal》
        trade = self._trades.get(data['OrderNo'])
        if trade is None:
            print(f'[系統訊息] 查無源頭 Order: {data}')

            event = Event(
                task= Task.Deal,
                order_id = data['OrderNo'],
                seqno = data['CNT'],
                action= side,
                category = category,
                symbol = symbol ,
                quantity= FillQty,
                price= price,
                trade_hour = tradehour , 
                ts = ts
                )

        else:
            deal =Deal(price=price,
                        quantity=FillQty,
                        ts=ts ,
                        reportseq = data['MarketNo'])
            trade.order_status.deals.append(deal)

            order_qty =  trade.order_status.modified_quantity

            if FillQty < order_qty:
                trade.order_status.status = Status.PartFilled
            else:
                trade.order_status.status = Status.Filled
            trade.order_status.modified_quantity = order_qty -FillQty

            event = Event(
                task= Task.Deal,
                order_id = data['OrderNo'],
                seqno = data['CNT'],
                action=trade.order.action,
                category = trade.order.category,
                symbol =symbol ,
                quantity= FillQty,
                price= price,
                time_in_force =trade.order.time_in_force,
                trade_hour = trade.order.trade_hour , 
                ts = ts
                )

        if symbol in self._deals:
            ls=self._deals[symbol]
            ls.append(event)
        else:
            self._deals[symbol] =[event]

        if self._show is True:
            self._event(event)

    def _update(self):
        self._show =False
        self._Order.ReceiveReport(self._broker_id ,self._account)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self.order_list) > N:
                N =len(self.order_list)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1) 
        self._show =True

    def set_event(self, event):
        """
        設定事件回調函數（callback）。

        Parameters
        ----------
        event : Callable
            接收一個資料物件（如委託或成交回報）的函數。
            預設函數為：

            >>> def event(data):
            ...     print(data)

        Notes
        -----
        支援以下 4 種事件類型
            - 'NewOrder'     : 新委託建立
            - 'UpdateOrder'  : 委託修改（如價格/口數調整）
            - 'CancelOrder'  : 委託取消
            - 'Deal'         : 成交回報

        資料格式依市場而異，期貨下列欄位會出現在事件資料中：

            - order_id : str         # 委託單號
            - action : Action        # 買賣別
            - category : str         # 類別
            - symbol : str           # 商品代碼
            - quantity : int         # 委託口數
            - price : float          # 委託價格
            - time_in_force : TimeInForce  # 委託條件
            - trade_hour : Any       # 交易時段
            - ts : str               # 時間戳（hhmmss）
        
        Returns
        -------
        None

        Examples
        --------
        自訂事件處理函數：

            >>> def my_event_handler(data):
            ...     print("商品:", data['symbol'], "價格:", data['price'])

            >>> api.FutOrder.set_event(my_event_handler)

        """
        self._event = event 

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
        
        # hour = pd.Timestamp.now().hour
        # if hour >=8 and hour <14:
        #     hour  = 'R'
        # elif hour >=15 or hour <=5:
        #     hour  = 'P'

        T_dic={}
        for key, value in self._trades.items():
            if key =='無效單':
                continue
            if value.order_status.status  in [Status.PartFilled ,Status.Submitted ,Status.PendingSubmitted]:
                T_dic[key] =value
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
            一個字典，鍵為商品代碼（symbol），
            值為該商品對應的 Deal 物件列表。
        """
        return self._deals
    
    def OrderReport(self):
        """
        取得當前帳戶的委託回報資訊

        Returns:
        DataFrame
            整理後的委託資料，欄位可能包含：
            - OrderFunc: 委託型態（I:新單, C:改量, R:改價, D:刪單）
            - FrontOffice: 委託來源系統代碼
            - SubAccount: 子帳號或營業員代號
            - IB: IB 代碼
            - TaiCode: 回報型態代碼（如 00:一般, 47:退單）
            - TradeHour: 交易時段（R:早盤, P:盤後）
            - Temp: 保留欄位
            - BrokerID: 分公司代號
            - OrderNo: 委託書號
            - ActNo: 帳號
            - TradeDate: 交易日期
            - ReportTime: 回報時間
            - SendTime: 發送時間（格式：YYYYMMDDHHMMSSsss）
            - WEBID: 下單主機別
            - CNT: 下單序號
            - Symbol: 商品代碼
            - BS: 買賣別（B/S）
            - Price: 委託價格
            - TimeInForce: 時效條件（F: FOK, I: IOC, R: ROD）
            - PriceFlag: 價格類型（1:市價, 2:限價, 3:停損）
            - PositionEffect: 倉別（0:新倉, 1:平倉, 2:當沖, 4:自動）
            - BeforeQty: 改量前數量
            - AfterQty: 改量後數量
            - ErrCode: 錯誤代碼
            - ErrLength: 錯誤訊息長度
            - ErrMsg: 錯誤訊息（UTF-8 中文）
    
        Notes
        -----
            欄位內容來源自期貨 OnOrderReport 2010 回傳資料

        """
        df = pd.DataFrame(self.order_list)
        col = [
            'OrderFunc', 'FrontOffice', 'SubAccount', 'IB', 'TaiCode', 'TradeHour', 'Temp',
            'BrokerID', 'OrderNo', 'ActNo', 'TradeDate', 'ReportTime', 'SendTime', 'WEBID',
            'CNT', 'Symbol', 'BS', 'Price', 'TimeInForce', 'PriceFlag', 'PositionEffect',
            'BeforeQty', 'AfterQty', 'ErrCode', 'ErrLength', 'ErrMsg'
        ]

        if df.empty:
            df = pd.DataFrame(columns=col)
        else:
            # 過濾存在欄位，避免KeyError
            exist_cols = [c for c in col if c in df.columns]
            df = df[exist_cols]

            cols_to_object = ['Price', 'BeforeQty', 'AfterQty']

            for c in cols_to_object:
                if c in df.columns:
                    # 先轉字串，再變object（object類型就是字串存為object）
                    df[c] = df[c].astype(str).astype(object)

        return df

    def ExecReport(self):
        """
        取得當前帳戶的成交回報資訊

        Returns:
        DataFrame
            成交回報資料，欄位可能包含：
                - OrderFunc: 委託型態（F：成交）
                - CNT: 下單序號（Speedy NID）
                - FrontOffice: 委託來源（0:AS400, 1:T-BUS, 2:Speedy-K, 3:Speed 等）
                - SubAccount: 子帳號或營業員代號
                - IB: IB代碼
                - TradeHour: 交易時段（R:早盤, P:盤後）
                - Temp: 保留欄位
                - BrokerID: 分公司代號
                - OrderNo: 委託書號
                - ActNo: 帳號
                - TradeDate: 交易日期
                - ReportTime: 成交回報時間
                - WEBID: 下單主機別
                - Symbol: 商品代碼
                - BS: 買賣別
                - Market: 商品類型（1:期貨, 2:選擇權, 3:選擇權複式, 4:期貨複式）
                - DealPrice: 成交價格（或退單基準價）
                - DealQty: 成交數量
                - CumQty: 累計成交量
                - LeaveQty: 剩餘未成交量
                - MarketNo: 市場成交序號
                - Symbol1: 第一隻腳商品代碼（複式單）
                - DealPrice1: 第一隻腳成交價格（或退單上下限價1）
                - Qty1: 第一隻腳成交數量
                - BS1: 第一隻腳買賣別
                - Symbol2: 第二隻腳商品代碼（複式單）
                - DealPrice2: 第二隻腳成交價格（或退單上下限價2）
                - Qty2: 第二隻腳成交數量
                - BS2: 第二隻腳買賣別

        Notes
        -----
            欄位內容來源自期貨 OnOrderPending 2011 回傳資料
        """

        df = pd.DataFrame(self.deal_list)

        col = [
            # 1. 委託與來源資訊
            'OrderFunc', 'CNT', 'FrontOffice', 'SubAccount', 'IB', 'TradeHour', 'Temp',
            # 2. 帳務與系統編號
            'BrokerID', 'OrderNo', 'ActNo', 'TradeDate', 'ReportTime', 'WEBID',
            # 3. 商品與基本成交資訊
            'Symbol', 'BS', 'Market', 'DealPrice', 'DealQty', 'CumQty', 'LeaveQty', 'MarketNo',
            # 4. 複式單第一隻腳
            'Symbol1', 'DealPrice1', 'Qty1', 'BS1',
            # 5. 複式單第二隻腳
            'Symbol2', 'DealPrice2', 'Qty2', 'BS2'
        ]

        # 若空的，回傳空欄位結構
        if df.empty:
            df = pd.DataFrame(columns=col)
        else:
            df = df[col]

            # 數值欄位轉換（避免 NaN 轉型錯誤）
            cols_to_object = ['CumQty', 'DealQty', 'Qty1', 'Qty2', 'LeaveQty',
                            'DealPrice', 'DealPrice1', 'DealPrice2']
            for c in cols_to_object:
                if c in df.columns:
                    # 先確保數值可轉成字串，再轉object
                    df[c] = df[c].astype(str).astype(object)

        return df

#=====================================================================================
    def PositionSum(self ):
        """
        #期貨分帳庫存彙總查詢

        Returns
        -------
        DataFrame
                包含以下欄位的持倉匯總資料：
                - Symbol: 交易品種代碼 
                - BrokerId: 券商代碼 
                - Account: 帳戶號碼 
                - Group: 帳戶分組 
                - Trader: 交易員代碼 
                - Exchange: 交易所代碼 
                - ComType: 商品類型 (例如 'F' 表示期貨，'O' 表示選擇權)
                - ComID: 商品代碼 
                - ComYM: 商品年月 
                - StrikePrice: 履約價格 
                - CP: 看漲/看跌 (例如 'C' 表示看漲，'P' 表示看跌)
                - BS: 買賣方向 (例如 'B' 表示買，'S' 表示賣)
                - DeliveryDate: 交割日期 
                - Currency: 幣別代碼 
                - OTQty: 未平倉數量 
                - TrdPrice: 成交均價 
                - MPrice: 市場價格 
                - PRTLOS: 未實現損益 
                - DealPrice: 成交價格 

        Notes
        -----
                欄位內容來源自期貨 OnQueryResult 1616 回傳資料
        """
        #for MType in ['I'] : #O國外 I國内
        MType='I'
        BrokerID = self._broker_id
        Account = self._account
        Group =''
        Trader =''
        self._p=[]
        self._Order.RetrivePositionSum( MType ,BrokerID ,Account ,Group ,Trader)

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

        if self._p==[]:
            print('[系統訊息] 未收到響應')
            return
        
        dfs = [pd.DataFrame(item) for item in self._p]
        dfs = pd.concat(dfs)

        col = [
            'Symbol',
            'BrokerId',       # 3 分公司代號
            'Account',        # 4 帳號
            'Group',          # 5 第二層組別
            'Trader',         # 6 第三層交易員
            'Exchange',       # 7 交易所
            'ComType',        # 8 F:期貨 O:期權
            'ComID',          # 9 商品代碼
            'ComYM',          # 10 商品年月
            'StrikePrice',    # 11 履約價7位整數，6位小數
            'CP',             # 12 買賣權
            'BS',             # 13 B/S
            'DeliveryDate',   # 14 交割日期YYYMMDD For國外
            'Currency',       # 15 幣別
            'OTQty',          # 16 未平倉量
            'TrdPrice',       # 17 結算價7位整數，6位小數
            'MPrice',         # 18 即時價7位整數，6位小數
            'PRTLOS',         # 19 第一位正負號(正:空白負:-) 十一位整數、二位小數右靠左補零
            'DealPrice'       # 20 成交均價整數7位小數後6位
        ]
        
        if dfs.empty==True:
            dfs = pd.DataFrame(columns=col)
            return dfs

        dfs['DealPrice']= dfs['DealPrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['MPrice']= dfs['MPrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['OTQty']= dfs['OTQty'].apply(lambda x: Decimal(int(x)))
        dfs['PRTLOS']= dfs['PRTLOS'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['StrikePrice']= dfs['StrikePrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['TrdPrice']= dfs['TrdPrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs.loc[dfs.Exchange.str.strip() =='TIMEX','Symbol'] = dfs[dfs.Exchange.str.strip() == 'TIMEX'].apply(compose_symbol, axis=1)
        #dfs['Symbol'] = dfs.apply(compose_symbol, axis=1)
        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs

    def _profit(self,js_string):
        data = json.loads(js_string)
        self._p.append(data)

#===================================================================================== 
    def PositionDetail(self):
        """
        取得期貨分帳庫存明細資料 

        Returns
        -------
        pd.DataFrame
            分帳庫存明細的結果資料表，包含以下欄位（若部分欄位不存在則跳過）：
            - Symbol: 商品代碼
            - SPREAD: 複式單註記
            - spKey: 複式單識別碼
            - BrokerId: 分公司代號
            - Account: 帳號
            - Group: 第二層分組
            - Trader: 第三層交易員
            - Exchange: 交易所
            - seqNo: 場內編號（國外用）
            - FcmActNo: 上手代碼（國外用）
            - tradeType: 交易方式
            - Fcm: 下單經紀商
            - DeliveryDate: 交割日
            - CloseDate: 結算日
            - WEB: 下單方式（WEB 或空白）
            - Cnt: 電子單號
            - OrdNo: 委託書號
            - MarketNo: 成交序號
            - sNo: 拆單序號
            - trade_date: 成交日期
            - com_id: 商品代碼
            - BS: 買賣別
            - ComID: 商品代碼
            - ComType: 商品類型（F 期貨、O 選擇權）
            - CP: 買權/賣權（C/P）
            - StrikePrice: 履約價（六位小數）
            - ComYM: 商品年月
            - Qty: 未平倉數量
            - TrdPrice: 成交均價
            - MPrice: 即時價
            - PRTLOS: 未實現損益
            - InitialMargin: 原始保證金
            - MTMargin: 維持保證金
            - Currency: 幣別
            - DealPrice: 成交價
            - mixQty1: 混合口數
            - DayTrade: 當沖註記（Y/N/R）
            
        Notes
        -----
            欄位內容來源自期貨 OnQueryResult 1618 回傳資料
        """
        MType='I'
        BrokerID = self._broker_id
        Account = self._account
        Group =''
        Trader =''
        self._pd=[]
        self._Order.RetrivePositionDetail( MType ,BrokerID ,Account ,Group ,Trader)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._pd) > N:
                N =len(self._pd)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._pd==[]:
            print('[系統訊息] 未收到響應')
            return
        
        dfs = [pd.DataFrame(item) for item in self._pd]
        dfs = pd.concat(dfs)
        col = ['Symbol','SPREAD','spKey','BrokerId','Account','Group', 
            'Trader','Exchange','seqNo','FcmActNo','tradeType', 
            'Fcm', 'DeliveryDate', 'CloseDate','WEB','Cnt', 
            'OrdNo','MarketNo','sNo','TradeDate','ComID',
            'BS','ComType', 'CP','StrikePrice', 'ComYM', 
            'Qty','TrdPrice','MPrice', 'PRTLOS','InitialMargin', 
            'MTMargin','Currency','DealPrice', 'mixQty1','DayTrade']

        if dfs.empty==True:
            dfs = pd.DataFrame(columns=col)
            return dfs
        
        dfs['DealPrice']= dfs['DealPrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['InitialMargin']= dfs['InitialMargin'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['MPrice']= dfs['MPrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['MTMargin']= dfs['MTMargin'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['PRTLOS']= dfs['PRTLOS'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['StrikePrice']= dfs['StrikePrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['TrdPrice']= dfs['TrdPrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['Qty']= dfs['Qty'].apply(lambda x: Decimal(int(x))) #.astype(int)
        dfs =dfs.rename(columns={'com_id':'ComID','trade_date':'TradeDate'})
        dfs.loc[dfs.Exchange.str.strip() =='TIMEX','Symbol'] = dfs[dfs.Exchange.str.strip() == 'TIMEX'].apply(compose_symbol, axis=1)
        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]

        #下面處理組合單
        cols = [
            'Symbol','OrdNo', 'MarketNo', 'sNo', 'BS', 'ComType', 'CP',
            'StrikePrice', 'ComID','ComYM', 'Qty', 'TrdPrice', 'MPrice',
            'PRTLOS', 'InitialMargin', 'MTMargin', 'Currency', 'DealPrice']
        ls=[]
        row_last= None
        for i, row in dfs.iterrows():
            if row_last is not None:
                new_cols = {}
                for c in cols:
                    new_cols[c+'2'] = row[c]
                combined = pd.concat([row_last, pd.Series(new_cols)])
                ls.append(combined)
                row_last=None
            else:
                if row['SPREAD']=='N':
                    ls.append(row)
                else:
                    row_last=row

        dfx=pd.DataFrame(ls).reset_index(drop=True).fillna('')
        dfx = dfx.reset_index(drop=True)
        return dfx

    def _profit_detail(self,js_string):
        data = json.loads(js_string)
        self._pd.append(data)

#=====================================================================================
    def COVER(self):
        """
        查詢期貨平倉

        Returns
        -------
        pd.DataFrame
            包含以下欄位的平倉明細資料：
            - Symbol : 商品代碼
            - BrokerId : 分公司代號
            - Account : 帳號
            - Group : 組別
            - Trader : 交易員代號
            - Exchange : 交易所代碼
            - ComID : 商品代碼
            - ComYM : 商品年月
            - StrikePrice : 履約價格
            - CP : 買賣權（CALL:C / PUT:P / 空白:期貨）
            - CURRENCY : 幣別
            - PRTLOS : 平倉損益
            - CTAXAMT : 交易稅
            - ORIGNFEE : 手續費
            - OSPRTLOS : 淨損益
            - Qty : 平倉口數，整數
            - OSPRTLOS_TWD : 約當台幣淨損益

        Notes
        -----
            - 欄位內容來源自期貨 OnQueryResult 1614 回傳資料。
        """
        MType='I'
        BrokerID = self._broker_id
        Account = self._account
        Group =''
        Trader =''
        Exchange= "******"
        ComID=''
        ComYM=''
        StrikePrice=''
        CP=''
        self._rp=[]
        self._Order.RetriveCOVER(MType,BrokerID,Account,Trader,
            ComID,ComYM,StrikePrice,CP,Exchange ,Group)
        
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

        if self._rp==[]:
            print('[系統訊息] 未收到響應')
            return
        
        col = [
            'Symbol','BrokerId', 'Account', 'Group', 'Trader', 'Exchange', 'ComID', 'ComYM',
            'StrikePrice', 'CP', 'CURRENCY', 'PRTLOS', 'CTAXAMT', 'ORIGNFEE',
            'OSPRTLOS', 'Qty', 'OSPRTLOS_TWD',
        ]

        dfs = [pd.DataFrame(item) for item in self._rp]
        dfs = pd.concat(dfs)

        if dfs.empty==True:
            dfs = pd.DataFrame(columns=col)
            return dfs

        dfs['CTAXAMT']= dfs['CTAXAMT'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['ORIGNFEE']= dfs['ORIGNFEE'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['OSPRTLOS']= dfs['OSPRTLOS'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['OSPRTLOS_TWD']= dfs['OSPRTLOS_TWD'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['PRTLOS']= dfs['PRTLOS'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['StrikePrice']= dfs['StrikePrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['Qty']= dfs['Qty'].apply(lambda x: Decimal(int(x)))
        dfs.loc[dfs.Exchange.str.strip() =='TIMEX','Symbol'] = dfs[dfs.Exchange.str.strip() == 'TIMEX'].apply(compose_symbol, axis=1)
        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs

    def _realprofit(self ,js_string):
        data = json.loads(js_string)
        self._rp.append(data)  

#=====================================================================================
    def COVERDetail(self):
        """
        查詢期貨平倉明

        Returns
        ----
        pd.DataFrame
            回傳包含以下欄位的平倉明細資料，且對金額及數量欄位已完成格式轉換：
            - Symbol : 商品代碼（若 Exchange 為 'TIMEX'，自動組合）
            - BrokerId : 分公司代號
            - Account : 帳號
            - Group : 組別
            - Trader : 交易員
            - Exchange : 交易所代碼
            - TrdDT1 : 平倉成交日期（YYYYMMDD）
            - OrdNo1 : 平倉委託編號
            - FirmOrd1 : 平倉成交序號
            - OffsetSpliteSeqNo : 平倉拆單序號
            - TrdDT2 : 被平成交日期（YYYYMMDD）
            - OrdNo2 : 被平委託編號
            - FirmOrd2 : 被平成交序號
            - OffsetSpliteSeqNo2 : 被平拆單序號
            - OffsetCode : 指定平倉碼 (Y/N)
            - offset : 互抵狀態 (N/Y/Z)
            - BS : 買賣別 (B/S)
            - ComID : 平倉商品代號
            - ComYM : 平倉商品年月
            - StrikePrice : 履約價格
            - CP : 買賣權（C: CALL，P: PUT，空白: 期貨）
            - ComID2 : 被平商品代號
            - QTY1 : 平倉口數
            - QTY2 : 被平口數
            - TRDPRC1 : 平倉成交價
            - TRDPRC2 : 被平成交價
            - PRTLOS : 平倉損益
            - AENO : 業務員代號
            - CURRENCY : 幣別
            - CTAXAMT : 交易稅
            - ORIGNFEE : 手續費
            - Premium1 : 平倉權利金
            - Premium2 : 被平權利金
            - InNo1 : 平倉場內編號
            - InNo2 : 被平場內編號
            - Cnt1 : 平倉電子單號
            - Cnt2 : 被平電子單號
            - OSPRTLOS : 淨損益

        Notes
        -----
            - 欄位內容來源自期貨 OnQueryResult 1624 回傳資料。

        """
        MType='I'
        BrokerID = self._broker_id
        Account = self._account
        Group =''
        Trader =''
        Exchange= "******"
        ComID=''
        ComYM=''
        StrikePrice=''
        CP=''
        RequestID = self._Order.RetriveRequestID()
        self._rpd=[]
        self._Order.RetriveCOVERDetail(MType,BrokerID,Account,Trader,
                ComID,ComYM,StrikePrice,CP,Exchange,RequestID ,Group)
        
        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._rpd) > N:
                N =len(self._rpd)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._rpd==[]:
            print('[系統訊息] 未收到響應')
            return
        
        dfs = [pd.DataFrame(item) for item in self._rpd]
        dfs = pd.concat(dfs)
        
        col = [
            'Symbol','OffsetSpliteSeqNo' ,  
            #'BrokerId', 'Account', 'Group', 'Trader', 'Exchange', 'TrdDT1', 'OrdNo1', 'FirmOrd1'
            'TrdDT2'	,'OrdNo2'	,'FirmOrd2',	'OffsetSpliteSeqNo2' ,'OffsetCode'	,'offset',
            'BS',	'ComID'	,'ComYM'	,'StrikePrice'	,'CP'	,'ComID2'	,'QTY1'	,'QTY2'	,'TRDPRC1'	,'TRDPRC2'	,
            'PRTLOS',	'AENO'	,'CURRENCY','CTAXAMT'	,'ORIGNFEE'	,'Premium1'	,'Premium2'	,'InNo1'	,'InNo2' ,'CNT1'	,'CNT2' ,'OSPRTLOS'
        ]

        if dfs.empty==True:
            dfs = pd.DataFrame(columns=col)
            return dfs

        dfs['CTAXAMT']= dfs['CTAXAMT'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['ORIGNFEE']= dfs['ORIGNFEE'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['OSPRTLOS']= dfs['OSPRTLOS'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['PRTLOS']= dfs['PRTLOS'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['TRDPRC1']= dfs['TRDPRC1'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['TRDPRC2']= dfs['TRDPRC2'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['QTY1']= dfs['QTY1'].apply(lambda x: Decimal(int(x)))
        dfs['QTY2']= dfs['QTY2'].apply(lambda x: Decimal(int(x)))
        dfs['Premium1']= dfs['Premium1'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['Premium2']= dfs['Premium2'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs['StrikePrice']= dfs['StrikePrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
        dfs['Symbol'] = dfs.apply(compose_symbol, axis=1)
        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs

    def _realprofit_detail(self ,js_string):
        data = json.loads(js_string)
        self._rpd.append(data)

#=====================================================================================
    def Margin(self):
        """
        分帳權益數

        Returns
        ----
        pd.DataFrame
            回傳包含以下欄位的分帳權益資料，且對金額及數量欄位已完成格式轉換：
            - Code : 回傳代碼
            - Count : 資料筆數
            - BrokerId : 分公司代號
            - Account : 帳號
            - Group : 第二層組別
            - Trader : 第三層交易員
            - CURRENCY : 幣別 (TWD:國內台幣；US$:國內美元；**:約當台幣)
            - LCTDAB : 前日帳款餘額（含正負號＋11位整數＋2位小數）
            - ORIGNFEE : 手續費（格式同上）
            - TAXAMT : 匯率（格式同上）
            - CTAXAMT : 期交稅（格式同上）
            - DWAMT : 存提金額（格式同上）
            - OSPRTLOS : 平倉損益（格式同上）
            - PRTLOS : 未平倉損益（格式同上）
            - BMKTVAL : 未沖銷買方選擇權市值（格式同上）
            - SMKTVAL : 未沖銷賣方選擇權市值（格式同上）
            - OPREMIUM : 委託下單預扣權利金（格式同上）
            - TPREMIUM : 當日權利金收支（格式同上）
            - EQUITY : 淨值權益數（格式同上）
            - IAMT : 原始保證金（格式同上）
            - MAMT : 維持保證金（格式同上）
            - EXCESS : 可用餘額（格式同上）
            - ORDEXCESS : 下單可用保證金（格式同上）
            - ORDAMT : 當日委託保證金（格式同上）
            - ExProfit : 到期履約損益（格式同上）
            - Premium : 變動權利金未沖銷選擇權市值
            - PTime : 洗價時間（hhmmss）
            - FloatProfit : 盤中浮動損益（格式同上）
            - LASSPRTLOS : 昨日未平倉損益（格式同上）
            - CLOSEAMT : 到期結算保證金（格式同上）
            - ORDIAMT : 足額原始保證金（格式同上）
            - ORDMAMT : 足額維持保證金（格式同上）
            - DayTradeAMT : 當沖原始保證金（格式同上）
            - ReductionAMT : 多空減收保證金
            - CreditAMT : 當沖應補保證金
            - balance : 本日餘額
            - IPremium : 本日權利金收入
            - OPremium : 本日權利金支出
            - Securities : 有價證券價值
            - SecuritiesOffset : 有價證券抵繳金額
            - OffsetAMT : 委託抵繳保證金
            - Offset : 剩餘可抵繳金額（格式同上）
            - FULLMTRISK : 足額總市值風險（格式同上）
            - FULLRISK : 足額風險係數（格式同上）
            - MarginCall : 追繳金額（格式同上）
            - SellVerticalSpread : 賣方垂直價差市值（格式同上）
            - StrikPrice : 履約價款（格式同上）
            - ActMarketValue : 帳戶總市值權益總值（格式同上）
            - TPRTLOS : 本日期貨平倉損益淨額（格式同上，不含履約損益）
            - AddMargin : 加收保證金（格式同上）
        
        Notes
        -----
            - 欄位內容來源自期貨 OnQueryResult 1626 回傳資料。

        
        """
        MType='I'
        BrokerID = self._broker_id
        Account = self._account
        Group =''
        Trader =''
        self._bal=[]
        self._Order.RetriveFMargin(MType ,BrokerID ,Account ,Group ,Trader)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._bal) > N:
                N =len(self._bal)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._bal==[]:
            print('[系統訊息] 未收到響應')
            return
        
        dfs = [pd.DataFrame(item) for item in self._bal]
        dfs = pd.concat(dfs)

        col = [
        'BrokerId', 'Account', 'Group', 'Trader', 'CURRENCY', 'LCTDAB',
        'ORIGNFEE', 'TAXAMT', 'CTAXAMT', 'DWAMT', 'OSPRTLOS', 'PRTLOS', 'BMKTVAL', 'SMKTVAL',
        'OPREMIUM', 'TPREMIUM', 'EQUITY', 'IAMT', 'MAMT', 'EXCESS', 'ORDEXCESS', 'ORDAMT',
        'ExProfit', 'Premium', 'PTime', 'FloatProfit', 'LASSPRTLOS', 'CLOSEAMT', 'ORDIAMT',
        'ORDMAMT', 'DayTradeAMT', 'ReductionAMT', 'CreditAMT', 'balance', 'IPremium', 'OPremium',
        'Securities', 'SecuritiesOffset', 'OffsetAMT', 'Offset', 'FULLMTRISK', 'FULLRISK',
        'MarginCall', 'SellVerticalSpread', 'StrikPrice', 'ActMarketValue', 'TPRTLOS', 'OMarginCall', 'AddMargin'
        ]

        if dfs.empty==True:
            dfs = pd.DataFrame(columns=col)
            return dfs

        for c  in dfs.columns:
            if c in ['Account','BrokerId','CURRENCY','Group','PTime','Trader']:
                continue
            dfs[c]= dfs[c].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))

        dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
        dfs = dfs.reset_index(drop=True)
        return dfs

    def _balance(self ,js_string):
        data = json.loads(js_string)
        self._bal.append(data)

    def Margin_EX(self):
        """
        分帳權益數可出金金額

        Notes
        -----
                - 欄位內容來源自期貨 OnQueryResult 1639 回傳資料。
        """
        MType='I'
        BrokerID = self._broker_id
        Account = self._account
        Group =''
        Trader =''
        self._balX=[]
        self._Order.RetriveFMargin_EX(MType ,BrokerID ,Account ,Group ,Trader)

        start_time = time.time()
        N= 0
        last_data_time = start_time
        while time.time() - start_time < 5:
            if len(self._balX) > N:
                N =len(self._balX)
                last_data_time = time.time() 
            if time.time() -last_data_time > 1 and  N>0:
                break
            time.sleep(0.1)

        if self._balX==[]:
            print('[系統訊息] 未收到響應')
            return
        
        col = [
            'BrokerId', 'Account', 'Group', 'Trader', 'CURRENCY', 'LCTDAB',
            'ORIGNFEE', 'TAXAMT', 'CTAXAMT', 'DWAMT', 'OSPRTLOS', 'PRTLOS',
            'BMKTVAL', 'SMKTVAL', 'OPREMIUM', 'TPREMIUM', 'EQUITY', 'IAMT',
            'MAMT', 'EXCESS', 'ORDEXCESS', 'ORDAMT', 'ExProfit', 'Premium',
            'PTime', 'FloatProfit', 'LASSPRTLOS', 'CLOSEAMT', 'ORDIAMT',
            'ORDMAMT', 'DayTradeAMT', 'ReductionAMT', 'CreditAMT', 'balance',
            'IPremium', 'OPremium', 'Securities', 'SecuritiesOffset', 'OffsetAMT',
            'Offset', 'FULLMTRISK', 'FULLRISK', 'MarginCall', 'SellVerticalSpread',
            'StrikPrice', 'ActMarketValue', 'TPRTLOS', 'MarginCall', 'AddMargin',
            'ORDAMTNOCN', 'WithdrawMnt', 'Account', 'ActMarketValue', 'AddMargin', 'BMKTVAL',
            'CLOSEAMT', 'CTAXAMT', 'CURRENCY', 'CreditAMT', 'DWAMT', 'DayTradeAMT',
            'EQUITY', 'EXCESS', 'ExProfit', 'FULLMTRISK', 'FULLRISK', 'FloatProfit',
            'Group', 'IAMT', 'IPremium', 'LASSPRTLOS', 'LCTDAB', 'MAMT',
            'MarginCall', 'OMarginCall', 'OPREMIUM', 'OPremium', 'ORDAMT',
            'ORDEXCESS', 'ORDIAMT', 'ORDMAMT', 'ORIGNFEE', 'OSPRTLOS', 'Offset',
            'OffsetAMT', 'PRTLOS', 'PTime', 'Premium', 'ReductionAMT', 'SMKTVAL',
            'Securities', 'SecuritiesOffset', 'SellVerticalSpread', 'StrikPrice',
            'TAXAMT', 'TPREMIUM', 'TPRTLOS', 'Trader', 'WithdrawMnt',
            'balance'
        ]
        
        dfs = [pd.DataFrame(item) for item in self._balX]
        dfs = pd.concat(dfs)

        if dfs.empty==True:
            dfs = pd.DataFrame(columns=col)
            return dfs
        
        for c in [
            'LCTDAB', 'ORIGNFEE', 'TAXAMT', 'CTAXAMT', 'DWAMT', 'OSPRTLOS', 'PRTLOS',
            'BMKTVAL', 'SMKTVAL', 'OPREMIUM', 'TPREMIUM', 'EQUITY', 'IAMT', 'MAMT',
            'EXCESS', 'ORDEXCESS', 'ORDAMT', 'ExProfit', 'Premium', 'FloatProfit',
            'LASSPRTLOS', 'CLOSEAMT', 'ORDIAMT', 'ORDMAMT', 'DayTradeAMT', 'ReductionAMT',
            'CreditAMT', 'balance', 'IPremium', 'OPremium', 'Securities', 'SecuritiesOffset',
            'OffsetAMT', 'Offset', 'FULLMTRISK', 'FULLRISK', 'MarginCall', 'SellVerticalSpread',
            'StrikPrice', 'ActMarketValue', 'TPRTLOS', 'AddMargin', 'ORDAMTNOCN', 'WithdrawMnt'
            ]:
            dfs[c]= dfs[c].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
        dfs = dfs.reset_index(drop=True)
        return  dfs

    def _balanceX(self ,js_string):
        data = json.loads(js_string)
        self._balX.append(data)

#=====================================================================================
    def get_position(self):
        """
        取得目前所有持倉資訊。

        此方法會回傳一個包含目前帳戶中所有部位的資料，通常包含商品代碼、買進/賣出部位、未實現損益等欄位。

        Returns:
            pd.DataFrame: 含有每一筆持倉資料的 DataFrame，欄位可能包含：
                - symbol (str): 商品代碼
                - quantity_B (int): 多單部位數量
                - quantity_S (int): 空單部位數量
                - quantity_td (int): 當日平倉部位數量
                - dealprice (float): 成交價格
                - lastprice (float): 最新價格
                - unrealized (float): 未實現損益
                - realized (float): 已實現損益
        """
        # 建立空 DataFrame，symbol 為欄位
        pos = self.PositionSum()
        rl = self.COVER()
        d = self.get_deals()

        symbols_list = set(d.keys()) | set(pos.Symbol.unique()) | set(rl.Symbol.unique())

        df = pd.DataFrame(columns=[
            'symbol', 'quantity_B', 'quantity_S', 'quantitytd_B_S',
            'dealprice_B_S', 'lastprice', 'unrealized', 'realized'
        ])

        for symbol, deal_list in d.items():
            row = {
                'symbol': symbol, 'quantity_B': 0, 'quantity_S': 0, 'quantitytd_B_S': 0,
                'dealprice_B_S': 0, 'lastprice': 0, 'unrealized': 0, 'realized': 0
            }
            qty_b = sum(deal.quantity for deal in deal_list if deal.action == Action.Buy)
            qty_s = sum(deal.quantity for deal in deal_list if deal.action == Action.Sell)
            row['quantity_B'] = qty_b
            row['quantity_S'] = qty_s
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)

        df = df.set_index('symbol')

        for symbol in symbols_list:
            if symbol not in df.index:
                df.loc[symbol] = 0

            p = pos[pos.Symbol == symbol]
            qty_td = [p[p.BS == "B"]['OTQty'].sum() , p[p.BS == "S"]['OTQty'].sum()]
            prtlos = p.PRTLOS.sum()
            lastprice = p['MPrice'].mean()
            buy = p[p.BS == "B"]
            sell = p[p.BS == "S"]
            buy_wap = (buy['DealPrice'] * buy['OTQty']).sum() / buy['OTQty'].sum() if not buy.empty and buy['OTQty'].sum() > 0 else 0
            sell_wap = (sell['DealPrice'] * sell['OTQty']).sum() / sell['OTQty'].sum() if not sell.empty and sell['OTQty'].sum() > 0 else 0
            if float(buy_wap).is_integer():
                buy_wap = int(buy_wap)
            else:
                buy_wap = round(float(buy_wap), 2)
            if float(sell_wap).is_integer():
                sell_wap = int(sell_wap)
            else:
                sell_wap = round(float(sell_wap), 2)

            df.at[symbol, 'quantitytd_B_S'] = qty_td
            df.at[symbol, 'unrealized'] = prtlos
            df.at[symbol, 'lastprice'] = lastprice
            df.at[symbol, 'dealprice_B_S'] = [buy_wap, sell_wap]

            cover = rl[rl.Symbol == symbol]
            realize = cover.OSPRTLOS.sum()
            df.at[symbol, 'realized'] = realize

        df = df.reset_index()
        df.fillna(0, inplace=True)

        df = df[['symbol','quantity_B', 'quantity_S',
            'quantitytd_B_S', 'dealprice_B_S', 'lastprice', 'realized', 'unrealized']]
        return df
    
