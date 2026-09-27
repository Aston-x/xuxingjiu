"""双击本文件 = 用默认浏览器打开控制台（自动带令牌，不用手输）。

和 启动.pyw 的区别：这个**只管打开**，不会去启动/重启任何东西；
如果控制台没在跑，会提示你先双击 启动.pyw。

出错时会弹一个消息框说明原因 —— .pyw 没有控制台，不弹框你就什么都看不到。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import launcher  # noqa: E402


def msgbox(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        pass


def main() -> int:
    port = launcher.console_port()

    if not launcher.is_up(port):
        msgbox("控制台没在运行",
               f"端口 {port} 上没有服务。\n\n"
               "请先双击：启动.pyw\n"
               "（它会补齐缺失的组件，并把面板打开）\n\n"
               "如果刚双击过 启动.pyw 还是不行，请双击 status-查看状态.py 看是哪一项没起来。")
        return 2

    url = launcher.console_url()
    try:
        os.startfile(url)
    except Exception as exc:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            msgbox("打不开浏览器", f"请手动把这个地址粘到浏览器：\n\n{url}\n\n（{exc}）")
            return 3

    if not launcher.console_token():
        msgbox("还需要令牌",
               f"控制台在 http://127.0.0.1:{port}/ 跑着，但令牌文件还不存在。\n\n"
               "页面会弹输入框，请把下面文件里的内容整行粘进去：\n"
               f"{BASE / 'state' / 'console-token'}\n\n"
               "（这个文件是 bot 启动控制台时自动生成的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
