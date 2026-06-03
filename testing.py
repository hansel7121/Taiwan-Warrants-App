import requests
import json
import twstock
from datetime import datetime

# get a few put warrant codes
today = datetime.today()
put_codes = [
    k
    for k, v in twstock.codes.items()
    if "權證" in v.type
    and ("台積電" in v.name or "2330" in v.name)
    and "售" in v.name
    and datetime.strptime(v.start, "%Y/%m/%d") <= today
]
print(f"Found {len(put_codes)} put codes")
print("Sample codes:", put_codes[:5])

# fetch just a few from the API
url = "https://www.warrantwin.com.tw/eyuanta/ws/GetWarData.ashx"
payload = {
    "format": "JSON",
    "factor": {
        "columns": [
            "FLD_WAR_ID",
            "FLD_WAR_NM",
            "FLD_OPTION_TYPE",
            "FLD_OBJ_TXN_PRICE",
            "FLD_WAR_BUY_PRICE",
            "FLD_WAR_SELL_PRICE",
            "FLD_DUR_END",
            "FLD_N_STRIKE_PRC",
            "FLD_N_UND_CONVER",
            "FLD_RISK_RATE_FREE",
        ],
        "condition": [
            {"field": "FLD_WAR_ID", "values": put_codes[:10]},
            {"field": "FLD_WAR_TYPE", "values": ["1", "2"]},
        ],
        "orderby": {"field": "FLD_WAR_TXN_VOLUME", "sort": "DESC", "agtfirst": "980"},
    },
    "pagination": {"row": 10, "page": "1"},
}
headers = {
    "Referer": "https://www.warrantwin.com.tw/eyuanta/Warrant/Info.aspx",
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/x-www-form-urlencoded",
}

r = requests.post(
    url, data={"data": json.dumps(payload)}, headers=headers, verify=False
)
results = r.json().get("result", [])

print(f"\nAPI returned {len(results)} results")
for raw in results:
    print(
        raw.get("FLD_WAR_ID"),
        raw.get("FLD_WAR_NM"),
        "OPTION_TYPE:",
        raw.get("FLD_OPTION_TYPE"),
        "BID:",
        raw.get("FLD_WAR_BUY_PRICE"),
        "ASK:",
        raw.get("FLD_WAR_SELL_PRICE"),
    )
