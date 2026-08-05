import numba ,inspect ,datetime ,os ,json 
import numpy as np
import pandas as pd
from numba import njit
from numba.typed import List

# dashboard html 
from .html_util import (generate_metrics_html, 
                        generate_return_metrics_html,
                         generate_additional_metrics_html,
                         generate_remainder_table_html,
                         create_pie_chart_html,
                         generate_heatmap_html,
                         generate_single_figure_html,
                         generate_dd_html,
                         generate_trade_table_html,
                         generate_daily_table_section_html,
                         generate_position_table_section_html)
from ._BT_util import process_for_dashboard ,_process_df ,get_longest_dd
from .dashboard import dashboard



import logging
# 配置 matplotlib 的日志级别为 ERROR，以完全阻止字体查找警告
logging.getLogger('matplotlib').setLevel(logging.ERROR)

import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)

if hasattr(pd.options, 'future') and hasattr(pd.options.future, 'no_silent_downcasting'):
    pd.set_option('future.no_silent_downcasting', True)

#新圖表使用
from ._BT_util import heatmap_returns  ,drawdown_plotly ,drawdowns_periods_plotly ,net_pnl_plotly ,pnlcost_plt_plotly 

def _prepare_returns(data, rf=0.0, nperiods=None):
    """Converts price data into returns + cleanup"""
    data = data.copy()
    function = inspect.stack()[1][3]
    if isinstance(data, pd.DataFrame):
        for col in data.columns:
            if data[col].dropna().min() >= 0 and data[col].dropna().max() > 1:
                data[col] = data[col].pct_change(fill_method=None)
    elif data.min() >= 0 and data.max() > 1:
        data = data.pct_change(fill_method=None)

    # cleanup data
    data = data.replace([np.inf, -np.inf], float("NaN"))

    if isinstance(data, (pd.DataFrame, pd.Series)):
        data = data.fillna(0).replace([np.inf, -np.inf], float("NaN"))
    unnecessary_function_calls = [
        "_prepare_benchmark",
        "cagr",
        "gain_to_pain_ratio",
        "rolling_volatility",
    ]

    if function not in unnecessary_function_calls:
        if rf > 0:
            return to_excess_returns(data, rf, nperiods)
    return data

def to_excess_returns(returns, rf, nperiods=None):
    """
    Calculates excess returns by subtracting
    risk-free returns from total returns

    Args:
        * returns (Series, DataFrame): Returns
        * rf (float, Series, DataFrame): Risk-Free rate(s)
        * nperiods (int): Optional. If provided, will convert rf to different
            frequency using deannualize
    Returns:
        * excess_returns (Series, DataFrame): Returns - rf
    """
    if isinstance(rf, int):
        rf = float(rf)

    if not isinstance(rf, float):
        rf = rf[rf.index.isin(returns.index)]

    if nperiods is not None:
        # deannualize
        rf = np.power(1 + rf, 1.0 / nperiods) - 1.0

    return returns - rf

def sharpe(returns, rf=0.0, periods=252, annualize=True, smart=False):
    """
    Calculates the sharpe ratio of access returns

    If rf is non-zero, you must specify periods.
    In this case, rf is assumed to be expressed in yearly (annualized) terms

    Args:
        * returns (Series, DataFrame): Input return series
        * rf (float): Risk-free rate expressed as a yearly (annualized) return
        * periods (int): Freq. of returns (252/365 for daily, 12 for monthly)
        * annualize: return annualize sharpe?
        * smart: return smart sharpe ratio
    """
    if rf != 0 and periods is None:
        raise Exception("Must provide periods if rf != 0")

    returns = _prepare_returns(returns, rf, periods)
    divisor = returns.std(ddof=1)
    if smart:
        # penalize sharpe with auto correlation
        divisor = divisor * autocorr_penalty(returns)
    res = returns.mean() / divisor

    if annualize:
        return res * np.sqrt(1 if periods is None else periods)

    return res

def autocorr_penalty(returns, prepare_returns=False):
    """Metric to account for auto correlation"""
    if prepare_returns:
        returns = _prepare_returns(returns)

    if isinstance(returns, pd.DataFrame):
        returns = returns[returns.columns[0]]

    # returns.to_csv('/Users/ran/Desktop/test.csv')
    num = len(returns)
    coef = np.abs(np.corrcoef(returns[:-1], returns[1:])[0, 1])
    corr = [((num - x) / num) * coef**x for x in range(1, num)]
    return np.sqrt(1 + 2 * np.sum(corr))

def sortino(returns, rf=0, periods=252, annualize=True, smart=False):
    """
    Calculates the sortino ratio of access returns

    If rf is non-zero, you must specify periods.
    In this case, rf is assumed to be expressed in yearly (annualized) terms

    Calculation is based on this paper by Red Rock Capital
    http://www.redrockcapital.com/Sortino__A__Sharper__Ratio_Red_Rock_Capital.pdf
    """
    if rf != 0 and periods is None:
        raise Exception("Must provide periods if rf != 0")

    returns = _prepare_returns(returns, rf, periods)

    downside = np.sqrt((returns[returns < 0] ** 2).sum() / len(returns))

    if smart:
        # penalize sortino with auto correlation
        downside = downside * autocorr_penalty(returns)

    res = returns.mean() / downside

    if annualize:
        return res * np.sqrt(1 if periods is None else periods)

    return res

def comp(returns):
    """Calculates total compounded returns"""
    return returns.add(1).prod() - 1

def group_returns(returns, groupby, compounded=False):
    """Summarize returns
    group_returns(df, df.index.year)
    group_returns(df, [df.index.year, df.index.month])
    """
    if compounded:
        return returns.groupby(groupby).apply(comp)
    return returns.groupby(groupby).sum()

def compsum(returns):
    """Calculates rolling compounded returns"""
    return returns.add(1).cumprod() - 1

def to_prices(returns, base=1e5):
    """Converts returns series to price data"""
    returns = returns.copy().fillna(0).replace([np.inf, -np.inf], float("NaN"))

    return base + base * compsum(returns)

def _prepare_prices(data, base=1.0):
    """Converts return data into prices + cleanup"""
    data = data.copy()
    if isinstance(data, pd.DataFrame):
        for col in data.columns:
            if data[col].dropna().min() <= 0 or data[col].dropna().max() < 1:
                data[col] = to_prices(data[col], base)

    # is it returns?
    # elif data.min() < 0 and data.max() < 1:
    elif data.min() < 0 or data.max() < 1:
        data = to_prices(data, base)

    if isinstance(data, (pd.DataFrame, pd.Series)):
        data = data.fillna(0).replace([np.inf, -np.inf], float("NaN"))

    return data

def max_drawdown(prices):
    """Calculates the maximum drawdown"""
    prices = _prepare_prices(prices)
    return (prices / prices.expanding(min_periods=0).max()).min() - 1

def to_drawdown_series(returns):
    """Convert returns series to drawdown series"""
    prices = _prepare_prices(returns)
    dd = prices / np.maximum.accumulate(prices) - 1.0
    return dd.replace([np.inf, -np.inf, -0], 0)

def remove_outliers(returns, quantile=0.95):
    """Returns series of returns without the outliers"""
    return returns[returns < returns.quantile(quantile)]

def drawdown_details(drawdown):
    """
    Calculates drawdown details, including start/end/valley dates,
    duration, max drawdown and max dd for 99% of the dd period
    for every drawdown period
    """

    def _drawdown_details(drawdown):
        # mark no drawdown
        no_dd = drawdown == 0

        # extract dd start dates, first date of the drawdown
        starts = ~no_dd & no_dd.shift(1)
        starts = list(starts[starts.values].index)

        # extract end dates, last date of the drawdown
        ends = no_dd & (~no_dd).shift(1)
        ends = ends.shift(-1, fill_value=False)
        ends = list(ends[ends.values].index)

        # no drawdown :)
        if not starts:
            return pd.DataFrame(
                index=[],
                columns=(
                    "start",
                    "valley",
                    "end",
                    "days",
                    "max drawdown",
                    "99% max drawdown",
                ),
            )

        # drawdown series begins in a drawdown
        if ends and starts[0] > ends[0]:
            starts.insert(0, drawdown.index[0])

        # series ends in a drawdown fill with last date
        if not ends or starts[-1] > ends[-1]:
            ends.append(drawdown.index[-1])

        # build dataframe from results
        data = []
        for i, _ in enumerate(starts):
            dd = drawdown[starts[i] : ends[i]]
            clean_dd = -remove_outliers(-dd, 0.99)
            data.append(
                (
                    starts[i],
                    dd.idxmin(),
                    ends[i],
                    (ends[i] - starts[i]).days + 1,
                    dd.min() * 100,
                    clean_dd.min() * 100,
                )
            )

        df = pd.DataFrame(
            data=data,
            columns=(
                "start",
                "valley",
                "end",
                "days",
                "max drawdown",
                "99% max drawdown",
            ),
        )
        df["days"] = df["days"].astype(int)
        df["max drawdown"] = df["max drawdown"].astype(float)
        df["99% max drawdown"] = df["99% max drawdown"].astype(float)

        df["start"] = df["start"].dt.strftime("%Y-%m-%d")
        df["end"] = df["end"].dt.strftime("%Y-%m-%d")
        df["valley"] = df["valley"].dt.strftime("%Y-%m-%d")

        return df

    if isinstance(drawdown, pd.DataFrame):
        _dfs = {}
        for col in drawdown.columns:
            _dfs[col] = _drawdown_details(drawdown[col])
        return pd.concat(_dfs, axis=1)

    return _drawdown_details(drawdown)

def get_longest_dd(returns, period=5):
    returns = _prepare_returns(returns)
    dd = to_drawdown_series(returns.fillna(0))
    dddf = drawdown_details(dd)
    longest_dd = dddf.sort_values(by="days", ascending=False, kind="mergesort")[:period]
    longest_dd = longest_dd.reset_index().drop(["index", "99% max drawdown"], axis=1)
    return longest_dd

#=======================================================================================

@njit
def sim_numba(timestamps, open_vals, close_vals, sig, sigout ,sig_values, stop_earn, stop_loss, qty,
              moveloss, max_hold, open_shift, close_shift, dynamic_stop=False):
    n = len(timestamps)
    trades = List()
    
    inventory = 0   # 0: 無部位, 1: 有部位
    entry = 0       # 1: 開倉訊號, -1: 平倉訊號
    # 用 -1 表示 pd.NaT 的時間
    opentime = -1  
    openPrice = 0.0
    movePrice = 0.0
    earnPrice = np.nan
    lossPrice = np.nan
    trade_qty = 0.0
    
    for i in range(n):
        current_time = timestamps[i]
        
        # 先檢查上一筆是否觸發平倉（exit），若是則先平倉後跳過本 row
        if entry == -1:
            entry = 0
            trades.append((0, opentime, openPrice, current_time, close_vals[i], -1, np.nan, trade_qty))
            inventory = 0
            # 跳過本 row，避免在同一 row 又重新開倉
            #continue

        # 若上一筆剛觸發開倉訊號，則設定開倉參數
        if entry == 1:
            entry = 0
            opentime = current_time
            openPrice = open_vals[i]
            movePrice = open_vals[i]
            trade_qty = qty[i]
            inventory = 1
            if not dynamic_stop:
                earnPrice = openPrice +stop_earn[i]
                lossPrice = openPrice +stop_loss[i]
            # 不跳過，因為後續可能會立即檢查平倉條件
            # (這裡與原始程式邏輯一致)
        
        # 當無持倉時，檢查是否有開倉訊號
        if inventory == 0:
            if (not np.isnan(sig[i])) and (sig[i] == 1.0):
                if open_shift:
                    entry = 1
                else:
                    opentime = current_time
                    openPrice = open_vals[i]
                    movePrice = open_vals[i]
                    trade_qty = qty[i]
                    inventory = 1
                    if not dynamic_stop:
                        earnPrice = openPrice +stop_earn[i]
                        lossPrice = openPrice +stop_loss[i]
        
        # 當持有部位時，檢查是否需要平倉
        if inventory == 1:
            if dynamic_stop:
                earnPrice = openPrice +stop_earn[i]
                lossPrice = openPrice +stop_loss[i]
            # 移動止損計算
            movesig = 0.0
            if moveloss != 1.0:
                if moveloss < 1.0:
                    if sig_values[i] > movePrice:
                        movePrice = sig_values[i]
                    movesig = sig_values[i] - movePrice * moveloss
                else:
                    if sig_values[i] < movePrice:
                        movePrice = sig_values[i]
                    movesig = movePrice * moveloss - sig_values[i]

            # 最大持倉天數檢查（timestamps 已轉換為天）
            cond = (
                ((current_time - opentime) >= max_hold)
                or ((not np.isnan(earnPrice)) and (sig_values[i] > earnPrice))
                or ((not np.isnan(lossPrice)) and (sig_values[i] < lossPrice))
                or (movesig < 0))
            if (cond and (sig[i] != 1.0)) or(sigout[i] == 1.0):                
                if close_shift:
                    entry = -1
                else:
                    trades.append((0, opentime, openPrice, current_time, close_vals[i], -1, np.nan, trade_qty))
                    inventory = 0
                    if (sig[i] == 1.0) and open_shift:
                        entry = 1

        # 處理最後一筆（當 i == n-1）
        if i == n - 1:
            if entry == 1:
                trades.append((1, -1, np.nan, -1, np.nan, current_time, sig_values[i], qty[i]))
            elif entry == -1:
                trades.append((2, opentime, openPrice, -1, np.nan, current_time, sig_values[i], trade_qty))
            elif inventory == 1:
                trades.append((3, opentime, openPrice, -1, np.nan, current_time, sig_values[i], trade_qty))
    return trades

# def _continuous_wins(data ,times ,amount):
#     df=data.copy()
#     df['rt']=df.apply(lambda row : 1 if (row['exit_price']>= row['entry_price'] and row['direction']=='long')
#              else (1 if (row['exit_price']<= row['entry_price'] and row['direction']=='short') else 0),axis=1)
#     df['group'] = (df['rt'] == 0).cumsum()
#     df['continuous_wins'] = df.groupby(['id', 'group'])['rt'].cumsum()
#     df['add'] = df.groupby('id')['continuous_wins'].shift(1) >= times
#     df.loc[df['add'] == True, 'qty'] = df['qty'] * amount
#     df=df.drop(columns=['rt', 'group','continuous_wins','add'])
#     return df

def _continuous_wins(data, times, amount):
    df = data.copy()
    
    # 1. 標記勝負：贏為 1, 輸為 -1
    # 這樣我們才能同時追蹤兩種狀態
    df['rt'] = df.apply(lambda row: 
        1 if (row['direction'] == 'long' and row['exit_price'] >= row['entry_price']) or 
             (row['direction'] == 'short' and row['exit_price'] <= row['entry_price']) 
        else -1, axis=1)

    # 2. 計算連續次數
    # 當 rt 變動時（從 1 變 -1 或反之），產生新的 block
    df['block'] = (df['rt'] != df['rt'].shift(1)).cumsum()
    
    # 在同一個 block 內累計次數
    # 如果 rt 是 1，streak 就會是 1, 2, 3...
    # 如果 rt 是 -1，streak 就會是 -1, -2, -3...
    df['streak'] = df.groupby(['id', 'block']).cumcount() + 1
    df['streak'] = df['streak'] * df['rt']

    # 3. 取得「前一筆」的連續紀錄
    # 因為我們要根據「已完成」的交易來決定「下一筆」的股數
    df['prev_streak'] = df.groupby('id')['streak'].shift(1)

    # 4. 根據 times 的正負號決定判定條件
    if times > 0:
        # 連勝邏輯：例如 times=2，則找 prev_streak >= 2
        mask = (df['prev_streak'] >= times)
    else:
        # 連敗邏輯：例如 times=-1，則找 prev_streak <= -1 (即連輸 1 次以上)
        mask = (df['prev_streak'] <= times)

    # 5. 套用股數變更
    df.loc[mask, 'qty'] = df['qty'] * amount

    # 移除輔助欄位
    df = df.drop(columns=['rt', 'block', 'streak', 'prev_streak'])
    return df

def _taxfee(df ,tax ,fee):
    trade=df.copy()
    def _tax(row):
        if row["id"][:2] == "00":
            return 0.001
        elif pd.isna(row["entry_time"]) or pd.isna(row["exit_time"]):
            if row["entry_time"].date() == row["exit_time"].date():
                return 0.0015
            else:
                return 0.003
        elif row["entry_time"].date() == row["exit_time"].date():
            return 0.0015
        else:
            return 0.003
    if tax == None:
        trade['tax']=trade.apply(_tax, axis=1)
    else:
        trade['tax']=tax
    trade['fee']=fee
    return trade

def _calculate(df):
    trade =df.copy()
    trade['cost'] = trade.apply(lambda row: row['exit_price']*row['tax'] + (row['entry_price']+row['exit_price'])*row['fee'] if row['direction']=='long'
                                        else row['entry_price']*row['tax'] + (row['entry_price']+row['exit_price'])*row['fee'], axis=1) *trade['qty']

    trade['entry_amount'] = trade.apply(lambda row:  row['entry_price']*(1+row['fee'])if row['direction']=='long'
                                                else row['entry_price']*(1+row['tax']+row['fee']), axis=1) *-trade['qty']
    
    trade['exit_amount'] = trade.apply(lambda row:  row['exit_price']*(1-row['fee']-row['tax'])if row['direction']=='long'
                                            else (row["entry_price"] * 2 - row["exit_price"] * (1 + row['fee'])), axis=1) *trade['qty']

    condtype = trade.type.isin(["準備平倉", "未平倉"])
    trade.loc[condtype, "predict"] =trade[condtype].apply(lambda row:   row["current_price"] * (1 - row["fee"] - row["tax"])* row["qty"]  if row["direction"] == "long"
                                                                else (row["entry_price"] * 2 - row["current_price"] * (1 + row["fee"]))* row["qty"] ,  axis=1,)    
    trade.loc[condtype, "cost"] = trade.apply(lambda row:   row['current_price']*row['tax'] + (row['entry_price']+row['current_price'])*row['fee'] if row['direction']=='long'
                                              else row['entry_price']*row['tax'] + (row['entry_price']+row['current_price'])*row['fee'], axis=1) *trade['qty']

    trade["pnl"] = trade["entry_amount"].fillna(0) + trade["exit_amount"].fillna(0) + trade["predict"].fillna(0)
    trade['returns'] = trade["pnl"] / (-1*trade['entry_amount'])
    return trade

#=======================================================================================

def make_trade(ls_sum ,tax ,fee ,continuous_wins ):
    trade = pd.DataFrame(ls_sum, columns=['id','direction','type','entry_time','entry_price','exit_time','exit_price','current_time','current_price','qty'])
    mapping = {0: '平倉', 1: '準備開倉', 2: '準備平倉', 3: '未平倉'}
    trade['type'] = trade['type'].map(mapping)
    for col in ['entry_time', 'exit_time', 'current_time']:
        trade[col] = trade[col].apply(lambda x: pd.NaT if x == -1 else pd.to_datetime(x, unit='m', origin='unix'))
    if continuous_wins is not None:
        trade=_continuous_wins(trade,times=continuous_wins[0],amount=continuous_wins[1])
    trade = trade[trade['qty']!=0]
    if trade.empty:
        return trade

    trade= _taxfee(trade ,tax ,fee)
    trade= _calculate(trade)
    for column in ['cost','entry_amount','exit_amount','predict','pnl']:
       trade[column] = round(trade[column]) 
    return trade

def make_posiiton( trade ,index_list ,daily_dic):

    df_wide = pd.DataFrame(daily_dic)
    df_wide = df_wide.sort_index()
    df_wide = df_wide.ffill()
    df_wide = df_wide[df_wide.index.isin(index_list)]

    T = trade[(trade.type != "準備開倉") & (trade.entry_time.dt.date != trade.exit_time.dt.date)].copy()
    if T.empty:
        columns = ['date', 'id', 'direction', 'qty', 'entry_date', 'entry_price',
                   'CLOSE', 'entry_cost', 'market_value', 'pnl']
        return pd.DataFrame(columns=columns)

    T['start'] = T.entry_time.dt.normalize()
    T['end'] = (T['exit_time'].fillna(index_list[-1] + pd.Timedelta('1d')) - pd.Timedelta('1d')).dt.normalize()

    T['date_range'] = T.apply(
    lambda row: pd.date_range(start=row.start, end=row.end , freq='D'),    axis=1)
    T_exploded = T.explode('date_range').rename(columns={'date_range': 'datetime'})
    df_melt = df_wide.reset_index().melt(id_vars='datetime', var_name='id', value_name='CLOSE')
    df_long = pd.merge(T_exploded, df_melt, how='left', on=['datetime', 'id'])
    df_long =df_long[df_long.datetime.isin(index_list)].reset_index(drop=True)

    df_long["market_value"] = np.where(
        df_long["direction"] == "long",
        df_long["CLOSE"] * (1 - df_long["tax"] - df_long["fee"]) * df_long["qty"],
        (df_long["entry_price"] * 2 - df_long["CLOSE"] * (1 + df_long["fee"])) * df_long["qty"]
    )
    df_long['pnl'] = df_long['entry_amount'] + df_long["market_value"]

    df_long.rename(columns={
        'datetime':'date',
        'start': 'entry_date',
        'entry_amount': 'entry_cost',
    }, inplace=True)

    position=df_long[['date','id','direction','qty','entry_date','entry_price','CLOSE','entry_cost','market_value','pnl']]
    for column in ['market_value','pnl']:
       position[column] = round(position[column])
    return position

def get_invest(trade_detail):
    import pandas as pd
    import numpy as np

    T = trade_detail.copy()

    # 建立 entry (+) 與 exit (−)
    df_entry = T[['entry_time', 'entry_amount']].rename(columns={'entry_time': 'time', 'entry_amount': 'value'})
    df_exit = T[T.exit_time.notnull()][['exit_time', 'entry_amount']].rename(columns={'exit_time': 'time', 'entry_amount': 'value'})
    df_exit['value'] = -df_exit['value']
    df_exit['time'] =df_exit['time'] + pd.Timedelta(seconds=1)

    df =pd.concat([df_entry, df_exit], ignore_index=True).sort_values('time')
    df['cumsum']=df['value'].cumsum()
    df['cummin'] = df['cumsum'].cummin()
    invest =(df.set_index('time')['cummin']*-1).resample('D').max().ffill().fillna(0)
    # 輸出 dict
    return invest.to_dict()

def _invest(trade_detail):
    T=trade_detail.copy()

    t_dic1={}
    t_dic2={}
    for sym in T.id.unique():
        t=T[T.id==sym]
        d1=t[['entry_time','entry_amount']]
        d1['type']=0
        d1=d1.rename(columns={'entry_time':'time'})
        
        d2=t[t.entry_time==t.exit_time][['exit_time','entry_amount']]
        d2['type']=1
        d2['entry_amount']=-d2['entry_amount']
        d2=d2.rename(columns={'exit_time':'time'})
        
        d3=t[t.entry_time<t.exit_time][['exit_time','entry_amount']]
        d3['type']=-1
        d3['entry_amount']=-d3['entry_amount']
        d3=d3.rename(columns={'exit_time':'time'})
        
        d = pd.concat([d1, d2, d3], axis=0).sort_values(by=['time', 'type'])
        d['cumsum']=d.entry_amount.cumsum()
        t_dic1[sym]=d.groupby('time')['cumsum'].last()
        t_dic2[sym]=d.groupby('time')['cumsum'].min()

    m1=pd.DataFrame(t_dic1).ffill().fillna(0)
    m2=pd.DataFrame(t_dic2).replace(0,np.nan)
    m=m2.fillna(m1)

    M1 =m.sum(axis=1).resample('D').last().ffill()
    M2 =m.sum(axis=1).resample('D').min()
    M = -M2.fillna(M1)
    return M.to_dict()

def make_daily(trade ,position ,index_list ,benchmark ):
    df =trade.copy()
    invest_map =_invest(df)

    df['entry_date'] = df['entry_time'].dt.normalize()
    df['exit_date']  = df['exit_time'].dt.normalize()

    # 2. 計算同一天交易數據（entry_date == exit_date）
    same_day = df[df['entry_date'] == df['exit_date']]

    # daytrade：同一天所有交易 entry_amount 之和（取絕對值）
    daytrade_series = same_day.groupby('entry_date')['entry_amount'].sum().abs()

    # daytradex：先按 [entry_date, id] 取每個 id 的最小 entry_amount，再按 entry_date 求和
    daytradex_series = same_day.groupby(['entry_date', 'id'])['entry_amount'].min().groupby('entry_date').sum()

    # profit_loss：按 exit_date 分組 pnl 求和
    profit_loss_series = df.groupby('exit_date')['pnl'].sum()

    # Cost：按 entry_date 分組 entry_amount 求和後取負值
    cost_series = -df.groupby('entry_date')['entry_amount'].sum()

    # 3. 處理 position 數據
    if not position.empty and position['date'].dropna().size > 0:
        pos_grouped = position.groupby('date').agg({'entry_cost': 'sum', 'market_value': 'sum'})
    else:
        pos_grouped = pd.DataFrame(columns=['entry_cost', 'market_value'])

    # 4. 建立 daily DataFrame，索引使用 index_list
    daily = pd.DataFrame(index=index_list)
    daily['index'] = daily.index

    # 將預先計算的結果合併到 daily 中
    daily['daytrade'] = daytrade_series
    daily['daytradex'] = daytradex_series
    daily['profit_loss'] = profit_loss_series
    daily['Cost'] = cost_series

    # 5. 合併 position 數據（根據日期）並補缺失值
    daily = daily.join(pos_grouped, how='left')
    daily=daily.fillna(0)

    daily['profit_loss'] = daily['market_value']+daily['entry_cost']+daily['profit_loss'].cumsum()
    daily = daily[daily.profit_loss!=0]
    daily['cashflow']=daily['profit_loss']-daily['market_value']
    daily['cashflow']= (daily.cashflow-daily.cashflow.shift(1).fillna(0))
    daily['unrealized'] = daily['entry_cost'] + daily['market_value']
    daily['realized'] = daily['profit_loss'] - daily['unrealized']
    cost_cumsum = daily.Cost.cumsum()
    daily['PNL/COST'] = daily['profit_loss']/cost_cumsum
    #插入前一日期
    newday = daily.index[0] - pd.Timedelta(days=1)
    new_index = pd.Index([newday]).append(daily.index) 
    daily = daily.reindex(new_index)

    daily['invest'] =daily.index.map(invest_map)#.fillna(0)
    daily.loc[newday, ['entry_cost', 'profit_loss','invest']] = 0
    daily['invest'] = abs(np.maximum.accumulate(daily['invest']))
    daily['invest'] = daily['invest'].ffill()
    # daily['invest'] =daily['invest'].where(daily['invest'] != 0, daily['daytrade'])
    # daily['invest'] = daily['invest'].cummax()

    daily['return_cumsum'] =  daily.profit_loss /daily['invest'].replace(0, np.nan)
    daily.loc[newday, ['return_cumsum']] = 0
    daily['return'] = daily.return_cumsum -daily.return_cumsum.shift(1)
    daily['return_pct'] = (daily['return_cumsum']+1)/(daily['return_cumsum'].shift(1)+1) -1
    # 年化波動:daily.return_pct.std(ddof=1) *np.sqrt(252)
    # CAGR: daily['return']/sum()**(252/(daily.shape[0]-1))
    daily = daily[['daytrade','cashflow','entry_cost','market_value','profit_loss','unrealized','realized','invest','PNL/COST','return','return_pct']]

    if benchmark is not None:
        benchmark= benchmark.groupby(benchmark.index.date).last()
        daily['benchmark']=daily.index.map(benchmark.to_dict()) 
        daily['benchmark']= daily['benchmark'].ffill()
        fenmu = daily.benchmark.loc[daily.benchmark.first_valid_index()]     
        daily['benchmark_return']= daily.benchmark.diff()/fenmu
        daily['benchmark_return_pct']=daily['benchmark'].pct_change(fill_method=None)
    return daily

def make_report(trade ,daily ,reinvest=False):
    df=trade.copy()
    daily=daily.copy()

    closed_trades = df[df['type'] == '平倉']
    if closed_trades.shape[0] !=0:
        win_rate = round((closed_trades['pnl'] > 0).mean(), 2)
    else:
        win_rate = ""

    # 計算盈虧比
    filtered_trades = df[df['type'].isin(['平倉', '準備平倉', '未平倉'])]
    if (filtered_trades['pnl'] < 0).any():
        profit_sum = filtered_trades.loc[filtered_trades['pnl'] > 0, 'pnl'].sum()
        loss_sum = -filtered_trades.loc[filtered_trades['pnl'] < 0, 'pnl'].sum()
        pnl_ratio = round(profit_sum / loss_sum, 2)
    else:
        pnl_ratio = ""

    AnnualisedVolatility = daily.return_pct.std(ddof=1) *np.sqrt(252)
    if 1+daily['return'].sum() >=0:
        cagr = (1+daily['return'].sum())**(252/(daily.shape[0]-1)) -1
    else:
        cagr = daily['return'].sum()/(daily.shape[0]-1)*252

    cash = round(daily['invest'].iloc[-1] )
    if reinvest:
        _sharp = sharpe(daily["return_pct"])
        _sortino = sortino(daily["return_pct"])
    else:
        _sharp = sharpe(daily["return"])
        _sortino = sortino(daily["return"])

    trade_times = df[~(df.type=='準備開倉')].shape[0]
    total_pnl = round(daily["profit_loss"].iloc[-1])
    
    #獲利交易次數
    profit_times =df[(df.type=='平倉') & (df.pnl>=0)].shape[0]
    #虧損交易次數
    loss_times =df[(df.type=='平倉') & (df.pnl<0)].shape[0]

    # 最近5根
    recent_w_df = daily.tail(5)
    # 最近21根
    recent_m_df = daily.tail(20)
    # 最近63根
    recent_q_df = daily.tail(60)
    # 最近252根
    recent_y_df = daily.tail(240)

    w_return =recent_w_df['return'].sum()
    m_return =recent_m_df['return'].sum()
    q_return  =recent_q_df['return'].sum()
    y_return =recent_y_df['return'].sum()

    output = {
        "StartDate": [df.entry_time.min().date()],
        "EndDate": [daily.index[-1].date()],
        "LastSignalDate":[datetime.date.today()],
        "TotalReturns": [daily['return'].sum()],
        "CAGR": [cagr],
        "MaxDrawdown": [max_drawdown(daily["return_pct"])],
        "MddDuration": [get_longest_dd(daily["return_pct"]).days.max()],
        "SharpeRatio": [_sharp],
        "SortinoRatio": [_sortino],
        "AnnualisedVolatility":[AnnualisedVolatility],
        "WinRate": [win_rate],
        "ProfitLossRatio": [pnl_ratio],
        "Cash": [cash],
        "AvgCashUtilizationRate": [daily["entry_cost"].abs().mean() / daily["entry_cost"].abs().max()],
        "TotalTrades": [trade_times],
        "ProfitTimes": [profit_times],
        "LossTimes": [loss_times],
        "NetProfit": [total_pnl],
        "TradeCost": [round(df.cost.sum())],
        "AvgProfitPerTrade" :[round(total_pnl/trade_times) ],
        "WeeklyReturn": ["{:.2%}".format(w_return)],
        "MonthlyReturn": ["{:.2%}".format(m_return)],
        "QuarterlyReturn": ["{:.2%}".format(q_return)],
        "AnnualReturn": ["{:.2%}".format(y_return)], }
    
    if 'benchmark' in daily.columns:
        benchmark_w_return = recent_w_df["benchmark_return_pct"].add(1).prod() - 1
        benchmark_m_return = recent_m_df["benchmark_return_pct"].add(1).prod() - 1
        benchmark_q_return = recent_q_df["benchmark_return_pct"].add(1).prod() - 1
        benchmark_y_return = recent_y_df["benchmark_return_pct"].add(1).prod() - 1
        output["RelativeWeeklyReturn"] = ["{:.2%}".format(w_return - benchmark_w_return)]
        output["RelativeMonthlyReturn"] = ["{:.2%}".format(m_return - benchmark_m_return)]
        output["RelativeQuarterlyReturn"] = ["{:.2%}".format(q_return - benchmark_q_return)]
        output["RelativeAnnualReturn"] = ["{:.2%}".format(y_return - benchmark_y_return)]

    output=pd.DataFrame(output).T
    output.columns = ["Strategy"]
    return output


#=======================================================================================


class _rpX():
    def __init__(self, trade):
        self.trade=trade

    def save(self, save_path='.')->None:
        os.makedirs(save_path , exist_ok=True )
        time = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')
        json_path = os.path.join(save_path, 'end.json')

        if self.trade.shape[0] == 0:
            with open(json_path, 'w') as json_file:
                json.dump({"code": "0", "message": "no trade","createTime":time}, json_file, indent=4)

        elif (self.trade.type.unique() == '準備開倉').all():
            with open(json_path, 'w') as json_file:
                json.dump({"code": "0", "message": "only open single","createTime":time}, json_file, indent=4)

        elif self.trade.shape[0] > 10000:
            with open(json_path, 'w') as json_file:
                json.dump({"code": "0", "message": "trade over ","createTime":time}, json_file, indent=4)


class _rp():
    def __init__(self, trade, position, daily, report ,dic):
        self.trade=trade
        self.position=position
        self.daily = daily.rename_axis('date').reset_index()
        self.report=report
        self._dic = dic

    @property
    def _get_table(self):
        daily= self.daily.set_index('date')

        max_5_dd = get_longest_dd(daily["return_pct"])
        drawdown= to_drawdown_series(daily["return_pct"]).fillna(0).reset_index()
        drawdown.columns=['index','drawdown']

        sum_Y = pd.DataFrame(group_returns(daily['return'], daily['return'].index.strftime("%Y")))
        sum_Y= sum_Y.reset_index()
        sum_Y.columns=['Year','Returns']

        try:
            sum_M = pd.DataFrame(daily['return'].resample('ME').sum())
        except ValueError:
            sum_M = pd.DataFrame(daily['return'].resample('M').sum())
        sum_M.columns = ['Returns']
        sum_M['Month'] = sum_M.index.strftime('%b').str.upper()
        sum_M['Year'] = sum_M.index.year

        return max_5_dd ,drawdown ,sum_Y ,sum_M
    
    def save(self, save_path='.', fig=False)->None:
        """Save the graphs and dataframes to local storage
            Example:
            rp = api.backtest()
            rp.save()
        """
        os.makedirs(save_path , exist_ok=True )

        trade =self.trade.copy()
        mapping = {
        '準備進場':'準備開倉',
        '準備出場':'準備平倉',
        '已出場':'平倉',
        '持有':'未平倉',
        }
        trade['type'] = trade['type'].replace(mapping)
        trade.to_json( os.path.join(save_path, "trade_detail.json"), orient="records"  )
        
        self.daily.to_json( os.path.join(save_path, "daily_report.json"), orient="records" )
        #self.position.to_json( os.path.join(save_path, "position.json"), orient="records" )
        self.report.to_json(os.path.join(save_path, "report_summary.json"), orient="columns"  )

        max_5_dd ,drawdown ,sum_Y ,sum_M = self._get_table

        max_5_dd.to_json(os.path.join(save_path, "max_5_dd.json"), orient="records"  )
        drawdown.to_json(os.path.join(save_path, "drawdown.json"), orient="records"  )
        sum_Y.to_json(os.path.join(save_path, "sum_Y.json"), orient="records" )
        sum_M.to_json(os.path.join(save_path, "sum_M.json"), orient="records" )

        time = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')
        json_path = os.path.join(save_path, 'end.json')
        with open(json_path, 'w') as json_file:
                json.dump({"code": "1", "message": "success","createTime":time}, json_file, indent=4)
        print('json successfully saved.')
        fig_path = save_path

        try:
            if fig:
                os.makedirs(fig_path, exist_ok=True)
                daily = self.daily.copy()
                daily = daily.set_index('date')        
                
                #熱量圖
                #print('write heatmap.png...')
                heatmap_returns(daily["return"], saveFig=os.path.join(fig_path,"heatmap.png") if fig_path is not None else None, show=False)
                        
                #回撤圖
                #print('write underwater.png...')
                drawdown_plotly(daily["return_pct"], saveFig=os.path.join(fig_path,"underwater.png") if fig_path is not None else None, show=False)
                        
                #最大5個回撤圖
                #print('write 5dd.png...')
                if 'benchmark' in daily.columns:
                    drawdowns_periods_plotly(daily["return_pct"], benchmark=daily["benchmark_return_pct"], title="回測", saveFig=os.path.join(fig_path,"5dd.png") if fig_path is not None else None, show=False)
                else:
                    drawdowns_periods_plotly(daily["return_pct"], title="回測", saveFig=os.path.join(fig_path,"5dd.png") if fig_path is not None else None, show=False)
            
                #損益圖
                #print('write netpnl.png...')
                net_pnl_plotly(daily["profit_loss"], saveFig=os.path.join(fig_path,"netpnl.png") if fig_path is not None else None, show=False)
                            
                #平均營利百分比圖
                #print('write pnl_cost.png...')
                pnlcost_plt_plotly(daily['PNL/COST'], saveFig=os.path.join(fig_path,"pnl_cost.png") if fig_path is not None else None, show=False)
            
                print('plotly successfully saved.')
        except Exception as e:
            logging.error("plotly write: %s", e)
            pass

    @property
    def plotly(self)->None:
        daily = self.daily.copy()
        daily = daily.set_index('date')        
                
        #平均營利百分比圖
        pnlcost_plt_plotly(daily['PNL/COST'])
                
        #熱量圖
        heatmap_returns(daily["return"])
                
        #回撤圖
        drawdown_plotly(daily["return_pct"])
                
        #最大5個回撤圖
        if 'benchmark' in daily.columns:
            drawdowns_periods_plotly(daily["return_pct"], benchmark=daily["benchmark_return_pct"], title="回測")
        else:
            drawdowns_periods_plotly(daily["return_pct"], title="回測")
          
        #損益圖
        net_pnl_plotly(daily["profit_loss"])

    def dashboard(self ,path=None,view ='notebook',page_size=20):
        """
        Generate a dashboard output.

        行為規則：
            僅在「Notebook 環境」且 path=None 且 view='notebook' 時，不會保存文件。
            其餘所有情況均會保存文件。

        參數：
            path : str or None, default None
                - None : 使用預設名稱儲存（除非在 notebook + view='notebook'）
                - 字串 : 自定義輸出文件路徑
            view : {'notebook','web',None}, default 'notebook'
                - notebook : Notebook 預覽
                - web      : Web 預覽
                - None     : 不顯示
            page_size : int
                預設每頁 20 筆

        行為表：

            Notebook & path=None & view='notebook'  → 不保存（唯一不保存條件）
            path=None & view='web'                  → 保存（預設檔名）
            path=None & view=None                   → 保存（預設檔名）
            path=str  & view='notebook'             → 保存（自定義檔名）
            path=str  & view='web'                  → 保存（自定義檔名）
            path=str  & view=None                   → 保存（自定義檔名）

        備註：
            儲存文件格式可能依內部實作而異。
        """

        html_content ={}
        daily_reindexed = self.daily.copy()
        daily_reindexed = daily_reindexed.set_index("date")
        heatmap_fig = heatmap_returns(daily_reindexed["return"], show=False)
        if "benchmark" in daily_reindexed.columns:
            drawdown_fig = drawdowns_periods_plotly(
                daily_reindexed["return_pct"],
                benchmark=daily_reindexed["benchmark_return_pct"],
                title="回測",
                show=False,
            )
        else:
            drawdown_fig = drawdowns_periods_plotly(
                daily_reindexed["return_pct"], title="回測", show=False
            )
        underwater_fig = drawdown_plotly(daily_reindexed["return_pct"], show=False)
        pnl_fig = net_pnl_plotly(daily_reindexed["profit_loss"], show=False)
        pnlcost_fig = pnlcost_plt_plotly(self.daily['PNL/COST'], show=False)


        metrics, return_rates, additional_metrics, remainder_df = process_for_dashboard(
            self.report
        )
        metrics_html = generate_metrics_html(metrics)
        html_content["_BASE_METRICS_HTML_"] = metrics_html
        html_content['_RETURN_METRICS_HTML_'] = generate_return_metrics_html(return_rates)
        html_content['_ADDITIONAL_METRICS_HTML_'] = generate_additional_metrics_html(additional_metrics)
        html_content['_REMAINDER_TABLE_HTML_'] = generate_remainder_table_html(remainder_df)


        cash_utilization = self.report.loc["AvgCashUtilizationRate", "Strategy"]
        html_content["_PIE_CHART_HTML_"] = create_pie_chart_html(cash_utilization)

        html_content["_HEATMAP_HTML_"] = generate_heatmap_html(heatmap_fig)

        html_content["_DRAWDOWN_GRAPH_HTML_"] = generate_single_figure_html(drawdown_fig, "drawdown-graph-container")
        html_content["_UNDERWATER_GRAPH_HTML_"] = generate_single_figure_html(underwater_fig, "underwater-graph-container")
        html_content["_PNL_GRAPH_HTML_"] = generate_single_figure_html(pnl_fig, "pnl-graph-container")
        html_content["_PNL_COST_GRAPH_HTML_"] = generate_single_figure_html(pnlcost_fig, "pnl-cost-graph-container")


        # tables
        # Max 5 Drawdowns section (complete with title + export button)
        drawdown_df = get_longest_dd(daily_reindexed["return_pct"],period=5)
        drawdown_df = _process_df(drawdown_df, table_id="longest_drawdown_table")
        html_content["_MAX5_DRAWDOWN_SECTION_"] = generate_dd_html(drawdown_df)

        # Trade table section (complete with title + export button + pagination)
        T =self.trade
        is_day_row =T['entry_time'].dt.time == pd.Timestamp('00:00:00').time()
        group_is_all_day = is_day_row.groupby(T['id']).transform('all')
        for c in ['entry_time', 'current_time', 'exit_time']:
            t_day = T[c].dt.strftime('%Y-%m-%d')
            t_sec = T[c].dt.strftime('%Y-%m-%d %H:%M:%S')
            T[c] = np.where(group_is_all_day, t_day, t_sec)

        trade_df = _process_df(T, table_id="trade_table")

        html_content["_TRADE_TABLE_SECTION_"] = generate_trade_table_html(trade_df, page_size=page_size)

        # Daily table section (complete with title + export + date filters + pagination)
        daily_df = _process_df(self.daily, table_id="daily_table")
        for col in ['pnl/累計交易金額','日報酬','日變動比率',
                    # '對標價格日報酬','對標價格日變動比率'
                    ]:
            daily_df[col] =daily_df[col].round(5)

        position_df = _process_df(self.position, table_id="position_table")
        date_col = position_df.columns[0]          # or "日期" explicitly
        start_default = pd.to_datetime(position_df[date_col].min()).strftime("%Y-%m-%d")
        end_default   = pd.to_datetime(position_df[date_col].max()).strftime("%Y-%m-%d")
        
        html_content["_DAILY_TABLE_SECTION_"] = generate_daily_table_section_html(
            df=daily_df,
            page_size=page_size,
            start_date=start_default,
            end_date=end_default
        )

        # Position table section (complete with title + export + stock/date filters + pagination)
        html_content["_POSITION_TABLE_SECTION_"] = generate_position_table_section_html(
            df=position_df,
            page_size=page_size,
            start_date=start_default,
            end_date=end_default
        )

        dashboard(
            html_content,
            path=path,
            scrollable=True,    
            frame_height=900,
            max_width=None,
            view=view
        )
        
            
