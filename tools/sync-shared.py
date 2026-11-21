"""把 shared/ 下的共享前端组件复制到各插件的 dist/vendor/。

为什么需要它：插件静态资产走 `/dashboard-plugins/<name>/<path>`，解析器用
`is_relative_to` 把请求限制在该插件自己的 `dashboard/` 目录内，所以**不能**用
`../shared/...` 跨插件引用。共享组件的唯一来源留在仓库 `shared/`，由本脚本复制成
每个插件私有的 vendored 副本；插件之间不互相引用，也不用新增任何 Hermes 配置。

用法（在仓库根目录，用 Hermes 自己的 venv 或任意 python3）：

    python tools/sync-shared.py            # 复制
    python tools/sync-shared.py --check     # 只校验：部署副本与源码不一致就非零退出

`--check` 供测试与 CI 使用：它防的是「改了 shared/ 却忘了同步」，这种漂移在浏览器里
表现为旧组件继续运行，看起来只是「改动没生效」。
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "shared" / "wb-agent-chat"

# 每个消费该组件的插件：插件根 → 它的 dashboard 目录
# 独立仓库里只有一个插件，所以只有一条。原工作台仓库另有 wb-learn。
CONSUMERS = [
    REPO / "plugin" / "dashboard",
]

FILES = ["wb-agent-chat.js", "wb-agent-chat.css"]


def targets(dashboard: Path) -> list[tuple[Path, Path]]:
    vendor = dashboard / "dist" / "vendor"
    return [(SRC / name, vendor / name) for name in FILES]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args()

    missing = [str(p) for p in [SRC / n for n in FILES] if not p.is_file()]
    if missing:
        print("共享组件源码缺失：\n  " + "\n  ".join(missing), file=sys.stderr)
        return 2

    drift: list[str] = []
    copied: list[str] = []
    for dashboard in CONSUMERS:
        if not dashboard.is_dir():
            continue
        for src, dst in targets(dashboard):
            if args.check:
                if not dst.is_file() or not filecmp.cmp(src, dst, shallow=False):
                    drift.append(str(dst))
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            copied.append(str(dst))

    if args.check:
        if drift:
            print("vendored 副本与 shared/ 不一致（运行 python tools/sync-shared.py）：", file=sys.stderr)
            for path in drift:
                print("  " + path, file=sys.stderr)
            return 1
        print("shared 组件与所有 vendored 副本一致")
        return 0

    for path in copied:
        print("同步 " + path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
