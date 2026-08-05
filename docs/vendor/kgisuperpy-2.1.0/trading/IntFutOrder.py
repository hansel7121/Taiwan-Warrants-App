import json ,time ,datetime
import pandas as pd
import numpy as np
from typing import Union
from ._trade_base import *
from decimal import Decimal
 
_TIF={'R':TimeInForce.ROD,
      'F':TimeInForce.FOK,
      'I':TimeInForce.IOC}

_PriceType={'1':PriceType.MKT,
            '2':PriceType.LMT,
            '3':PriceType.StopLossMarket,
            '4':PriceType.StopLossLimit}

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

class IntFutOrderAPI:
      def   __init__(self , ObjOrder ,broker_id , account ):
            self._Order = ObjOrder
            self._broker_id = broker_id
            self._account = account
            self._uid = ObjOrder.person_id 
            self._trades = {'無效單':[]}
            self._deals={}

            self.contracts = ObjOrder.get_Fcontracts('df')
            self._rid ={}
            self._cnt ={}
            self._info={}
            self._pending=[]
            self._order_report=[]
            self._exec_report=[]
            self._p=[]
            self._pd=[]
            self._rp=[]
            self._rpd=[]
            self._bal=[]
            self._balX =[]
            self.set_event(event)

      def   create_order(self ,
                        exchange:str ,

                        action:Action ,
                        com_id:str, 
                        cp:CP,
                        com_ym :str,
                        strike_price =None ,
                        
                        action2:Action =None,
                        com_id2:str=None,
                        cp2:CP=None,
                        com_ym2=None,
                        strike_price2=None,

                        qty :int =None,
                        price : Union[float, int]= 0,
                        price_type : PriceType =PriceType.LMT,
                        stop_price : Union[float, int]= None,
                        time_in_force=TimeInForce.ROD 
                        ):
            """
            exchange :交易所

            action : Action
            買進或賣出動作，使用 enum Action，例如：
                - Action.Buy：買進
                - Action.Sell：賣出
            com_id : 商品代碼
            cp : CP 期貨、買賣權
            com_ym : 商品年月
            strike_price : 履約價

            qty :
            price :
            price_type :
            stop_price :
            time_in_force :
            """
            OrderKind =0
            orderType =0  #0:新單 1:刪單 2:改量 3:改價 4:改單
            RequestId = self._Order.RetriveRequestID()
            marketFlag = 0 if cp == CP.Futures else 1 
            BrokerID = self._broker_id
            Account = self._account
            AE = ''
            #exchange
            df = self.contracts[self.contracts.Exchange==exchange]
            if df.shape[0]==0:
                  raise ValueError("查無此交易所")
            #com_id
            if com_id not in df.ComID.tolist():
                  raise ValueError("此交易所，查無此COMID")
            #com_ym
            if not (isinstance(com_ym, (str, int)) and str(com_ym).isdigit() and len(str(com_ym)) == 6):
                  raise ValueError("com_ym 格式錯誤，應為6碼數字（如 202509）")
            if strike_price is None:
                  strike_price=''
            if strike_price2 is None:
                  strike_price2=''
            if isinstance(strike_price, (float, int)):
                  strike_price= strike_price
                  if strike_price == int(strike_price): 
                        strike_price = int(strike_price) 
            #cp #買賣權, ‘C’ 買權, ‘P’ 賣權, ‘F’ 期貨
            #action #買賣別, ‘B’ 買進, ‘S’ 賣出
            if price_type not in [PriceType.LMT ,PriceType.MKT ,PriceType.StopLossMarket ,PriceType.StopLossLimit ]:
                  raise ValueError("外期PriceType僅支持 LMT、MKT、StopLossLimit 及StopLossMarket  ") 
            PriceFlag =price_type
            DayTrade = 'N'
            PF = 3 #0:新倉 1:平倉 2:當沖 3:自動 
            #time_in_force
            #OrderPrice
            if price_type in [PriceType.MKT ,PriceType.StopLossMarket]:
                  OrderPrice=0
            else:
                  if isinstance(price, (float, int)):
                        OrderPrice =price
                        if OrderPrice == int(OrderPrice): 
                              OrderPrice = int(OrderPrice)    
            if stop_price is None:
                stop_price=0  
            if isinstance(stop_price, (float, int)):
                  stop_price= stop_price
                  if stop_price == int(stop_price): 
                        stop_price = int(stop_price)  
            #qty
            Idno = self._uid
            FCM = ''
            FFUT_ACCOUNT =''
            SOURCE = ''
            #新單不用輸入
            CNT =''
            ordno = ''
            TradeDate = ''
            WEBID =''
            KeyIn =''

            symbol=Symbol( action=action ,
                          com_id=com_id ,
                          cp=cp ,
                          com_ym=com_ym ,
                          strike_price=strike_price)
            if action2 is not None:
                  MLEG='Y'
                  symbol2=Symbol( action=action2 ,
                              com_id=com_id2 ,
                              cp=cp2 ,
                              com_ym=com_ym2 ,
                              strike_price=strike_price2)
            else:
                  symbol2=None
                  
            order = Order(
                  market=exchange,
                  symbol = symbol,
                  symbol2= symbol2,
                  quantity= qty,
                  price =OrderPrice,
                  price_type =PriceFlag,
                  stop_price = stop_price,
                  time_in_force= time_in_force,
                  )
            order_status = OrderStatus()
            operation = Operation(
                  nid =RequestId ,
                  task=Task.NewOrder,
                  status= Status.Pending,
                  op_time= datetime.datetime.now().strftime('%H%M%S') )
            trade = Trade(order=order ,
                          order_status=order_status ,
                          operations=[operation])
            self._rid[str(RequestId)] = trade

            if action2 is None:
                  rt =self._Order.FOrderSingle(
                                          OrderKind,
                                          orderType,
                                          RequestId,
                                          marketFlag,
                                          BrokerID,
                                          Account,
                                          AE,
                                          exchange,
                                          com_id,
                                          com_ym,
                                          str(strike_price),
                                          cp.value,
                                          action.value,
                                          int(PriceFlag.value),
                                          DayTrade,
                                          PF,
                                          time_in_force.value ,
                                          str(OrderPrice),
                                          str(stop_price),
                                          qty,
                                          Idno,
                                          FCM,
                                          FFUT_ACCOUNT,
                                          SOURCE,
                                          CNT,
                                          ordno,
                                          TradeDate,
                                          WEBID,
                                          KeyIn)
            else:
                  rt =self._Order.FOrderMulti(
                                          OrderKind,
                                          orderType,
                                          RequestId,
                                          marketFlag,
                                          BrokerID,
                                          Account,
                                          AE,
                                          exchange,
                                          com_id,
                                          com_ym,
                                          str(strike_price),
                                          cp.value,
                                          action.value,
                                          int(PriceFlag.value),
                                          DayTrade,
                                          PF,
                                          time_in_force.value ,
                                          str(OrderPrice),
                                          str(stop_price),
                                          qty,
                                          Idno,
                                          FCM,
                                          FFUT_ACCOUNT,
                                          SOURCE,
                                          CNT,
                                          ordno,
                                          TradeDate,
                                          WEBID,
                                          KeyIn,
                                          MLEG ,
                                          com_id2,
                                          action2.value,
                                          com_ym2,
                                          str(strike_price2),
                                          cp2.value
                                          )

            if rt != 0:
                  print(f'Order return {rt}, {self._Order.GetOrderErrMsg(rt)}') 
            else:
                  return trade
            
      def   cancel_order(self ,order_id):

            if isinstance(order_id, Trade):
                  order_id = order_id.order.order_id
            if order_id in self.get_trades().keys():
                  trade = self._trades[order_id]
            else:
                  raise ValueError("查無此order_id，或已失效") 
            
            OrderKind=0
            orderType=1
            RequestId = self._Order.RetriveRequestID()
            symbol =trade.order.symbol
            marketFlag =  0 if symbol.cp == CP.Futures else 1 
            BrokerID = self._broker_id
            Account = self._account
            AE = ''
            exchange = trade.order.market
            com_id = symbol.com_id
            com_ym =symbol.com_ym
            strike_price = symbol.strike_price
            cp =symbol.cp
            action =symbol.action
            PriceFlag =trade.order.price_type
            DayTrade = 'N'
            PF =3
            time_in_force = trade.order.time_in_force
            OrderPrice = trade.order.price
            stop_price =trade.order.stop_price
            qty =trade.order.quantity
            Idno = self._uid
            KeyIn =''

            dic = self._info[trade.order.order_id]

            operation = Operation(
                  nid =RequestId ,
                  task=Task.CancelOrder,
                  status=Status.Pending,
                  op_time= datetime.datetime.now().strftime('%H%M%S') )
            trade.operations.append(operation)
            self._rid[str(RequestId)] = trade

            symbol2 =trade.order.symbol2
            if symbol2 is None:
                  rt =self._Order.FOrderSingle(
                              OrderKind,
                              orderType,
                              RequestId,
                              marketFlag,
                              BrokerID,
                              Account,
                              AE,
                              exchange,
                              com_id,
                              com_ym,
                              str(strike_price),
                              cp.value,
                              action.value,
                              int(PriceFlag.value),
                              DayTrade,
                              PF,
                              time_in_force.value ,
                              str(OrderPrice),
                              str(stop_price),
                              qty,
                              Idno,
                              dic['FCM'],
                              dic['FFUT_ACCOUNT'],
                              dic['SOURCE'],
                              dic['orgcnt'],
                              dic['ORDNO'],
                              dic['TradeDate'],
                              dic['WEBID'],
                              KeyIn)
            else:
                  com_id2 = symbol2.com_id
                  action2 = symbol2.action
                  com_ym2 = symbol2.com_ym
                  strike_price2 =symbol2.strike_price
                  cp2 =symbol2.cp
                  rt =self._Order.FOrderMulti(
                              OrderKind,
                              orderType,
                              RequestId,
                              marketFlag,
                              BrokerID,
                              Account,
                              AE,
                              exchange,
                              com_id,
                              com_ym,
                              str(strike_price),
                              cp.value,
                              action.value,
                              int(PriceFlag.value),
                              DayTrade,
                              PF,
                              time_in_force.value ,
                              str(OrderPrice),
                              str(stop_price),
                              qty,
                              Idno,
                              dic['FCM'],
                              dic['FFUT_ACCOUNT'],
                              dic['SOURCE'],
                              dic['orgcnt'],
                              dic['ORDNO'],
                              dic['TradeDate'],
                              dic['WEBID'],
                              KeyIn,
                              "Y",
                              com_id2,
                              action2.value,
                              com_ym2,
                              str(strike_price2),
                              cp2.value
                              )

            if rt != 0:
                  print(f'Order return {rt}, {self._Order.GetOrderErrMsg(rt)}')  
                  trade.operations.pop()
                  return 
            
            return trade

      def   update_order(self ,order_id ,price=None, qty=None):
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
            if trade.order.price_type not in [PriceType.LMT ,PriceType.StopLossLimit] and price is not None:
                  raise ValueError("只有限價單、觸價單可以改價")
            
            OrderKind=0
            RequestId = self._Order.RetriveRequestID()
            symbol =trade.order.symbol
            marketFlag =  0 if symbol.cp == CP.Futures else 1 
            BrokerID = self._broker_id
            Account = self._account
            AE = ''
            exchange = trade.order.market
            com_id = symbol.com_id
            com_ym =symbol.com_ym
            strike_price = symbol.strike_price
            cp =symbol.cp
            action =symbol.action
            PriceFlag =trade.order.price_type
            DayTrade = 'N'
            PF =3
            time_in_force = trade.order.time_in_force
            OrderPrice = trade.order.price
            stop_price =trade.order.stop_price
            Idno = self._uid
            KeyIn =''

            dic = self._info[trade.order.order_id]

            operation = Operation(
                  nid =RequestId ,
                  status=Status.Pending,
                  op_time= datetime.datetime.now().strftime('%H%M%S') )
            
            if price is None: #改量
                  orderType = 2
                  AfterQty= trade.order_status.modified_quantity -int(qty)
                  if AfterQty < 0:
                        raise ValueError("減量不得多餘剩餘量")  
                  OrderPrice = trade.order_status.modified_price
                  Qty=AfterQty
                  operation.task=Task.UpdateQty
                  operation.msg =f"NewQty_{AfterQty}"

            if qty is None:#改價
                  orderType = 3
                  OrderPrice = price
                  Qty= trade.order_status.modified_quantity
                  operation.task=Task.UpdatePrice
                  operation.msg =f"NewPrice_{price}"

            if OrderPrice == int(OrderPrice): 
                  OrderPrice = int(OrderPrice)    

            trade.operations.append(operation)
            self._rid[str(RequestId)] = trade

            symbol2 =trade.order.symbol2
            if symbol2 is None:
                  rt =self._Order.FOrderSingle(
                                    OrderKind,
                                    orderType,
                                    RequestId,
                                    marketFlag,
                                    BrokerID,
                                    Account,
                                    AE,
                                    exchange,
                                    com_id,
                                    com_ym,
                                    str(strike_price),
                                    cp.value,
                                    action.value,
                                    int(PriceFlag.value),
                                    DayTrade,
                                    PF,
                                    time_in_force.value ,
                                    str(OrderPrice),
                                    str(stop_price),
                                    Qty,
                                    Idno,
                                    dic['FCM'],
                                    dic['FFUT_ACCOUNT'],
                                    dic['SOURCE'],
                                    dic['orgcnt'],
                                    dic['ORDNO'],
                                    dic['TradeDate'],
                                    dic['WEBID'],
                                    KeyIn)
            else:
                  com_id2 = symbol2.com_id
                  action2 = symbol2.action
                  com_ym2 = symbol2.com_ym
                  strike_price2 =symbol2.strike_price
                  cp2 =symbol2.cp
                  rt =self._Order.FOrderMulti(
                                    OrderKind,
                                    orderType,
                                    RequestId,
                                    marketFlag,
                                    BrokerID,
                                    Account,
                                    AE,
                                    exchange,
                                    com_id,
                                    com_ym,
                                    str(strike_price),
                                    cp.value,
                                    action.value,
                                    int(PriceFlag.value),
                                    DayTrade,
                                    PF,
                                    time_in_force.value ,
                                    str(OrderPrice),
                                    str(stop_price),
                                    Qty,
                                    Idno,
                                    dic['FCM'],
                                    dic['FFUT_ACCOUNT'],
                                    dic['SOURCE'],
                                    dic['orgcnt'],
                                    dic['ORDNO'],
                                    dic['TradeDate'],
                                    dic['WEBID'],
                                    KeyIn,
                                    "Y",
                                    com_id2,
                                    action2.value,
                                    com_ym2,
                                    str(strike_price2),
                                    cp2.value
                                    )
            
            if rt != 0:
                  print(f'Order return {rt}, {self._Order.GetOrderErrMsg(rt)}')  
                  trade.operations.pop()
                  return 
            
            return trade

      def   cancel_order_all(self):
            T_dic = self.get_trades()
            sub_oid = list(T_dic.keys())

            for oid in sub_oid:
                  self.cancel_order(oid)
            return 

      def   _OnOrderPending(self,data):
            self._pending.append(data)

            rid = data['RequestID']

            #《拿檔》 
            if rid in self._rid.keys():
                  trade = self._rid[rid]
                  self._cnt[data['CNT']] = rid
            else:
                  print(f'[系統訊息] 查無源頭 Order: {data}')
                  return 
            
            if data['ErrorCode']!= '0':
                  ls = self._trades['無效單']
                  ls.append(trade)

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

            if data['ErrorCode'] != '0':
                  num = "".join(filter(str.isdigit, data['ErrorCode']))
                  mg = self._Order.errorMap.get(num, "") 
                  msg = mg if not msg else f"{msg}|{mg}"
                  status =  Status.Failed  
      
            #《operation》  
            operation.nid = data['CNT']
            operation.ts = ts
            operation.msg = msg 
            operation.status =status

            #《event》
            event= Event(
                  task = operation.task,
                  status= operation.status,
                  order_id = trade.order.order_id,
                  seqno = operation.nid,
                  market= trade.order.market,
                  symbol =trade.order.symbol,
                  symbol2 =trade.order.symbol2,
                  #quantity
                  #price
                  price_type =trade.order.price_type,
                  stop_price =trade.order.stop_price ,
                  time_in_force =trade.order.time_in_force,
                  ts = ts,
                  msg =msg)
                  
            if operation.task ==Task.NewOrder:
                  event.quantity =trade.order.quantity
                  event.price = trade.order.price

            elif operation.task ==Task.UpdateQty:
                  value = operation.msg.split("_")[1]
                  event.quantity = trade.order_status.modified_quantity or trade.order.quantity
                  event.price= value

            elif operation.task ==Task.UpdatePrice:
                  value = operation.msg.split("_")[1]
                  event.quantity = value
                  event.price = trade.order_status.modified_price or trade.order.price

            elif operation.task==Task.CancelOrder:
                  event.quantity = 0
                  event.price= trade.order_status.modified_price or trade.order.price

            if event.price==0:
                  event.price = '市價'
            self._event(event)

      def   _OnOrderReport(self,data):
            if 'Symbol2' in data:
                  data['OrgCnt'] = data.pop('OrgCNT')
                  data['Temp'] =data.pop('Kind')
                  data['Keyin'] =data.pop('OrgKeyin')

            self._order_report.append(data)
            oid = data['ORDNO']

      #       #《拿檔》
            dic={ 'FCM':data['FCM'],
                  'FFUT_ACCOUNT':data['FFUT_ACCOUNT'],
                  'TradeDate':data['TradeDate'],
                  'WEBID':data['WEBID'],
                  'SOURCE':data['SOURCE'],
                  'ORDNO':data['ORDNO'],
                  'orgcnt':data['OrgCnt']}
            self._info[oid] = dic

            rid=0
            if data['CNT'] in self._cnt:
                  rid = self._cnt[data['CNT']]
                  trade = self._rid[rid] 
            elif oid in self._trades.keys():
                  trade = self._trades[oid]
            elif data['OrderFunc']=='O': 
                  trade= Trade(order=Order(),order_status=OrderStatus(),operations=[])
            else:
                  print(f'[系統訊息] 查無源頭 Order: {data}')
                  return 

            if oid not in self._trades.keys():
                  if data['ErrCode'] != '0':
                        ls = self._trades['無效單']
                        ls.append(trade)
                  else:
                        self._trades[oid] = trade

            operation = next((x for x in trade.operations if str(x.nid).strip() == str(rid).strip()), None)
            if operation is None:
                  operation = next((x for x in trade.operations if str(x.nid).strip() == str(data['CNT']).strip()), None)
            if operation is None:
                  operation = Operation()
                  trade.operations.append(operation)

            #《data》
            market = data['EXCHANGE']

            action=data['BS']
            if action in {a.value for a in Action}:
                  action = Action(action)
            com_id =data['Symbol'].split()[0]
            cp =data['CP']
            if cp in {a.value for a in CP}:
                  cp = CP(cp)
            com_ym= data['ComYM']
            strike_price = _str_to_num(data['StrikePrice'][:-6] + '.' + data['StrikePrice'][-6:])

            qty = _str_to_num(data['AfterQty'])
            price = _str_to_num(data['Price'][:-6] + '.' + data['Price'][-6:])
            price_type = data['PriceFlag']
            if price_type in _PriceType.keys():
                  price_type = _PriceType[ price_type]
            stop_price = _str_to_num(data['StopPrice'][:-6] + '.' + data['StopPrice'][-6:])
            time_in_force = data['TimeInForce']
            if time_in_force in _TIF.keys():
                  time_in_force = _TIF[time_in_force]
            ts = data['ReportTime'][:-3]

            symbol =Symbol( action=action ,com_id=com_id ,cp=cp ,com_ym=com_ym ,strike_price=strike_price)
            if 'Symbol2' in data:
                  action2=data['BS2']
                  if action2 in {a.value for a in Action}:
                        action2 = Action(action2)
                  cp2 =data['CP2']
                  if cp2 in {a.value for a in CP}:
                        cp2 = CP(cp2)
                  strike_price2 = _str_to_num(data['StrikePrice2'][:-6] + '.' + data['StrikePrice2'][-6:])
                  symbol2 =Symbol( action=action2 ,
                                 com_id=data['Symbol2'],
                                 cp=cp2 ,
                                 com_ym=data['ComYM2'] ,
                                 strike_price=strike_price2)
            else:
                  symbol2=None

            msg = operation.msg
            msg = None
            if data['OrderFunc']=='M':#改單
                  org_price = trade.order_status.modified_price
                  if price != org_price:#改價
                        func = 'P'
                        mg = f"NewPrice_{price}"
                        msg = mg if not msg else f"{msg}|{mg}"
                  else:#改量
                        func = 'Q'
                        mg = f"NewQty_{qty}"
                        msg = mg if not msg else f"{msg}|{mg}"

            if data['ErrCode'] != '0' :
                  mg = data['ErrMsg']
                  msg = mg if not msg else f"{msg}|{mg}"

            #《order》
            if data['OrderFunc']=='O':
                  order = Order(
                        order_id = oid,
                        market=market,
                        symbol = symbol,
                        symbol2 = symbol2,
                        quantity= qty,
                        price =price,
                        price_type =price_type,
                        stop_price = stop_price,
                        time_in_force= time_in_force,
                        )
                  trade.order = order

            #《order_status》
            if data['ErrCode'] != '0' :
                  pass
            else:
                  trade.order_status.nid=data['CNT']
                  trade.order_status.status=Status.Submitted 
                  trade.order_status.modified_time = ts
                  trade.order_status.modified_price = price
                  trade.order_status.modified_quantity = qty

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

            if data['OrderFunc']=='O':
                  operation.task=Task.NewOrder
                  operation.status = Status.Success
            elif data['OrderFunc']=='C':#刪單
                  operation.task = Task.CancelOrder
                  operation.status = Status.Success
            elif data['OrderFunc']=='M':
                  if func=='P':
                        operation.task = Task.UpdatePrice
                        operation.status = Status.Success
                  elif func=='Q':
                        operation.task = Task.UpdateQty
                        operation.status = Status.Success
            if data['ErrCode'] != '0' :
                  operation.status =Status.Failed

            #《event》
            event= Event(
                  task = operation.task,
                  status= operation.status,
                  order_id = oid,
                  seqno = operation.nid,
                  market= trade.order.market,
                  symbol =trade.order.symbol,
                  symbol2 =trade.order.symbol2,
                  quantity =qty ,
                  price = price,
                  price_type =trade.order.price_type,
                  stop_price =trade.order.stop_price ,
                  time_in_force =trade.order.time_in_force,
                  ts = ts,
                  msg =msg)

            if event.price==0:
                  event.price = '市價'

            if self._show is True:
                  self._event(event)

      def   _OnExecReport(self,data):
            if 'LeaveQty' in data:
                  data['LeavesQty'] = data.pop('LeaveQty')
                  data['Temp'] =data.pop('Kind')

            self._exec_report.append(data)

            #《data》
            org_cnt = data['ORDNO']

            side = data['BS']
            if side in {a.value for a in Action}:
                  side = Action(side)
            com_id =data['Symbol']
            cp =data['CP']
            if cp in {a.value for a in CP}:
                  cp = CP(cp)
            com_ym =data['ComYM']
            strike_price = _str_to_num(data['StrikePrice'][:-6] + '.' + data['StrikePrice'][-6:])
            symbol = Symbol( action=side ,com_id=com_id ,cp=cp ,com_ym=com_ym ,strike_price=strike_price)
            
            FillQty = _str_to_num(data['DealQty'])
            dealprice = _str_to_num(data['DealPrice'][:-6]+'.'+data['DealPrice'][-6:])
            price_type =data['PriceFlag']
            if price_type in _PriceType.keys():
                  price_type = _PriceType[ price_type]
            time_in_force = data['TimeInForce']
            ts = data['ReportTime']

            event = Event(
                  task =Task.Deal ,
                  order_id =org_cnt ,
                  seqno =data['PATSNo'],
                  market=data['EXCHANGE'],
                  symbol=symbol,
                  quantity=FillQty,
                  price=dealprice,
                  price_type=price_type,
                  time_in_force =time_in_force,
                  ts =ts)

            #《deal》
            trade = self._trades.get(org_cnt)
            if trade is None:
                  print(f'[系統訊息] 查無源頭 Order: {data}')
            else:
                  if trade.order.symbol2 is None:
                        deal =Deal( price=dealprice,
                                    quantity=FillQty,
                                    ts=ts, 
                                    reportseq=data['PATSNo'])
                  else:
                        deal =Deal( symbol=symbol,
                                    price=dealprice,
                                    quantity=FillQty,
                                    ts=ts, 
                                    reportseq=data['PATSNo'])
                  trade.order_status.deals.append(deal)
                  order_qty =  trade.order_status.modified_quantity
                  if FillQty < order_qty:
                        trade.order_status.status = Status.PartFilled
                  else:
                        trade.order_status.status = Status.Filled
                  trade.order_status.modified_quantity = order_qty -FillQty

            if self._show is True:
                  self._event(event)

            event.symbol =None
            if symbol in self._deals:
                  ls=self._deals[symbol]
                  ls.append(event)
            else:
                  self._deals[symbol] =[event]
            
      def   _update(self ):
            self._show =False
            self._Order.ReceiveFReport(self._broker_id ,self._account , '',False)
            self._Order.ReceiveFReport(self._broker_id ,self._account , '',True)

            start_time = time.time()
            N= len(self._order_report)
            last_data_time = start_time
            while time.time() - start_time < 5:
                  if len(self._order_report) > N:
                        N =len(self._order_report)
                        last_data_time = time.time() 
                  if time.time() -last_data_time > 1 and  N>0:
                        break
                  time.sleep(0.1) 
            
            self._show =True

      def   get_deals(self):
            return self._deals
      
      def   get_trades(self ,full=False):

            if full==True:
                  return self._trades
            T_dic={}
            for key, value in self._trades.items():
                  if key =='無效單':
                        continue
                  if value.order_status.status not in [Status.Cancelled ,Status.Failed ,Status.Filled ,Status.PartFilled_Cancelled]:
                        T_dic[key] =value
            return  T_dic

      def set_event(self, event):
            self._event = event 

#=====================================================================================
      def   PositionSum(self):
            """
            #期貨分帳庫存彙總查詢  1616 回傳資料

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
            MType='O'
            BrokerID = self._broker_id
            Account = self._account
            Group =''
            Trader =''
            self._p=[]
            self._Order.RetrivePositionSum( MType ,BrokerID ,Account ,Group ,Trader)

            #拿資料
            start_time = time.time()
            N= 0
            last_data_time = start_time
            while time.time() - start_time < 5:
                  if len(self._p) > N:
                        N =len(self._p)
                        last_data_time = time.time() 
                  if time.time() - last_data_time > 1:
                        break
                  time.sleep(0.1)

            #空資料
            if self._p ==[]:
                  print('[系統訊息] 未收到響應')
                  return
            
            dfs = [pd.DataFrame(item) for item in self._p]
            dfs = pd.concat(dfs)

            col = [
                  'OrigComID',
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
            dfs['OTQty']= dfs['OTQty'].astype(int)
            dfs['PRTLOS']= dfs['PRTLOS'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
            dfs['StrikePrice']= dfs['StrikePrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
            dfs['TrdPrice']= dfs['TrdPrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
            dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
            return dfs

      def   _profit(self,js_string):
            data = json.loads(js_string)
            self._p.append(data)

#=====================================================================================
      def   PositionDetail(self):
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
            MType='O'
            BrokerID = self._broker_id
            Account = self._account
            Group =''
            Trader =''
            self._pd=[]
            self._Order.RetrivePositionDetail( MType ,BrokerID ,Account ,Group ,Trader)

            #拿資料
            start_time = time.time()
            N= 0
            last_data_time = start_time
            while time.time() - start_time < 5:
                  if len(self._pd) > N:
                        N =len(self._pd)
                        last_data_time = time.time() 
                  if time.time() - last_data_time > 1:
                        break
                  time.sleep(0.1)

            #空資料
            if self._pd ==[]:
                  print('[系統訊息] 未收到響應')
                  return
            
            dfs = [pd.DataFrame(item) for item in self._pd]
            dfs = pd.concat(dfs)
            col = ['SPREAD', 'spKey', 'BrokerId', 'Account', 'Group', 'Trader',
                  'Exchange', 'seqNo', 'FcmActNo', 'tradeType', 'Fcm',
                  'DeliveryDate', 'CloseDate', 'WEB', 'Cnt', 'OrdNo',
                  'MarketNo', 'sNo', 'BS', 'ComType', 'CP', 'StrikePrice','com_id',
                  'ComYM', 'Qty', 'TrdPrice', 'MPrice', 'PRTLOS',
                  'InitialMargin', 'MTMargin', 'Currency', 'DealPrice',
                  'mixQty1', 'DayTrade' ,'trade_date']

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
            dfs['Qty']= dfs['Qty'].astype(int)
            dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
            
            #下面處理組合單
            cols = [
            'OrdNo', 'MarketNo', 'sNo', 'BS', 'ComType', 'CP',
            'StrikePrice','com_id' ,'ComYM', 'Qty', 'TrdPrice', 'MPrice',
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
            
      def   _profit_detail(self,js_string):
            data = json.loads(js_string)
            self._pd.append(data)

#=====================================================================================
      def   COVER(self ):
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
            MType='O'
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
            
            #拿資料
            start_time = time.time()
            N= 0
            last_data_time = start_time
            while time.time() - start_time < 5:
                  if len(self._rp) > N:
                        N =len(self._rp)
                        last_data_time = time.time() 
                  if time.time() - last_data_time > 1:
                        break
                  time.sleep(0.1)

            #空資料
            if self._rp ==[]:
                  print('[系統訊息] 未收到響應')
                  return

            col = [
                  'OrigComID','BrokerId', 'Account', 'Group', 'Trader', 'Exchange', 'ComID', 'ComYM',
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
            dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
            return dfs

      def   _realprofit(self ,js_string):
            data = json.loads(js_string)
            self._rp.append(data)    

#=====================================================================================
      def   COVERDetail(self ):
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
            MType='O'
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
            
            #拿資料
            start_time = time.time()
            N= 0
            last_data_time = start_time
            while time.time() - start_time < 5:
                  if len(self._rpd) > N:
                        N =len(self._rpd)
                        last_data_time = time.time() 
                  if time.time() - last_data_time > 1:
                        break
                  time.sleep(0.1)

            #空資料
            if self._rpd ==[]:
                  print('[系統訊息] 未收到響應')
                  return

            dfs = [pd.DataFrame(item) for item in self._rpd]
            dfs = pd.concat(dfs)
            
            col = [
                  'OrigComID','BrokerId', 'Account', 'Group', 'Trader', 'Exchange', 'TrdDT1', 'OrdNo1', 'FirmOrd1', 'OffsetSpliteSeqNo',
                  'TrdDT2', 'OrdNo2', 'FirmOrd2', 'OffsetSpliteSeqNo2', 'OffsetCode', 'offset', 'BS', 'ComID', 'ComYM',
                  'StrikePrice', 'CP', 'ComID2', 'QTY1', 'QTY2', 'TRDPRC1', 'TRDPRC2', 'PRTLOS', 'AENO', 'CURRENCY',
                  'CTAXAMT', 'ORIGNFEE', 'Premium1', 'Premium2', 'InNo1', 'InNo2', 'Cnt1', 'Cnt2', 'OSPRTLOS', 'Symbol'
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
            dfs['QTY1']= dfs['QTY1'].astype(int)
            dfs['QTY2']= dfs['QTY2'].astype(int)
            dfs['Premium1']= dfs['Premium1'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
            dfs['Premium2']= dfs['Premium2'].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))
            dfs['StrikePrice']= dfs['StrikePrice'].apply(lambda x : Decimal(x[:7] +'.'+x[7:]))
            dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
            return dfs

      def   _realprofit_detail(self ,js_string):
            data = json.loads(js_string)
            self._rpd.append(data)

#=====================================================================================
      def   Margin(self):
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
            MType = 'O'
            BrokerID = self._broker_id
            Account = self._account
            Group =''
            Trader =''

            self._bal=[]
            self._Order.RetriveFMargin(MType ,BrokerID ,Account ,Group ,Trader)
            
            #拿資料
            start_time = time.time()
            N= 0
            last_data_time = start_time
            while time.time() - start_time < 5:
                  if len(self._bal) > N:
                        N =len(self._bal)
                        last_data_time = time.time() 
                  if time.time() - last_data_time > 1:
                        break
                  time.sleep(0.1)

            #空資料
            if self._bal ==[]:
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
            'MarginCall', 'SellVerticalSpread', 'StrikPrice', 'ActMarketValue', 'TPRTLOS', 'MarginCall', 'AddMargin'
            ]

            if dfs.empty==True:
                  dfs = pd.DataFrame(columns=col)
                  return dfs

            for col  in dfs.columns:
                  if col in ['Account','BrokerId','CURRENCY','Group','PTime','Trader']:
                        continue
                  dfs[col]= dfs[col].apply(lambda x : Decimal(x[:-2] +'.'+x[-2:]))

            dfs = dfs[[c for c in col if c in dfs.columns] + [c for c in dfs.columns if c not in col]]
            return dfs

      def   _balance(self ,js_string):
            data = json.loads(js_string)
            self._bal.append(data)

      def Margin_EX(self):
            """
            分帳權益數可出金金額

            Notes
            -----
                  - 欄位內容來源自期貨 OnQueryResult 1639 回傳資料。
            """
            MType='O'
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

            return  dfs
      
      def _balanceX(self ,js_string):
            data = json.loads(js_string)
            self._balX.append(data)

      def OrderReport(self):
            df = pd.DataFrame(self._order_report)
            col = ['OrderFunc', 'EXCHANGE', 'FCM', 'Temp', 'FFUT_ACCOUNT', 'ORDNO',
            'ASorderNo', 'BrokerID', 'ActNo', 'AE', 'TradeDate', 'ReportTime',
            'WEBID', 'SOURCE', 'OrgCnt', 'Symbol', 'ComYM', 'StrikePrice', 'CP',
            'BS', 'Symbol2', 'ComYM2', 'StrikePrice2', 'CP2', 'BS2', 'TimeInForce',
            'PriceFlag', 'PositionEffect', 'DayTrade', 'Price', 'StopPrice',
            'BeforeQty', 'AfterQty', 'Keyin', 'ErrCode', 'CNT', 'ErrMsg']
            if df.empty:
                  df = pd.DataFrame(columns=col)

            df = df[[c for c in col if c in df.columns] + [c for c in df.columns if c not in col]]
            df['Price'] = df['Price'] .apply(lambda x : Decimal(x[:-6] +'.'+x[-6:]))
            df['StrikePrice'] = df['StrikePrice'] .apply(lambda x : Decimal(x[:-6] +'.'+x[-6:]))
            df['StopPrice'] = df['StopPrice'] .apply(lambda x : Decimal(x[:-6] +'.'+x[-6:]))
            return df
      
      def ExecReport(self):
            df = pd.DataFrame(self._exec_report)
            col = ['OrderFunc', 'EXCHANGE', 'FCM', 'Temp', 'FFUT_ACCOUNT', 'ORDNO',
            'ASorderNo', 'BrokerID', 'ActNo', 'AE', 'TradeDate', 'ReportTime',
            'WEBID', 'SOURCE', 'CNT', 'Symbol', 'ComYM', 'StrikePrice', 'CP', 'BS',
            'TimeInForce', 'PriceFlag', 'PositionEffect', 'DayTrade', 'DealPrice',
            'AvgPrice', 'DealQty', 'PATSNo', 'LeavesQty', 'CumQty', 'Keyin',
            'SysDate']
            if df.empty:
                  df = pd.DataFrame(columns=col)

            df = df[[c for c in col if c in df.columns] + [c for c in df.columns if c not in col]]
            df['DealPrice'] = df['DealPrice'] .apply(lambda x : Decimal(x[:-6] +'.'+x[-6:]))
            df['StrikePrice'] = df['StrikePrice'] .apply(lambda x : Decimal(x[:-6] +'.'+x[-6:]))
            df['AvgPrice'] = df['AvgPrice'] .apply(lambda x : Decimal(x[:-6] +'.'+x[-6:]))
            return df

#=====================================================================================
      # def   get_position(self):
      #       pos = self.PositionSum()
      #       rl = self.COVER()
      #       d = self.get_deals()
      #       print('尚未完成')
      #       return