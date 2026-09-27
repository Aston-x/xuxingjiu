"""双击本文件 = 停止许杏玖（只停 bot 与空间桥接，绝不动 QQ / NapCat）。"""
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

try:
    import launcher
    launcher.main(["launcher.py", "stop"])
except Exception:
    try:
        with (BASE / "launcher.log").open("a", encoding="utf-8") as f:
            f.write("\n[停止.pyw 异常]\n" + traceback.format_exc() + "\n")
    except Exception:
        pass
