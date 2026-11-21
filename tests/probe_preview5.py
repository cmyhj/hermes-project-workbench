"""第五轮：验证 `Content-Security-Policy: sandbox` 能否在**直接导航** SVG 时阻止内联脚本，
以及带该头的 SVG 在 <img> 中是否仍能正常显示（部署侧要同时满足这两条）。"""
from __future__ import annotations

import http.server
import json
import socketserver
import threading
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

TMP = Path(tempfile.mkdtemp(prefix="wbsvg-"))

SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="160" height="60">
<script>window.__S__=1;document.title='pwned';</script>
<rect width="160" height="60" fill="#2f8f5b"/><text x="8" y="36" fill="#fff" font-size="16">svg ok</text>
</svg>"""

HTML = """<!doctype html><body><img id="i" src="/guarded.svg" width="160" height="60"></body>"""

# 候选头
POLICIES = {
    "sandbox": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
    "nosandbox": "default-src 'none'; style-src 'unsafe-inline'",
    "scriptnone": "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'",
}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _s(self, body, ct, extra=None):
        self.send_response(200); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):  # noqa: N802
        p, _, q = self.path.partition("?")
        if p == "/": self._s(HTML.encode(), "text/html; charset=utf-8")
        elif p == "/guarded.svg":
            pol = POLICIES.get(q or "sandbox", POLICIES["sandbox"])
            self._s(SVG, "image/svg+xml", {"Content-Security-Policy": pol,
                                           "Content-Disposition": "inline"})
        else: self.send_error(404)


def main() -> None:
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    out = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        for name in POLICIES:
            pg = b.new_page()
            pg.goto(f"{base}/guarded.svg?{name}", wait_until="load")
            pg.wait_for_timeout(700)
            direct = {"title": pg.title(), "var": pg.evaluate("() => !!window.__S__")}
            pg2 = b.new_page()
            pg2.goto(f"{base}/", wait_until="networkidle")
            pg2.wait_for_timeout(600)
            img = pg2.evaluate("() => {const i=document.querySelector('#i');"
                               "return {w:i.naturalWidth,h:i.naturalHeight,ok:i.complete}}")
            out[name] = {"direct": direct, "in_img": img}
            pg.close(); pg2.close()
        b.close()
    srv.shutdown()
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
