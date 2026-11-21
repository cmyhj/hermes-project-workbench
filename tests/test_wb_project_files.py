"""wb-project 文件浏览 / 预览 / 下载 / 创建纳管 —— 后端安全与行为测试。

**走真实 HTTP**（FastAPI TestClient），不是直接调函数：状态码、查询参数校验、
响应头（nosniff / CSP / Content-Disposition）都必须在传输层为真。

**全部数据都在 tmp 目录**：不读不写任何真实项目根，也不碰真实 plugin-data。

    venv/Scripts/python.exe -m pytest tests/test_wb_project_files.py -q

覆盖点（每条对应实现里的一条安全约束）：
1. 目录穿越：``..``、绝对路径、盘符、反斜杠、NUL、控制字符一律 4xx
2. 链接逃逸：symlink / Windows junction 一律 403
3. 敏感文件：.env / .git / id_rsa / *.pem / auth.json 不出现在 tree，也拿不到 file/raw
4. 大小限制：超上限的文件只给下载；文本超限只截断并标记
5. UTF-8 / GB18030 兜底解码；二进制伪装成文本 → 降级为下载
6. 超大目录分页：offset/limit、total/has_more 如实，越界拒绝
7. 懒加载：tree 每次只返回一层（不是整树递归）
8. raw 响应头：nosniff / CSP sandbox / CORP / inline vs attachment
9. Office、压缩包、可执行、未知二进制 → attachment + octet-stream
10. 归档项目可浏览但只读；新建/纳管只在活动根
11. 创建/纳管：同名拒绝、Windows 保留名、YYYY-MM_ 规范、plan_id 校验、
    原子写入、存在 .pm 不覆盖、落盘后读回校验
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_API = Path(os.environ.get(
    "WB_PROJECT_API",
    REPO_ROOT / "plugin" / "dashboard" / "plugin_api.py",
))
WIN = os.name == "nt"
PREFIX = "/api/plugins/wb-project"


def _load_module():
    name = f"wb_project_files_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_API)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _pm(name: str, tasks: int = 3, archived: bool = False) -> dict:
    return {
        "version": "1.0",
        "schema_version": 3,
        "project": {
            "id": f"pm-{name}", "name": name, "archived": archived,
            "start_date": "2026-10-01", "end_date": "2026-12-31",
            "created_at": "2026-10-01T00:00:00", "updated_at": "2026-11-01T10:00:00",
            "color": "#3366cc", "template": "todo",
            "kanban_columns": ["待办", "进行中", "已完成"],
        },
        "tasks": [{"id": f"t{i}", "title": f"{name} 任务 {i}", "status": "进行中",
                   "priority": "medium", "due_date": f"2026-11-{10 + i:02d}",
                   "subtasks": [], "tags": [], "dependencies": []} for i in range(tasks)],
        "milestones": [],
    }


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8", newline="\n")
    return path


@pytest.fixture()
def world(tmp_path, monkeypatch):
    active = tmp_path / "active"
    archive = tmp_path / "archive"
    active.mkdir()
    archive.mkdir()

    name = "2026-11_测试项目"
    proj = active / name
    proj.mkdir()
    _write(proj / f"{name}.pm", json.dumps(_pm(name), ensure_ascii=False))

    _write(proj / "README.md", "# 测试项目\n\n中文正文 hello\n")
    _write(proj / "notes.txt", "line1\nline2\n")
    _write(proj / "data.json", json.dumps({"a": 1, "b": "中文"}, ensure_ascii=False))
    _write(proj / "gbk.txt", "中文 GBK 编码内容".encode("gb18030"))
    _write(proj / "binary.md", b"PK\x03\x04" + b"\x00" * 64)
    _write(proj / "pic.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    _write(proj / "icon.svg", '<svg xmlns="http://www.w3.org/2000/svg"><script>'
                             'window.__X__=1</script><rect width="4" height="4"/></svg>')
    _write(proj / "paper.pdf", b"%PDF-1.4\n%fake\n")
    _write(proj / "report.docx", b"PK\x03\x04docx")
    _write(proj / "bundle.zip", b"PK\x03\x04zip")
    _write(proj / "tool.exe", b"MZ\x90\x00")
    _write(proj / "empty.txt", "")

    sub = proj / "docs"
    sub.mkdir()
    _write(sub / "guide.md", "# guide\n")
    _write(sub / "deep" / "hidden.md", "# deep\n")

    # —— 敏感文件：都必须被服务端剔除
    _write(proj / ".env", "OPENAI_KEY=sk-should-never-leak\n")
    _write(proj / ".env.local", "X=1\n")
    _write(proj / "id_rsa", "private\n")
    _write(proj / "server.pem", "pem\n")
    _write(proj / "auth.json", "{}\n")
    _write(proj / "credentials.json", "{}\n")
    _write(proj / ".git" / "config", "[remote]\nurl=https://token@x/y\n")
    _write(proj / "node_modules" / "x.js", "x")
    _write(proj / "__pycache__" / "a.pyc", b"\x00")

    # —— 链接逃逸目标
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "secret.txt", "outside secret\n")
    link_ok = True
    try:
        (proj / "link_out").symlink_to(outside, target_is_directory=True)
        (proj / "link_file.txt").symlink_to(outside / "secret.txt")
    except (OSError, NotImplementedError):
        link_ok = False

    # —— 归档项目（另一个根，只读）
    arc_name = "2024-05_老项目"
    arc = archive / arc_name
    arc.mkdir()
    _write(arc / f"{arc_name}.pm", json.dumps(_pm(arc_name, archived=True), ensure_ascii=False))
    _write(arc / "old.txt", "old\n")

    # —— 纳管候选
    (active / "2026-10_待纳管").mkdir()
    _write(active / "2026-10_待纳管" / "keep.md", "keep me\n")
    (active / "scratch").mkdir()

    home = tmp_path / "hermes_home"
    (home / "plugin-data" / "wb-project").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("WB_PROJECT_MAX_RAW_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("WB_PROJECT_MAX_TEXT_BYTES", str(4096))
    monkeypatch.setenv("WB_PROJECT_MAX_IMAGE_BYTES", str(1024 * 1024))

    roots_cfg = tmp_path / "project-roots.json"
    roots_cfg.write_text(json.dumps({
        "active_root": str(active),
        "archive_roots": [{"path": str(archive), "label": "ProjectArchives", "kind": "backup"}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("WB_PROJECT_ROOTS_CONFIG", str(roots_cfg))

    api = _load_module()
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    client = TestClient(app)

    return {
        "api": api, "client": client, "active": active, "archive": archive,
        "proj": proj, "proj_id": name, "arc_id": arc_name,
        "tmp": tmp_path, "link_ok": link_ok, "home": home,
    }


def _names(payload):
    return {e["name"] for e in payload["entries"]}


def _h(resp):
    return {k.lower(): v for k, v in resp.headers.items()}


# ---------------------------------------------------------------- 懒加载
def test_tree_lists_one_level_only(world):
    c = world["client"]
    root = c.get(f"{PREFIX}/projects/{world['proj_id']}/tree").json()
    assert root["path"] == ""
    names = _names(root)
    assert "README.md" in names and "docs" in names
    assert "guide.md" not in names, "子目录内容不该出现在父层（懒加载而非整树）"
    assert "hidden.md" not in names

    docs = c.get(f"{PREFIX}/projects/{world['proj_id']}/tree", params={"path": "docs"}).json()
    assert _names(docs) == {"guide.md", "deep"}
    deep = c.get(f"{PREFIX}/projects/{world['proj_id']}/tree",
                 params={"path": "docs/deep"}).json()
    assert _names(deep) == {"hidden.md"}


def test_tree_dirs_first_then_casefold_name(world):
    data = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/tree").json()
    kinds = [e["kind"] for e in data["entries"]]
    assert kinds == sorted(kinds, key=lambda k: k != "dir")
    dirs = [e["name"] for e in data["entries"] if e["kind"] == "dir"]
    assert dirs == sorted(dirs, key=str.casefold)


def test_tree_entry_metadata(world):
    data = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/tree").json()
    by = {e["name"]: e for e in data["entries"]}
    assert by["README.md"]["category"] == "text"
    assert by["README.md"]["size"] > 0 and by["README.md"]["mtime"]
    assert by["pic.png"]["category"] == "image" and by["pic.png"]["mime"] == "image/png"
    assert by["icon.svg"]["category"] == "image", "SVG 归类为图片（走 <img>，脚本不执行）"
    assert by["paper.pdf"]["category"] == "pdf"
    assert by["report.docx"]["category"] == "download"
    assert by["bundle.zip"]["category"] == "download"
    assert by["tool.exe"]["category"] == "download"
    assert by["docs"]["category"] == "dir"


def test_tree_not_a_dir_is_400(world):
    r = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/tree",
                            params={"path": "README.md"})
    assert r.status_code == 400


# ---------------------------------------------------------------- 敏感文件
def test_sensitive_files_never_listed(world):
    data = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/tree",
                               params={"include_hidden": True}).json()
    names = _names(data)
    for bad in (".env", ".env.local", "id_rsa", "server.pem", "auth.json",
                "credentials.json", ".git", "node_modules", "__pycache__"):
        assert bad not in names, f"{bad} 泄漏到目录列表"


def test_sensitive_files_cannot_be_read_even_by_name(world):
    c = world["client"]
    for bad in (".env", "id_rsa", "server.pem", "auth.json", ".git/config", "credentials.json"):
        r = c.get(f"{PREFIX}/projects/{world['proj_id']}/file", params={"path": bad})
        assert r.status_code == 403, (bad, r.status_code)
        r2 = c.get(f"{PREFIX}/projects/{world['proj_id']}/raw", params={"path": bad})
        assert r2.status_code == 403, (bad, r2.status_code)


def test_hidden_toggle(world):
    _write(world["proj"] / ".notes.md", "hidden but harmless\n")
    c = world["client"]
    shown = _names(c.get(f"{PREFIX}/projects/{world['proj_id']}/tree",
                         params={"include_hidden": True}).json())
    assert ".notes.md" in shown
    hidden = _names(c.get(f"{PREFIX}/projects/{world['proj_id']}/tree").json())
    assert ".notes.md" not in hidden


# ---------------------------------------------------------------- 目录穿越
@pytest.mark.parametrize("bad", [
    "../outside/secret.txt", "docs/../../outside/secret.txt", "..",
    "/etc/passwd", "<system-path>/win.ini", "c:\\windows\\win.ini",
    "docs\\guide.md", "docs/\x00", "\x01bad", "..\\..\\x",
])
def test_path_traversal_rejected(world, bad):
    r = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/tree", params={"path": bad})
    assert r.status_code in (400, 403), (bad, r.status_code, r.text[:200])


def test_raw_rejects_absolute_and_parent(world):
    c = world["client"]
    for bad in ("/etc/passwd", "../outside/secret.txt", "<abs>/x.txt", "docs/../docs"):
        r = c.get(f"{PREFIX}/projects/{world['proj_id']}/raw", params={"path": bad})
        assert r.status_code in (400, 403), (bad, r.status_code)


def test_unknown_project_id_is_404(world):
    """绝不把 URL 字符串当路径：未知 id 一律 404。"""
    c = world["client"]
    for pid in ("no-such-project", "%2E%2E%2Factive", "<not-a-project>"):
        r = c.get(f"{PREFIX}/projects/{pid}/tree")
        assert r.status_code == 404, (pid, r.status_code)


def test_missing_project_dir_is_404(world):
    """目录被搬走的项目：一律 404（.pm 随目录一起消失，扫描不到 → id 解析不出来）。

    两种原因（id 不存在 / 目录不可达）都只允许 404，绝不给空列表假装目录是空的。
    """
    import shutil
    shutil.move(str(world["proj"]), str(world["tmp"] / "moved"))
    r = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/tree")
    assert r.status_code == 404
    assert r.json()["detail"]


def test_unreachable_dir_path_is_404_not_empty_list(world):
    """目录不在了但项目还能解析（storage 指向已删除目录）→ 明确 404，不是空 entries。"""
    api = world["api"]
    target = api.resolve_project(world["proj_id"])
    assert target is not None
    import shutil
    shutil.move(str(world["proj"]), str(world["tmp"] / "moved2"))
    # 项目索引仍能命中（monkeypatch resolve_project 回放旧对象）→ 走到「目录不可达」分支
    api.resolve_project = lambda pid: target if pid == world["proj_id"] else None
    from fastapi import FastAPI as _FA
    from fastapi.testclient import TestClient as _TC
    app = _FA(); app.include_router(api.router, prefix=PREFIX)
    r = _TC(app).get(f"{PREFIX}/projects/{world['proj_id']}/tree")
    assert r.status_code == 404
    assert "不可达" in r.json()["detail"]


# ---------------------------------------------------------------- 链接逃逸
def test_symlink_escape_is_refused(world):
    if not world["link_ok"]:
        pytest.skip("当前环境不允许建 symlink")
    c = world["client"]
    r = c.get(f"{PREFIX}/projects/{world['proj_id']}/tree", params={"path": "link_out"})
    assert r.status_code == 403
    r2 = c.get(f"{PREFIX}/projects/{world['proj_id']}/file",
               params={"path": "link_file.txt"})
    assert r2.status_code == 403


@pytest.mark.skipif(not WIN, reason="junction 只在 Windows 上存在")
def test_windows_junction_escape_is_refused(world):
    outside = world["tmp"] / "outside"
    junction = world["proj"] / "junc_out"
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                          capture_output=True, text=True, errors="replace")
    if proc.returncode != 0 or not junction.exists():
        pytest.skip(f"无法创建 junction：{proc.stdout}{proc.stderr}")
    r = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/tree",
                            params={"path": "junc_out"})
    assert r.status_code == 403, r.text[:200]
    assert world["api"]._is_link(junction) is True


# ---------------------------------------------------------------- 文本预览
def test_text_preview_plain(world):
    f = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/file",
                            params={"path": "README.md"}).json()
    assert f["kind"] == "text"
    assert "测试项目" in f["text"]
    assert f["render"] == "textContent", "契约：前端只做纯文本渲染"
    assert f["encoding"] == "utf-8" and f["truncated"] is False
    assert f["download_url"].endswith("download=1")


def test_text_preview_gb18030_fallback(world):
    f = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/file",
                            params={"path": "gbk.txt"}).json()
    assert f["kind"] == "text" and f["encoding"] == "gb18030"
    assert f["decode_error"] and "GB18030" in f["decode_error"]
    assert "GBK" in f["text"]


def test_binary_disguised_as_text_degrades_to_download(world):
    f = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/file",
                            params={"path": "binary.md"}).json()
    assert f["kind"] == "download" and f["download_only"] is True
    assert "NUL" in f["reason"] or "解码失败" in f["reason"]


def test_text_truncation_is_labelled(world):
    _write(world["proj"] / "long.txt", "a" * 10_000)
    f = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/file",
                            params={"path": "long.txt"}).json()
    assert f["kind"] == "text" and f["truncated"] is True
    assert f["shown_bytes"] == 4096 and f["bytes"] == 10_000
    assert "超过预览上限" in f["notice"]


def test_empty_text_file(world):
    f = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/file",
                            params={"path": "empty.txt"}).json()
    assert f["kind"] == "text" and f["text"] == ""


# ---------------------------------------------------------------- 大小 / 格式
def test_oversized_file_download_only(world):
    _write(world["proj"] / "too-big.log", b"z" * (3 * 1024 * 1024))
    c = world["client"]
    f = c.get(f"{PREFIX}/projects/{world['proj_id']}/file",
              params={"path": "too-big.log"}).json()
    assert f["kind"] == "download" and f["download_only"] is True
    assert "超" in f["reason"]
    r = c.get(f"{PREFIX}/projects/{world['proj_id']}/raw", params={"path": "too-big.log"})
    assert r.status_code == 413


def test_office_and_binary_are_download_only(world):
    c = world["client"]
    for name in ("report.docx", "bundle.zip", "tool.exe"):
        f = c.get(f"{PREFIX}/projects/{world['proj_id']}/file", params={"path": name}).json()
        assert f["kind"] == "download" and f["download_only"] is True, name
        assert "只能下载" in f["reason"], name


def test_image_and_pdf_are_previewable(world):
    c = world["client"]
    img = c.get(f"{PREFIX}/projects/{world['proj_id']}/file", params={"path": "pic.png"}).json()
    assert img["kind"] == "image" and img["mime"] == "image/png" and img["raw_url"]
    svg = c.get(f"{PREFIX}/projects/{world['proj_id']}/file", params={"path": "icon.svg"}).json()
    assert svg["kind"] == "image" and svg["mime"] == "image/svg+xml"
    pdf = c.get(f"{PREFIX}/projects/{world['proj_id']}/file", params={"path": "paper.pdf"}).json()
    assert pdf["kind"] == "pdf" and pdf["preview_supported"] is True
    assert "阅读器" in pdf["notice"]


# ---------------------------------------------------------------- 分页
def test_large_directory_pagination(world):
    bulk = world["proj"] / "many"
    bulk.mkdir()
    for i in range(60):
        _write(bulk / f"f{i:03d}.txt", str(i))
    c = world["client"]
    p1 = c.get(f"{PREFIX}/projects/{world['proj_id']}/tree",
               params={"path": "many", "offset": 0, "limit": 25}).json()
    assert p1["total"] == 60 and p1["returned"] == 25 and p1["has_more"] is True
    p3 = c.get(f"{PREFIX}/projects/{world['proj_id']}/tree",
               params={"path": "many", "offset": 50, "limit": 25}).json()
    assert p3["returned"] == 10 and p3["has_more"] is False
    seen = [e["name"] for e in p1["entries"]] + [e["name"] for e in p3["entries"]]
    assert len(set(seen)) == len(seen), "分页不能重复条目"


def test_pagination_bounds(world):
    c = world["client"]
    base = f"{PREFIX}/projects/{world['proj_id']}/tree"
    assert c.get(base, params={"offset": -1}).status_code == 422
    assert c.get(base, params={"limit": 0}).status_code == 422
    assert c.get(base, params={"limit": 10_000}).status_code == 422


# ---------------------------------------------------------------- raw 头
def test_raw_png_is_inline_with_hardening(world):
    r = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/raw",
                            params={"path": "pic.png"})
    assert r.status_code == 200
    h = _h(r)
    assert h["content-type"].startswith("image/png")
    assert h["content-disposition"].startswith("inline")
    assert h["x-content-type-options"] == "nosniff"
    csp = h["content-security-policy"]
    assert "sandbox" in csp and "default-src 'none'" in csp
    assert h["cross-origin-resource-policy"] == "same-origin"


def test_raw_svg_inline_but_script_cannot_run(world):
    """SVG 保持原样（可 <img> 预览），安全性来自 sandbox CSP + <img> 渲染。"""
    r = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/raw",
                            params={"path": "icon.svg"})
    h = _h(r)
    assert h["content-type"].startswith("image/svg+xml")
    assert "sandbox" in h["content-security-policy"]
    assert "__X__" in r.text


def test_raw_pdf_is_inline(world):
    r = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/raw",
                            params={"path": "paper.pdf"})
    h = _h(r)
    assert h["content-type"] == "application/pdf"
    assert h["content-disposition"].startswith("inline")
    assert h["x-content-type-options"] == "nosniff"


def test_raw_office_is_attachment_octet_stream(world):
    c = world["client"]
    for name in ("report.docx", "bundle.zip", "tool.exe"):
        r = c.get(f"{PREFIX}/projects/{world['proj_id']}/raw",
                  params={"path": name, "inline": "true"})
        h = _h(r)
        assert h["content-type"] == "application/octet-stream", (name, h["content-type"])
        assert h["content-disposition"].startswith("attachment"), name
        assert "filename*=UTF-8''" in h["content-disposition"]


def test_raw_text_never_inline(world):
    """文本也不给 inline：避免被浏览器当 HTML/JS 解释。"""
    r = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/raw",
                            params={"path": "README.md", "inline": "true"})
    h = _h(r)
    assert h["content-type"] == "application/octet-stream"
    assert h["content-disposition"].startswith("attachment")


def test_raw_download_flag_forces_attachment(world):
    r = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/raw",
                            params={"path": "pic.png", "download": "true"})
    h = _h(r)
    assert h["content-type"] == "application/octet-stream"
    assert h["content-disposition"].startswith("attachment")


def test_raw_bytes_match_disk(world):
    r = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/raw",
                            params={"path": "README.md", "download": "true"})
    assert r.content == (world["proj"] / "README.md").read_bytes()


# ---------------------------------------------------------------- 归档只读
def test_archived_project_browsable_but_read_only(world):
    c = world["client"]
    data = c.get(f"{PREFIX}/projects/{world['arc_id']}/tree").json()
    assert data["archived"] is True and data["read_only"] is True
    assert "old.txt" in _names(data)
    f = c.get(f"{PREFIX}/projects/{world['arc_id']}/file", params={"path": "old.txt"}).json()
    assert f["read_only"] is True and f["kind"] == "text"


# ---------------------------------------------------------------- 创建 / 纳管
def test_creatable_lists_untracked_only(world):
    data = world["client"].get(f"{PREFIX}/projects/creatable").json()
    names = {u["name"] for u in data["untracked"]}
    assert "2026-10_待纳管" in names and "scratch" in names
    assert world["proj_id"] not in names, "已纳管项目不该出现在候选里"
    assert world["arc_id"] not in names, "归档根不参与纳管"
    by = {u["name"]: u for u in data["untracked"]}
    assert by["2026-10_待纳管"]["name_valid"] is True
    assert by["scratch"]["name_valid"] is False
    assert data["name_pattern"] == "YYYY-MM_名称"
    assert data["active_root"] == str(world["active"])


@pytest.mark.parametrize("bad", [
    "没有日期前缀", "2026-13_坏月份", "2026-11_带/斜杠", "2026-11_带\\反斜杠",
    "2026-11_带:冒号", "x" * 200, "2026-11_尾点.", "2026-11_带\x01控制符",
    ".2026-11_点开头", "2026-11_带*星号", "2026-11_带|竖线", "2026-11_带?问号",
])
def test_project_name_validation_rejects(world, bad):
    r = world["client"].post(f"{PREFIX}/projects/plan",
                             json={"mode": "new", "name": bad})
    # 超长由 schema 拦（422），其它由业务校验拦（400）；都不许落地
    assert r.status_code in (400, 422), (bad, r.status_code, r.text[:200])
    assert not (world["active"] / bad.strip()).exists()


def test_project_name_whitespace_is_trimmed_not_kept(world):
    """首尾空格被裁掉（目录名不能带尾空格），返回的计划里是裁过的名字。"""
    plan = world["client"].post(f"{PREFIX}/projects/plan",
                                json={"mode": "new", "name": "  2026-11_裁空格  "}).json()
    assert plan["name"] == "2026-11_裁空格"
    assert Path(plan["target_dir"]).name == "2026-11_裁空格"


def test_windows_reserved_name_rejected(world):
    r = world["client"].post(f"{PREFIX}/projects/plan", json={"mode": "new", "name": "con"})
    assert r.status_code == 400 and "保留名" in r.json()["detail"]


def test_plan_is_dry_run(world):
    r = world["client"].post(f"{PREFIX}/projects/plan", json={
        "mode": "new", "name": "2026-11_新项目", "template": "default",
        "description": "说明", "start_date": "2026-11-01", "end_date": "2026-12-31"})
    assert r.status_code == 200
    plan = r.json()
    assert plan["ok"] is True and plan["blockers"] == []
    assert any("创建目录" in a for a in plan["actions"])
    assert plan["template_dirs"] == ["Bitmap", "Hardware", "Software", "Reference", "DataSheet"]
    assert plan["pm_preview"]["kanban_columns"] == ["待办", "进行中", "已完成"]
    assert plan["pm_preview"]["schema_version"] == 3
    assert not Path(plan["target_dir"]).exists(), "plan 绝不能落盘"
    assert plan["plan_id"]


def test_new_rejects_existing_dir(world):
    plan = world["client"].post(f"{PREFIX}/projects/plan",
                                json={"mode": "new", "name": world["proj_id"]}).json()
    assert plan["ok"] is False
    assert any("目录已存在" in b for b in plan["blockers"])


def test_adopt_rejects_already_tracked(world):
    plan = world["client"].post(f"{PREFIX}/projects/plan",
                                json={"mode": "adopt", "name": world["proj_id"]}).json()
    assert plan["ok"] is False
    assert any("已纳管" in b for b in plan["blockers"])


def test_adopt_rejects_missing_dir(world):
    plan = world["client"].post(f"{PREFIX}/projects/plan",
                                json={"mode": "adopt", "name": "2026-01_不存在"}).json()
    assert plan["ok"] is False
    assert any("不存在" in b for b in plan["blockers"])


def test_adopt_rejects_symlinked_dir(world):
    if not world["link_ok"]:
        pytest.skip("无 symlink 支持")
    link = world["active"] / "2026-12_链接项目"
    try:
        link.symlink_to(world["tmp"] / "outside", target_is_directory=True)
    except OSError:
        pytest.skip("无法创建 symlink")
    plan = world["client"].post(f"{PREFIX}/projects/plan",
                                json={"mode": "adopt", "name": "2026-12_链接项目"}).json()
    assert plan["ok"] is False and any("链接" in b for b in plan["blockers"])


def test_create_requires_matching_plan_id(world):
    c = world["client"]
    body = {"mode": "new", "name": "2026-11_待建项目", "template": "todo"}
    plan = c.post(f"{PREFIX}/projects/plan", json=body).json()
    r = c.post(f"{PREFIX}/projects/create", json=body, params={"plan_id": "0" * 32})
    assert r.status_code == 409
    r2 = c.post(f"{PREFIX}/projects/create", json=body)  # 缺 plan_id
    assert r2.status_code == 422
    assert not Path(plan["target_dir"]).exists()


def test_create_round_trip_and_verify(world):
    c = world["client"]
    body = {"mode": "new", "name": "2026-11_待建项目", "template": "web",
            "description": "一句说明", "start_date": "2026-11-05", "end_date": "2026-12-20"}
    plan = c.post(f"{PREFIX}/projects/plan", json=body).json()
    r = c.post(f"{PREFIX}/projects/create", json=body, params={"plan_id": plan["plan_id"]})
    assert r.status_code == 200
    res = r.json()
    assert res["ok"] is True and res["verified"] is True
    target = Path(res["target_dir"])
    pm = target / "2026-11_待建项目.pm"
    assert target.is_dir() and pm.is_file() and (target / "README.md").is_file()
    assert (target / "src").is_dir() and (target / "docs").is_dir(), "web 模板目录要建出来"
    data = json.loads(pm.read_text(encoding="utf-8-sig"))
    assert data["schema_version"] == 3 and data["project"]["name"] == "2026-11_待建项目"
    assert data["project"]["kanban_columns"] == ["待办", "进行中", "已完成"]
    assert res["verify"]["pm_parses"] is True
    assert res["verify"]["visible_in_projects"] is True
    assert res["verify"]["project_id"] == "2026-11_待建项目"
    tree = c.get(f"{PREFIX}/projects/2026-11_待建项目/tree").json()
    assert "2026-11_待建项目.pm" in _names(tree)


def test_create_is_idempotent_refused(world):
    c = world["client"]
    body = {"mode": "new", "name": "2026-11_只建一次", "template": "todo"}
    plan = c.post(f"{PREFIX}/projects/plan", json=body).json()
    assert c.post(f"{PREFIX}/projects/create", json=body,
                  params={"plan_id": plan["plan_id"]}).status_code == 200
    pm = Path(plan["pm_path"])
    before = pm.read_bytes()
    r = c.post(f"{PREFIX}/projects/create", json=body, params={"plan_id": plan["plan_id"]})
    assert r.status_code == 409
    assert pm.read_bytes() == before, "第二次调用绝不能改动已有 .pm"


def test_adopt_does_not_touch_existing_files(world):
    c = world["client"]
    name = "2026-10_待纳管"
    body = {"mode": "adopt", "name": name, "template": "todo"}
    plan = c.post(f"{PREFIX}/projects/plan", json=body).json()
    assert plan["ok"] is True
    res = c.post(f"{PREFIX}/projects/create", json=body,
                 params={"plan_id": plan["plan_id"]}).json()
    assert res["verified"] is True
    assert (world["active"] / name / "keep.md").read_text(encoding="utf-8") == "keep me\n"
    assert (world["active"] / name / f"{name}.pm").is_file()
    again = {u["name"] for u in c.get(f"{PREFIX}/projects/creatable").json()["untracked"]}
    assert name not in again


def test_adopt_refuses_when_pm_appears_between_plan_and_create(world):
    c = world["client"]
    name = "2026-10_待纳管"
    body = {"mode": "adopt", "name": name, "template": "todo"}
    plan = c.post(f"{PREFIX}/projects/plan", json=body).json()
    (world["active"] / name / "抢注.pm").write_text('{"version":"1.0"}', encoding="utf-8")
    r = c.post(f"{PREFIX}/projects/create", json=body, params={"plan_id": plan["plan_id"]})
    assert r.status_code == 409
    assert (world["active"] / name / f"{name}.pm").exists() is False


def test_write_endpoints_only_touch_active_root(world):
    plan = world["client"].post(f"{PREFIX}/projects/plan",
                                json={"mode": "new", "name": "2026-11_新项目"}).json()
    assert Path(plan["target_dir"]).parent == world["active"]
    assert str(world["archive"]) not in plan["target_dir"]
    assert str(world["api"]._active_root()) == str(world["active"])


def test_create_rejects_bad_dates(world):
    for kw in ({"start_date": "2026/11/01"}, {"end_date": "abc"},
               {"start_date": "2026-12-31", "end_date": "2026-01-01"}):
        r = world["client"].post(f"{PREFIX}/projects/plan",
                                 json={"mode": "new", "name": "2026-11_日期", **kw})
        assert r.status_code == 400, (kw, r.status_code)


def test_create_rejects_bad_template_and_mode(world):
    c = world["client"]
    assert c.post(f"{PREFIX}/projects/plan",
                  json={"mode": "new", "name": "2026-11_x", "template": "nope"}
                  ).status_code == 400
    assert c.post(f"{PREFIX}/projects/plan",
                  json={"mode": "steal", "name": "2026-11_x"}).status_code == 400


def test_pm_summary_reports_ownership(world):
    data = world["client"].get(f"{PREFIX}/projects/{world['proj_id']}/pm").json()
    assert data["tracked"] is True
    assert data["pm_file"] == f"{world['proj_id']}.pm"
    assert data["read_only"] is False


def test_plan_id_is_stable_for_same_params(world):
    c = world["client"]
    a = c.post(f"{PREFIX}/projects/plan", json={
        "mode": "new", "name": "2026-11_稳定", "description": "d"}).json()["plan_id"]
    b = c.post(f"{PREFIX}/projects/plan", json={
        "mode": "new", "name": "2026-11_稳定", "description": "d"}).json()["plan_id"]
    d = c.post(f"{PREFIX}/projects/plan", json={
        "mode": "new", "name": "2026-11_稳定", "description": "别的"}).json()["plan_id"]
    assert a == b and a != d


def test_no_endpoint_accepts_a_raw_path(world):
    """契约守卫：文件相关端点只接受 (项目id, 相对路径)，没有「任意路径」入口。"""
    import inspect
    api = world["api"]
    for fn in (api.project_tree, api.project_file, api.project_raw):
        params = list(inspect.signature(fn).parameters)
        assert params[0] == "project_id", params
        assert "path" in params
    paths = [r.path for r in api.router.routes]
    assert not any(p in ("/fs", "/read", "/download", "/open") for p in paths), paths
