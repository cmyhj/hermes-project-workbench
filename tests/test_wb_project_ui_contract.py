"""wb-project 前端契约（纯源码断言，**不需要 dashboard**，因此不会 skip）。

把「归档是物理位置」「切换开关必须重拉」这些约定钉在源码上：
它们一旦被改回去，UI 会静默变成「只看 .pm 标记」或「归档项根本不在响应里」，
而这两种退化在有 dashboard 的浏览器用例里也要靠人眼看得出，不如直接断言源码。

    venv/Scripts/python.exe -m pytest tests/test_wb_project_ui_contract.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "plugin" / "dashboard"
JS = PLUGIN / "dist" / "index.js"
CSS = PLUGIN / "dist" / "style.css"
PY = PLUGIN / "plugin_api.py"


@pytest.fixture(scope="module")
def js() -> str:
    assert JS.is_file(), JS
    return JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    assert CSS.is_file(), CSS
    return CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def py() -> str:
    assert PY.is_file(), PY
    return PY.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 前端
def test_archived_filter_uses_backend_flag(js):
    """列表按后端返回的 ``archived`` 过滤，不再自己判 .pm 标记。"""
    assert "include_archived=" in js
    assert re.search(r"function \(p\) \{ return !p\.archived; \}", js), \
        "「隐藏已归档」必须按后端 archived 字段过滤"
    assert "pm.archived" not in js, "前端不该自己读 .pm 的 archived 标记"


def test_toggle_refetches_with_include_archived(js):
    """切换「隐藏已归档」必须重新请求 —— 归档项在另一个根里，首屏响应里没有。"""
    blocks = re.findall(r"\}, \[onlyOpen\]\);", js)
    assert len(blocks) >= 2, "reload 与切换 effect 都应依赖 onlyOpen"
    assert js.count("include_archived=") >= 3, "三处请求都应带 include_archived"


def test_archive_origin_and_anomaly_are_visible(js):
    """归档来源与「未迁移」必须在 UI 上有落点，不能被静默吞掉。"""
    assert "-tag-archive" in js, "归档项目应显示来源角标"
    assert "archive_anomaly" in js, "未真正迁移必须显示"
    assert "-warnline" in js, "异常需要有可见的一行说明"
    assert "归档于" in js, "页头应说明归档位置"
    # 跨根同名时角标必须带根标签，否则列表里两行同名分不清
    assert "id_conflict_with_active" in js


def test_roots_are_surfaced_in_sidebar(js):
    """归档根清单要在界面上可见：让「归档在哪」是事实，不是代码里的常量。"""
    assert "projects.roots" in js
    assert "-rootlist" in js
    assert "不可达" in js, "登记了但不存在的根应显式标注"


def test_chat_context_carries_real_directory(js):
    """对话上下文里的 cwd 与路径说明用后端给的 dir_path（含归档路径）。"""
    assert "cwd: current.dir_path" in js
    assert "项目位置：" in js
    assert 'current.dir_path + "/" + current.pm_file' in js


def test_shared_chat_loading_is_untouched(js):
    """共享聊天组件的加载路径没被这轮改动碰过 —— 对话不能被归档改造带崩。"""
    assert "/dashboard-plugins/wb-project/dist/vendor/" in js
    assert "wb-agent-chat.js" in js and "wb-agent-chat.css" in js
    assert "api.makePanel(SDK)" in js
    assert "__WB_CHAT_READY__" in js


def test_project_content_sections_and_todo_order(js):
    """Selecting a project shows active tasks due first, with undated work last."""
    gantt = js.index('className: NS + "-content-section " + NS + "-gantt-section"')
    files = js.index("filesOpen ? h(ProjectFilesPanel")
    zotero = js.index('className: NS + "-content-section " + NS + "-zotero-section"')
    assert gantt < files < zotero
    assert 'className: NS + "-recent-todos"' in js
    assert 'filter(function (t) { return t.status_group !== "done"; })' in js
    assert 'var ad = a.due || "9999-99-99", bd = b.due || "9999-99-99"' in js
    assert '"aria-expanded": ganttOpen' in js
    assert 'ganttOpen ? h(React.Fragment' in js


def test_file_browser_is_inline_and_create_is_layered(js, css):
    assert "function ProjectFilesPanel(props)" in js
    assert "filesOpen ? h(ProjectFilesPanel" in js
    assert "-modal-layer" in js
    assert ".wbp-modal-layer {" in css
    assert "position: fixed" in css.split(".wbp-modal-layer {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 32%) minmax(0, 1fr)" in css
    assert ".wbp-panels {" not in css


def test_create_manage_entry_is_in_project_sidebar(js):
    assert "-sidebar-create" in js
    assert '"新建/纳管"' in js
    # The empty project state also has a sidebar, so first use remains reachable.
    assert 'NS + "-side-count" }, "0"' in js


# ---------------------------------------------------------------- CSS
def test_archive_badges_have_styles(css):
    for cls in (".wbp-tag-archive", ".wbp-tag-warn", ".wbp-warnline",
                ".wbp-rootlist", ".wbp-rootitem", ".wbp-rootitem-missing"):
        assert cls in css, f"缺少样式 {cls}"


def test_badges_do_not_break_list_layout(css):
    """角标是行内元素，不能把 260px 的项目列表撑高/撑破。"""
    block = css.split(".wbp-tag {", 1)[1].split("}", 1)[0]
    assert "inline-block" in block
    assert "white-space: nowrap" in block


# ---------------------------------------------------------------- 后端契约
def test_archived_is_location_derived_in_backend(py):
    assert "is_archived = location == \"archive\"" in py
    assert "pm_archived" in py
    assert "archive_anomaly" in py


def test_roots_endpoint_has_no_path_parameter(py):
    """``/roots`` 只能是只读无参端点：不得出现「客户端传路径来加根」的入口。"""
    body = py.split('@router.get("/roots")', 1)[1].split("@router.", 1)[0]
    assert "Query(" not in body, "/roots 不得接受任何查询参数"
    assert "path:" not in body


def test_scan_is_shallow_and_skips_links(py):
    assert "_is_link" in py
    assert "realpath" in py, "必须校验 realpath 仍在根内（防目录逃逸）"
    assert "glob(\"*.pm\")" in py
    # 不递归：实现里不应出现 rglob 扫 .pm
    assert 'rglob("*' not in py, "不得递归扫 .pm（课程资料树会被扫穿）"


def test_no_write_operations_touch_project_files(py):
    """插件对项目目录只读：没有 shutil.move / unlink / write_text 到 .pm 的路径。"""
    for forbidden in ("shutil.move", "shutil.rmtree", ".unlink()", "os.remove("):
        assert forbidden not in py, f"出现可疑写/删操作：{forbidden}"
