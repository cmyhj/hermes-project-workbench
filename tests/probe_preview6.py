"""第六轮（关键判定）：
1. 无 CSP 时直接导航 SVG，内联脚本是否执行（证明必须加头）。
2. PDF 在 iframe 中，分别测量：无 CSP / CSP default-src none / CSP sandbox /
   iframe 带 sandbox="" 属性 —— 哪种还能渲染出内容。
3. PDF 响应带 Content-Disposition: attachment 是否会让 iframe 变空白。
"""
from __future__ import annotations

import http.server
import json
import socketserver
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

TMP = Path(tempfile.mkdtemp(prefix="wbcsp-"))

SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">
<script>window.__S__=1;document.title='pwned';</script>
<rect width="120" height="40" fill="#3366cc"/></svg>"""

CASES = {
    # name: (csp, disposition, iframe_sandbox_attr)
    "plain": (None, None, None),
    "csp_default_none": ("default-src 'none'", None, None),
    "csp_sandbox": ("sandbox; default-src 'none'", None, None),
    "csp_none_sandbox_attr": (None, None, ""),
    "attachment": (None, "attachment", None),
}


def _pdf() -> Path:
    import fitz
    p = TMP / "s.pdf"
    d = fitz.open(); pg = d.new_page()
    for i in range(8):
        pg.insert_text((60, 90 + i * 22), f"PDF LINE {i} visible content abc {i}", fontsize=13)
    d.save(str(p)); d.close()
    return p


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _s(self, body, ct, extra=None):
        self.send_response(200); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):  # noqa: N802
        p, _, case = self.path.partition("?")
        case = case or "plain"
        csp, disp, _ = CASES.get(case, CASES["plain"])
        extra = {}
        if csp: extra["Content-Security-Policy"] = csp
        if disp: extra["Content-Disposition"] = f'{disp}; filename="s.pdf"'
        if p == "/raw.svg":
            self._s(SVG, "image/svg+xml", extra)
        elif p == "/s.pdf":
            self._s((TMP / "s.pdf").read_bytes(), "application/pdf", extra)
        else:
            self.send_error(404)


def colors(png: Path) -> int:
    import fitz
    d = fitz.open(str(png)); pix = d[0].get_pixmap()
    seen = set()
    for y in range(0, pix.height, 3):
        for x in range(0, pix.width, 3):
            seen.add(pix.pixel(x, y))
            if len(seen) > 10: break
        if len(seen) > 10: break
    d.close(); return len(seen)


def main() -> None:
    _pdf()
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    out: dict = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=False, channel="chrome")
        # 1) 直接导航 SVG（无头 / 有头都测）
        for headless in (True, False):
            bb = pw.chromium.launch(headless=headless)
            for case in ("plain", "csp_default_none", "csp_sandbox"):
                pg = bb.new_page()
                pg.goto(f"{base}/raw.svg?{case}", wait_until="load")
                pg.wait_for_timeout(500)
                out[f"svg_direct|{case}|headless={headless}"] = {
                    "title": pg.title(), "ran": pg.evaluate("() => !!window.__S__")}
                pg.close()
            bb.close()

        # 2) PDF iframe 各变体
        for case, (csp, disp, sandbox_attr) in CASES.items():
            sb = f' sandbox="{sandbox_attr}"' if sandbox_attr is not None else ""
            html = (f"<!doctype html><body style='margin:0;background:#fff'>"
                    f"<iframe id='f' src='/s.pdf?{case}' width='480' height='360'{sb} "
                    f"style='border:0'></iframe></body>")
            pg = b.new_page(viewport={"width": 700, "height": 500})
            pg.set_content(html, wait_until="load")
            pg.wait_for_timeout(3200)
            shot = TMP / f"shot-{case}.png"
            try:
                pg.locator("#f").screenshot(path=str(shot))
                ncol = colors(shot)
            except Exception as e:
                ncol = f"err:{str(e)[:60]}"
            out[f"pdf|{case}"] = {"colors": ncol, "shot": str(shot),
                                  "frames": [f.url for f in pg.frames]}
            pg.close()
        b.close()
    srv.shutdown()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
