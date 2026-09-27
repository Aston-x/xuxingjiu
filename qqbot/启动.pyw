"""双击本文件 = 一键启动许杏玖（不会出现终端黑窗口）。

它做了这些事：
  1. 只补启**缺失**的组件（bot / 空间桥接 / NapCat），**不会杀任何进程**；
     已在运行的直接跳过，你的 QQ 与大号登录状态不受影响。
  2. 起完后**自动用默认浏览器打开控制台**（已带令牌，不用手输）。
  3. 然后常驻做守护：bot 崩了自动拉起（两分钟内反复崩 5 次就停手并记日志）。

本进程没有窗口（Windows 用 pythonw 运行 .pyw），所以它不会出现在任务栏，
任务管理器里会看到一个 pythonw.exe —— 属正常。

要停止：双击 停止.pyw    要看状态：双击 status-查看状态.py
所有日志都在控制台的「日志」页里看。
"""
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

try:
    import launcher
    launcher.main(["launcher.py", "start"])
except Exception:
    # pythonw 没有控制台，异常只能落日志，否则会静默消失
    try:
        with (BASE / "launcher.log").open("a", encoding="utf-8") as f:
            f.write("\n[启动.pyw 异常]\n" + traceback.format_exc() + "\n")
    except Exception:
        pass
