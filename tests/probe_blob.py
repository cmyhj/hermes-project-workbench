"""第八轮：前端要用的 blob 路线 —— authedFetch → blob → <img>/<iframe>。
判定：blob: 的图片与 PDF 是否真的渲染（不受 cookie/token 影响，避开 <img src> 不能带 token 的问题）。"""
from __future__ import annotations

import http.server
import json
import socketserver
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

TMP = Path(tempfile.mkdtemp(prefix="wbblob-"))

HTML = """<!doctype html><html><body style="margin:0;background:#eef">
<div id="imgwrap"></div>
<div id="pdfwrap"></div>
<script>
(async () => {
  // 1) 图片：fetch → blob → img
  const r = await fetch('/s.png');
  const b = await r.blob();
  const url = URL.createObjectURL(b);
  const img = document.createElement('img');
  img.id = 'bi'; img.width = 120; img.height = 90; img.src = url;
  document.getElementById('imgwrap').appendChild(img);
  // 2) PDF：fetch → blob → iframe
  const r2 = await fetch('/s.pdf');
  const b2 = await r2.blob();
  const pu = URL.createObjectURL(b2);
  const f = document.createElement('iframe');
  f.id = 'bf'; f.width = 480; f.height = 380; f.style.border = '0'; f.src = pu;
  document.getElementById('pdfwrap').appendChild(f);
  window.__READY__ = true;
})();
</script></body></html>"""


def _png() -> bytes:
    import zlib, struct
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    # 8x8 红色方块，确保有颜色
    rows = b"".join(b"\x00" + b"\xcc\x22\x22" * 8 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _pdf() -> None:
    import fitz
    d = fitz.open()
    for _ in range(2):
        p = d.new_page()
        for i in range(10):
            p.insert_text((50, 80 + i * 22), f"BLOB PDF LINE {i} 中文内容 {i}", fontsize=13)
    d.save(str(TMP / "s.pdf")); d.close()


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _s(self, body, ct):
        self.send_response(200); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):  # noqa: N802
        p = self.path.split("?")[0]
        if p == "/": self._s(HTML.encode(), "text/html; charset=utf-8")
        elif p == "/s.png": self._s(_png(), "image/png")
        elif p == "/s.pdf": self._s((TMP / "s.pdf").read_bytes(), "application/pdf")
        else: self.send_error(404)


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
        for ch in ("chrome", "msedge"):
            b = pw.chromium.launch(headless=False, channel=ch)
            pg = b.new_page(viewport={"width": 760, "height": 620})
            pg.goto(base + "/", wait_until="networkidle")
            pg.wait_for_timeout(3800)
            out[ch] = {
                "img": pg.evaluate("() => {const i=document.querySelector('#bi');"
                                         "return {w:i.naturalWidth,h:i.naturalHeight,ok:i.complete}}"),
                "pdf_frames": [f.url[:40] for f in pg.frames],
                "pdf_colors": ncolors(TMP / f"blob-{ch}.png") if (
                    pg.locator("#bf").screenshot(path=str(TMP / f"blob-{ch}.png")) or True) else None,
            }
            b.close()
    srv.shutdown()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
