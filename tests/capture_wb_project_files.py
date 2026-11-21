"""截屏留证：375 / 1280，明暗两个主题，文件面板 + 新建面板。

    WB_PROJECT_UI=http://127.0.0.1:9147 venv/Scripts/python.exe tests/capture_wb_project_files.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = (os.environ.get("WB_PROJECT_UI") or "http://127.0.0.1:9147").rstrip("/")
OUT = Path(os.environ.get("WB_SHOTS_DIR") or (Path(__file__).resolve().parent.parent / "notes" / "wb-project-shots"))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        for width, height in ((1280, 1000), (375, 900)):
            for theme in ("dark", "light"):
                ctx = b.new_context(viewport={"width": width, "height": height},
                                    color_scheme=theme)
                pg = ctx.new_page()
                pg.goto(BASE + "/projects", wait_until="networkidle")
                pg.wait_for_selector(".wbp-gantt", timeout=30000)
                pg.wait_for_timeout(2000)
                pg.locator(".wbp-projitem").first.click()
                pg.wait_for_timeout(1200)
                if pg.locator(".wbp-files").count() == 0:
                    pg.get_by_role("button", name="文件").click()
                pg.wait_for_timeout(2200)
                # 选一个文本文件做预览
                txt = pg.locator(".wbp-ficon-text")
                if txt.count():
                    txt.first.click()
                    pg.wait_for_timeout(1500)
                f = OUT / f"files-{width}-{theme}.png"
                pg.screenshot(path=str(f), full_page=True)
                made.append(f)

                if pg.locator(".wbp-create").count() == 0:
                    pg.get_by_role("button", name="新建/纳管").click()
                    pg.wait_for_timeout(1800)
                f2 = OUT / f"create-{width}-{theme}.png"
                pg.screenshot(path=str(f2), full_page=True)
                made.append(f2)
                ctx.close()
        b.close()
    for f in made:
        print(f"{f}  {f.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
