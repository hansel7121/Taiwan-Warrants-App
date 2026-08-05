import re

_VALID_MINUTES = {1, 3, 5, 15, 30, 60}
_VALID_N       = {1, 2, 3, 4, 5, 10, 20, 60 ,80 ,100 ,120 ,180 ,240}
_VALID_MARKET  = {'F', 'P', 'M', 'A'}

def _is_date8(v) -> bool:
    return isinstance(v, str) and bool(re.match(r'^\d{8}$', v))

def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

# Rule lookup — derived from CONFIG.TABLE_MAP and CONFIG.USTABLE_MAP
# test_map 項目不包含
_RULES: dict = {

    # ── no args ──────────────────────────────────────────────────────────────
    '台股產業對照表':                                            'no_args',
    '全額交割股':                                                'no_args',
    '處置股票':                                                  'no_args',
    '分盤交易':                                                  'no_args',
    '禁現沖標的':                                                'no_args',
    '今日暫停交易':                                              'no_args',
    '注意股票':                                                  'no_args',
    '上市櫃/ETF清單(含當沖限制)':                                'no_args',
    'ROE/ROA-多檔股票最新時間':                                  'no_args',
    '股市行事曆-多檔股票最新時間':                               'no_args',
    'EPS/本益比/殖利率-多檔股票最新時間':                        'no_args',
    '日K價量資料(個股、ETF)-多檔股票最新時間':                  'no_args',
    '籌碼買賣資料(個股、ETF)-多檔股票最新時間':           'no_args',
    '三大法人買賣超-多檔股票最新時間':                          'no_args',
    '漲跌家數(盤中)':                                           'no_args',
    '漲跌列表(盤中)':                                           'no_args',
    '漲跌家數分鐘歷史(盤中)':                                   'no_args',
    '粗產業多(盤中)':                                           'no_args',
    '粗產業空(盤中)':                                           'no_args',
    '即時周轉率排行':                                            'no_args',
    '收盤20日周轉率':                                            'no_args',
    '券資比-多檔股票最新時間':                                   'no_args',
    '前15大主力券商買賣超佔比-多檔股票最新時間':                'no_args',
    '即將下市及過去五年下市股票-多檔股票最新時間':              'no_args',
    '大盤-法人買賣資料-多檔股票最新時間':                       'no_args',
    # US
    '美股產業對照表':                                           'no_args',
    '日K價量資料(個股、ETF)-多檔股票最新時間':                  'no_args',
    '取得美股盤後收盤價-多檔股票最新時間':                      'no_args',
    '取得當前所有產業資金流':                                    'no_args',

    # ── date (yyyyMMdd) ───────────────────────────────────────────────────────
    '成交金額及股本-多檔股票指定時間':                          'date',

    # ── symbol (1 str) ────────────────────────────────────────────────────────
    '日K/技術指標/價量資料-單檔股票多個區間':                    'symbol',
    '週K/技術指標/價量/籌碼資料-單檔股票多個區間':              'symbol',
    '月K/技術指標/價量/籌碼資料-單檔股票多個區間':              'symbol',
    '還原日K價量資料(個股、ETF、大盤)-單檔股票多個區間':        'symbol',
    '還原週K價量資料(個股、ETF、大盤)-單檔股票多個區間':        'symbol',
    '還原月K價量資料(個股、ETF、大盤)-單檔股票多個區間':        'symbol',
    '季毛利率/季營益率/ROE-單檔股票多個區間':                    'symbol',
    '公司基本資料':                                              'symbol',
    '季稅後淨利/季稅後EPS-單檔股票多個區間':                     'symbol',
    '個股季財報資訊-單檔股票多個區間':                           'symbol',
    '個股股利資訊(年)-單檔股票多個區間':                         'symbol',
    '個股籌碼分佈-單檔股票多個區間':                             'symbol',
    '個股資本支出-單檔股票多個區間':                             'symbol',
    '個股營收資訊-單檔股票多個區間':                             'symbol',
    '籌碼資料(個股、ETF、大盤)-單檔股票多個區間':               'symbol',
    'ETF持股清單':                                              'symbol',
    '個股股利資訊(季、含填息天數)-單檔股票多個區間':            'symbol',
    '大/小股東持股變化-單檔股票多個區間':                        'symbol',
    '市值/淨利/淨值(個股、ETF、大盤)-單檔股票多個區間':         'symbol',
    '查詢八大行庫買賣超-單檔股票多個區間':                       'symbol',
    '股市行事曆-單檔股票多個區間':                              'symbol',
    '近四季財報加總-單檔股票多個區間':                           'symbol',
    '個股資餘資券相抵-單檔股票多個區間':                         'symbol',
    '籌碼集中渙散/主力買賣超-單檔股票多個區間':                 'symbol',
    '個股券商買賣超Top10-單檔股票多個區間':                      'symbol',
    '月營收創歷史新高新低':                                      'symbol',
    '股價報酬(日)Beta值-單檔股票多個區間':                       'symbol',
    '取得即時分K-含興櫃':                                        'symbol',
    '個股上個交易日成交均張(成交量/成交筆數)':                   'symbol',
    '取得個股盤中內外盤-含興櫃':                                'symbol',
    '取得今日即時成交明細(含五檔報價)':                          'symbol',
    '取得今日即時成交明細(貼標大戶/散戶)':                       'symbol',
    '取得零股成交明細':                                          'symbol',
    '取得最新一筆成交明細(含五檔報價)':                          'symbol',
    '查詢近100日買賣張數加總_高至低排序':                        'symbol',
    '分價量':                                                    'symbol',  # TW & US 同名
    # US
    '日K價量資料(個股、ETF)-單檔股票多個區間':                   'symbol',
    '週K價量資料(個股、ETF)-單檔股票多個區間':                   'symbol',
    '月K價量資料(個股、ETF)-單檔股票多個區間':                   'symbol',
    '取得最新一筆成交明細':                                      'symbol',
    '取得個股內外盤-單檔股票多個區間':                           'symbol',

    # ── symbol + minutes ──────────────────────────────────────────────────────
    '取得即時分K(最後交易日)':                                    'symbol_minutes',
    '取得歷史分K(含加權/櫃買指數)':                              'symbol_minutes',
    '取得歷史1/3/5/15/30/60分K':                                 'symbol_minutes',   # US

    # ── symbol + date ─────────────────────────────────────────────────────────
    '取得歷史3分K(指定當日)':                                     'symbol_date',
    '取得歷史5分K(指定當日)':                                     'symbol_date',

    # ── symbol + date + minutes ───────────────────────────────────────────────
    '取得歷史分K(指定日期前)':                                    'symbol_date_minutes',

    # ── symbol + date + days ──────────────────────────────────────────────────
    '取得指定日期前幾天的分價量歷史資料':                         'symbol_date_days',

    # ── symbol + endDate ──────────────────────────────────────────────────────
    '取得歷史1分K(指定日期前5天)':                               'symbol_enddate',

    # ── symbol + startDate + endDate ──────────────────────────────────────────
    '取得歷史1分K(指定起訖日)':                                   'symbol_start_end',
    '查詢TOP15主力分點買賣超加總資料(指定起訖日)':                'symbol_start_end',

    # ── list of symbols ───────────────────────────────────────────────────────
    '批次取得個股盤中行情':                                       'list',
    '批次取得個股盤中行情-含興櫃(tick含試搓)':                   'list',
    '批次取得個股盤中內外盤-含興櫃與權證':                       'list',
    '批次取得盤中預估量':                                         'list',
    '批次取得個股盤中多筆分K':                                    'list',   # US
    '批次取得個股盤中最新行情':                                   'list',   # US
    '批次取得個股盤中內外盤':                                     'list',   # US

    # ── brokerId ──────────────────────────────────────────────────────────────
    '查詢分點最新TOP10買賣超資料':                                'brokerid',

    # ── symbol + brokerId ─────────────────────────────────────────────────────
    '查詢近100日該分點資料':                                      'symbol_brokerid',

    # ── brokerId + N ──────────────────────────────────────────────────────────
    '查詢分點近N日Top15買賣超資料':                               'brokerid_n',

    # ── sector ────────────────────────────────────────────────────────────────
    '產業個股清單':                                               'sector',

    # ── 大盤法人：任意 1 個 str 代號 ─────────────────────────────────────────
    '大盤-法人買賣資料-單檔股票多個區間':                         'arg1_str',

    # ── US: symbol + market ───────────────────────────────────────────────────
    '取得全天含盤前盤後歷史分k(最後交易日)':                     'symbol_market',

    # ── 多股多期：tableId + 至少 1 欄位 ──────────────────────────────────────
    '多股多期':                                                   'multi_period',
}

_mins = sorted(_VALID_MINUTES)
_ns   = sorted(_VALID_N)

def _usage(table: str, rule: str, prefix: str = 'Data') -> str:
    t = table
    p = prefix
    sym = 'AAPL' if p == 'USData' else '2330'
    sym2 = 'TSM'  if p == 'USData' else '2317'
    if rule == 'no_args':
        return f"{p}.get('{t}')"
    elif rule == 'date':
        return f"{p}.get('{t}', 'yyyyMMdd')"
    elif rule == 'symbol':
        return f"{p}.get('{t}', '{sym}')"
    elif rule == 'symbol_minutes':
        return f"{p}.get('{t}', '{sym}', 1)  # minutes 可選: {_mins}"
    elif rule == 'symbol_date':
        return f"{p}.get('{t}', '{sym}', 'yyyyMMdd')"
    elif rule == 'symbol_date_minutes':
        return f"{p}.get('{t}', '{sym}', 'yyyyMMdd', 1)  # minutes 可選: {_mins}"
    elif rule == 'symbol_date_days':
        return f"{p}.get('{t}', '{sym}', 'yyyyMMdd', 5)"
    elif rule == 'symbol_enddate':
        return f"{p}.get('{t}', '{sym}', 'yyyyMMdd')"
    elif rule == 'symbol_start_end':
        return f"{p}.get('{t}', '{sym}', 'yyyyMMdd', 'yyyyMMdd')"
    elif rule == 'list':
        return f"{p}.get('{t}', ['{sym}', '{sym2}', ...])"
    elif rule == 'brokerid':
        return f"{p}.get('{t}', '9268 凱基台北')"
    elif rule == 'symbol_brokerid':
        return f"{p}.get('{t}', '{sym}', '9268 凱基台北')"
    elif rule == 'brokerid_n':
        return f"{p}.get('{t}', '9268 凱基台北', N)  # N 可選: {_ns}"
    elif rule == 'sector':
        return f"{p}.get('{t}', '半導體')  # 參考「台股產業對照表」的「指數彙編分類」欄位"
    elif rule == 'arg1_str':
        return f"{p}.get('{t}', '代號')"
    elif rule == 'symbol_market':
        return f"{p}.get('{t}', 'AAPL', 'F')  # market: F全天 P盤前 M盤中 A盤後"
    elif rule == 'multi_period':
        return f"{p}.get('多股多期', 'tableId', '欄位1', '欄位2', ...)"
    return f"{p}.get('{t}', ...)"

def _err(table: str, rule: str, prefix: str = 'Data') -> ValueError:
    return ValueError(f"參數輸入錯誤，正確用法：{_usage(table, rule, prefix)}")

def _err_msg(table ,prefix: str = 'Data')-> ValueError:
    rule = _RULES.get(table)
    raise ValueError(f"參數輸入錯誤，正確用法：{_usage(table, rule, prefix)}")

def check_args(table: str, args: tuple, prefix: str = 'Data' ,index=None) -> None:

    rule = _RULES.get(table)
    if rule is None:
        return  # 未知 table，放行

    if rule == 'no_args':
        if args:
            raise _err(table, rule, prefix)

    elif rule == 'date':
        if len(args) != 1 or not _is_date8(args[0]):
            raise _err(table, rule, prefix)

    elif rule == 'symbol':
        if len(args) != 1 or not isinstance(args[0], str):
            raise _err(table, rule, prefix)

    elif rule == 'symbol_minutes':
        if len(args) != 2:
            raise _err(table, rule, prefix)
        if not isinstance(args[0], str):
            raise _err(table, rule, prefix)
        m = _to_int(args[1])
        if m is None or m not in _VALID_MINUTES:
            raise _err(table, rule, prefix)

    elif rule == 'symbol_date':
        index =index[-60].strftime('%Y%m%d')
        if len(args) != 2:
            raise _err(table, rule, prefix)
        if not isinstance(args[0], str):
            raise _err(table, rule, prefix)
        if not _is_date8(args[1]):
            raise _err(table, rule, prefix)
        if args[1] < index:
            raise ValueError(f"參數輸入錯誤，date需 >={index}")

    elif rule == 'symbol_date_minutes':
        if len(args) != 3:
            raise _err(table, rule, prefix)
        if not isinstance(args[0], str):
            raise _err(table, rule, prefix)
        if not _is_date8(args[1]):
            raise _err(table, rule, prefix)
        m = _to_int(args[2])
        if m is None or m not in _VALID_MINUTES:
            raise _err(table, rule, prefix)
        if args[2]==1:
            index =index[-60].strftime('%Y%m%d')
            if args[1] < index:
                raise ValueError(f"參數輸入錯誤，date需 >={index}")
        elif args[2]==3:
            index =index[-11].strftime('%Y%m%d')
            if args[1] < index:
                raise ValueError(f"參數輸入錯誤，date需 >={index}")
        elif args[2]==5:
            index =index[-62].strftime('%Y%m%d')
            if args[1] < index:
                raise ValueError(f"參數輸入錯誤，date需 >={index}")
        elif args[2]==15:
            index =index[-122].strftime('%Y%m%d')
            if args[1] < index:
                raise ValueError(f"參數輸入錯誤，date需 >={index}")
        elif args[2]==30:
            index =index[-180].strftime('%Y%m%d')
            if args[1] < index:
                raise ValueError(f"參數輸入錯誤，date需 >={index}")
        elif args[2]==60:
            index =index[-200].strftime('%Y%m%d')
            if args[1] < index:
                raise ValueError(f"參數輸入錯誤，date需 >={index}")
        
    elif rule == 'symbol_date_days':
        if len(args) != 3:
            raise _err(table, rule, prefix)
        if not isinstance(args[0], str):
            raise _err(table, rule, prefix)
        if not _is_date8(args[1]):
            raise _err(table, rule, prefix)
        d = _to_int(args[2])
        if d is None or d <= 0:
            raise _err(table, rule, prefix)

    elif rule == 'symbol_enddate':
        if len(args) != 2:
            raise _err(table, rule, prefix)
        if not isinstance(args[0], str):
            raise _err(table, rule, prefix)
        if not _is_date8(args[1]):
            raise _err(table, rule, prefix)

    elif rule == 'symbol_start_end':
        index =index[-240].strftime('%Y%m%d')
        if len(args) != 3:
            raise _err(table, rule, prefix)
        if not isinstance(args[0], str):
            raise _err(table, rule, prefix)
        if not _is_date8(args[1]):
            raise _err(table, rule, prefix)
        if not _is_date8(args[2]):
            raise _err(table, rule, prefix)
        if args[1] < index and table=='查詢TOP15主力分點買賣超加總資料(指定起訖日)':
            raise ValueError(f"參數輸入錯誤，startDate需 >={index}")

    elif rule == 'list':
        if len(args) != 1:
            raise _err(table, rule, prefix)
        first = args[0]
        if isinstance(first, list):
            if len(first) == 0:
                raise _err(table, rule, prefix)
        elif not isinstance(first, str):
            raise _err(table, rule, prefix)

    elif rule == 'brokerid':
        if len(args) != 1 or not isinstance(args[0], str):
            raise _err(table, rule, prefix)

    elif rule == 'symbol_brokerid':
        if len(args) != 2:
            raise _err(table, rule, prefix)
        if not isinstance(args[0], str):
            raise _err(table, rule, prefix)
        if not isinstance(args[1], str):
            raise _err(table, rule, prefix)

    elif rule == 'brokerid_n':
        if len(args) != 2:
            raise _err(table, rule, prefix)
        if not isinstance(args[0], str):
            raise _err(table, rule, prefix)
        n = _to_int(args[1])
        if n is None or n not in _VALID_N:
            raise _err(table, rule, prefix)

    elif rule == 'sector':
        if len(args) != 1 or not isinstance(args[0], str):
            raise _err(table, rule, prefix)

    elif rule == 'arg1_str':
        if len(args) != 1 or not isinstance(args[0], str):
            raise _err(table, rule, prefix)

    elif rule == 'symbol_market':
        if len(args) != 2:
            raise _err(table, rule, prefix)
        if not isinstance(args[0], str):
            raise _err(table, rule, prefix)
        if args[1] not in _VALID_MARKET:
            raise _err(table, rule, prefix)

    elif rule == 'multi_period':
        if len(args) < 2:
            raise _err(table, rule, prefix)
        if not isinstance(args[0], str):
            raise _err(table, rule, prefix)
