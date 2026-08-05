import gzip
import json
import os
import pandas as pd
import numpy as np
import hashlib
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
import requests
import zipfile
import shutil
from .download import smart_download

basedir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_data')
file_path = os.path.join(basedir,'date_index.json')
with open(file_path , 'r') as f:
    list_ex = json.load(f)
INDEX = pd.to_datetime(list_ex)

file_path = os.path.join(basedir, "list.json")
with open(file_path, "r", encoding="utf-8") as f:
    _list = json.load(f)

file_path = os.path.join(basedir, "info.json")
_info = pd.read_json(file_path, orient="records")
_info['下市日']=pd.to_datetime(_info['下市日'] ,unit='ms')
_info['上市日']=pd.to_datetime(_info['上市日'] ,unit='ms')
delist_dates =_info[_info['下市日'].notnull()] .set_index('股票代號')['下市日']

class ProboDataFrame(pd.DataFrame):
    _metadata = ['_type']

    @property
    def _constructor(self):
        return ProboDataFrame
    
    def __lt__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__lt__(df1, df2)

    def __gt__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__gt__(df1, df2)

    def __le__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__le__(df1, df2)

    def __ge__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__ge__(df1, df2)

    def __eq__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__eq__(df1, df2)

    def __ne__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__ne__(df1, df2)

    def __sub__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__sub__(df1, df2)

    def __add__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__add__(df1, df2)

    def __mul__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__mul__(df1, df2)

    def __truediv__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__truediv__(df1, df2)

    def __rshift__(self, other):
        return self.shift(-other)

    def __lshift__(self, other):
        return self.shift(other)

    def __and__(self, other):
        # if getattr(self, '_type', None) == 'pos' and getattr(other, '_type', None) == 'pos':
        #     df1, df2 = self.reshape_union(self, other)

        #     mask = ~df2.isna()

        #     # 確保 dtype=object 以兼容字串
        #     arr1 = df1.values.astype(object).copy()
        #     arr2 = df2.values.astype(object)

        #     arr1[mask.values] = arr2[mask.values]

        #     result = ProboDataFrame(arr1, index=df1.index, columns=df1.columns)
        #     result._type = 'pos'
        #     return result
        # else:
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__and__(df1, df2)

    def __or__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__or__(df1, df2)

    # def __getitem__(self, other):
    #     df1, df2 = self.reshape(self, other)
    #     return pd.DataFrame.__getitem__(df1, df2)

    def _to_std_str_year(self):
        index = [y[:4] + '-Y' for y in self.index]
        df = ProboDataFrame(self.values, index= index, columns=self.columns)
        df.rename_axis(columns=None, inplace=True)
        return df

    def _to_std_str_month(self):
        index = [ym[:4] + '-M' + ym[4:] for ym in self.index]
        df = ProboDataFrame(self.values, index= index, columns=self.columns)
        df.rename_axis(columns=None, inplace=True)
        return df

    def _to_std_str_quarter(self):
        index = [yq[:4] + '-Q' + yq[-1] for yq in self.index]
        df = ProboDataFrame(self.values, index= index, columns=self.columns)
        df.rename_axis(columns=None, inplace=True)
        return df
    
    def last_of_week(df):
        weekly_last_days = df.index.to_series().resample('W-FRI').last().dropna()
        result = pd.DatetimeIndex(weekly_last_days)
        df = df.loc[result]
        return df        

    def last_of_month(df):
        s = df.index.to_series()
        try:
            monthly_last_days = s.resample('ME').last().dropna()
        except ValueError:
            monthly_last_days = s.resample('M').last().dropna()
        result = pd.DatetimeIndex(monthly_last_days)
        df = df.loc[result]
        return df
    
    # def set_qty(self ,data):
    #     if isinstance(data, pd.DataFrame):
    #         qty = data
    #     else:
    #         qty = pd.DataFrame(data , index=self.index, columns=self.columns)

    #     df = qty.where(self , other=np.nan)
    #     df = ProboDataFrame(df)
    #     df._type ='pos'
    #     return df

    @staticmethod
    def _to_trading_day(index_ls):
        # print(index_df)
        # Load from the pickle file
        # with open('trading_day_indices.pkl', 'rb') as f:
        #     loaded_indices = pickle.load(f)
        # need to add condition for updating the indices file
        loaded_indices = INDEX
        # Find the indices to insert your_dates into reference_date    
        insert_indices = np.searchsorted(loaded_indices, index_ls )
        # Get the nearest larger date for each date in your_dates
        def skip_weekend(d):
            add_days = {5: 2, 6: 1}
            wd = d.weekday()
            if wd in add_days: d += timedelta(days=add_days[wd])
            return d
        # converted_indices = loaded_indices[insert_indices] 
        max_d = loaded_indices.max()
        min_d = loaded_indices.min()
        # print(index_ls)
        converted_indices = [loaded_indices[insert_indices[i]] if min_d<= index_ls[i] <=max_d else skip_weekend(index_ls[i]) for i in range(len(index_ls))]
        
        return converted_indices
    
    def get_index_str_frequency(df):

        if len(df.index) == 0:
            return None

        if not isinstance(df.index[0], str):
            return None

        if (df.index.str.find('M') != -1).all():
            return 'month'

        if (df.index.str.find('Q') != -1).all():
            return 'season'
        
        if (df.index.str.find('Y') != -1).all():
            return 'year'

        return None

    @staticmethod
    def reshape(df1, df2):
        if isinstance(df1, pd.DataFrame) and isinstance(df2, pd.DataFrame) :
            col1 = df1.columns
            col2 = df2.columns
            name1= df1.index.name 
            name2 = df2.index.name 

            if len(col1)==1 and col1[0] in ['TSE','OTC']:
                c1 = col1[0]
                value = df1[c1]                        # value now Series not DataFrame
                df1 = ProboDataFrame(columns=sorted(col2), index=value.index)
                df1 = df1.assign(**{
                    col: pd.to_numeric(value, errors='coerce') 
                    for col in col2
                })
                df1.index.name = name1

            # ---- second dataset reshape ----
            if len(col2)==1 and col2[0] in ['TSE','OTC']:
                c2 = col2[0]
                value = df2[c2]
                df2 = ProboDataFrame(columns=sorted(col1), index=value.index)
                df2 = df2.assign(**{
                    col: pd.to_numeric(value, errors='coerce') 
                    for col in col1
                })
                df2.index.name = name2

        ispdf1 = isinstance(df1, ProboDataFrame)
        ispdf2 = isinstance(df2, ProboDataFrame)
        isdf1 = isinstance(df1, pd.DataFrame)
        isdf2 = isinstance(df2, pd.DataFrame)
        both_are_dataframe = (ispdf1 + isdf1) * (ispdf2 + isdf2) != 0
        
        d1_index_freq = df1.get_index_str_frequency() if ispdf1 else None
        d2_index_freq = df2.get_index_str_frequency() if ispdf2 else None

        if ((d1_index_freq or d2_index_freq)and (d1_index_freq != d2_index_freq) and both_are_dataframe):
            df1 = df1.index_str_to_date() if ispdf1 else df1
            df2 = df2.index_str_to_date() if ispdf2 else df2

        if isinstance(df2, pd.Series):
            df2 = pd.DataFrame({c: df2 for c in df1.columns})

        if both_are_dataframe:
            index = df1.index.union(df2.index)
            columns = df1.columns.intersection(df2.columns)
            if len(df1.index) * len(df2.index) != 0:
                index_start = max(df1.index[0], df2.index[0])
                index = [t for t in index if index_start <= t]
            dfx = df1.reindex(index=index, method='ffill')[columns],df2.reindex(index=index, method='ffill')[columns]
            return dfx
        else:
            return df1, df2

    @staticmethod
    def reshape_union(df1, df2):
        ispdf1 = isinstance(df1, ProboDataFrame)
        ispdf2 = isinstance(df2, ProboDataFrame)
        isdf1 = isinstance(df1, pd.DataFrame)
        isdf2 = isinstance(df2, pd.DataFrame)
        both_are_dataframe = (ispdf1 + isdf1) * (ispdf2 + isdf2) != 0

        d1_index_freq = df1.get_index_str_frequency() if ispdf1 else None
        d2_index_freq = df2.get_index_str_frequency() if ispdf2 else None

        if ((d1_index_freq or d2_index_freq) and (d1_index_freq != d2_index_freq) and both_are_dataframe):
            df1 = df1.index_str_to_date() if ispdf1 else df1
            df2 = df2.index_str_to_date() if ispdf2 else df2

        if isinstance(df2, pd.Series):
            df2 = pd.DataFrame({c: df2 for c in df1.columns})

        if both_are_dataframe:
            index = df1.index.union(df2.index)
            columns = df1.columns.union(df2.columns)
            return df1.reindex(index=index, columns=columns), df2.reindex(index=index, columns=columns)
        else:
            return df1, df2
        
    def index_str_to_date(self):
        if len(self.index) == 0 or not isinstance(self.index[0], str):
            df=self.copy()
        elif self.index[0].find('M') != -1:
            df =self._index_mstr_to_datetime()
        elif self.index[0].find('Q') != -1:
            df=self._index_qstr_to_datetime()
        elif self.index[0].find('Y') != -1:
           df=self._index_ystr_to_datetime()

        idx = df.index.values[:, None]  # 變成 (N, 1) 的形狀
        cols = df.columns.values        # 變成 (M,) 的形狀
        target_delist_dates = delist_dates.reindex(df.columns).fillna(pd.Timestamp('2099-12-31')).values
        mask = idx >= target_delist_dates

        if not df.columns.empty and df.dtypes.iloc[0] == 'bool':
            # 布林矩陣：下市的地方填 False
            new_values = np.where(mask, False, df.values)
            df = ProboDataFrame(new_values, index=df.index, columns=df.columns).astype(bool)
        else:
            df=df.mask(mask)
            # # 數值矩陣：下市的地方填 np.nan
        return df
    
    def _index_qstr_to_datetime(self):
        index = self.index.copy()
        mapping = {1:(5, 15), 2:(8,14),3:(11,14),4:(3,21)}
        def convert_to_datetime(date_str):
            year, q = map(int, date_str.split('-Q'))
            if q == 4:
                year += 1
        
            return pd.Timestamp(year, *mapping[q])
            
        date_idx = [convert_to_datetime(idx) for idx in index]
        index = self._to_trading_day(date_idx)

        modified_df = ProboDataFrame(self.values, index=index, columns=self.columns)
        modified_df.index.name = 'date'

        return modified_df
    
    def _index_mstr_to_datetime(self):
        index = self.index.copy()
        
        def convert_to_datetime(date_str):
            year, month = map(int, date_str.split('-M'))
            day = 10
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
        
            return pd.Timestamp(year, month, day)
            
        date_idx = [convert_to_datetime(idx) for idx in index]
        index = self._to_trading_day(date_idx)

        modified_df = ProboDataFrame(self.values, index=index, columns=self.columns)
        modified_df.index.name = 'date'

        return modified_df
    
    def _index_ystr_to_datetime(self):
        index = self.index.str[:4].copy()
        dic={
        '2000':pd.Timestamp('2000-12-29 00:00:00'),
        '2001':pd.Timestamp('2001-12-30 00:00:00'),
        '2002':pd.Timestamp('2002-12-31 00:00:00'),
        '2003':pd.Timestamp('2003-12-31 00:00:00'),
        '2004':pd.Timestamp('2004-12-31 00:00:00'),
        '2005':pd.Timestamp('2005-12-30 00:00:00'),
        '2006':pd.Timestamp('2006-12-29 00:00:00'),
        '2007':pd.Timestamp('2007-12-31 00:00:00'),
        '2008':pd.Timestamp('2008-12-31 00:00:00'),
        '2009':pd.Timestamp('2009-12-31 00:00:00'),
        '2010':pd.Timestamp('2010-12-31 00:00:00'),
        '2011':pd.Timestamp('2011-12-30 00:00:00'),
        '2012':pd.Timestamp('2012-12-31 00:00:00'),
        '2013':pd.Timestamp('2013-12-31 00:00:00'),
        '2014':pd.Timestamp('2014-12-31 00:00:00'),
        '2015':pd.Timestamp('2015-12-31 00:00:00'),
        '2016':pd.Timestamp('2016-12-30 00:00:00'),
        '2017':pd.Timestamp('2017-12-29 00:00:00'),
        '2018':pd.Timestamp('2018-12-31 00:00:00'),
        '2019':pd.Timestamp('2019-12-31 00:00:00'),
        '2020':pd.Timestamp('2020-12-31 00:00:00'),
        '2021':pd.Timestamp('2021-12-30 00:00:00'),
        '2022':pd.Timestamp('2022-12-30 00:00:00'),
        '2023':pd.Timestamp('2023-12-29 00:00:00'),
        '2024':pd.Timestamp('2024-12-31 00:00:00'),
        '2025':pd.Timestamp('2025-12-31 00:00:00'),
        '2026':pd.Timestamp('2026-12-31 00:00:00'),
        '2027':pd.Timestamp('2027-12-31 00:00:00'),
        '2028':pd.Timestamp('2028-12-29 00:00:00'),
        '2029':pd.Timestamp('2029-12-31 00:00:00'),
        '2030':pd.Timestamp('2030-12-31 00:00:00'),
        '2031':pd.Timestamp('2031-12-31 00:00:00'),
        '2032':pd.Timestamp('2032-12-31 00:00:00'),
        '2033':pd.Timestamp('2033-12-30 00:00:00'),
        '2034':pd.Timestamp('2034-12-29 00:00:00'),
        }
        date_idx = [dic[year] for year in index]
        date_idx = date_idx[:-1]+[INDEX[-1]]
        index = self._to_trading_day(date_idx)

        modified_df = ProboDataFrame(self.values, index=index, columns=self.columns)
        modified_df.index.name = 'date'
        return modified_df
    
def _probo_df(df):
    df = ProboDataFrame(df)
    # 將 index 轉為字串來判斷格式
    idx = list(map(str, df.index[-12:]))

    if len(idx[0]) == 4:
        df = df._to_std_str_year()
    elif len(idx[0]) == 6:
        suffixes = set(s[-2:] for s in idx)
        if len(suffixes) > 4:
            df = df._to_std_str_month()
        else:
            df = df._to_std_str_quarter()
    elif '-Q' in idx[0]:
        df = ProboDataFrame(df.values, index= df.index, columns=df.columns)
    elif '-M' in idx[0]:
        df = ProboDataFrame(df.values, index= df.index, columns=df.columns)
    else:
        df.index=pd.to_datetime(df.index,errors='coerce')
        df= df[df.index.notna()]
        df.index.name='date'
    df.columns = df.columns.astype(str)
    return df

def _year_cumsum(df):
    dfx=df.copy()
    dfx['year']=dfx.index.str[:4]
    return dfx.groupby('year').cumsum()

def _week(df):
    df=df.ffill()
    df['week']=df.index.to_period('W')
    index=df.groupby('week').tail(1).index
    df=df.loc[index].drop(['week'], axis=1)
    return df

def _month(df):
    df=df.ffill()
    df['mon']=df.index.to_period('M')
    index=df.groupby('mon').tail(1).index
    df=df.loc[index].drop(['mon'], axis=1)
    return df
