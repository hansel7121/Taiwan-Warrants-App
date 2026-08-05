import pandas as pd
import numpy as np
from ._BT_helper import _rp ,_rpX ,sim_numba ,make_trade ,make_posiiton ,make_daily ,make_report

def backtest(data ,symbol=None ,date=None ,qty=None, 
            sig_value=None ,open_value=None ,open_shift=True ,close_value=None ,close_shift=True,
            longsig=None ,longout=None ,shortsig=None ,shortout=None,
            tax=None ,fee=0.00025 ,slippage=0,
            earn=None ,loss=None , 
            moveloss=0, max_hold=None ,continuous_wins=None , force_exit=[],
            benchmark=None, messages =True, _maxtrades =10000 ):
    
    dynamic_stop=False

    if isinstance(data, dict):
        dic= data.copy()
    else:
        if symbol is None:
            raise ValueError('請設定symbol欄位')
        else:
            dic = {id_value: group.drop(columns=[symbol]).reset_index(drop=True) for id_value, group in data.groupby(symbol)}
    if date is None:
        raise ValueError('請設定date欄位')
    if qty is None:
        raise ValueError('請設定qty欄位')
    
    if sig_value is None:
        raise ValueError('請設定sig_value欄位')
    if open_value is None:
        open_value = sig_value
    if close_value is None:
        close_value = sig_value

    if longsig is None and shortsig is None:
        raise ValueError('請設定longsig或shortsig欄位')
    
    if max_hold is None:
        max_hold_val = 10**9   # 或者設為 10**9 表示無限制
    else:
        max_hold_val = max_hold * 1440  # 轉換成分鐘數

    if messages: print('資料預處理:')
    dicX ={}
    index_list=[]
    ls_sum=[]
    daily_dic = {}
    close_map ={}
    for stockid in dic.keys():
        df=dic[stockid]
        index_list.extend(df[date].dt.normalize().unique().tolist())

        dfX =pd.DataFrame()
        has_trade = False
        if longsig is not None:
            if df[longsig].any()==True:
                has_trade = True
                dfX['longsig'] =df[longsig]
            if longout is not None:
                dfX['longout'] = (df[longsig] == False) | (df[longout] == True)
            else:
                dfX['longout'] = (df[longsig] == False) 

        if shortsig is not None:
            if df[shortsig].any()==True:
                dfX['shortsig'] = df[shortsig]
                has_trade = True
            if shortout is not None:
                dfX['shortout'] = (df[shortsig] == False) | (df[shortout] == True)
            else:
                dfX['shortout'] = (df[shortsig] == False) 

        if not has_trade:
            continue

        # 利用一次性建立 DataFrame 的方式來複製其他欄位
        assign_dict = {
            'datetime': df[date],
            'qty': df[qty],
            'sig_value': df[sig_value],
            'open_value': df[open_value],
            'close_value': df[close_value]
        }
        if isinstance(earn, str):
            assign_dict['earn'] = df[earn]
        if isinstance(loss, str):
            assign_dict['loss'] = df[loss]
        
        dfX = dfX.assign(**assign_dict)
        dicX[stockid] = dfX
        close_map[stockid] = dfX.set_index('datetime')['sig_value']
        daily_dic[stockid] = dfX.set_index('datetime')['sig_value'].resample('D').last()

        if 'longsig' in dfX.columns:
            timestamps = dfX['datetime'].to_numpy().astype('datetime64[m]').astype(np.int64)
            open_vals  = (dfX['open_value']*(1+slippage)).to_numpy(np.float64)
            close_vals = (dfX['close_value']*(1-slippage)).to_numpy(np.float64)
            # 將信號欄位轉成 np.float64，True 會變成 1.0，False 變成 0.0，nan 保持 nan
            sig        = dfX['longsig'].to_numpy(np.float64)
            sigout     = dfX['longout'].to_numpy(np.float64)
            sig_values = dfX['sig_value'].to_numpy(np.float64)
            if 'earn' in dfX.columns:
                stop_earn = (dfX['earn']).to_numpy(np.float64)
            elif isinstance(earn, (int, float)):
                stop_earn = (dfX['open_value']*(0.01*earn)).to_numpy(np.float64)
            else:
                stop_earn = np.full(len(dfX), np.nan, dtype=np.float64)
            
            if 'loss' in dfX.columns:
                stop_loss = (- dfX['loss']).to_numpy(np.float64)
            elif isinstance(loss, (int, float)):
                stop_loss = (dfX['open_value']*(-0.01*loss)).to_numpy(np.float64)
            else:
                stop_loss = np.full(len(dfX), np.nan, dtype=np.float64)
            
            qty_arr = dfX['qty'].to_numpy(np.float64)
            move_loss = 1-0.01*moveloss
            ls = sim_numba(timestamps, open_vals, close_vals, sig, sigout ,sig_values, stop_earn, stop_loss, qty_arr,
                move_loss, max_hold_val, open_shift, close_shift ,dynamic_stop)
            
            for trade in ls:
                ls_sum.append([stockid, 'long'] + list(trade))

        if 'shortsig' in dfX.columns:
            timestamps = dfX['datetime'].to_numpy().astype('datetime64[m]').astype(np.int64)
            # 短倉開倉賣出價格，滑點使價格下降
            open_vals  = (dfX['open_value']*(1-slippage)).to_numpy(np.float64)
            # 短倉平倉買回價格，滑點使價格上升
            close_vals = (dfX['close_value']*(1+slippage)).to_numpy(np.float64)
            # 將信號欄位轉成 np.float64，True 會變成 1.0，False 變成 0.0，nan 保持 nan
            sig        = dfX['shortsig'].to_numpy(np.float64)
            sigout     = dfX['shortout'].to_numpy(np.float64)
            sig_values = dfX['sig_value'].to_numpy(np.float64)
            
            # 計算止盈：短倉獲利時價格下跌，所以止盈低於開倉價格
            if 'earn' in dfX.columns:
                stop_earn = (- dfX['earn']).to_numpy(np.float64)
            elif isinstance(earn, (int, float)):
                stop_earn = (dfX['open_value']*(-0.01*earn)).to_numpy(np.float64)
            else:
                stop_earn = np.full(len(dfX), np.nan, dtype=np.float64)
            
            # 計算止損：短倉止損時價格上升，所以止損高於開倉價格
            if 'loss' in dfX.columns:
                stop_loss = (dfX['loss']).to_numpy(np.float64)
            elif isinstance(loss, (int, float)):
                stop_loss = (dfX['open_value']*(0.01*loss)).to_numpy(np.float64)
            else:
                stop_loss = np.full(len(dfX), np.nan, dtype=np.float64)
            
            qty_arr = dfX['qty'].to_numpy(np.float64)
            move_loss = 1+0.01*moveloss
            ls_short = sim_numba(timestamps, open_vals, close_vals, sig ,sigout , sig_values, stop_loss, stop_earn, qty_arr,
                    move_loss, max_hold_val, open_shift, close_shift ,dynamic_stop)
            
            for trade in ls_short:
                ls_sum.append([stockid, 'short'] + list(trade))

        if len(ls_sum)>_maxtrades:
            print(f'交易超過{_maxtrades}次')
            trade = pd.DataFrame(ls_sum, columns=['id','direction','type','entry_time','entry_price','exit_time','exit_price','current_time','current_price','qty'])
            rp = _rpX(trade)
            return rp

    if messages: print('產出:交易明細')
    trade = make_trade(ls_sum ,tax ,fee ,continuous_wins )
    if trade.shape[0] ==0:
        print('交易次數為0，請從新設定信號')
        rp = _rpX(trade)
        return rp

    elif (trade.type.unique() == '準備開倉').all():
        print('只有準備開倉信號，僅返回交易明細')
        rp = _rpX(trade)
        return rp

    mask = trade['id'].isin(force_exit) & (trade['type'] == '未平倉')
    trade.loc[mask, 'exit_time'] = trade.loc[mask, 'current_time']
    trade.loc[mask, 'exit_price'] = trade.loc[mask, 'current_price']
    trade.loc[mask, 'exit_amount'] = trade.loc[mask, 'predict']
    trade.loc[mask, 'type'] = '平倉'
    trade.loc[mask, 'current_time'] =np.nan
    trade.loc[mask, 'current_price']=np.nan
    trade.loc[mask, 'predict']=np.nan

    if messages: print('產出:每日持倉')
    index_list = sorted(set(index_list))
    position = make_posiiton( trade ,index_list ,daily_dic)
    if messages: print('產出:每日賬戶損益')
    daily = make_daily(trade ,position ,index_list ,benchmark)
    if messages: print('產出:策略績效報告')
    report= make_report(trade ,daily)

    rp = _rp(trade, position, daily, report ,dicX)
    mapping = {
    '準備開倉': '準備進場',
    '準備平倉': '準備出場',
    '平倉': '已出場',
    '未平倉': '持有'
    }
    rp.trade['type'] = rp.trade['type'].replace(mapping)
    return  rp 
            