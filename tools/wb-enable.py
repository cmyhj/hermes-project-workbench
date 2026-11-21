#!/usr/bin/env python
"""扫 ``$HERMES_HOME/plugins/*/dashboard/manifest.json`` 生成 ``plugins.enabled`` 列表。

为什么需要它：``hermes plugins enable <name>`` 只认 agent 插件，对 dashboard 插件报
``No plugin named '<x>'``；唯一杠杆是 ``config.yaml → plugins.enabled``，而
``hermes config set`` 会整体替换数组，手改容易丢条目。

    python tools/wb-enable.py            # 只打印将要写入的列表（dry-run）
    python tools/wb-enable.py --apply    # 真的调 hermes config set
    python tools/wb-enable.py --apply --only-wb   # 只保留 wb-* 插件 + 现有非 wb 项

默认保留所有已启用、但目录里没有 manifest 的条目（例如 bundled 插件），
不会被静默删掉。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return local / "hermes"


def dashboard_plugins(root: Path) -> list[str]:
    """所有带 dashboard/manifest.json 的插件目录名（按 manifest 的 name 归一）。"""
    found: list[str] = []
    plugins = root / "plugins"
    if not plugins.is_dir():
        return found
    for child in sorted(plugins.iterdir()):
        manifest = child / "dashboard" / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except Exception:
            print(f"! 跳过无法解析的 manifest: {manifest}", file=sys.stderr)
            continue
        name = str(data.get("name") or child.name)
        found.append(name)
    return found


def current_enabled(root: Path) -> list[str]:
    cfg = root / "config.yaml"
    if not cfg.is_file():
        return []
    try:
        import yaml  # type: ignore
    except Exception:
        print("! 缺少 PyYAML，无法读现有列表，将只输出扫描结果", file=sys.stderr)
        return []
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    plugins = (data.get("plugins") or {}) if isinstance(data, dict) else {}
    enabled = plugins.get("enabled") or []
    return [str(x) for x in enabled] if isinstance(enabled, list) else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="调用 hermes config set 写入")
    ap.add_argument("--only-wb", action="store_true", help="只保留 wb-* 前缀 + 已启用但无目录的条目")
    args = ap.parse_args()

    root = hermes_home()
    scanned = dashboard_plugins(root)
    enabled = current_enabled(root)

    keep = [p for p in enabled if p not in scanned]
    if args.only_wb:
        keep = [p for p in keep if not p.startswith("wb-")]

    wanted = keep + [p for p in scanned if p not in keep]
    seen, ordered = set(), []
    for p in wanted:
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    print(f"HERMES_HOME = {root}")
    print(f"扫描到 dashboard 插件 {len(scanned)}: {scanned}")
    print(f"现有 enabled {len(enabled)}: {enabled}")
    print(f"未在目录中、将被保留: {keep}")
    print(f"最终列表（{len(ordered)}）: {ordered}")

    if not args.apply:
        print("\n(dry-run) 加 --apply 才写入")
        return 0

    payload = json.dumps(ordered, ensure_ascii=False)
    cmd = ["hermes", "config", "set", "plugins.enabled", payload]
    print("\n执行:", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.stdout:
        print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return proc.returncode
    print("已写入。dashboard 的插件发现是进程级缓存，需要重启一次 dashboard 才生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
