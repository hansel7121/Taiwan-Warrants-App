import pandas as pd

class ProboDataFrame(pd.DataFrame):
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
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__and__(df1, df2)

    def __or__(self, other):
        df1, df2 = self.reshape(self, other)
        return pd.DataFrame.__or__(df1, df2)
    
    def last_of_week(df):
        weekly_last_days = df.index.to_series().resample('W-FRI').last().dropna()
        result = pd.DatetimeIndex(weekly_last_days)
        df = df.loc[result]
        return df        
    
    def first_of_week(df):
        weekly_first_days = (
            df.index.to_series().resample('W-MON').first().dropna())
        result = pd.DatetimeIndex(weekly_first_days)
        df = df.loc[result]
        return df

    def last_of_month(df):
        # 將索引轉為 Series 並按月重新採樣，選取每個月的最後一個日期
        monthly_last_days = df.index.to_series().resample('M').last().dropna()
        # 轉為 DatetimeIndex
        result = pd.DatetimeIndex(monthly_last_days)
        # 從原始 DataFrame 中選出這些日期的數據
        df = df.loc[result]
        return df
    
    def first_of_month(df):
        # 將索引轉為 Series 並按月重新採樣，選取每個月的最後一個日期
        monthly_last_days = df.index.to_series().resample('M').first().dropna()
        # 轉為 DatetimeIndex
        result = pd.DatetimeIndex(monthly_last_days)
        # 從原始 DataFrame 中選出這些日期的數據
        df = df.loc[result]
        return df

    @staticmethod
    def reshape(df1, df2):
        ispdf1 = isinstance(df1, ProboDataFrame)
        ispdf2 = isinstance(df2, ProboDataFrame)
        isdf1 = isinstance(df1, pd.DataFrame)
        isdf2 = isinstance(df2, pd.DataFrame)
        both_are_dataframe = (ispdf1 + isdf1) * (ispdf2 + isdf2) != 0
        
        d1_index_freq = df1.get_index_str_frequency() if ispdf1 else None
        d2_index_freq = df2.get_index_str_frequency() if ispdf2 else None

        if ((d1_index_freq or d2_index_freq)and (d1_index_freq != d2_index_freq) and both_are_dataframe):
            df1 = df1.index_str_to_date().ffill() if ispdf1 else df1
            df2 = df2.index_str_to_date().ffill() if ispdf2 else df2

        if isinstance(df2, pd.Series):
            df2 = pd.DataFrame({c: df2 for c in df1.columns})

        if both_are_dataframe:
            index = df1.index.union(df2.index)
            columns = df1.columns.intersection(df2.columns)
            if len(df1.index) * len(df2.index) != 0:
                index_start = max(df1.index[0], df2.index[0])
                index = [t for t in index if index_start <= t]
            return df1.reindex(index=index, method='ffill')[columns],df2.reindex(index=index, method='ffill')[columns]
        else:
            return df1, df2
        
    def index_str_to_date(self):
        if len(self.index) == 0 or not isinstance(self.index[0], str):
            return self
        if self.index[0].find('M') != -1:
            return self._index_mstr_to_datetime()
        elif self.index[0].find('Q') != -1:
            return self._index_qstr_to_datetime()
        elif self.index[0].find('Y') != -1:
            return self._index_ystr_to_datetime()
        return self
    
    def _index_qstr_to_datetime(self):
        # 取得財報發布日資料
        from .data_us import getdata
        disclosure_df = getdata('US-10010a', '財報發布日',server_site=True)
        disclosure_df = pd.to_datetime(disclosure_df.stack(), errors='coerce').unstack()

        # 對齊欄位
        intersect_cols = self.columns.intersection(disclosure_df.columns)

        # 展平 disclosure
        disclosure_dates = disclosure_df.reindex(self.index)[intersect_cols].unstack()

        # 展平 value
        unstacked = self[intersect_cols].unstack()
        ret = pd.DataFrame({
            'value': unstacked.values,
            'disclosure': disclosure_dates.values,
        }, index=unstacked.index)

        dfx = ret.reset_index()
        # 取得第一欄的名稱（不論它是 None 轉成的 level_0 還是原本的名稱）
        first_col = dfx.columns[0] 
        dfx = dfx.drop_duplicates(['disclosure', first_col])
        dfx = dfx.pivot(index='disclosure', columns=first_col, values='value')
        dfx = dfx[dfx.index.notna()]
        dfx.index.name='date'
        dfx.columns.name = None
        return ProboDataFrame(dfx)
    
    def _index_ystr_to_datetime(self):
        from .data_us import getdata
        disclosure_df = getdata('US-10010a', '財報發布日', server_site=True)
        disclosure_df = pd.to_datetime(disclosure_df.stack(), errors='coerce').unstack()
        years = disclosure_df.index.astype(str).str[:4]
        disclosure_df = disclosure_df.groupby(years).last()
        disclosure_df.index = disclosure_df.index + '-Y'

        # 對齊欄位
        intersect_cols = self.columns.intersection(disclosure_df.columns)

        # 展平 disclosure
        disclosure_dates = disclosure_df.reindex(self.index)[intersect_cols].unstack()
        
        # 展平 value
        unstacked = self[intersect_cols].unstack()
        ret = pd.DataFrame({
            'value': unstacked.values,
            'disclosure': disclosure_dates.values,
        }, index=unstacked.index)

        dfx = ret.reset_index()
        # 取得第一欄的名稱（不論它是 None 轉成的 level_0 還是原本的名稱）
        first_col = dfx.columns[0] 
        dfx = dfx.drop_duplicates(['disclosure', first_col])
        dfx = dfx.pivot(index='disclosure', columns=first_col, values='value')
        dfx = dfx[dfx.index.notna()]
        dfx.index.name='date'
        dfx.columns.name = None
        return ProboDataFrame(dfx)

    def _to_std_str_quarter(self):
        index = [yq[:4] + '-Q' + yq[-1] for yq in self.index.astype(str)]
        df = ProboDataFrame(self.values, index=index, columns=self.columns)
        df.rename_axis(columns=None, inplace=True)
        return df

    def _to_std_str_year(self):
        index = [y[:4] + '-Y' for y in self.index.astype(str)]
        df = ProboDataFrame(self.values, index=index, columns=self.columns)
        df.rename_axis(columns=None, inplace=True)
        return df

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