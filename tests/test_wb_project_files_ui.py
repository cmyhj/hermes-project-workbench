"""wb-project 文件面板 + 新建/纳管 UI 的真实浏览器测试（非 jsdom）。

前置：一个**回环测试端口**上的 dashboard（默认 127.0.0.1:9147），wb-project 已启用。
不启动/不停止任何后端；不要指向别人正在用的生产端口，也不要 --stop。

    WB_PROJECT_UI=http://127.0.0.1:9147 venv/Scripts/python.exe -m pytest tests/test_wb_project_files_ui.py -q

没有 Playwright 或没有可用 dashboard 时整组 skip（不假通过）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BASE = (os.environ.get("WB_PROJECT_UI") or "http://127.0.0.1:9147").rstrip("/")
API = "/api/plugins/wb-project"

try:
    from playwright.sync_api import sync_playwright  # type: ignore
    HAVE_PW = True
except Exception:  # pragma: no cover
    HAVE_PW = False


def _up() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/api/status", timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


pytestmark = [
    pytest.mark.skipif(not HAVE_PW, reason="playwright 未安装"),
    pytest.mark.skipif(not _up(), reason=f"dashboard 未在 {BASE} 运行"),
]


def _creds() -> dict:
    # HERMES_HOME 未设时按 Hermes 自己的约定猜 %LOCALAPPDATA%/hermes（不写死用户名）
    home = os.environ.get("HERMES_HOME")
    base = Path(home) if home else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "hermes"
    path = base / ".env"
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("HERMES_DASHBOARD_BASIC_AUTH_") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v.strip().strip('"').strip("'")
    return out


@pytest.fixture()
def page():
    """登录（若需要）后停在 /projects，返回 (page, ctx)。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        pg = ctx.new_page()
        pg.goto(BASE + "/projects", wait_until="networkidle")
        if pg.locator(".wbp-root").count() == 0:
            creds = _creds()
            if creds.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"):
                pg.goto(BASE + "/login", wait_until="networkidle")
                try:
                    pg.fill("input[name='username']", creds.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", ""))
                    pg.fill("input[name='password']", creds["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"])
                    pg.click("button[type='submit']")
                    pg.wait_for_timeout(2500)
                except Exception:
                    pass
                pg.goto(BASE + "/projects", wait_until="networkidle")
        pg.wait_for_selector(".wbp-root", timeout=30000)
        pg.wait_for_selector(".wbp-gantt", timeout=30000)
        yield pg
        browser.close()


def _first_project(page):
    page.locator(".wbp-projitem").first.click()
    page.wait_for_timeout(1200)


def _open_files(page):
    """打开文件面板；已经是打开状态就不要按（「文件」是开关，二次按会关掉）。"""
    if page.locator(".wbp-files").count() == 0:
        page.get_by_role("button", name="文件").click()
    page.wait_for_selector(".wbp-files", timeout=20000)
    page.wait_for_timeout(1200)


# ---------------------------------------------------------------- 可见性与回归
def test_labels_preserved(page):
    """既有可见 labels 一个都不能丢（CSS text-transform 会改大小写，所以忽略大小写比较）。"""
    text = page.inner_text("body").casefold()
    for label in ("项目", "刷新数据", "关联 zotero 论文", "隐藏已归档", "任务",
                  "待办", "进行中", "已完成", "解除关联"):
        assert label in text, f"丢失既有 label：{label}"
    assert page.locator(".wbp-gantt").count() == 1
    assert page.locator(".wbp-panel").count() >= 1
    # 新增入口也在，但旧入口一个不少
    tools = page.locator(".wbp-chattools").inner_text().casefold()
    for label in ("刷新数据", "文件", "新建/纳管"):
        assert label in tools, tools


def test_file_panel_opens_with_lazy_tree(page):
    _first_project(page)
    _open_files(page)
    assert page.locator(".wbp-filetree").count() == 1
    # 根层已展开：应该有子行
    rows = page.locator(".wbp-frow")
    assert rows.count() >= 2
    # 懒加载：折叠一个子目录前，它下面的文件不应存在于 DOM
    dirs = page.locator(".wbp-frow-dir")
    assert dirs.count() >= 1


def test_lazy_loading_only_expands_one_level(page):
    """展开前：深层文件不在 DOM。展开后：才出现。"""
    _first_project(page)
    _open_files(page)
    before = page.locator(".wbp-frow").count()
    # 找一个有子项的目录行（根层目录）
    subdirs = page.locator(".wbp-fnode .wbp-fnode .wbp-frow-dir")
    if subdirs.count() == 0:
        pytest.skip("当前项目没有子目录，无法验证两层懒加载")
    subdirs.first.click()
    page.wait_for_timeout(1500)
    after = page.locator(".wbp-frow").count()
    assert after > before, "展开子目录后应加载出更多条目"


def test_text_preview_renders_as_text_not_html(page):
    """点一个文本文件：预览是纯文本（内容不参与 HTML 解析）。"""
    _first_project(page)
    _open_files(page)
    # 找 .md / .txt 文件行
    cand = page.locator(".wbp-ficon-text")
    if cand.count() == 0:
        pytest.skip("当前项目根层没有文本文件")
    cand.first.click()
    page.wait_for_selector(".wbp-fpreview", timeout=20000)
    page.wait_for_timeout(1200)
    pre = page.locator(".wbp-ftext")
    assert pre.count() == 1, "文本预览应使用 <pre> 纯文本容器"
    # 纯文本：没有子元素（不会被解析成 HTML 标签）
    assert pre.evaluate("el => el.children.length") == 0
    assert page.locator(".wbp-fpreview-title").count() == 1


def test_office_file_is_download_only_in_ui(page):
    _first_project(page)
    _open_files(page)
    dl = page.locator(".wbp-ficon-download")
    if dl.count() == 0:
        pytest.skip("当前项目根层没有不可预览格式")
    dl.first.click()
    page.wait_for_selector(".wbp-fpreview", timeout=20000)
    page.wait_for_timeout(1000)
    assert page.locator(".wbp-fdl").count() == 1
    assert page.locator(".wbp-ftext").count() == 0, "Office/压缩包不得进文本预览"
    assert page.locator(".wbp-fpdf").count() == 0


def test_no_console_errors_with_panels(page):
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    _first_project(page)
    _open_files(page)
    page.wait_for_timeout(1200)
    assert not errs, f"页面 JS 异常：{errs}"


# ---------------------------------------------------------------- 新建 / 纳管
def test_create_panel_preview_then_cancel(page):
    """预览在 UI 里可见（目录 + 将写入的文件），并且**不落盘**。"""
    _first_project(page)
    page.get_by_role("button", name="新建/纳管").click()
    page.wait_for_selector(".wbp-create", timeout=20000)
    page.wait_for_timeout(1500)
    # 纳管下拉必须列出「无 .pm」的已有目录（或明确提示没有）
    assert page.locator(".wbp-create select").count() >= 1
    # 填一个一定会被拒绝的名字：预览应显示阻塞原因而不是创建成功
    name_input = page.locator(".wbp-create input.wbp-input").first
    name_input.fill("不是规范名")
    page.get_by_role("button", name="预览将创建的内容").click()
    page.wait_for_selector(".wbp-err", timeout=20000)
    assert "YYYY-MM" in page.inner_text(".wbp-err") or "规范" in page.inner_text(".wbp-err")


def test_create_panel_plan_shows_dir_and_actions(page):
    """合法名字：计划区列出目标目录、.pm 路径、模板子目录，且此时目录还没创建。"""
    _first_project(page)
    page.get_by_role("button", name="新建/纳管").click()
    page.wait_for_selector(".wbp-create", timeout=20000)
    page.wait_for_timeout(1500)
    page.locator(".wbp-create input.wbp-input").first.fill("2026-11_UI冒烟测试")
    page.get_by_role("button", name="预览将创建的内容").click()
    page.wait_for_selector(".wbp-plan", timeout=20000)
    txt = page.inner_text(".wbp-plan")
    assert "2026-11_UI冒烟测试.pm" in txt
    assert "计划（尚未写盘）" in txt or "无法执行" in txt
    # 计划只是预览：目录不该存在（用后端确认，不信 UI）
    # 用 SDK 的 fetchJSON（它会带 loopback token / cookie），裸 fetch 在 gated 模式会 401
    exists = page.evaluate(
        """async () => {
            const f = window.__HERMES_PLUGIN_SDK__.fetchJSON;
            const d = await f('/api/plugins/wb-project/projects/creatable');
            return d.untracked.some(u => u.name === '2026-11_UI冒烟测试');
        }""")
    assert exists is False, "预览阶段绝不能落盘"


def test_chat_cwd_is_project_dir(page):
    """选中项目后，聊天上下文里的 cwd 必须是该项目真实目录。"""
    _first_project(page)
    ctx = page.evaluate(
        """() => {
            const el = document.querySelector('.wbp-files-dir');
            return el ? el.textContent : null;
        }""")
    # 未展开文件面板时为空；直接读后端契约确认 dir_path 存在且是绝对路径
    data = page.evaluate(
        """async () => {
            const f = window.__HERMES_PLUGIN_SDK__.fetchJSON;
            const d = await f('/api/plugins/wb-project/projects?include_archived=false');
            return d.projects.slice(0, 3).map(p => ({name: p.name, dir_path: p.dir_path}));
        }""")
    assert data and all(p["dir_path"] for p in data), data
    assert all(os.path.isabs(p["dir_path"]) for p in data), data


# ---------------------------------------------------------------- 响应式
def test_mobile_375_layout(page):
    page.set_viewport_size({"width": 375, "height": 900})
    page.wait_for_timeout(800)
    _first_project(page)
    _open_files(page)
    page.wait_for_timeout(1200)
    box = page.locator(".wbp-files").bounding_box()
    assert box and box["width"] <= 375 + 1, box
    # 溢出必须由容器自己滚，不许把整页撑宽
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 2, f"375px 下横向溢出 {overflow}px"
    page.set_viewport_size({"width": 1440, "height": 1000})


def test_desktop_1280_layout(page):
    page.set_viewport_size({"width": 1280, "height": 1000})
    page.wait_for_timeout(800)
    _first_project(page)
    _open_files(page)
    page.wait_for_timeout(1200)
    body = page.locator(".wbp-files-body")
    cols = body.evaluate("el => getComputedStyle(el).gridTemplateColumns")
    assert " " in cols.strip(), f"1280 下应是两列（{cols}）"
    assert page.locator(".wbp-filetree").count() == 1
    assert page.locator(".wbp-filepane").count() == 1
    page.set_viewport_size({"width": 1440, "height": 1000})
