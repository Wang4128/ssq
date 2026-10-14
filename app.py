# -*- coding: utf-8 -*-
"""双色球手机版服务端（纯 Python 标准库，适配 Render 免费版）。

职责：
  1. 服务端代理抓取开奖数据（官方中彩网为主源，500 彩票网备用），规避浏览器跨域限制
  2. 内存 + 磁盘缓存，每 15 分钟自动刷新（云端 7x24 运行，开奖后自动获取新一期）
  3. 对手机页面提供静态托管与 /api/history 数据接口

预测、统计分析与回测全部在手机页面本地完成（见 index.html），
预测记录保存在各人手机的 localStorage 中，服务端不做持久化。
"""
import json
import os
import ssl
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "data_cache.json")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

FETCH_INTERVAL = 900  # 自动刷新间隔：15 分钟
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

try:
    _SSL_CTX = ssl.create_default_context()
except Exception:
    _SSL_CTX = ssl._create_unverified_context()  # noqa: SLF001


def _http_get(url, headers=None, timeout=25):
    hdr = {"User-Agent": _UA}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return resp.read()
    except ssl.SSLCertVerificationError:
        ctx = ssl._create_unverified_context()  # noqa: SLF001
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read()


def fetch_official(count=300):
    """中彩网官方开奖接口。"""
    url = ("https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
           "?name=ssq&issueCount=%d" % count)
    raw = _http_get(url, {
        "Referer": "https://www.cwl.gov.cn/kjxx/ssq/kjgg/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("state") != 0 or not payload.get("result"):
        raise RuntimeError("官方接口返回异常：%s" % payload.get("message"))
    draws = []
    for item in payload["result"]:
        draws.append({
            "issue": str(item["code"]),
            "date": str(item.get("date", "")).split("(")[0],
            "red": sorted(int(x) for x in str(item["red"]).split(",")),
            "blue": int(item["blue"]),
        })
    draws.sort(key=lambda d: d["issue"])
    return draws


def fetch_500(count=300):
    """500 彩票网备用源。"""
    url = ("https://datachart.500.com/ssq/history/newinc/history.php"
           "?limit=%d&sort=0" % count)
    import re
    html = _http_get(url).decode("utf-8", errors="ignore")
    draws = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        cells = [c.replace("&nbsp;", "").strip() for c in cells]
        cells = [c for c in cells if c]
        for i, cell in enumerate(cells):
            if not re.fullmatch(r"\d{5}", cell):
                continue
            balls = cells[i + 1: i + 8]
            if len(balls) < 7 or not all(re.fullmatch(r"\d{2}", b) for b in balls):
                continue
            issue = "20" + cell
            if any(d["issue"] == issue for d in draws):
                continue
            draws.append({"issue": issue, "date": "",
                          "red": sorted(int(b) for b in balls[:6]),
                          "blue": int(balls[6])})
    if not draws:
        raise RuntimeError("未能从 500 彩票网解析出数据")
    draws.sort(key=lambda d: d["issue"])
    return draws


def fetch_latest():
    errors = []
    try:
        return fetch_official(300), "中彩网官方"
    except Exception as exc:
        errors.append("官方接口：%s" % exc)
    try:
        return fetch_500(300), "500彩票网"
    except Exception as exc:
        errors.append("500彩票网：%s" % exc)
    raise RuntimeError("；".join(errors))


# ---------------------------------------------------------------- 缓存与刷新

_state_lock = threading.Lock()
_history = []
_last_fetch = 0.0
_source = ""
_ready = threading.Event()


def _load_cache():
    global _history
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                _history = sorted(data, key=lambda d: d["issue"])
                return True
        except Exception:
            pass
    return False


def _save_cache():
    try:
        with _state_lock:
            snapshot = list(_history)
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False)
    except Exception:
        pass


def _refresh():
    """抓取并合并最新数据；成功或已 有缓存兜底时置 ready。"""
    global _history, _last_fetch, _source
    try:
        fetched, src = fetch_latest()
        with _state_lock:
            known = {d["issue"]: d for d in _history}
            for d in fetched:
                known.setdefault(d["issue"], d)
            _history = sorted(known.values(), key=lambda x: x["issue"])
            _last_fetch = time.time()
            _source = src
        _save_cache()
        return True
    except Exception:
        with _state_lock:
            has_cache = bool(_history)
            if has_cache:
                _last_fetch = time.time()  # 失败也退避一个周期，避免高频重试
        return has_cache
    finally:
        if _history:
            _ready.set()


def _refresh_loop():
    while True:
        _refresh()
        time.sleep(FETCH_INTERVAL)


def _snapshot():
    with _state_lock:
        return list(_history), _source, _last_fetch


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = "SSQMobile/1.0"

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                with open(INDEX_FILE, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            elif path == "/api/history":
                if not _ready.wait(25) and not _snapshot()[0]:
                    self._json(200, {"ok": False, "error": "数据获取中，请稍后重试"})
                    return
                # 数据过期则后台刷新，不阻塞本次响应
                if time.time() - _snapshot()[2] > FETCH_INTERVAL:
                    threading.Thread(target=_refresh, daemon=True).start()
                draws, source, fetched = _snapshot()
                self._json(200, {
                    "ok": True,
                    "source": source,
                    "count": len(draws),
                    "fetched_at": datetime.fromtimestamp(fetched).strftime("%Y-%m-%d %H:%M")
                                  if fetched else "",
                    "draws": draws,
                })
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._json(500, {"ok": False, "error": str(exc)})
            except Exception:
                pass

    def log_message(self, fmt, *args):  # 静音访问日志
        pass


def main():
    if _load_cache():
        _ready.set()
    threading.Thread(target=_refresh_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print("双色球手机版服务已启动，端口 %d" % port, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
