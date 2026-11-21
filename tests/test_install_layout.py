"""安装产物自检：仓库里的 `plugin/` 必须是**可直接复制**的完整插件。

不依赖 Hermes、不依赖网络、不碰任何真实数据：
只断言「复制过去就能用」这件事在结构上成立，并守住几条容易被改坏的不变量。

    python -m pytest tests/test_install_layout.py -q
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin" / "dashboard"
DIST = PLUGIN / "dist"

# Hermes `_PLUGIN_ASSET_CONTENT_TYPES` 允许的静态资产后缀；其余后缀走 plugin_api 的 FileResponse。
ALLOWED_ASSETS = {".js", ".mjs", ".css", ".json", ".html", ".svg", ".png", ".jpg", ".jpeg",
                  ".gif", ".webp", ".ico", ".woff2", ".woff", ".ttf", ".otf", ".map"}


def test_manifest_is_valid_and_points_at_existing_files():
    m = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8-sig"))
    assert m["name"] == "wb-project"
    assert m["has_api"] is True and m["api"] == "plugin_api.py"
    for key in ("entry", "css"):
        assert (PLUGIN / m[key]).is_file(), f"manifest.{key} 指向不存在的文件：{m[key]}"
    assert m["tab"]["path"].startswith("/")


def test_entry_is_plain_iife_no_build_step():
    """dist/index.js 必须是手写 IIFE：不 import、不 JSX，最终自己注册组件。"""
    src = (DIST / "index.js").read_text(encoding="utf-8")
    assert not re.search(r"^\s*import\s", src, re.M), "插件入口不能有 ES import（宿主不编译）"
    assert "window.__HERMES_PLUGINS__.register(" in src
    assert "__HERMES_PLUGIN_SDK__" in src, "必须从宿主 SDK 取 React/组件，而不是自己打包"
    # 没有 JSX 语法残留（`<div>` 这类）
    assert not re.search(r"return\s*\(\s*<\w", src)


def test_all_dist_assets_are_servable_suffixes():
    for p in DIST.rglob("*"):
        if p.is_file():
            assert p.suffix.lower() in ALLOWED_ASSETS, (
                f"{p.relative_to(PLUGIN)} 后缀不在 Hermes 的静态资产白名单里，"
                "浏览器会 404；如需下发请走 plugin_api 的 FileResponse")


def test_no_cdn_references():
    """CSP 是 script-src 'self'：任何 CDN 引用都会在浏览器里被拦。"""
    for p in list(DIST.glob("*.*")) + list(DIST.rglob("*.js")):
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in ("https://cdn.", "http://cdn.", "unpkg.com", "jsdelivr", "cdnjs"):
            assert bad not in text, f"{p.name} 引用了 CDN（{bad}），会被 CSP 拦掉"


def test_no_absolute_machine_paths_in_shipped_code():
    """仓库是公开模板：代码里不许出现某台机器的具体路径。"""
    pat = re.compile(r"[A-Za-z]:[\\/](?:Users|ActiveProjects|Backups|Libraries)[\\/]", re.I)
    for p in list(DIST.rglob("*.js")) + [PLUGIN / "plugin_api.py", REPO / "README.md"]:
        if not p.is_file():
            continue
        hits = pat.findall(p.read_text(encoding="utf-8", errors="ignore"))
        assert not hits, f"{p.name} 里有本机绝对路径：{hits[:3]}"


def test_vendored_chat_is_in_sync():
    """vendored 副本必须与 shared/ 一致（否则浏览器会继续跑旧组件）。"""
    import filecmp
    src = REPO / "shared" / "wb-agent-chat"
    vendor = DIST / "vendor"
    for f in sorted(src.iterdir()):
        if not f.is_file():
            continue
        target = vendor / f.name
        assert target.is_file(), f"缺少 vendored 副本：{target.name}（运行 tools/sync-shared.py）"
        assert filecmp.cmp(f, target, shallow=False), (
            f"{target.name} 与 shared/ 不一致（运行 tools/sync-shared.py）")


def test_config_example_has_no_real_paths_and_parses():
    cfg = json.loads((REPO / "config" / "project-roots.example.json").read_text(encoding="utf-8-sig"))
    assert "active_root" in cfg and "archive_roots" in cfg
    blob = json.dumps(cfg, ensure_ascii=False)
    assert "<" in blob, "示例配置应使用占位路径（<...>），不能是某台机器的真实目录"
    for entry in cfg["archive_roots"]:
        assert entry.get("path")


def test_license_and_notice_exist():
    assert (REPO / "LICENSE").is_file()
    lic = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in lic
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    assert "Hermes" in notice and "第三方" in notice


@pytest.mark.parametrize("name", ["README.md", "docs/SECURITY-AND-PREVIEW.md",
                                  "shared/README.md"])
def test_docs_exist(name):
    assert (REPO / name).is_file() and (REPO / name).stat().st_size > 500


def test_readme_documents_restart_requirement():
    """README 必须写明「改后端要重启」，这是最容易踩的坑。"""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "重启" in text
    assert "plugins.enabled" in text
    assert "hermes plugins enable" in text, "必须说明该命令对 dashboard 插件无效"
