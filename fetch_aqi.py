#!/usr/bin/env python3
"""抓環境部空氣品質歷史資料（AQI/PM2.5/PM10/O3…）。

資料源：環境部環境資料開放平台 data.moenv.gov.tw
  歷史 AQI： GET /api/v2/aqx_p_488?format=json&year_month=YYYY_MM&offset=&limit=&api_key=
  需免費 API 金鑰（會員註冊，含 reCAPTCHA，須人工取得）。

用法：  python fetch_aqi.py --key <API_KEY> --start 2021-01 --end 2026-06
產出：
  data/aqi/stations.json          站清單（SiteId/名稱/縣市/經緯度）
  data/aqi/daily/<SiteId>.json     每站每日 {date:[aqi,pm25,pm10,o3,...]}（由逐時聚合）
"""
import os
import json
import time
import argparse
from collections import defaultdict

import requests

BASE = "https://data.moenv.gov.tw/api/v2/aqx_p_488"
HERE = os.path.dirname(os.path.abspath(__file__))
AQI = os.path.join(HERE, "data", "aqi")
DAILY = os.path.join(AQI, "daily")

# 空品要素 schema（與氣象分開）
AQI_ELEMS = ["aqi", "pm25", "pm10", "o3", "co", "so2", "no2"]
FIELD = {  # API 欄位名 -> 我們的 key
    "aqi": "aqi", "pm2.5": "pm25", "pm10": "pm10",
    "o3": "o3", "co": "co", "so2": "so2", "no2": "no2",
}


def fnum(v):
    try:
        f = float(v)
        return None if f < 0 else f
    except (TypeError, ValueError):
        return None


def months(start, end):
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    out = []
    while y < ey or (y == ey and m <= em):
        out.append(f"{y}_{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def fetch_month(sess, key, ym):
    """抓某 year_month 全測站逐時 → list of records。"""
    recs, offset = [], 0
    while True:
        url = (f"{BASE}?format=json&year_month={ym}"
               f"&offset={offset}&limit=1000&api_key={key}")
        for attempt in range(3):
            try:
                r = sess.get(url, timeout=60)
                j = r.json()
                break
            except (requests.RequestException, ValueError):
                time.sleep(2 + attempt * 2)
        else:
            return recs
        batch = j.get("records", [])
        recs.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="環境部 API 金鑰")
    ap.add_argument("--start", default="2021-01")
    ap.add_argument("--end", default="2026-06")
    ap.add_argument("--throttle", type=float, default=0.3)
    args = ap.parse_args()

    os.makedirs(DAILY, exist_ok=True)
    sess = requests.Session()
    stations = {}
    # 每站每日各要素值的清單（之後取日均/日最大）
    bucket = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))  # site->date->key->[vals]

    yms = months(args.start, args.end)
    print(f"[aqi] 抓 {len(yms)} 個月：{yms[0]} ~ {yms[-1]}")
    for i, ym in enumerate(yms, 1):
        recs = fetch_month(sess, args.key, ym)
        for rec in recs:
            sid = rec.get("siteid") or rec.get("SiteId")
            if not sid:
                continue
            stations.setdefault(sid, {
                "id": sid,
                "name": rec.get("sitename") or rec.get("SiteName", ""),
                "county": rec.get("county") or rec.get("County", ""),
                "lat": fnum(rec.get("latitude") or rec.get("Latitude")),
                "lon": fnum(rec.get("longitude") or rec.get("Longitude")),
            })
            dt = (rec.get("datacreationdate") or rec.get("monitordate") or "")[:10].replace("/", "-")
            if not dt:
                continue
            for api_f, key in FIELD.items():
                v = fnum(rec.get(api_f) or rec.get(api_f.upper()))
                if v is not None:
                    bucket[sid][dt][key].append(v)
        print(f"  {i}/{len(yms)} {ym} 累計站 {len(stations)}")
        time.sleep(args.throttle)

    # 聚合成每日（AQI/PM 取日均，四捨五入）
    for sid, days in bucket.items():
        daily = {}
        for dt, keys in sorted(days.items()):
            row = []
            for k in AQI_ELEMS:
                vals = keys.get(k, [])
                row.append(round(sum(vals) / len(vals), 1) if vals else None)
            daily[dt] = row
        rec = {**stations[sid], "elems": AQI_ELEMS, "daily": daily}
        with open(os.path.join(DAILY, f"{sid}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))

    with open(os.path.join(AQI, "stations.json"), "w", encoding="utf-8") as f:
        json.dump(list(stations.values()), f, ensure_ascii=False)
    print(f"[done] {len(stations)} 站空品資料寫入 data/aqi/")


if __name__ == "__main__":
    main()
