"""仓库统一入口：停止 qqbot 与空间桥接。

**绝不动 QQ / NapCat** —— 你的日常 QQ 和机器人号是同一个进程，
按镜像名杀会把大号一起带走。详见 qqbot/launcher.py 的说明。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QQBOT = ROOT / "qqbot"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def msgbox(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        pass


def main() -> int:
    exe = None
    for name in ("pythonw.exe", "python.exe"):
        p = QQBOT / ".venv" / "Scripts" / name
        if p.exists():
            exe = p
            break
    if exe is None or not (QQBOT / "launcher.py").exists():
        msgbox("停不了", f"没找到虚拟环境或 launcher.py：{QQBOT}")
        return 2
    try:
        subprocess.Popen([str(exe), str(QQBOT / "launcher.py"), "stop"],
                         cwd=str(QQBOT), creationflags=CREATE_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        msgbox("停止失败", f"{type(exc).__name__}: {exc}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
