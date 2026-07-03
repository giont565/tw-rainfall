#!/usr/bin/env python3
"""本機伺服器：靜態檔 + 逐時雨量現抓 proxy。

用法：在本資料夾跑 `python serve.py`（預設 port 8848），
瀏覽器開 http://localhost:8848/web/index.html

- 靜態：直接服務專案目錄（web/、data/）
- 逐時：GET /api/hourly?id=<站號>&month=YYYY-MM
    瀏覽器因跨網域+憑證問題無法直接打 CODiS，故由本伺服器代抓。
    一次抓「一個月逐時」（report_date 上限），抓回後快取在 data/hourly/，
    同站同月第二次直接讀快取。
"""
import os
import re
import json
import calendar
import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from fetch import make_session, BASE, HEADERS, to_mm, cln_temp, cln_nonneg, ELEMENTS


def hourly_row_to_elems(row):
    """逐時列(report_date，瞬時值)轉成 ELEMENTS 順序陣列；溫度三格都放瞬時溫。"""
    at = row.get("AirTemperature") or {}
    rh = row.get("RelativeHumidity") or {}
    pr = row.get("StationPressure") or {}
    ws = row.get("WindSpeed") or {}
    pg = row.get("PeakGust") or {}
    wd = row.get("WindDirection") or {}
    su = row.get("SunshineDuration") or {}
    ti = cln_temp(at.get("Instantaneous"))
    return [
        to_mm(row.get("Precipitation")),
        ti, ti, ti,
        cln_nonneg(rh.get("Instantaneous")),
        cln_nonneg(pr.get("Instantaneous")),
        cln_nonneg(ws.get("Mean")),
        cln_nonneg(pg.get("Maximum")),
        cln_nonneg(wd.get("Mean")),
        cln_nonneg(su.get("Total")),
    ]

HERE = os.path.dirname(os.path.abspath(__file__))
HOURLY_CACHE = os.path.join(HERE, "data", "hourly")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# 站號 -> stn_type（從已抓好的 stations.json 讀）
_STN_TYPE = {}


def load_station_types():
    path = os.path.join(HERE, "data", "stations.json")
    with open(path, encoding="utf-8") as f:
        for s in json.load(f):
            _STN_TYPE[s["id"]] = s["stn_type"]


def fetch_hourly(stn_id, month):
    """抓某站某月逐時雨量 -> {datetime: mm}；快取於 data/hourly/。"""
    cache = os.path.join(HOURLY_CACHE, f"{stn_id}__{month}.json")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)

    stn_type = _STN_TYPE.get(stn_id)
    if not stn_type:
        return {"error": "unknown station", "id": stn_id}

    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    data = {
        "date": f"{month}-01",
        "type": "report_date",  # 逐時
        "stn_ID": stn_id,
        "stn_type": stn_type,
        "start": f"{month}-01T00:00:00",
        "end": f"{month}-{last:02d}T23:59:59",
    }
    sess = make_session()
    try:
        r = sess.post(f"{BASE}/api/station", data=data, headers=HEADERS, timeout=60)
        j = r.json()
    except Exception as e:  # noqa
        return {"error": f"fetch failed: {e}"}
    if j.get("code") != 200 or not j.get("data"):
        return {"hourly": {}}  # 該站該月無資料

    hourly = {}
    for row in j["data"][0].get("dts", []):
        t = row.get("DataTime") or ""
        if t:
            hourly[t[:16]] = hourly_row_to_elems(row)
    result = {"id": stn_id, "month": month, "hourly": dict(sorted(hourly.items()))}

    os.makedirs(HOURLY_CACHE, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    return result


# 私人金鑰閘門：雲端(有 PORT 環境變數)才啟用；本機開發不用鑰匙
ACCESS_KEY = "9c52924e32"
REQUIRE_KEY = bool(os.environ.get("PORT"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)

    def _authed(self, parsed):
        if not REQUIRE_KEY:
            return True
        q = parse_qs(parsed.query)
        if (q.get("k") or [""])[0] == ACCESS_KEY:
            self._issue_cookie = True  # 首次帶金鑰 → 發 cookie，之後免帶
            return True
        return f"k={ACCESS_KEY}" in (self.headers.get("Cookie") or "")

    def end_headers(self):
        if getattr(self, "_issue_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"k={ACCESS_KEY}; Path=/; Max-Age=31536000; SameSite=Lax",
            )
        super().end_headers()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")  # 讓 GitHub Pages 前端可呼叫
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # CORS 預檢
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json({"ok": True})
            return
        if not self._authed(parsed):
            self._send_json({"error": "此為私人服務，需要金鑰（網址加 ?k=金鑰）"}, 403)
            return
        if parsed.path == "/api/hourly":
            q = parse_qs(parsed.query)
            stn_id = (q.get("id") or [""])[0]
            month = (q.get("month") or [""])[0]
            if not stn_id or not MONTH_RE.match(month):
                self._send_json({"error": "需要 id 與 month=YYYY-MM"}, 400)
                return
            try:
                self._send_json(fetch_hourly(stn_id, month))
            except Exception as e:  # noqa
                self._send_json({"error": str(e)}, 500)
            return
        super().do_GET()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8848)))
    args = ap.parse_args()
    # 雲端(Render)有 PORT 環境變數→綁 0.0.0.0 對外；本機→綁 127.0.0.1
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    load_station_types()
    os.makedirs(HOURLY_CACHE, exist_ok=True)
    srv = ThreadingHTTPServer((host, args.port), Handler)
    print(f"伺服器啟動：{host}:{args.port}（本機開 http://localhost:{args.port}/web/index.html）")
    print(f"逐時現抓 + 快取於 data/hourly/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
