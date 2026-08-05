import os
import threading
import time
import datetime
import requests
import pandas as pd
from dataclasses import dataclass, field, fields

columns = [
    'full_symbol', "market", "name_zh", "name_en", "type", "currency",
    "ref_price", "prev_close", "start_date", "board_lot",
    "bloomberg_ticker", "isin", "security_class", "cusip", "sedol"
]

@dataclass
class Contract:
    symbol: str
    full_symbol: str = field(repr=False)
    market: str
    name_zh: str
    name_en: str
    type: str
    currency: str
    ref_price: float
    prev_close: float
    start_date: str
    board_lot: int
    bloomberg_ticker: str
    isin: str
    security_class: str
    cusip: str
    sedol: str

class SubContracts:
    def __init__(self, url):
        self.url = url
        self.UPDATE_HOURS = ('0818','1321')
        self.cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ProListRc.txt.zip")
        self.contracts = pd.DataFrame()
        self.contracts_dic = {}
        self._bg_thread_started = False

    def get_contracts(self, type='dic'):
        if self.contracts.empty:
            # 先嘗試載入本地檔案
            if os.path.exists(self.cache_path):
                self._load_local_file()
                if self._need_update():
                    self._download_contracts()
            else:
                self._download_contracts()

            if not getattr(self, "_bg_thread_started", False):
                threading.Thread(target=self._auto_update_loop, daemon=True).start()
                self._bg_thread_started = True

        if type == 'df':
            return self.contracts
        elif type == 'dic':
            return self.contracts_dic
        else:
            raise ValueError("type 必須是 'df' 或 'dic'")
        
    def _load_local_file(self):
        """讀取已存在的 zip 檔案並初始化 contracts / contracts_dic"""
        df = pd.read_csv(self.cache_path, compression='zip', sep='|', header=None, low_memory=False)
        df.columns = columns
        df['symbol'] = df['full_symbol'].str.rsplit('.', n=1).str[0]
        df = df[['symbol'] + columns]
        df = df[df.market == 'US'].reset_index(drop=True)
        df = df.drop_duplicates(subset='symbol', keep='first')

        # contracts df
        self.contracts = df.drop(columns=['full_symbol'])
        # contracts dic
        contract_fields = {f.name for f in fields(Contract)}
        self.contracts_dic = {
            row['symbol']: Contract(**{k: v for k, v in row.to_dict().items() if k in contract_fields})
            for _, row in df.iterrows()
        }

    def _download_contracts(self):
        print("下載複委托商品檔:", self.url)
        r = requests.get(self.url)
        r.raise_for_status()
        with open(self.cache_path, "wb") as f:
            f.write(r.content)
        print(f"已儲存至 {self.cache_path}")
        self._load_local_file()

    def _need_update(self):
        """判斷是否需要更新"""
        if not os.path.exists(self.cache_path):
            return True

        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(self.cache_path))
        now = datetime.datetime.now()

        # 將 UPDATE_HOURS 字串轉成今天的 datetime
        update_times = []
        for h in self.UPDATE_HOURS:
            hh = int(h[:2])
            mm = int(h[2:])
            update_times.append(now.replace(hour=hh, minute=mm, second=0, microsecond=0))

        # 找出已經過的時間點
        passed_times = [t for t in update_times if now >= t]
        if not passed_times:
            # 還沒到今天第一個更新時間，不需要下載
            return False
        last_scheduled_time = max(passed_times)

        if mtime.date() == now.date() and mtime >= last_scheduled_time:
            return False
        else:
            return True

    def _auto_update_loop(self):
        """背景自動更新迴圈"""
        while True:
            try:
                if self._need_update():
                    self._download_contracts()
            except Exception as e:
                print("更新發生錯誤:", e)
            # 每分鐘檢查一次
            time.sleep(60)

    def _dic(self ,symbol =None):
        if self.contracts.empty:
            # 先嘗試載入本地檔案
            if os.path.exists(self.cache_path):
                self._load_local_file()
                if self._need_update():
                    self._download_contracts()
            else:
                self._download_contracts()

            if not getattr(self, "_bg_thread_started", False):
                threading.Thread(target=self._auto_update_loop, daemon=True).start()
                self._bg_thread_started = True

        if symbol is None:
            return self.contracts_dic
        if symbol not in self.contracts_dic:
            raise ValueError(f"股票代號 {symbol} 不存在於商品檔中。")
        return self.contracts_dic[symbol]
    
    def _symbol_to_full(self ,symbol=None):
        if self.contracts.empty:
            # 先嘗試載入本地檔案
            if os.path.exists(self.cache_path):
                self._load_local_file()
                if self._need_update():
                    self._download_contracts()
            else:
                self._download_contracts()

            if not getattr(self, "_bg_thread_started", False):
                threading.Thread(target=self._auto_update_loop, daemon=True).start()
                self._bg_thread_started = True

        if symbol is None:
            return {v.full_symbol: v.symbol for k, v in self.contracts_dic.items()}
        else:
            return {v.full_symbol: v.symbol for k, v in self.contracts_dic.items()}.get(symbol ,symbol)
    

    
