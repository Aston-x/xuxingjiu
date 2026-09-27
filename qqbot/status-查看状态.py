"""双击本文件 = 在控制台窗口里看各组件状态（这个需要窗口，故意用 .py）。"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import launcher  # noqa: E402

launcher.status()
try:
    input("\n按回车关闭…")
except EOFError:
    pass
