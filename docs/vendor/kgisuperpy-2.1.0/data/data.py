import json
import requests
from dataclasses import dataclass
import pandas as pd
import numpy as np
import datetime
#pd.set_option('display.float_format', lambda x: '%.0f' % x if x == int(x) else '%.4f' % x)
pd.set_option('display.float_format', lambda x: '%.0f' % x if (np.isfinite(x) and x == int(x)) else '%.4f' % x)
import hashlib
from ._data import _get
from ._check_args import check_args ,_err_msg

@dataclass
class Contract:
    symbol: str
    name: str
    update_date: str
    market: str
    category: str
    sub_category: str
    bull_limit: float
    bear_limit: float
    ref_price: float
    day_trade: str  

@dataclass
class Stock:
    exchange:str
    datetime: str
    price: float
    volume: int
    total_volume: int
    bid_price: float
    ask_price: float
    bid_volume: int
    ask_volume: int
    open_price: float
    high_price: float
    low_price: float
    limit_up: float
    limit_down: float
    reference_price: float

def symbol_to_settlement_ym(symbol: pd.Series) -> pd.Series:
    """
    解析 symbol → 'YYYY-MM'
    - 例如 TXFI5 → 2025-09（假設今年是 2025）
    - YM_map 會第一次呼叫時計算，後續呼叫直接使用
    """
    # 靜態 cache 變數
    if not hasattr(symbol_to_settlement_ym, "_YM_map"):
        # 計算月份碼
        _month_map = {chr(ord('A') + i): i + 1 for i in range(12)}

        # 最近 12 年最後一碼 map
        now_year = datetime.datetime.now().year
        years = list(range(now_year, now_year - 12, -1))
        _year_map = {}
        for y in years:
            d = y % 10
            if d not in _year_map:
                _year_map[d] = y

        # 合併成一個 dict cache，方便函式內使用
        symbol_to_settlement_ym._YM_map = {
            "month": _month_map,
            "year": _year_map
        }

    YM_map = symbol_to_settlement_ym._YM_map

    s = symbol.astype(str)
    month_code = s.str[-2]
    last_digit = pd.to_numeric(s.str[-1], errors="coerce").fillna(-1).astype(int)

    month = month_code.map(YM_map["month"])
    year = last_digit.map(YM_map["year"])

    invalid = month.isna() | year.isna()
    out = year.astype("Int64").astype(str) + "-" + month.astype("Int64").astype(str).str.zfill(2)
    out[invalid] = None
    return out

def dt_to_settlement_near(date):
    """
    輸入: date: pd.Series of Timestamps (df['DateTime'])
    回傳: pd.Series of settlement Timestamp (same index as date)
    規則:
      - 若 DateTime <= 當月第三個週三 14:00 -> 當月結算日
      - 若 DateTime > 當月第三個週三 14:00 -> 下個月結算日
    """
    def _third_wednesday(year, month):
        d = datetime.date(year, month, 1)
        while d.weekday() != 2:   # 2 = Wednesday
            d += datetime.timedelta(days=1)
        return d + datetime.timedelta(days=14)

    def _settlement_days(start_date, end_date):
        result = []
        y, m = start_date.year, start_date.month
        end_y, end_m = end_date.year, end_date.month
    
        while (y < end_y) or (y == end_y and m <= end_m):
            td = _third_wednesday(y, m)
            result.append(td)
            m += 1
            if m == 13:
                m = 1
                y += 1
        return result

    # 取得唯一的日期範圍（保持原意）
    ls = date.dt.date.unique()
    first_d = ls[0]
    last_d = (ls[-1] + pd.DateOffset(months=1)).date()
    settles = _settlement_days(first_d, last_d)

    days=[]
    n=0
    for d in ls:
        s = settles[n]
        if d>=s:
            n=n+1
            days.append(pd.Timestamp(d) + pd.Timedelta('14h'))

    if n <= len(settles):
        s = settles[n]
        days.append(pd.Timestamp(s) + pd.Timedelta('14h'))
    days = np.array(pd.to_datetime(days))

    # 找到每筆 DateTime 所屬的結算日
    # side='left' → 第一個 days[i] >= dt
    dt_array = date.to_numpy()
    idx = np.searchsorted(days, dt_array, side='left')
    # 回傳該筆所對應到的結算時間
    return pd.Series(days[idx], index=date.index)

# df['settlement'] = symbol_to_settlement_ym(df['symbol'])
# s2 = dt_to_settlement_near(df['DateTime']).dt.month

# s1 = df['settlement'].str[-1].astype(int)
# s1 = np.where(s1 < s2, s1 + 12, s1)

# df['ContractMonth'] = 'R' + (s1 - s2 + 1).astype(str)


class _Data():
    def __init__(self ,URL):
        self._base_url = URL.base_url
        self._URL =URL
        self._contracts_df = None
        self._contracts_dic = None
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._URL.token}"
        }
        self._index =_get('日K/技術指標/價量資料-單檔股票多個區間',self._base_url ,headers ,'Y9999').日期.tolist()
  
    def get(self, table: str, *args) -> pd.DataFrame:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._URL.token}"
        }
        check_args(table, args ,index=self._index)
        df = _get(table ,self._base_url ,headers, *args)
        if len(df)==0:
            _err_msg(table)

        return df
    
    def get_snapshots(self,data):
        headers = {"Content-Type": "application/json", 'Authorization': f'Bearer {self._URL.token}'} 

        if isinstance(data, str): 
            data=[data]
        payload = {    "symbols": list(set(data))}

        response = requests.post( self._base_url +  f"/api/stock/match", headers=headers, json=payload, verify=True) 
        data = response.json()

        dic = {}
        for d in data:
            # 解析 match 字段
            if len(d['match'].split(','))==9:
                price, volume, total_volume, open, dt, bid_price, ask_price, bid_volume, ask_volume = d['match'].split(',')
            elif len(d['match'].split(','))==7:
                price, volume, total_volume, open, dt, bid_price, ask_price = d['match'].split(',')
                bid_volume =0
                ask_volume =0
            else:
                continue

            # 解析 dayHighLow 字段
            high, low = d['dayHighLow'].split(',')
            # 解析 bullBearRefPx 字段
            limitup, limitdn, reference = d['bullBearRefPx'].split(',')

            # 創建 Stock 實例
            dic[d['symbol']] = Stock(
                price=float(price),
                volume=int(volume),
                total_volume=int(total_volume),
                open_price=float(open),
                datetime=dt,
                bid_price=float(bid_price),
                ask_price=float(ask_price),
                bid_volume=int(bid_volume),
                ask_volume=int(ask_volume),
                high_price=float(high),
                low_price=float(low),
                limit_up=float(limitup),
                limit_down=float(limitdn),
                reference_price=float(reference),
                exchange=d['exchange']
            )
        return dic
    
    def contract(self ,type='dic'):
        """
        取得合約資訊

        :param type: 回傳格式
                        - 'dic' : 以字典方式回傳 {symbol: Contract}
                        - 'df'  : 回傳 pandas DataFrame
        :return: 合約資料 (dict 或 DataFrame)
        """
        # 已經有快取直接回傳
        if self._contracts_df is not None and self._contracts_dic is not None:
            return self._contracts_dic if type=='dic' else self._contracts_df
        
        contract = {}
        df = self.get('台股商品檔')
        df['交易所'] = df['交易所'].apply(lambda x: { '1': 'tse','2': 'otc', '3': 'oes', '4': 'tib'}[x])
        df['day_trade'] = np.where(df['可先買後賣當沖標的'] == 'Y', 1, 0) +np.where(df['可先賣後買當沖標的'] == 'Y', 1, 0) +np.where(df['當日禁止當沖標的'] == 'Y', -3, 0)
        df['day_trade'] = df['day_trade'].replace(-3, 'No').replace(2, 'Yes').replace(1, 'OnlyBuy')
        for _, row in df.iterrows():
            try:
                symbol = row['股票代號']
                contract[symbol] = Contract(
                    symbol=symbol,
                    name=row['股票名稱'],
                    update_date= row['日期'],
                    market= row['交易所'],
                    category= row.get('粗產業', ''),
                    sub_category= row.get('細產業', ''),
                    bull_limit= float(row['次日漲停價']),
                    bear_limit= float(row['次日跌停價']),
                    ref_price= float(row['次日開盤參考價']),
                    day_trade= row['day_trade']
                )
            except Exception as e:
                print(f"Error processing row {row['股票代號']}: {e}")

        # 將 DataFrame 也存成快取
        self._contracts_dic = contract
        self._contracts_df = pd.DataFrame([vars(c) for c in contract.values()])

        return self._contracts_dic if type=='dic' else self._contracts_df
    
