#!/usr/bin/env python3
"""抓台灣氣象署 CODiS 全測站每日雨量（近 N 年）。

資料源：codis.cwa.gov.tw 內部 API（無官方文件，已逆向確認）
  測站清單： GET  /api/station_list
  逐日資料： POST /api/station   type=report_month + 整年範圍 -> 該年逐日

只擷取「每日累積雨量 (mm)」，其餘欄位丟棄，存成每站一個精簡 JSON。
支援斷點續傳：已抓且涵蓋目標年份的站會跳過。
"""
import json
import os
import time
import calendar
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import ssl

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

BASE = "https://codis.cwa.gov.tw"


class _CodisTLSAdapter(HTTPAdapter):
    """CODiS 憑證缺 Subject Key Identifier，OpenSSL 3 嚴格模式會拒驗。
    這裡保留完整憑證鏈 + 主機名驗證，只關掉過度嚴格的 X509_STRICT 旗標，
    不是整個關閉 TLS 驗證（仍能擋 MITM）。"""

    def init_poolmanager(self, *a, **kw):
        ctx = create_urllib3_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


def make_session():
    s = requests.Session()
    s.mount("https://", _CodisTLSAdapter())
    return s
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://codis.cwa.gov.tw/StationData",
}
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DAILY = os.path.join(DATA, "daily")


def stn_type_for(group, sid):
    """把 station_list 的分組 + 站號前綴 對應到資料 API 要的 stn_type。"""
    if group == "cwb":
        return "cwb"
    if group == "agr":
        return "agr"
    # group == "auto": C0xxxx -> auto_C0, C1xxxx -> auto_C1, 其餘當 C0
    if sid.startswith("C1"):
        return "auto_C1"
    return "auto_C0"


def build_station_index(sess):
    r = sess.get(f"{BASE}/api/station_list", headers=HEADERS, timeout=40)
    r.raise_for_status()
    payload = r.json()
    stations = []
    for grp in payload["data"]:
        group = grp["stationAttribute"]
        for it in grp["item"]:
            sid = it["stationID"]
            stations.append({
                "id": sid,
                "name": it.get("stationName", ""),
                "nameEN": it.get("stationNameEN", ""),
                "county": it.get("countryName", ""),
                "area": it.get("area", ""),
                "address": it.get("address", ""),
                "lat": it.get("latitude"),
                "lon": it.get("longitude"),
                "alt": it.get("altitude"),
                "group": group,
                "stn_type": stn_type_for(group, sid),
                "start": (it.get("stationStartDate") or "")[:10],
                "end": (it.get("stationEndDate") or "")[:10],
            })
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "stations.json"), "w", encoding="utf-8") as f:
        json.dump(stations, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[index] {len(stations)} 站寫入 stations.json")
    return stations


def to_mm(precip):
    """把 Precipitation.Accumulation 轉成數值 mm；微量(T)記 0.0；無資料/缺測記 None。

    CWA 慣例：'-'=0、'T'=微量(<0.5mm)、'X'=故障無紀錄、負數(-9.8/-99/-9999.x)=缺測/異常。
    雨量恆非負，任何負值一律視為缺測(None)，不可加進總和。"""
    if precip is None:
        return None
    v = precip.get("Accumulation") if isinstance(precip, dict) else precip
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return None if v < 0 else round(float(v), 1)
    s = str(v).strip()
    if s in ("T", "t"):                 # trace 微量
        return 0.0
    if s == "-":                        # CWA: '-' = 0mm
        return 0.0
    if s in ("X", "x", "/", "--"):      # 儀器故障 / 無觀測
        return None
    try:
        f = round(float(s), 1)
        return None if f < 0 else f
    except ValueError:
        return None


# 每日要素 schema（陣列順序，前端共用）：
#  0 雨量mm 1 最高溫 2 最低溫 3 平均溫 4 濕度% 5 氣壓hPa 6 平均風速 7 最大陣風 8 盛行風向 9 日照hr
ELEMENTS = ["p", "tmax", "tmin", "tavg", "rh", "pres", "wmean", "gust", "wdir", "sun"]


def cln_nonneg(v):
    """非負量(風/濕/壓/日照/風向)：負值=缺測 sentinel → None。"""
    if not isinstance(v, (int, float)):
        return None
    return None if v < 0 else round(float(v), 1)


def cln_temp(v):
    """氣溫：負值是合法低溫(玉山-11°C)，只把明顯 sentinel(<=-90 或 >=70)當缺測。"""
    if not isinstance(v, (int, float)):
        return None
    return None if (v <= -90 or v >= 70) else round(float(v), 1)


def row_to_elems(row):
    """把一筆日資料列轉成 ELEMENTS 順序的數值陣列。"""
    at = row.get("AirTemperature") or {}
    rh = row.get("RelativeHumidity") or {}
    pr = row.get("StationPressure") or {}
    ws = row.get("WindSpeed") or {}
    pg = row.get("PeakGust") or {}
    wd = row.get("WindDirection") or {}
    su = row.get("SunshineDuration") or {}
    return [
        to_mm(row.get("Precipitation")),
        cln_temp(at.get("Maximum")),
        cln_temp(at.get("Minimum")),
        cln_temp(at.get("Mean")),
        cln_nonneg(rh.get("Mean")),
        cln_nonneg(pr.get("Mean")),
        cln_nonneg(ws.get("Mean")),
        cln_nonneg(pg.get("Maximum")),
        cln_nonneg(wd.get("Prevailing")),
        cln_nonneg(su.get("Total")),
    ]


def fetch_year(sess, sid, stn_type, year):
    last = calendar.monthrange(year, 12)[1]
    data = {
        "date": f"{year}-01-01",
        "type": "report_month",
        "stn_ID": sid,
        "stn_type": stn_type,
        "start": f"{year}-01-01T00:00:00",
        "end": f"{year}-12-{last}T23:59:59",
    }
    for attempt in range(3):
        try:
            r = sess.post(f"{BASE}/api/station", data=data, headers=HEADERS, timeout=60)
            if r.status_code != 200:
                time.sleep(2 + attempt * 2)
                continue
            j = r.json()
            if j.get("code") != 200 or not j.get("data"):
                return {}  # 該站該年無資料（正常）
            out = {}
            for row in j["data"][0].get("dts", []):
                d = (row.get("DataDate") or "")[:10]
                if d:
                    out[d] = row_to_elems(row)
            return out
        except (requests.RequestException, ValueError):
            time.sleep(2 + attempt * 2)
    return None  # 三次都失敗


def station_path(sid):
    return os.path.join(DAILY, f"{sid}.json")


def need_fetch(sid, years):
    p = station_path(sid)
    if not os.path.exists(p):
        return True
    try:
        with open(p, encoding="utf-8") as f:
            existing = json.load(f)
        daily = existing.get("daily", {})
        for v in daily.values():        # 舊格式(值非陣列)需重抓
            if not isinstance(v, list):
                return True
            break
        have_years = {k[:4] for k in daily}
        return not all(str(y) in have_years for y in years)
    except (json.JSONDecodeError, OSError):
        return True


def fetch_station(st, years, throttle):
    sid = st["id"]
    sess = make_session()
    # 先載入既有資料再合併，避免「只補某幾年」時把舊年份洗掉
    daily = {}
    p = station_path(sid)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                ex = json.load(f).get("daily", {})
            if ex and all(isinstance(v, list) for v in ex.values()):
                daily = ex          # 只承接新格式；舊格式丟掉重抓
        except (json.JSONDecodeError, OSError):
            daily = {}
    ok_any = False
    for y in years:
        res = fetch_year(sess, sid, st["stn_type"], y)
        if res is None:
            continue  # 該年抓失敗，留待下次續傳補
        ok_any = True
        daily.update(res)
        time.sleep(throttle)
    rec = {
        "id": sid, "name": st["name"], "county": st["county"],
        "lat": st["lat"], "lon": st["lon"],
        "daily": dict(sorted(daily.items())),
    }
    os.makedirs(DAILY, exist_ok=True)
    with open(station_path(sid), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
    return sid, len(daily), ok_any


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2021,2022,2023,2024,2025,2026")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--throttle", type=float, default=0.4, help="同站每年請求間隔秒")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 站（測試用）")
    args = ap.parse_args()
    years = [int(y) for y in args.years.split(",")]

    sess = make_session()
    stations = build_station_index(sess)
    if args.limit:
        stations = stations[:args.limit]

    todo = [s for s in stations if need_fetch(s["id"], years)]
    print(f"[fetch] 共 {len(stations)} 站，需抓 {len(todo)} 站（其餘已完成，跳過），年份 {years}")

    done = 0
    total_pts = 0
    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_station, s, years, args.throttle): s for s in todo}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                sid, n, ok = fut.result()
                done += 1
                total_pts += n
                if not ok:
                    failed.append(sid)
                if done % 25 == 0 or done == len(todo):
                    print(f"  進度 {done}/{len(todo)}  最新 {sid} {n} 天  累計 {total_pts} 點")
            except Exception as e:  # noqa
                failed.append(s["id"])
                print(f"  !! {s['id']} 失敗: {e}")
    print(f"[done] 完成 {done} 站；無資料/失敗 {len(failed)} 站")
    if failed:
        print("  失敗站號(可重跑續傳):", ",".join(failed[:30]), "..." if len(failed) > 30 else "")


if __name__ == "__main__":
    main()
