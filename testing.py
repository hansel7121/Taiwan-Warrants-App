import requests
import json
import twstock
from datetime import datetime

today = datetime.today()
codes = [
    k
    for k, v in twstock.codes.items()
    if "權證" in v.type
    and ("台積電" in v.name or "2330" in v.name)
    and "購" in v.name
    and datetime.strptime(v.start, "%Y/%m/%d") <= today
][:5]

print("Sample codes:", codes)

url = "https://www.warrantwin.com.tw/eyuanta/ws/GetWarData.ashx"
payload = {
    "format": "JSON",
    "factor": {
        "columns": [
            "FLD_WAR_ID",
            "FLD_WAR_NM",
            "FLD_WAR_TXN_VOLUME",
        ],
        "condition": [
            {"field": "FLD_WAR_ID", "values": codes},
            {"field": "FLD_WAR_TYPE", "values": ["1", "2"]},
        ],
        "orderby": {"field": "FLD_WAR_ID", "sort": "DESC", "agtfirst": "980"},
    },
    "pagination": {"row": 5, "page": "1"},
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
    print(raw)
