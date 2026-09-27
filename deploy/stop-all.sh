#!/usr/bin/env bash
# 停止 qqbot 与空间桥接。Linux / macOS。
#
# ⚠️ **绝不动 QQ / NapCat** —— launcher.py 只按端口定位进程、核对命令特征，
#    QQ 永远不在候选列表里。这一点在 Windows 和 POSIX 上是一样的。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QQBOT="$ROOT/qqbot"

if [ ! -x "$QQBOT/.venv/bin/python" ]; then
  echo "找不到 $QQBOT/.venv/bin/python" >&2
  exit 3
fi

cd "$QQBOT"
exec "$QQBOT/.venv/bin/python" launcher.py stop
