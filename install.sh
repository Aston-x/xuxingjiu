#!/usr/bin/env bash
# 许杏玖 · 一键安装（Linux / macOS）
#
# 做这些事（**幂等**，重复跑不会破坏已有配置）：
#   1. 环境检查（python / node / git）
#   2. 没有源码就取一份（git clone 优先）
#   3. 建 .venv 并装依赖
#   4. 生成 config.json（**已存在就跳过，绝不覆盖**）
#   5. 空间桥接 npm ci（按 lock 哈希判断要不要重装）
#   6. 跑体检 doctor.py，把结果摊给你看
#
# 用法：
#   bash install.sh                # 正常安装
#   bash install.sh --check        # 只检查、不动任何文件（CI 用）
#   REPO_URL=... TARGET_DIR=... bash install.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/your-name/xuxingjiu.git}"
TARGET_DIR="${TARGET_DIR:-$PWD/xuxingjiu}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
die()  { printf '  ❌ %s\n' "$*" >&2; exit 1; }

# ── 1. 环境 ──────────────────────────────────────────────────────────
say "1/6 环境检查"
PY=""
# 明确列出的优先（避免不同机器上 python3 指向不一致）；含 3.14 以兼容新版 Python。
# 兜底再扫 python3.11~3.20：防止系统只装了某个小版本（如 3.14）且没建 python3 软链。
for c in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  for n in $(seq 11 20); do
    c="python3.$n"
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  done
fi
[ -n "$PY" ] || die "找不到 Python 3.11+。Debian/Ubuntu: sudo apt install python3 python3-venv"
ok "Python: $("$PY" --version 2>&1) ($PY)"

if command -v node >/dev/null 2>&1; then
  ok "Node: $(node --version)（空间桥接用，可选）"
else
  warn "没有 node —— 空间桥接装不了；只聊天的话不影响"
fi

# ── 2. 源码 ──────────────────────────────────────────────────────────
say "2/6 源码"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SELF_DIR/qqbot/bot.py" ]; then
  ROOT="$SELF_DIR"
  ok "就在当前目录：$ROOT"
elif [ -d "$TARGET_DIR/qqbot" ]; then
  ROOT="$TARGET_DIR"
  ok "已有源码：$ROOT"
elif [ "$CHECK_ONLY" = "1" ]; then
  die "找不到源码（--check 模式不会去下载）"
elif command -v git >/dev/null 2>&1; then
  echo "  → git clone $REPO_URL $TARGET_DIR"
  git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
  ROOT="$TARGET_DIR"
  ok "已 clone 到 $ROOT"
else
  die "没有 git。请手动下载 Release 源码包解压，再在本目录里跑本脚本"
fi

QQBOT="$ROOT/qqbot"
[ -f "$QQBOT/requirements.txt" ] || die "缺 $QQBOT/requirements.txt，源码不完整"

# ── 3. venv + 依赖 ───────────────────────────────────────────────────
say "3/6 Python 依赖"
VENV_PY="$QQBOT/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
  ok "已有 .venv，跳过创建"
elif [ "$CHECK_ONLY" = "1" ]; then
  warn "没有 .venv（--check 模式不创建）"
else
  "$PY" -m venv "$QQBOT/.venv"
  ok "已创建 $QQBOT/.venv"
fi
if [ -x "$VENV_PY" ] && [ "$CHECK_ONLY" != "1" ]; then
  # ★ 必须用 venv 里的 python -m pip：用系统 pip 会装到全局，然后 import 失败
  "$VENV_PY" -m pip install --upgrade pip -q
  "$VENV_PY" -m pip install -r "$QQBOT/requirements.txt" -q
  ok "依赖装好"
elif [ "$CHECK_ONLY" = "1" ]; then
  :
fi

# ── 4. config.json ───────────────────────────────────────────────────
say "4/6 配置"
if [ -f "$QQBOT/config.json" ]; then
  ok "config.json 已存在，**不覆盖**（这条最重要：覆盖会把你的 token/QQ 号弄丢）"
elif [ -f "$QQBOT/config.example.json" ] && [ "$CHECK_ONLY" != "1" ]; then
  cp "$QQBOT/config.example.json" "$QQBOT/config.json"
  ok "已从模板生成 config.json —— 记得改 bot_qq / admin.user_ids / access_token"
else
  warn "没有 config.json"
fi

# ── 5. 空间桥接（可选） ──────────────────────────────────────────────
say "5/6 空间桥接（可选）"
BRIDGE="$ROOT/qzone-bridge"
if [ ! -d "$BRIDGE" ]; then
  warn "没有 qzone-bridge 目录，跳过"
elif ! command -v npm >/dev/null 2>&1; then
  warn "没有 npm，跳过（装完 node 后：cd qzone-bridge && npm ci && npm run build）"
elif [ "$CHECK_ONLY" = "1" ]; then
  warn "跳过安装（--check）"
else
  # 只在 lock 变了或没装过时才重装 —— npm ci 会删掉整个 node_modules 重下几百 MB
  LOCK_HASH_FILE="$BRIDGE/node_modules/.lock-hash"
  WANT_HASH="$( (command -v shasum >/dev/null && shasum -a 256 "$BRIDGE/package-lock.json") \
                || sha256sum "$BRIDGE/package-lock.json" | awk '{print $1}')"
  if [ -f "$LOCK_HASH_FILE" ] && [ "$(cat "$LOCK_HASH_FILE" 2>/dev/null)" = "$WANT_HASH" ]; then
    ok "依赖已是最新（lock 未变），跳过 npm ci"
  else
    ( cd "$BRIDGE" && npm ci --no-audit --no-fund )
    printf '%s' "$WANT_HASH" > "$LOCK_HASH_FILE"
    ok "依赖装好"
  fi
  if [ ! -f "$BRIDGE/dist/main.js" ] && [ ! -f "$BRIDGE/.env" ]; then
    warn "还没配 .env —— 复制 .env.example 改名并填 QQ 空间 Cookie"
  fi
fi

# ── 6. 体检 ──────────────────────────────────────────────────────────
say "6/6 体检"
if [ -x "$VENV_PY" ]; then
  set +e
  "$VENV_PY" "$QQBOT/tools/doctor.py"
  DOCTOR_RC=$?
  set -e
else
  warn "没有 venv，跳过体检（--check 模式下正常）"
  DOCTOR_RC=1
fi

say "下一步"
cat <<'EOF'
  1) 改 qqbot/config.json：bot_qq（机器人 QQ）、admin.user_ids（你的 QQ）、
     access_token（自己编一串随机字符，接入端要填一样的）
  2) 配模型：设环境变量（例如 export DEEPSEEK_API_KEY=sk-...），
     或改 providers.endpoints 指向本地模型
  3) 装 QQ 接入端：见 docs/部署-Linux.md 的「QQ 接入端」一节
     （用 Docker / Lagrange 时把 config.json 的 onebot.adapter 改成 "none"）
  4) 启动：bash deploy/start-all.sh
  5) 看日志：控制台 http://127.0.0.1:6200/  或  journalctl --user -u qqbot -f

  ⚠️ 绝不要 pkill -f QQ —— 你的日常号和机器人号可能是同一个客户端进程。
EOF

exit $DOCTOR_RC
