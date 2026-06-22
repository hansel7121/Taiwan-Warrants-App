import requests
import re

r = requests.get(
    "https://www.cmoney.tw/finance/warrantsquery.aspx?warrant=051666",
    headers={"User-Agent": "Mozilla/5.0"},
    verify=False,
)

print(r.text[:3000])  # print first 3000 chars to see what's in the HTML
matches = re.findall(r'cmkey=([^&"\']+)', r.text)
print("cmkey matches:", matches)
