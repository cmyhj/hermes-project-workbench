"""第三轮：确认 <img> 里的 SVG 脚本不会执行（无 CSP 对照组），以及 PDF 可见性判定。
同时验证同源 <img> 加载 SVG 时，SVG 内 <use href> 外部引用是否被浏览器拦截。
"""
from __future__ import annotations

import http.server
import json
import socketserver
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

TMP = Path(tempfile.mkdtemp(prefix="wbprev3-"))

SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="160" height="60">
<script>window.__SVG_SCRIPT_RAN__=true;document.title='ran';</script>
<image href="/leak.png" width="10" height="10"/>
<rect width="160" height="60" fill="#2f8f5b"/><text x="8" y="36" fill="#fff">ok</text>
</svg>"""

HTML = """<!doctype html><html><body style="margin:0;background:#fff">
<img id="a" src="/evil.svg" width="160" height="60">
<img id="b" src="/clean.svg" width="160" height="60">
<iframe id="p" src="/sample.pdf" width="420" height="320"></iframe>
<img id="png" src="/leak.png" width="24" height="24">
</body></html>"""

CLEAN = b"""<svg xmlns="http://www.w3.org/2000/svg" width="160" height="60"><rect width="160" height="60" fill="#c97a12"/></svg>"""


def _png() -> bytes:
    import zlib, struct
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    raw = b"\x00\xff\x00\x00"
    idat = zlib.compress(raw)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _pdf() -> bool:
    try:
        import fitz
    except Exception:
        return False
    d = fitz.open(); p = d.new_page(); p.insert_text((72, 100), "PDF PROBE VISIBLE TEXT"); d.save(str(TMP/"s.pdf")); d.close()
    return True


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _s(self, body, ct):
        self.send_response(200); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):  # noqa: N802
        p = self.path.split("?")[0]
        if p == "/": self._s(HTML.encode(), "text/html; charset=utf-8")
        elif p == "/evil.svg": self._s(SVG, "image/svg+xml")
        elif p == "/clean.svg": self._s(CLEAN, "image/svg+xml")
        elif p == "/leak.png": self._s(_png(), "image/png")
        elif p == "/sample.pdf": self._s((TMP/"s.pdf").read_bytes(), "application/pdf")
        else: self.send_error(404)


def main() -> None:
    have = _pdf()
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    print("base", base, "pdf", have)
    out = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 900, "height": 700})
        pg.goto(base + "/", wait_until="networkidle"); pg.wait_for_timeout(1500)
        out["img_svg_painted"] = pg.evaluate(
            "() => {const i=document.querySelector('#b');return i.naturalWidth>0 && i.complete}")
        out["img_svg_size"] = pg.evaluate(
            "() => {const i=document.querySelector('#a');return [i.naturalWidth,i.naturalHeight,i.complete]}")
        out["doc_title_after_img"] = pg.title()
        out["top_has_svg_var"] = pg.evaluate("() => !!window.__SVG_SCRIPT_RAN__")
        # screenshot pdf iframe & count non-white pixels
        shot = pg.locator("#p").screenshot()
        out["pdf_shot_bytes"] = len(shot)
        png = Path(TMP / "pdfshot.png"); png.write_bytes(shot)
        # 用 fitz 判断是否空白（PDF 插件渲染出的内容里有文字/线条）
        try:
            import fitz
            doc = fitz.open(str(png))
            pix = doc[0].get_pixmap()
            samples = set()
            for y in range(0, pix.height, 7):
                for x in range(0, pix.width, 7):
                    samples.add(pix.pixel(x, y))
                    if len(samples) > 6: break
            out["pdf_shot_distinct_colors"] = len(samples)
            doc.close()
        except Exception as e:
            out["pdf_shot_err"] = str(e)[:120]
        out["pdf_title"] = ""
        for f in pg.frames:
            if "/sample.pdf" in (f.url or ""):
                try: out.setdefault("pdf_frame_titles", []).append((f.url, f.title()[:40]))
                except Exception: pass
        b.close()
    srv.shutdown()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
