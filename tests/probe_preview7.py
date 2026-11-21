"""第七轮（真实页面刷新，修正第六轮 about:blank 问题）：
PDF 在 iframe 中，响应头变体对渲染的影响：
  plain / csp_default_none / csp_sandbox / attachment / attachment+inline-param
判定：iframe 区域截图里不同颜色数（>3 视为真的画出内容）。
"""
from __future__ import annotations

import http.server
import json
import socketserver
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

TMP = Path(tempfile.mkdtemp(prefix="wbpdf7-"))
CASES = ["plain", "csp_none", "csp_sandbox", "attachment", "inline"]


def _pdf() -> None:
    import fitz
    d = fitz.open()
    for _ in range(2):
        p = d.new_page()
        for i in range(10):
            p.insert_text((60, 90 + i * 22), f"PDF LINE {i} visible content 中文 {i}", fontsize=13)
    d.save(str(TMP / "s.pdf")); d.close()


CSP = {
    "plain": None,
    "csp_none": "default-src 'none'; style-src 'unsafe-inline'",
    "csp_sandbox": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
    "attachment": "default-src 'none'; style-src 'unsafe-inline'",
    "inline": "default-src 'none'; style-src 'unsafe-inline'",
}
DISP = {
    "plain": "inline", "csp_none": "inline", "csp_sandbox": "inline",
    "attachment": "attachment", "inline": "inline",
}

HTML = """<!doctype html><html><body style="margin:0;background:#eef">
<iframe id="f" src="/s.pdf?CASE" width="480" height="420" style="border:0"></iframe>
</body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _s(self, body, ct, extra=None):
        self.send_response(200); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):  # noqa: N802
        p, _, c = self.path.partition("?")
        c = c or "plain"
        if p == "/":
            self._s(HTML.replace("CASE", c).encode(), "text/html; charset=utf-8")
        elif p == "/s.pdf":
            extra = {"X-Content-Type-Options": "nosniff",
                     "Content-Disposition": f'{DISP[c]}; filename="s.pdf"'}
            if CSP[c]:
                extra["Content-Security-Policy"] = CSP[c]
            self._s((TMP / "s.pdf").read_bytes(), "application/pdf", extra)
        else:
            self.send_error(404)


def ncolors(path: Path) -> int:
    import fitz
    d = fitz.open(str(path)); pix = d[0].get_pixmap()
    seen = set()
    for y in range(0, pix.height, 3):
        for x in range(0, pix.width, 3):
            seen.add(pix.pixel(x, y))
            if len(seen) > 12: break
        if len(seen) > 12: break
    d.close(); return len(seen)


def main() -> None:
    _pdf()
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    out = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=False, channel="chrome")
        for c in CASES:
            pg = b.new_page(viewport={"width": 760, "height": 560})
            pg.goto(f"{base}/?{c}", wait_until="networkidle")
            pg.wait_for_timeout(3500)
            shot = TMP / f"{c}.png"
            pg.locator("#f").screenshot(path=str(shot))
            out[c] = {"colors": ncolors(shot), "shot": str(shot),
                      "frames": [f.url for f in pg.frames]}
            pg.close()
        b.close()
    srv.shutdown()
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
