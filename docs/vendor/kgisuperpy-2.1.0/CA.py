from .pushClient.pyTradeCom import TradeComAPI
from .pushClient.contract import SubContracts
from .pushClient.Fcontract import get_Fcontracts
import time, os, json ,gc ,sys
from datetime import date
from .trading.FutOrder import FutOrderAPI
from .trading.Order import OrderAPI
from .trading.SubOrder import SubOrderAPI

from IPython.display import display
from .url import AutoRefresh

def load_errmsg():
    filename="errMsg.ini"
    encoding="utf-8"
    # 主進程的工作目錄 (登入下載的檔案應該就在這裡)
    work_dir = os.getcwd()
    path = os.path.join(work_dir, filename)

    err_dict = {}
    try:
        with open(path, "r", encoding=encoding) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    err_dict[k.strip()] = v.strip()
        return err_dict
    except Exception as e:
        # 其他異常，可選擇記錄 log
        print(f"[load_errmsg] 讀取 {path} 失敗: {e}")
        return {}

class TradeCom(TradeComAPI):
    def __init__(self, person_id=None, person_pwd=None ,simulation =None ,func=None):
        self._disconnect_callback = func
        iid = person_id
        today_str = date.today().strftime("%Y%m%d")
        number = 0
        log_dir = os.path.join(os.path.dirname(__file__), "pushClient")
        filename = os.path.join(log_dir, f'TradeCom.{today_str}-{iid}_{number}.log')

        while os.path.exists(filename):
            number += 1
            filename = os.path.join(log_dir, f'TradeCom.{today_str}-{iid}_{number}.log')

        super().__init__(iid= f'{iid}_{number}')
        self.FIsConnected = None
        self.FIsLogon = None
        self._list_account =[]
        self.person_id = person_id
        self.person_pwd = person_pwd

        if simulation==False:
            print(f"[系統訊息] 路徑設定完成（環境=正式下單環境）。")
        else:
            print(f"[系統訊息] 路徑設定完成（環境=虛擬下單環境）。")

        self._URL = AutoRefresh(self.person_id ,self.person_pwd ,simulation) 
        self._login()

    def _login(self):
        #連綫
        self.Connect(*self._URL.host[:3])
        T=0
        while self.FIsConnected is None:
            time.sleep(1)
            T=T+1
            if T==20:
                print("[系統訊息] 連線超時，請檢查網路狀態。")
                return 
        if self.FIsConnected is False:
            return 

        #登入
        self.FIsLogon = None 
        try:
            super().Login(self.person_id, self.person_pwd)
        except OSError as e:
            msg = str(e)
            if "0x0000000000000000" in msg:
                print("[系統訊息] 請重新安裝 CGEnvDetectATL、KGICGCAPIATL2 元件。")
                return 
            else:
                print(msg)
                return 
        T = 0
        timeout=20
        while self.FIsLogon is None:
            time.sleep(0.5)
            T += 1
            if T >= timeout:
                print("[系統訊息] 等待登入回報失敗，請檢查憑證狀態。")
                return 
    
    def __del__(self):
        print("TradeCom被釋放！")
        gc.collect()

    def _show_account(self):
        if len(self._list_account) != 0:
            return self._list_account
        obj = self.GetAccounts()
        ls = json.loads(obj)

        self._acc={}
        for row in ls:
            row['account_flag'] = row['account_flag'].replace('F','期貨').replace('O','複委託').replace('S','證券')
            self._acc[row['account']] = row['broker_id']
            self._list_account.append(row)

        def format_list(lst):
            if not lst:
                return '[]'
            lines = []
            # 第一個字典與 [ 在同一行
            dict_items = [f"'{k}': '{v}'" for k, v in lst[0].items()]
            first_dict = '{' + ', '.join(dict_items) + '}'
            lines.append(f"[{first_dict}" + (',' if len(lst) > 1 else ''))
            # 中間的字典（如果有）
            for i, item in enumerate(lst[1:-1], 1):
                dict_items = [f"'{k}': '{v}'" for k, v in item.items()]
                dict_str = '{' + ', '.join(dict_items) + '}'
                lines.append(f" {dict_str},")
            # 最後一個字典與 ] 在同一行（如果不是只有一個項目）
            if len(lst) > 1:
                dict_items = [f"'{k}': '{v}'" for k, v in lst[-1].items()]
                last_dict = '{' + ', '.join(dict_items) + '}'
                lines.append(f" {last_dict}]")
            else:
                lines[-1] = lines[-1] + ']'
            return '\n'.join(lines)
        
        print(format_list(self._list_account))
        return self._list_account

    def set_Account(self ,broker_id , account):
        print(f"[系統訊息] 設定證券賬號 broker_id: {broker_id}")
        self.O = OrderAPI(self ,broker_id , account)
        print('[系統訊息] Order 及 Account已加載 ,開始進行補檔')
        self.O._update()
        print("[系統訊息] 補檔完成 ")

    def set_FutAccount(self ,broker_id , account):
        print(f"[系統訊息] 設定期貨賬號 broker_id: {broker_id}")
        self.F = FutOrderAPI(self ,broker_id , account)
        print('[系統訊息] FutOrder 及 FutAccount已加載 ,開始進行補檔')
        self.F._update()
        print("[系統訊息] 補檔完成 ")

    def set_SubAccount(self ,broker_id , account):
        print(f"[系統訊息] 設定複委托賬號 broker_id: {broker_id}")
        self.S = SubOrderAPI(self ,broker_id , account)
        print('[系統訊息] SubOrder 及 SubAccount已加載 ,開始進行補檔')
        self.S._update()
        print("[系統訊息] 補檔完成 ")

    def OnStatusChanged(self, status):
        if status == 0:
            self.OnDisconnected()
        elif status == 1:
            self.OnDisconnected()
        elif status == 3:
            self.OnConnected()
        elif status == 4:
            self.OnLogonResponse(True, '登入成功')
            self.FIsLogon = True
            self._SubContracts =SubContracts(self._URL.host[3])
            self.get_contracts = self._SubContracts.get_contracts
            self.get_Fcontracts = get_Fcontracts
            self.get_Fcontracts.url =self._URL.host[4]
            self.errorMap = load_errmsg()
            self._show_account()
        elif status == 5:
            self.OnLogonResponse(False, '登入失敗：請檢查賬號密碼。')
            self.FIsLogon = False
        
    def OnConnected(self):
        print('-------------- OnConnected() --------------')
        self.FIsConnected = True
        
    def set_disconnect_callback(self, func):
            """設定斷線時的回呼函式"""
            self._disconnect_callback = func

    def OnDisconnected(self):
        print('-------------- OnDisconnected() --------------')
        self.FIsConnected = False

        if self._disconnect_callback:
            # 檢查這個 function 的名字
            # 如果名字是 'pass_func' 或 'logout_callback'，我們就不印訊息
            if self._disconnect_callback.__name__ in ['pass_func']:
                print("[系統訊息] 偵測到主動斷線")
                return 

            print("[系統訊息] 偵測到斷線，觸發使用者定義的回呼函式...")
            self._disconnect_callback()

    def OnLogonResponse(self, IsSucceed, ReplyString):
        print('-------------- OnLogonResponse() --------------')
        print('--- IsSucceed:{0} ReplyString:{1}'.format(IsSucceed, ReplyString))

#======================================================================================================================================================================================
    def OnOrderPending(self, format_id, response):
        print(f'------------- OnOrderPending. format_id:{format_id} -----')
        rsp_jsobj = json.loads(response)
        data = {k: v.strip() if isinstance(v, str) else v for k, v in rsp_jsobj.items()}

        if hasattr(self, 'F'):
            if format_id==2002:
                self.F._OnOrderPending(data)

        if hasattr(self, 'O'):
            if format_id in [6002 ,4002]:
                self.O._OnOrderPending(data)
            # if format_id==4002:
            #     self.O._OnOrderPendingX(data)

        if hasattr(self, 'S'):
            if format_id==4102:
                self.S._OnOrderPending(data)

    def OnOrderReport(self, format_id, response):
        print(f'------------- OnOrderReport. format_id:{format_id} -----')
        rsp_jsobj = json.loads(response)
        data = {k: v.strip() if isinstance(v, str) else v for k, v in rsp_jsobj.items()}
        #js_string = json.dumps(rsp_jsobj, ensure_ascii=False, indent=4)
        #print(data)
        if hasattr(self, 'F'):
            if format_id==2010:
                self.F._OnOrderReport(data)

        if hasattr(self, 'O'):
            if format_id==4010:
                self.O._OnOrderReport(data)

            if format_id==2110:
                self.O._OnOrderName(data)

        if hasattr(self, 'S'):
            if format_id==4110:
                self.S._OnOrderReport(data)

            if format_id==2110:
                self.S._OnOrderName(data)

    def OnExecReport(self, format_id, response):
        print(f'------------- OnExecReport. format_id:{format_id} -----')
        rsp_jsobj = json.loads(response)
        data = {k: v.strip() if isinstance(v, str) else v for k, v in rsp_jsobj.items()}
        #js_string = json.dumps(rsp_jsobj, ensure_ascii=False, indent=4)
        #print(data)
        if hasattr(self, 'F'):
            if format_id==2011:
                self.F._OnExecReport(data)

        if hasattr(self, 'O'):
            if format_id==4011:
                self.O._OnExecReport(data)

        if hasattr(self, 'S'):
            if format_id==4111:
                self.S._OnExecReport(data)
        
    def OnQueryResult(self, format_id, response):
        print(f'------------- OnQueryResult. format_id:{format_id} -----')
        rsp_jsobj = json.loads(response)
        js_string = json.dumps(rsp_jsobj, ensure_ascii=False, indent=4)
        # print(js_string)

        if hasattr(self, 'O'):
            if format_id==4310: #庫存匯總
                self.O._profit(js_string)

            if format_id==4316: #庫存損益及即時維持率試算
                self.O._profit_realize(js_string)

            if format_id==4302: #當日交割試算
                self.O._realprofit(js_string)

            if format_id==4314: #已實現
                self.O._realprofit_realize(js_string)

            if format_id==4304: #當日交割試算
                self.O._settlement_td(js_string)  

            if format_id==4306:  #三日交割
                self.O._settlement_3d(js_string)

            if format_id==4312:  #對賬單
                self.O._statement(js_string)

            if format_id==4033:  #證劵資劵餘額
                self.O._CreditInfo(js_string)

        if hasattr(self, 'F'):
            if format_id==1616:
                self.F._profit(js_string)

            if format_id==1618:
                self.F._profit_detail(js_string)

            if format_id==1614:
                self.F._realprofit(js_string)

            if format_id==1624:
                self.F._realprofit_detail(js_string)

            if format_id==1626:
                self.F._balance(js_string)

            if format_id==1639:
                self.F._balanceX(js_string)

        if hasattr(self, 'S'):
            if format_id==4113:
                self.S._getorder(js_string)

            if format_id==4117:
                self.S._getdeal(js_string)

            if format_id==4123:
                self.S._profit(js_string)

            if format_id==4119:
                self.S._statement(js_string)

            if format_id==4121:
                self.S._statementX(js_string)

            if format_id==4125:
                self.S._settlement(js_string)

            # data = json.loads(js_string)
            # display(data)
