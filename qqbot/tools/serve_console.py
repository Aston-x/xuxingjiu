"""只起控制台并保持存活（供 tools\\shoot_console.js 截图/交互验证用）。

隔离措施：
1. 端口由内核动态分配 —— 不撞正在运行的控制台（撞上会静默测到别人的进程）。
2. CONFIG_PATH 指向临时副本 —— 截图脚本会在 UI 上点开关，
   不隔离就会改动真实的 config.json。
3. 启动后打印 `CONSOLE_READY <port>`，由调用方解析。
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import socket
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

import bot  # noqa: E402

_s = socket.socket()
_s.bind(("127.0.0.1", 0))
PORT = _s.getsockname()[1]
_s.close()

TMP_CFG = BASE_DIR / "_tmp_shoot_config.json"
shutil.copy2(bot.CONFIG_PATH, TMP_CFG)
bot.CONFIG_PATH = TMP_CFG
(bot.CFG.setdefault("console", {}))["port"] = PORT


async def main() -> None:
    bot.LOOP = asyncio.get_running_loop()
    bot._start_console()
    print(f"CONSOLE_READY {PORT}", flush=True)
    await asyncio.Future()


asyncio.run(main())
