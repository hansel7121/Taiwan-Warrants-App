import requests
import pandas as pd
from dataclasses import dataclass
from ._dataus import _get
from ._dataus_url import  _response
from ._check_args import check_args

pd.set_option('display.float_format', lambda x: '%.0f' % x if x == int(x) else '%.4f' % x)

@dataclass
class Symbol:
    timestamp:str
    open:float
    high:float
    low:float
    close:float
    volume:int
    previousClose:float
    netChange:float
    percentChange:float

class _Data():
    def __init__(self ,URL ):
        self._base_url = URL.USbase_url
        self._URL =URL

    def get(self, table: str, *args) -> pd.DataFrame:
        """
        Retrieve table as a DataFrame, apply caching, column renaming, and filtering as needed.
        """
        headers = {"Content-Type": "application/json", 'Authorization': f'Bearer {self._URL.token}'}
        check_args(table, args, prefix='USData')
        df = _get(table ,self._base_url ,headers, *args)
        return df
    
    def get_snapshots(self,*args):
        headers = {"Content-Type": "application/json", 'Authorization': f'Bearer {self._URL.token}'}
        #ls = list(set(args[0] if len(args) == 1 and isinstance(args[0], list) else args))
        ls = list(dict.fromkeys(args[0] if len(args) == 1 and isinstance(args[0], list) else args))
        payload = {"symbols": ls}
        data_url = self._base_url +  f"/api/stock/snapshot"
        response = requests.post(data_url, headers=headers, json=payload, verify=True)
        response = _response(response)
        data = response.json()
        dic = {}
        for d in data:
            dic[d['symbol']]= Symbol(
                timestamp = str(d['timestamp']),
                open = float(d['open']),
                high = float(d['high']),
                low = float(d['low']),
                close = float(d['close']),
                volume = int(d['volume']),
                previousClose = float(d['previousClose']),
                netChange = float(d['netChange']),
                percentChange = float(d['percentChange']),
            )
        return dic