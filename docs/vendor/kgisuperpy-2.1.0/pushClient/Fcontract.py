import requests
import xml.etree.ElementTree as ET
import pandas as pd
from datetime import datetime, timedelta

class FContract:
    def __init__(self, url):
        self.url = url
        self.data=None
        self.last_loaded = None
        
    def _load(self):
        response = requests.get(self.url)
        response.encoding = 'utf-8'  # 如果檔案內是 UTF-8，可設定編碼
        root = ET.fromstring(response.text)  # 用 fromstring 而不是 parse

        records = []
        for topic in root.findall("Topic"):
            record = {
                "Exchange": topic.findtext("Exchange"),
                "ComID": topic.findtext("ComID"),
                "ComTyp": topic.findtext("ComTyp"),
                "DecimalLength": topic.findtext("DecimalLength"),
                "SPPoint": topic.findtext("SPPoint"),
                "NorValu": topic.findtext("NorValu"),
                "ContractType": topic.findtext("ContractType"),
                "ContractValue": topic.findtext("ContractValue"),
                "TaxRate": topic.findtext("TaxRate"),
                "Tick": topic.findtext("Tick"),
                "SPTick": topic.findtext("SPTick"),
                "Currency": topic.findtext("Currency"),
                "ComCName": topic.findtext("ComCName"),
                "ComEName": topic.findtext("ComEName"),
                "DDSCExchange": topic.findtext("DDSCExchange"),
                "DDSCComID": topic.findtext("DDSCComID"),
                "TasType": topic.findtext("TasType"),
                "PMultiplier": topic.findtext("PMultiplier"),
                "PriceFlag": topic.findtext("PriceFlag"),
                "MarketType": {},
                "TradingTime": {}
            }

            for m in topic.findall("MarketType"):
                typ = m.attrib.get("type")
                record["MarketType"][typ] = {
                    "StartTime": m.attrib.get("StartTime"),
                    "EndTime": m.attrib.get("EndTime")
                }

            for t in topic.findall("TradingTime"):
                typ = t.attrib.get("type")
                record["TradingTime"][typ] = {
                    "Season": t.attrib.get("Season"),
                    "OpenTime": t.attrib.get("OpenTime"),
                    "CloseTime": t.attrib.get("CloseTime")
                }

            records.append(record)
        df = pd.DataFrame(records)
        self.data = df
        self.last_loaded = datetime.now()
        return 

    def get_contracts(self):
        if self.data is None:
            self._load()
        elif datetime.now() - self.last_loaded > timedelta(hours=1):
            self._load()
        return self.data


from dataclasses import dataclass

@dataclass
class FContractData:
    Exchange: str
    ComID: str
    ComTyp: str
    DecimalLength: str
    SPPoint: str
    NorValu: str
    ContractType: str
    ContractValue: str
    TaxRate: str
    Tick: str
    SPTick: str
    Currency: str
    ComCName: str
    ComEName: str
    DDSCExchange: str
    DDSCComID: str
    TasType: str
    PMultiplier: str
    PriceFlag: str
    MarketType: dict
    TradingTime: dict


# 全域快取
_Fcontracts_df = None
_Fcontracts_dic = None

def get_Fcontracts(type='dic'):
    """
    取得期貨合約資訊

    :param type: 回傳格式
                - 'df'  : pandas DataFrame
                - 'dic' : {ComID: FContractData}
    :return: 合約資料
    """
    global _Fcontracts_df, _Fcontracts_dic

    # 第一次或快取不存在時重新載入
    if _Fcontracts_df is None or _Fcontracts_dic is None:
        loader = FContract( get_Fcontracts.url)  
        df = loader.get_contracts()
        _Fcontracts_df = df

        dic = {}
        for _, row in df.iterrows():
            comid = row['ComID']
            if comid not in dic:
                dic[comid] = []
            dic[comid].append(FContractData(**row.to_dict()))

    if type == 'df':
        return _Fcontracts_df
    elif type == 'dic':
        return dic
    else:
        raise ValueError("type 必須是 'df' 或 'dic'")

