import time ,os ,warnings
MAIN_DIR = os.getcwd()

import pandas as pd
from .backtest.BT import backtest as _BT
from .data.USdata import _Data as us_data
from .data.data import _Data as tw_data
from .Quote import Subscribe, _STKQuoteManager, _USQuoteManager, _FutQuoteManager
from .CA import TradeCom 
from .Quote_sw import SubscribeList
from .trading.pyi import _Order ,_Account ,_FutOrder ,_FutAccount ,_SubOrder ,_SubAccount ,_IntFutOrder ,_IntFutAccount

warnings.filterwarnings("ignore", category=FutureWarning)
pd.options.mode.chained_assignment = None
try:
    warnings.filterwarnings("ignore", category=pd.errors.ChainedAssignmentError)
except AttributeError:
    pass

class login:
    class _Data:
        def __init__(self):
            pass
    class _USData:
        def __init__(self):
            pass
    def __repr__(self):
        return "Api Instance is ready" 

#=========================================================================
    def __init__(self ,person_id=None ,person_pwd=None ,simulation:bool=True):
        self._saved_callback = None
        self.person_id = person_id
        self.person_pwd = person_pwd
        self.simulation =  simulation
        self.path = MAIN_DIR
        self.__need_upload =True
        if self.person_id is  None or self.person_pwd is  None:
            print("[系統訊息] 登錄失敗：帳號或密碼欄位不可為空。")
            return 
        self.login()

    def login(self ,person_id=None ,person_pwd=None ,simulation:bool=None):
        if person_id is not None:
            self.person_id = person_id
        if person_pwd is not None:
            self.person_pwd = person_pwd
        if simulation is not None:
            self.simulation =  simulation

        self._ObjOrder = TradeCom(self.person_id ,self.person_pwd ,self.simulation  ,self._saved_callback)

        if self.__need_upload :
            self.__need_upload=False
            AutoRefresh = self._ObjOrder._URL
            if hasattr(AutoRefresh, '_manager'):
                self._set_data(AutoRefresh)

        if self._ObjOrder.FIsLogon == True:
            if self._saved_callback :
                print("[系統訊息] 自動為新連線套用斷線回呼函式。")

            print("[系統訊息] CA 驗證通過，帳務與下單功能已初始化。")
            self.__info={}
            self.logout =self._logout
            self.set_Account =self._set_Account
            self.set_FutAccount = self._set_FutAccount
            self.set_SubAccount = self._set_SubAccount
            self.show_account = self._show_account
            self.login_info = self._login_info
            self.set_disconnect_cb =self._set_disconnect_callback
            print("[系統訊息] API 加載完成，歡迎使用。")

    def _set_data(self ,URL):
        self.backtest = _BT

        if URL._msmp is not None:
            print("[系統訊息] MSMP 物件初始化完成。")
            self.MSMP = URL._msmp

        if URL._msmpUS is not None:
            print("[系統訊息] USMSMP 物件初始化完成。")
            self.USMSMP = URL._msmpUS

        if not URL.token:
            print("[系統訊息] Token無效，停止初始化行情與資料")
            return  

        self._Q = Subscribe(URL)  
        self.Quote = _STKQuoteManager(self._Q.Quote)
        self.USQuote = _USQuoteManager(self._Q.USQuote)
        self.FutQuote = _FutQuoteManager(self._Q.FutQuote)
        self.SWQuote =SubscribeList(URL)

        d= tw_data(URL)
        self.Data = self._Data()
        self.Data.get_table = URL._get_table
        self.Data.get = d.get
        self.Data.get_snapshots =d.get_snapshots
        self._contract =d.contract

        USd= us_data( URL)
        self.USData = self._USData()
        self.USData.get_table = URL._get_UStable
        self.USData.get = USd.get
        self.USData.get_snapshots =USd.get_snapshots

    def _logout(self):
        if hasattr(self, '_Q'):
            self._Q.stop()

        if hasattr(self, '_ObjOrder'):
            def pass_func():
                        pass
            self._ObjOrder.set_disconnect_callback(pass_func)
            self._ObjOrder.Logout()

        preserve = {}
        preserve['person_id'] = self.person_id
        preserve['person_pwd'] = self.person_pwd
        preserve['simulation'] = self.simulation
        preserve['_saved_callback'] = self._saved_callback
        self.__dict__.clear()
        self.__dict__.update(preserve)
        self.__need_upload=True

    def _set_disconnect_callback(self ,func):
        self._saved_callback = func
        self._ObjOrder.set_disconnect_callback(func)

    def _show_account(self):
        if not hasattr(self, '_ObjOrder') :
            print('尚未登入python api')
            return
        return self._ObjOrder._show_account()
    
    def _login_info(self):
        return self.__info
#=========================================================================
    def _set_Account(self ,account):
        if not hasattr(self, '_ObjOrder') :
            print('尚未登入python api')
            return 

        elif account in self._ObjOrder._acc.keys():
            brokerid = self._ObjOrder._acc[account]
            self._ObjOrder.set_Account(brokerid , account)
            O = self._ObjOrder.O
            
            self.Order = _Order()
            self.Order.create_order = O.create_order
            self.Order.update_order = O.update_order
            self.Order.cancel_order = O.cancel_order
            self.Order.cancel_order_all = O.cancel_order_all
            self.Order.get_trades = O.get_trades
            self.Order.get_deals = O.get_deals
            self.Order.get_position = O.get_position
            self.Order.TSECreditInfo =O.TSECreditInfo     #4033 查看信用額度
            self.Order.set_event = O.set_event
            try:
                self.Order.contract = self._contract
            except:
                pass

            self.Account = _Account()
            self.Account.InventorySum = O.InventorySum      #4310   證券庫存彙總查詢   
            self.Account.SettleAmtTrial = O.SettleAmtTrial  #4302   當日交割金額試算查詢   
            self.Account.SettleAmt = O.SettleAmt            #4306   近三日交割    
            self.Account.Inventory =O.Inventory             #4316   結賬後庫存
            self.Account.RealizePL =O.RealizePL             #4314   帳中已實現損益查詢
            self.Account.BalanceStatement = O.BalanceStatement    #4312     對照單
            self.Account.SettleAmtDetail = O.SettleAmtDetail  #4304   當日交割金額_沖銷明細(非當沖)
            self.Account.OrderReport = O.OrderReport 
            self.Account.ExecReport = O.ExecReport   

            self.__info['證券']={'broker_id':brokerid ,'account':account}
        else:
            print(f'輸入賬號錯誤')

#=========================================================================
    def _set_FutAccount(self ,account):
        if not hasattr(self, '_ObjOrder') :
            print('尚未登入python api')
            return 

        elif account in self._ObjOrder._acc.keys():
            brokerid = self._ObjOrder._acc[account]
            print(f'設定期貨交易 broker_id為 {brokerid}')
            self._ObjOrder.set_FutAccount(brokerid , account)
            F = self._ObjOrder.F

            self.FutOrder = _FutOrder()
            self.FutOrder.create_order = F.create_order
            self.FutOrder.update_order = F.update_order
            self.FutOrder.cancel_order = F.cancel_order
            self.FutOrder.cancel_order_all = F.cancel_order_all
            self.FutOrder.get_trades = F.get_trades
            self.FutOrder.get_deals = F.get_deals
            self.FutOrder.get_position = F.get_position
            self.FutOrder.set_event = F.set_event

            self.FutAccount = _FutAccount()
            self.FutAccount.PositionSum = F.PositionSum  #1616  分帳庫存彙總
            self.FutAccount.PositionDetail = F.PositionDetail #1618 分帳庫存明細
            self.FutAccount.COVER = F.COVER #1614   分帳平倉
            self.FutAccount.COVERDetail = F.COVERDetail #1624   分帳平倉明細
            self.FutAccount.Margin = F.Margin #1626 分帳權益數
            self.FutAccount.Margin_EX = F.Margin_EX  #1639 分帳權益數(支援人民幣、國內外可出金金額)
            self.FutAccount.OrderReport = F.OrderReport 
            self.FutAccount.ExecReport = F.ExecReport   

            self.__info['期貨']={'broker_id':brokerid ,'account':account}
        else:
            print(f'輸入賬號錯誤')

# #=========================================================================
    def _set_SubAccount(self ,account):
        if not hasattr(self, '_ObjOrder') :
            print('尚未登入python api')
            return 

        elif account in self._ObjOrder._acc.keys():
            brokerid = self._ObjOrder._acc[account]
            print(f'設定複委託交易 broker_id為 {brokerid}')

            self._ObjOrder.set_SubAccount( brokerid , account)
            S = self._ObjOrder.S

            self.SubOrder = _SubOrder()
            self.SubOrder.contract =self._ObjOrder.get_contracts
            self.SubOrder.create_order = S.create_order
            self.SubOrder.update_order = S.update_order
            self.SubOrder.cancel_order = S.cancel_order
            self.SubOrder.cancel_order_all = S.cancel_order_all
            self.SubOrder.get_deals  = S.get_deals
            self.SubOrder.get_trades  = S.get_trades
            self.SubOrder.get_position =S.get_position
            self.SubOrder.set_event = S.set_event

            self.SubAccount = _SubAccount()
            self.SubAccount.StockPositionReport  = S.StockPositionReport   #4123 複委託-股票庫存部位查詢
            self.SubAccount.DeliveryReport = S.DeliveryReport #4125 複委託-當日交割金額查詢
            self.SubAccount.PositionDetailReport = S.PositionDetailReport #4119 複委託-整戶部位明細查詢
            self.SubAccount.OrderReport = S.OrderReport 
            self.SubAccount.ExecReport =S.ExecReport 

            self.__info['複委託']={'broker_id':brokerid ,'account':account}
            #update 4117 4113
        else:
            print(f'輸入賬號錯誤')




 

