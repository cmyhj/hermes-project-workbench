"""部署级 E2E 验收（真实部署副本 + 真实浏览器，只读，不写用户数据）。

与 `test_wb_project_files_ui.py`（按 UI 行为断言）的分工：
本文件走**与前端完全相同的取数路径**（`SDK.fetchJSON` / `SDK.authedFetch`），
逐格式确认「真的渲染出来了」，而不是「按钮点了没报错」。

    WB_PROJECT_UI=http://127.0.0.1:9147 python -m pytest tests/test_wb_project_e2e.py -q

前置：一个回环端口上的 dashboard（`hermes dashboard --port <非生产端口>`）。
缺环境时整组 skip。**不调用 /projects/create**，所以不会产生任何目录。
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

BASE = (os.environ.get("WB_PROJECT_UI") or "").rstrip("/")

try:
    from playwright.sync_api import sync_playwright  # type: ignore
    HAVE_PW = True
except Exception:  # pragma: no cover
    HAVE_PW = False


def _up() -> bool:
    if not BASE:
        return False
    try:
        with urllib.request.urlopen(BASE + "/api/status", timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


pytestmark = [
    pytest.mark.skipif(not HAVE_PW, reason="playwright 未安装"),
    pytest.mark.skipif(not _up(), reason="未设置 WB_PROJECT_UI 或该 dashboard 未运行"),
]


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1400, "height": 1000})
        pg = ctx.new_page()
        pg.goto(BASE + "/projects", wait_until="networkidle")
        pg.wait_for_selector(".wbp-gantt", timeout=30000)
        pg.wait_for_timeout(2000)
        yield pg
        b.close()


def test_gantt_and_list_render(page):
    assert page.locator(".wbp-gantt").count() == 1
    assert page.locator(".wbp-projitem").count() >= 1


def test_lazy_tree_loads_one_more_level(page):
    """展开一个子目录后条目变多，证明是逐层请求而不是一次性整树。"""
    page.locator(".wbp-projitem").first.click()
    page.wait_for_timeout(1200)
    if page.locator(".wbp-files").count() == 0:
        page.get_by_role("button", name="文件").click()
    page.wait_for_timeout(2000)
    before = page.locator(".wbp-frow").count()
    sub = page.locator(".wbp-fnode .wbp-fnode .wbp-frow-dir")
    if sub.count() == 0:
        pytest.skip("当前项目根层没有子目录")
    sub.first.click()
    page.wait_for_timeout(1800)
    assert page.locator(".wbp-frow").count() > before


def test_image_preview_really_paints(page):
    """图片走 authedFetch → blob → <img>，必须真的画出像素（naturalWidth > 0）。"""
    data = page.evaluate(
        """async () => {
          const f = window.__HERMES_PLUGIN_SDK__.fetchJSON;
          const af = window.__HERMES_PLUGIN_SDK__.authedFetch || fetch;
          const list = await f('/api/plugins/wb-project/projects?include_archived=true');
          const walk = async (pid, path, depth) => {
            const t = await f('/api/plugins/wb-project/projects/' + encodeURIComponent(pid) +
                              '/tree' + (path ? '?path=' + encodeURIComponent(path) : ''));
            for (const e of (t.entries || [])) {
              if (e.category === 'image') return e;
              if (e.kind === 'dir' && depth < 3) {
                const hit = await walk(pid, e.path, depth + 1);
                if (hit) return hit;
              }
            }
            return null;
          };
          for (const pr of list.projects) {
            let img;
            try { img = await walk(pr.id, '', 0); } catch (e) { continue; }
            if (!img) continue;
            const meta = await f('/api/plugins/wb-project/projects/' + encodeURIComponent(pr.id) +
                                 '/file?path=' + encodeURIComponent(img.path));
            const r = await af(meta.raw_url);
            const bl = await r.blob();
            const el = new Image(); el.src = URL.createObjectURL(bl);
            await new Promise(res => { el.onload = res; el.onerror = res; });
            return { project: pr.id, archived: pr.archived, path: img.path, kind: meta.kind,
                     bytes: meta.bytes, painted: el.naturalWidth > 0,
                     w: el.naturalWidth, blobType: bl.type, blobSize: bl.size,
                     status: r.status, nosniff: r.headers.get('x-content-type-options') };
          }
          return null;
        }""")
    if data is None:
        pytest.skip("已登记的项目里找不到图片文件")
    assert data["kind"] == "image", data
    assert data["status"] == 200
    assert data["nosniff"] == "nosniff"
    assert data["painted"] is True, f"图片没画出来：{data}"
    assert data["blobType"].startswith("image/"), data
    assert data["blobSize"] == data["bytes"], "blob 大小必须等于文件字节数"


def test_pdf_is_previewable_and_inline(page):
    data = page.evaluate(
        """async () => {
          const f = window.__HERMES_PLUGIN_SDK__.fetchJSON;
          const list = await f('/api/plugins/wb-project/projects?include_archived=true');
          const walk = async (pid, path, depth) => {
            const t = await f('/api/plugins/wb-project/projects/' + encodeURIComponent(pid) +
                              '/tree' + (path ? '?path=' + encodeURIComponent(path) : ''));
            for (const e of (t.entries || [])) {
              if (e.category === 'pdf') return e;
              if (e.kind === 'dir' && depth < 3) {
                const hit = await walk(pid, e.path, depth + 1);
                if (hit) return hit;
              }
            }
            return null;
          };
          for (const pr of list.projects) {
            let pdf;
            try { pdf = await walk(pr.id, '', 0); } catch (e) { continue; }
            if (!pdf) continue;
            const meta = await f('/api/plugins/wb-project/projects/' + encodeURIComponent(pr.id) +
                                 '/file?path=' + encodeURIComponent(pdf.path));
            const af = window.__HERMES_PLUGIN_SDK__.authedFetch || fetch;
            const r = await af(meta.raw_url);
            return { project: pr.id, path: pdf.path, kind: meta.kind, bytes: meta.bytes,
                     status: r.status, ctype: r.headers.get('content-type'),
                     disposition: (r.headers.get('content-disposition') || '') };
          }
          return null;
        }""")
    if data is None:
        pytest.skip("已登记的项目里找不到 PDF")
    assert data["kind"] == "pdf" and data["status"] == 200
    assert data["ctype"] == "application/pdf", data
    # PDF 必须 inline：attachment 会让 iframe 变空白（实测）
    assert data["disposition"].startswith("inline"), data


def test_office_is_attachment_octet_stream_live(page):
    data = page.evaluate(
        """async () => {
          const f = window.__HERMES_PLUGIN_SDK__.fetchJSON;
          const list = await f('/api/plugins/wb-project/projects?include_archived=true');
          const exts = ['.docx', '.xlsx', '.pptx', '.zip', '.doc', '.xls', '.ppt', '.pdf.bak'];
          const walk = async (pid, path, depth) => {
            const t = await f('/api/plugins/wb-project/projects/' + encodeURIComponent(pid) +
                              '/tree' + (path ? '?path=' + encodeURIComponent(path) : ''));
            for (const e of (t.entries || [])) {
              if (e.kind === 'file' && exts.includes(e.ext)) return e;
              if (e.kind === 'dir' && depth < 3) {
                const hit = await walk(pid, e.path, depth + 1);
                if (hit) return hit;
              }
            }
            return null;
          };
          for (const pr of list.projects) {
            let hit;
            try { hit = await walk(pr.id, '', 0); } catch (e) { continue; }
            if (!hit) continue;
            const af = window.__HERMES_PLUGIN_SDK__.authedFetch || fetch;
            const url = '/api/plugins/wb-project/projects/' + encodeURIComponent(pr.id) +
                        '/raw?path=' + encodeURIComponent(hit.path) + '&inline=1';
            const r = await af(url);
            return { path: hit.path, ext: hit.ext, category: hit.category, status: r.status,
                     ctype: r.headers.get('content-type'),
                     disposition: (r.headers.get('content-disposition') || ''),
                     nosniff: r.headers.get('x-content-type-options') };
          }
          return null;
        }""")
    if data is None:
        pytest.skip("已登记的项目里找不到 Office/压缩包文件")
    assert data["status"] == 200
    assert data["category"] == "download", data
    assert data["ctype"] == "application/octet-stream", data
    assert data["disposition"].startswith("attachment"), data
    assert data["nosniff"] == "nosniff"


def test_no_console_errors(page):
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".wbp-gantt", timeout=30000)
    page.wait_for_timeout(1800)
    assert not errs, errs
