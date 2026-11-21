"""让测试目录内的辅助模块可以被同目录用例直接 import。

pytest 的 rootdir 插 sys.path 是版本相关的（`rootdir` 不一定是 tests/），这里显式补一次，
避免 `from test_wb_chat_ws_contract import ...` 在某些 invocation 下 ImportError。
"""

import sys
from pathlib import Path

HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
