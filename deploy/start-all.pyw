"""仓库统一入口：按正确顺序静默启动全套（qqbot → 空间桥 → NapCat）。

为什么要有这个文件：整套东西的启动顺序是有讲究的（qqbot 得先在 6199 上等着，
NapCat 才能反向连进来），而 NapCat 自己那套 .bat 会弹黑窗口。
这里只是把 `qqbot/launcher.py start` 用一个没有控制台的解释器跑起来，
真正的编排逻辑（端口探测、进程归属校验、绝不杀 QQ）都在 launcher.py 里。

双击即可。出错会弹消息框 —— 没有控制台，不弹框你就什么都看不到。
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
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x30)
    except Exception:
        pass


def pick_python() -> Path | None:
    for name in ("pythonw.exe", "python.exe"):
        p = QQBOT / ".venv" / "Scripts" / name
        if p.exists():
            return p
    return None


def main() -> int:
    if not (QQBOT / "launcher.py").exists():
        msgbox("目录不对", f"没找到 {QQBOT / 'launcher.py'}\n\n"
                          "请把本文件放在仓库根的 deploy/ 目录下，"
                          "并保证 qqbot/ 与它平级。")
        return 2

    exe = pick_python()
    if exe is None:
        msgbox("还没装依赖",
               f"没找到虚拟环境：{QQBOT / '.venv'}\n\n"
               "请先在 qqbot 目录里执行：\n"
               "    python -m venv .venv\n"
               "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n\n"
               "详见 docs/部署.md")
        return 3

    if not (QQBOT / "config.json").exists():
        msgbox("还没有配置文件",
               f"缺少 {QQBOT / 'config.json'}\n\n"
               "请复制 config.example.json 改名成 config.json，"
               "至少改 bot_qq（机器人 QQ）与 admin.user_ids（你的 QQ）。")
        return 4

    try:
        subprocess.Popen([str(exe), str(QQBOT / "launcher.py"), "start"],
                         cwd=str(QQBOT), creationflags=CREATE_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        msgbox("启动失败", f"{type(exc).__name__}: {exc}\n\n"
                          f"也可以手工在 qqbot 目录里跑：\n"
                          f"    .venv\\Scripts\\python.exe launcher.py start")
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
