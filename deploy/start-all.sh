#!/usr/bin/env bash
# 一键启动全套（qqbot → 空间桥 → QQ 接入端）。Linux / macOS。
#
# 与 Windows 上的 deploy/start-all.pyw 等价：真正的编排逻辑都在
# qqbot/launcher.py 里（端口探测、进程归属校验、绝不误杀 QQ），这里只是
# 用正确的解释器把它拉起来，并且**不占终端**。
#
# 「不弹黑窗口」在 POSIX 上的含义 = 不占着当前终端：
#   launcher.py 内部的 _spawn 带 start_new_session=True（等价 setsid），
#   所以关掉终端 / SSH 断开都不会把 bot 带走。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QQBOT="$ROOT/qqbot"

if [ ! -x "$QQBOT/.venv/bin/python" ]; then
  echo "还没装依赖：$QQBOT/.venv/bin/python 不存在" >&2
  echo "先跑  bash install.sh" >&2
  exit 3
fi
if [ ! -f "$QQBOT/config.json" ]; then
  echo "缺少 $QQBOT/config.json" >&2
  echo "先复制 config.example.json 改名成 config.json，至少改 bot_qq 与 admin.user_ids" >&2
  exit 4
fi

cd "$QQBOT"
exec "$QQBOT/.venv/bin/python" launcher.py start
