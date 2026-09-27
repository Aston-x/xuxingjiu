#!/usr/bin/env bash
# 许杏玖 · 一键安装（Linux / macOS，Windows 上的 Git Bash 也能跑）
#
# 做这些事（**幂等**，重复跑不会破坏已有配置）：
#   0. 环境探测：系统 / 发行版 / 包管理器 / 权限 / Python / Node / git / 网络
#   1. 缺什么补什么：能自动装的自己装，装不了的把命令摊在你面前
#   2. 没有源码就取一份（git clone 优先）
#   3. 建 .venv 并装依赖（官方源慢就自动切国内镜像）
#   4. 生成 config.json（**已存在就跳过，绝不覆盖**）
#   5. 空间桥接 npm ci（按 lock 哈希判断要不要重装）
#   6. 跑体检 doctor.py，把结果摊给你看
#
# 用法：
#   bash install.sh                  # 正常安装（缺系统依赖时会问一句）
#   bash install.sh --detect         # 只报告本机环境，一个文件都不动
#   bash install.sh --detect --json  # 环境报告的机器可读版（给 CI / 脚本消费）
#   bash install.sh --check          # 只检查、不动任何文件（CI 用）
#   bash install.sh --yes            # 无人值守：系统级依赖也不再问
#   bash install.sh --no-system      # 绝不碰系统：缺什么只打印命令
#   bash install.sh --dry-run        # 把要做的事全打印出来，什么都不做
#   bash install.sh --mirror off     # 镜像策略：auto（默认）/ off / tuna / aliyun
#   bash install.sh --with-browser   # 额外装 Playwright chromium（约 150 MB）
#   bash install.sh --allow-download # 允许下载官方 Python 安装器（仅 Windows，见下）
#   REPO_URL=... TARGET_DIR=... bash install.sh
#
# 关于「自动装系统依赖」的边界，写在下面 ensure_* 函数里，三条原则：
#   1. **先找、再用现成工具、最后才动系统包管理器** —— 能不装就不装；
#   2. 动系统包管理器一律先问一句（`--yes` 才跳过），`--no-system` 直接禁止；
#   3. **绝不「下载一段东西直接执行」**。Windows 上下载 python.org 安装器属于这一类，
#      所以只有你显式加 `--allow-download` 才会做。这也是 PR 里最常见的投毒手法，
#      本文件自己当然不能先这么干。

set -euo pipefail

SCHEMA=1
REPO_URL="${REPO_URL:-https://github.com/your-name/xuxingjiu.git}"
TARGET_DIR="${TARGET_DIR:-$PWD/xuxingjiu}"

CHECK=0; DETECT=0; JSON=0; YES=0; NO_SYSTEM=0; DRY=0; WITH_BROWSER=0; ALLOW_DOWNLOAD=0
MIRROR="auto"

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check)          CHECK=1 ;;
    --detect)         DETECT=1 ;;
    --json)           JSON=1 ;;
    --yes|-y)         YES=1 ;;
    --no-system)      NO_SYSTEM=1 ;;
    --dry-run)        DRY=1 ;;
    --with-browser)   WITH_BROWSER=1 ;;
    --allow-download) ALLOW_DOWNLOAD=1 ;;
    --mirror)         MIRROR="${2:-auto}"; shift ;;
    --mirror=*)       MIRROR="${1#*=}" ;;
    --target-dir)     TARGET_DIR="${2:?--target-dir 后面要跟路径}"; shift ;;
    --target-dir=*)   TARGET_DIR="${1#*=}" ;;
    --help|-h)        usage; exit 0 ;;
    *) echo "不认识的参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

case "$MIRROR" in auto|off|tuna|aliyun) ;; *) echo "--mirror 只认 auto/off/tuna/aliyun" >&2; exit 2 ;; esac

# --detect 与 --check 都是只读，两者叠加也不会写东西
readonly_maybe() { [ "$CHECK" = 1 ] || [ "$DETECT" = 1 ] || [ "$DRY" = 1 ]; }

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
die()  { printf '  ❌ %s\n' "$*" >&2; exit 1; }
note() { printf '     %s\n' "$*"; }
# 一条「本该执行的动作」：--dry-run 时只打印
mut() {
  if [ "$DRY" = 1 ] || [ "$CHECK" = 1 ] || [ "$DETECT" = 1 ]; then
    printf '  [dry] %s\n' "$*"
  else
    printf '  [run] %s\n' "$*"
    "$@"
  fi
}
# 问一句。非交互（CI）或 --yes 时按默认值走，绝不卡住等输入。
ask() {  # $1=问题  $2=默认 y/n
  local q="$1" d="${2:-n}"
  if [ "$YES" = 1 ]; then printf '  → %s [自动 Yes]\n' "$q"; return 0; fi
  if [ ! -t 0 ] || [ ! -t 1 ]; then
    printf '  → %s [非交互，按 %s 处理]\n' "$q" "$d"
    [ "$d" = y ] && return 0 || return 1
  fi
  local a=""
  printf '  ? %s [%s] ' "$q" "$([ "$d" = y ] && echo 'Y/n' || echo 'y/N')"
  read -r a || a=""
  a="${a:-$d}"
  case "$a" in y|Y|yes|YES|是) return 0 ;; *) return 1 ;; esac
}

# ── 0. 环境探测 ──────────────────────────────────────────────────────
# 这一整段**只读**：不建文件、不改配置、不等输入。--detect 就是把它打印出来。

UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
case "$UNAME_S" in
  MINGW*|MSYS*|CYGWIN*) OS_FAMILY=windows; IS_MSYS=1 ;;
  Darwin)               OS_FAMILY=macos;   IS_MSYS=0 ;;
  Linux)                OS_FAMILY=linux;   IS_MSYS=0 ;;
  *)                    OS_FAMILY=unknown; IS_MSYS=0 ;;
esac
OS_ARCH="$(uname -m 2>/dev/null || echo unknown)"

os_name() {
  case "$OS_FAMILY" in
    linux)
      if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        printf '%s' "${PRETTY_NAME:-${NAME:-Linux}}"
      else
        printf 'Linux'
      fi ;;
    macos)
      printf 'macOS %s' "$(sw_vers -productVersion 2>/dev/null || echo '')" ;;
    windows)
      # Git Bash 里 cmd 要写成 //c，否则 MSYS 会把 /c 当路径展开
      local v build
      v="$(cmd.exe //c ver 2>/dev/null | tr -d '\r' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
      # ver 永远说 "10.0.<build>"，Win11 也是。build >= 22000 才是 11 —— 不这么判，
      # 报告里写「Windows 10」而用户明明装着 11，看着就像探测不准。
      build="${v##*.}"
      if [ -n "$build" ] && [ "$build" -ge 22000 ] 2>/dev/null; then
        printf 'Windows 11（build %s）' "$build"
      elif [ -n "$v" ]; then
        printf 'Windows %s' "$v"
      else
        printf 'Windows（版本没探到，不影响安装）'
      fi ;;
    *) printf 'Unknown' ;;
  esac
}
OS_NAME="$(os_name)"

# 容器 / WSL 会影响「怎么装 QQ 接入端」的建议，探测一下没坏处
IN_CONTAINER=0
{ [ -f /.dockerenv ] || grep -qiE '(docker|containerd|podman)' /proc/1/cgroup 2>/dev/null; } && IN_CONTAINER=1
IN_WSL=0
grep -qi microsoft /proc/version 2>/dev/null && IN_WSL=1

# --- 包管理器 ---
PKG=none; PKG_VER=""; NEED_ROOT=1
if [ "$OS_FAMILY" = windows ]; then
  if command -v winget >/dev/null 2>&1; then PKG=winget; NEED_ROOT=0; fi
  command -v choco  >/dev/null 2>&1 && [ "$PKG" = none ] && { PKG=choco; NEED_ROOT=1; }
  command -v scoop  >/dev/null 2>&1 && [ "$PKG" = none ] && { PKG=scoop; NEED_ROOT=0; }
elif [ "$OS_FAMILY" = macos ]; then
  command -v brew >/dev/null 2>&1 && { PKG=brew; NEED_ROOT=0; }
else
  for m in apt-get dnf yum pacman zypper apk; do
    command -v "$m" >/dev/null 2>&1 && { PKG="$m"; break; }
  done
fi
case "$PKG" in
  winget) PKG_VER="$(winget --version 2>/dev/null | tr -d '\r' | head -1 || true)" ;;
  brew)   PKG_VER="$(brew --version 2>/dev/null | head -1 || true)" ;;
  none)   PKG_VER="" ;;
  *)      PKG_VER="$("$PKG" --version 2>/dev/null | head -1 || true)" ;;
esac

# --- 提权能力 ---
SUDO=""; IS_ROOT=0
if [ "$(id -u 2>/dev/null || echo 1)" = 0 ]; then IS_ROOT=1; fi
if [ "$IS_ROOT" = 0 ] && command -v sudo >/dev/null 2>&1; then SUDO=sudo; fi
# 提权命令（需要 root 的动作都用它包一层；没有就失败，由调用方给出提示）
as_root() {
  if [ "$IS_ROOT" = 1 ]; then mut "$@"
  elif [ -n "$SUDO" ]; then mut "$SUDO" "$@"
  else return 1
  fi
}

# --- Python ---
# 明确列出的优先（避免不同机器上 python3 指向不一致）；含 3.14 以兼容新版 Python。
# 兜底再扫 python3.11~3.20：防止系统只装了某个小版本（如 3.14）且没建 python3 软链。
PY=""; PY_VER=""; PY_PATH=""; PY_VENV_MOD=0; PY_LAYOUT=bin
_py_version_of() {
  # 只认形如 "3.13.2" 的输出。Microsoft Store 的 python3 是「应用执行别名」假壳：
  # 它能被 command -v 找到，但一执行就打印 "Python was not found; run without
  # arguments to install from the Microsoft Store..."，版本号解析不出来 —— 正好被这里筛掉。
  "$1" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null | tr -d '\r' | head -1
}
_try_py() {
  local c="$1" v=""
  case "$c" in */*) [ -x "$c" ] || return 1 ;; *) command -v "$c" >/dev/null 2>&1 || return 1 ;; esac
  v="$(_py_version_of "$c" || true)"
  case "$v" in [0-9]*.[0-9]*.[0-9]*) ;; *) return 1 ;; esac
  # 注意别写成 `local maj="${v%%.*}" min="${maj#*.}"` —— local 的右侧展开发生在
  # 变量真正声明之前，第二个引用会撞上 set -u 的 unbound variable。
  local maj min tail
  maj="${v%%.*}"
  tail="${v#*.}"
  min="${tail%%.*}"
  [ "$maj" -eq 3 ] && [ "$min" -ge 11 ] || return 1
  PY="$c"; PY_VER="$v"
  return 0
}
detect_python() {
  PY=""; PY_VER=""; PY_PATH=""
  local c n
  for c in python3.14 python3.13 python3.12 python3.11 python3 python py; do
    _try_py "$c" && break
  done
  if [ -z "$PY" ]; then
    for n in $(seq 11 20); do
      c="python3.$n"
      _try_py "$c" && break
    done
  fi
  if [ -z "$PY" ]; then
    # 常见非 PATH 安装位置（pyenv / Homebrew / 官方安装器默认目录 / MSYS）
    local cands=""
    if [ "$OS_FAMILY" = macos ]; then
      cands="$(ls -1 /opt/homebrew/bin/python3.* /usr/local/bin/python3.* 2>/dev/null || true)"
    elif [ "$OS_FAMILY" = windows ]; then
      cands="$(ls -1 "$LOCALAPPDATA"/Programs/Python/Python3*/python.exe \
                     /c/Python3*/python.exe \
                     "/c/Program Files/Python3"*/python.exe 2>/dev/null || true)"
    else
      cands="$(ls -1 /usr/bin/python3.* /usr/local/bin/python3.* 2>/dev/null || true)"
    fi
    cands="$cands $(ls -1 "$HOME"/.pyenv/shims/python3.1* 2>/dev/null || true)"
    for c in $cands; do _try_py "$c" && break; done
  fi
  if [ -z "$PY" ] && command -v uv >/dev/null 2>&1; then
    c="$(uv python find 2>/dev/null | tr -d '\r' | head -1 || true)"
    [ -n "$c" ] && _try_py "$c"
  fi
  if [ -n "$PY" ]; then
    PY_PATH="$(_py_path "$PY")"
    case "$PY_PATH" in *Scripts/python.exe|*Scripts/python) PY_LAYOUT=scripts ;; esac
    "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1 && PY_VENV_MOD=1 || PY_VENV_MOD=0
  fi
}
_py_path() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *) command -v "$1" 2>/dev/null | head -1 ;;
  esac
}
detect_python

# --- Node / npm ---
NODE=""; NODE_VER=""; NPM_VER=""; NODE_OK=0
if command -v node >/dev/null 2>&1; then
  NODE=node
  NODE_VER="$(node --version 2>/dev/null | tr -d '\r' | head -1 || true)"
  # package.json 声明 engines.node>=18，太老的直接标不合格（后面提示升级）
  local_maj="${NODE_VER#v}"; local_maj="${local_maj%%.*}"
  if [ -n "${local_maj:-}" ] && [ "$local_maj" -ge 18 ] 2>/dev/null; then NODE_OK=1; fi
fi
command -v npm >/dev/null 2>&1 && NPM_VER="$(npm --version 2>/dev/null | tr -d '\r' | head -1 || true)"

# --- git ---
GIT_VER=""
command -v git >/dev/null 2>&1 && GIT_VER="$(git --version 2>/dev/null | tr -d '\r' | head -1 || true)"

# --- 现成的多版本工具链 ---
# 有这些的话，「补 Python」可以完全不碰系统：uv 能自己下一份独立 CPython，
# pyenv/conda 能装到用户目录。探测出来先摆给用户看，省得他以为非 sudo 不可。
TOOLCHAINS=""
for t in uv pyenv conda; do
  command -v "$t" >/dev/null 2>&1 && TOOLCHAINS="$TOOLCHAINS $t"
done
TOOLCHAINS="${TOOLCHAINS# }"

# --- 网络 & 镜像 ---
# 只探 2 秒，探不到就当不可达 —— 探测失败绝不能把安装搞挂。
probe() {
  command -v curl >/dev/null 2>&1 || { printf ''; return 0; }
  curl -sS -m 2 -o /dev/null -w '%{time_total}' "$1" 2>/dev/null || printf ''
}
T_PYPI="$(probe https://pypi.org/simple/ || true)"
T_TUNA="$(probe https://pypi.tuna.tsinghua.edu.cn/simple/ || true)"
T_NPM="$(probe https://registry.npmjs.org/ || true)"
T_NPMMIRROR="$(probe https://registry.npmmirror.com/ || true)"

MIRROR_PIP=""; MIRROR_PIP_NAME="官方 pypi.org"
MIRROR_NPM=""; MIRROR_NPM_NAME="官方 registry.npmjs.org"
_slow() { [ -z "$1" ] || awk "BEGIN{exit !($1 > $2)}" 2>/dev/null; }
case "$MIRROR" in
  off) : ;;
  tuna)
    MIRROR_PIP=https://pypi.tuna.tsinghua.edu.cn/simple; MIRROR_PIP_NAME="清华 pypi.tuna"
    MIRROR_NPM=https://registry.npmmirror.com; MIRROR_NPM_NAME="npmmirror" ;;
  aliyun)
    MIRROR_PIP=https://mirrors.aliyun.com/pypi/simple/; MIRROR_PIP_NAME="阿里 mirrors.aliyun"
    MIRROR_NPM=https://registry.npmmirror.com; MIRROR_NPM_NAME="npmmirror" ;;
  auto)
    # 只在「官方不可达」或「官方明显比镜像慢」时切，避免无缘无故改下载源
    if [ -n "$T_TUNA" ] && { [ -z "$T_PYPI" ] || { _slow "$T_PYPI" 3 && ! _slow "$T_TUNA" 1; }; }; then
      MIRROR_PIP=https://pypi.tuna.tsinghua.edu.cn/simple; MIRROR_PIP_NAME="清华 pypi.tuna（官方源慢/不通，已自动切换）"
    fi
    if [ -n "$T_NPMMIRROR" ] && { [ -z "$T_NPM" ] || { _slow "$T_NPM" 3 && ! _slow "$T_NPMMIRROR" 1; }; }; then
      MIRROR_NPM=https://registry.npmmirror.com; MIRROR_NPM_NAME="npmmirror（官方源慢/不通，已自动切换）"
    fi ;;
esac

# --- 目标目录 ---
DIR_WRITABLE=0; DIR_FREE_MB=""
_probe_dir() {
  local d="$1"
  [ -d "$d" ] || d="$(dirname "$d")"
  [ -w "$d" ] 2>/dev/null && DIR_WRITABLE=1
  DIR_FREE_MB="$(df -Pm "$d" 2>/dev/null | awk 'NR==2{print $4}' || true)"
}
_probe_dir "$TARGET_DIR"

_secs() { [ -n "${1:-}" ] && printf '%ss' "$1" || printf '不可达'; }

env_table() {
  printf '  操作系统      %s（%s，%s）\n' "$OS_NAME" "$OS_FAMILY" "$OS_ARCH"
  [ "$IS_MSYS" = 1 ] && printf '  运行环境      Git Bash / MSYS（Windows 上的 POSIX 层）\n'
  [ "$IN_CONTAINER" = 1 ] && printf '  运行环境      容器内\n'
  [ "$IN_WSL" = 1 ] && printf '  运行环境      WSL\n'
  printf '  包管理器      %s%s\n' "$PKG" "${PKG_VER:+（$PKG_VER）}"
  if [ "$NEED_ROOT" = 1 ]; then
    if [ "$IS_ROOT" = 1 ]; then printf '  提权          已是 root\n'
    elif [ -n "$SUDO" ]; then printf '  提权          可用 sudo（装系统包时会问密码）\n'
    else printf '  提权          没有 sudo 也不是 root —— 系统级依赖装不了，只会给你命令\n'; fi
  fi
  if [ -n "$PY" ]; then
    printf '  Python        %s（%s，%s）\n' "$PY_VER" "$PY" "$PY_PATH"
    [ "$PY_VENV_MOD" = 0 ] && printf '                ⚠️ 缺 venv/ensurepip 模块（Debian/Ubuntu 上要另装 python3-venv）\n'
  else
    printf '  Python        未找到 3.11+\n'
  fi
  if [ -n "$NODE" ]; then
    printf '  Node / npm    %s / %s%s\n' "$NODE_VER" "${NPM_VER:-未找到}" \
      "$([ "$NODE_OK" = 1 ] && echo '' || echo '  ⚠️ Node 太旧（空间桥接要 >=18）')"
  else
    printf '  Node / npm    未找到（只有空间桥接需要，纯聊天不影响）\n'
  fi
  printf '  git           %s\n' "${GIT_VER:-未找到（要 clone 源码就得有）}"
  printf '  工具链        %s\n' "${TOOLCHAINS:-无 uv / pyenv / conda（补 Python 时就得动系统或手动装）}"
  printf '  网络          pypi.org %s / pypi.tuna %s\n' "$(_secs "$T_PYPI")" "$(_secs "$T_TUNA")"
  printf '                npmjs.org %s / npmmirror %s\n' "$(_secs "$T_NPM")" "$(_secs "$T_NPMMIRROR")"
  printf '  下载源        pip: %s\n' "$MIRROR_PIP_NAME"
  printf '                npm: %s\n' "$MIRROR_NPM_NAME"
  printf '  目标目录      %s%s%s\n' "$TARGET_DIR" \
    "$([ "$DIR_WRITABLE" = 1 ] && echo '（可写）' || echo '（不可写！）')" \
    "${DIR_FREE_MB:+，剩余 ${DIR_FREE_MB} MB}"
}

env_json() {
  local py_found=false node_found=false git_found=false
  [ -n "$PY" ] && py_found=true
  [ -n "$NODE" ] && node_found=true
  [ -n "$GIT_VER" ] && git_found=true
  _j() { printf '%s' "$1" | sed 's/\\/\//g; s/"/\\"/g'; }
  _n() { if [ -n "${1:-}" ]; then printf '%s' "$1"; else printf 'null'; fi; }
  _j_list() {  # 空格分隔的工具名 → JSON 字符串数组
    local out="" w
    for w in $1; do out="$out${out:+, }\"$w\""; done
    printf '%s' "$out"
  }
  cat <<EOF
{
  "schema": $SCHEMA,
  "os": {"family": "$OS_FAMILY", "name": "$(_j "$OS_NAME")", "arch": "$OS_ARCH", "msys": $([ "$IS_MSYS" = 1 ] && echo true || echo false), "container": $([ "$IN_CONTAINER" = 1 ] && echo true || echo false), "wsl": $([ "$IN_WSL" = 1 ] && echo true || echo false)},
  "shell": "${SHELL:-unknown}",
  "toolchains": [$(_j_list "$TOOLCHAINS")],
  "pkg": {"name": "$PKG", "version": "$(_j "$PKG_VER")", "need_root": $([ "$NEED_ROOT" = 1 ] && echo true || echo false), "can_root": $([ "$IS_ROOT" = 1 ] || [ -n "$SUDO" ] && echo true || echo false)},
  "python": {"found": $py_found, "cmd": "$PY", "version": "$PY_VER", "path": "$(_j "$PY_PATH")", "ok": $py_found, "venv_module": $([ "$PY_VENV_MOD" = 1 ] && echo true || echo false), "layout": "$PY_LAYOUT"},
  "node": {"found": $node_found, "version": "$NODE_VER", "npm": "$NPM_VER", "ok": $([ "$NODE_OK" = 1 ] && echo true || echo false)},
  "git": {"found": $git_found, "version": "$(_j "$GIT_VER")"},
  "net": {"pypi": $(_n "$T_PYPI"), "tuna": $(_n "$T_TUNA"), "npm": $(_n "$T_NPM"), "npmmirror": $(_n "$T_NPMMIRROR"), "mirror_pip": $(_n "$MIRROR_PIP"), "mirror_npm": $(_n "$MIRROR_NPM")},
  "dir": {"target": "$(_j "$TARGET_DIR")", "writable": $([ "$DIR_WRITABLE" = 1 ] && echo true || echo false), "free_mb": $(_n "$DIR_FREE_MB")}
}
EOF
}

if [ "$DETECT" = 1 ]; then
  if [ "$JSON" = 1 ]; then env_json; else
    say "环境探测（一个文件都没动）"
    env_table
    printf '\n  提示：--detect 只报告。要装东西就跑不带参数的 bash install.sh。\n'
  fi
  exit 0
fi

say "0/7 环境探测"
env_table

# ── 1. 缺什么补什么 ──────────────────────────────────────────────────

_system_hint() {  # 没有包管理器 / 没权限时，把能粘贴的命令给出来
  case "$1" in
    python)
      case "$OS_FAMILY:$PKG" in
        linux:apt-get) printf 'sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip' ;;
        linux:dnf|linux:yum) printf 'sudo dnf install -y python3 python3-pip' ;;
        linux:pacman) printf 'sudo pacman -S --needed python python-pip' ;;
        linux:zypper) printf 'sudo zypper install -y python3 python3-pip' ;;
        linux:apk) printf 'sudo apk add python3 py3-pip' ;;
        macos:*) printf 'brew install python@3.13' ;;
        windows:*) printf 'winget install --id Python.Python.3.13 -e --scope user' ;;
        *) printf '到 https://www.python.org/downloads/ 下 3.11+ 装好，勾上 "Add to PATH"' ;;
      esac ;;
    node)
      case "$OS_FAMILY:$PKG" in
        linux:apt-get) printf 'sudo apt-get install -y nodejs npm   # 要更新的版本见 docs/部署-Linux.md' ;;
        linux:dnf|linux:yum) printf 'sudo dnf install -y nodejs npm' ;;
        linux:pacman) printf 'sudo pacman -S --needed nodejs npm' ;;
        linux:zypper) printf 'sudo zypper install -y nodejs npm' ;;
        linux:apk) printf 'sudo apk add nodejs npm' ;;
        macos:*) printf 'brew install node' ;;
        windows:*) printf 'winget install --id OpenJS.NodeJS.LTS -e --scope user' ;;
        *) printf '到 https://nodejs.org/ 下 LTS' ;;
      esac ;;
    git)
      case "$OS_FAMILY:$PKG" in
        linux:apt-get) printf 'sudo apt-get install -y git' ;;
        linux:dnf|linux:yum) printf 'sudo dnf install -y git' ;;
        linux:pacman) printf 'sudo pacman -S --needed git' ;;
        linux:zypper) printf 'sudo zypper install -y git' ;;
        linux:apk) printf 'sudo apk add git' ;;
        macos:*) printf 'brew install git' ;;
        windows:*) printf 'winget install --id Git.Git -e' ;;
        *) printf '到 https://git-scm.com/downloads 装' ;;
      esac ;;
  esac
}

# 装系统包：先问一句（除非 --yes / 非交互默认），--no-system 直接拒绝
_pkg_install() {  # $1 = 人类可读的说明  剩下 = 包名
  local what="$1"; shift
  if [ "$NO_SYSTEM" = 1 ]; then
    warn "--no-system：不碰系统包管理器。请自己跑：bash install.sh --detect 看建议命令"
    return 1
  fi
  if [ "$PKG" = none ]; then
    warn "这台机器上没有可用的包管理器，装不了；也没关系 —— 见下面的手动命令"
    return 1
  fi
  if [ "$NEED_ROOT" = 1 ] && [ "$IS_ROOT" = 0 ] && [ -z "$SUDO" ]; then
    warn "装 $what 需要管理员权限，但当前既不是 root 也没有 sudo"
    return 1
  fi
  ask "用 $PKG 装 $what？（会动系统，装完可以用包管理器卸掉）" n || { warn "跳过装 $what"; return 1; }
  case "$PKG" in
    apt-get) as_root apt-get update && as_root apt-get install -y "$@" ;;
    dnf|yum) as_root "$PKG" install -y "$@" ;;
    pacman)  as_root pacman -S --needed --noconfirm "$@" ;;
    zypper)  as_root zypper install -y "$@" ;;
    apk)     as_root apk add "$@" ;;
    brew)    mut brew install "$@" ;;
    winget)
      local ids=""
      for p in "$@"; do
        case "$p" in
          python) ids="Python.Python.3.13" ;;
          node)   ids="OpenJS.NodeJS.LTS" ;;
          git)    ids="Git.Git" ;;
          *)      ids="$p" ;;
        esac
        # --scope user：装在用户目录，不要管理员，也不动系统 Python
        mut winget install --id "$ids" -e --scope user --silent \
            --accept-package-agreements --accept-source-agreements || warn "winget 装 $ids 失败（已装的可以先忽略）"
      done ;;
    choco)   as_root choco install -y "$@" ;;
    scoop)   mut scoop install "$@" ;;
  esac
}

install_python() {
  [ -n "$PY" ] && return 0
  say "1/7 补 Python 3.11+"
  # ① uv：用户级下载一份独立 CPython，不动系统、不要管理员，最干净的一条路
  if command -v uv >/dev/null 2>&1 && [ "$NO_SYSTEM" != 1 ]; then
    if ask "用 uv 装一份独立的 Python 3.12（用户级，不动系统 Python）？" y; then
      mut uv python install 3.12 || warn "uv python install 失败"
      detect_python
      [ -n "$PY" ] && { ok "uv 装好了：$PY_VER（$PY_PATH）"; return 0; }
    fi
  fi
  # ② 系统包管理器。
  #    这里必须写成 if 而不是 `_pkg_install ... && { ...; [ -n "$PY" ] && {...}; }` ——
  #    如果装在 PATH 里但探测不到（新装的 Python 常要重开终端），那个 && 链最后一项
  #    会返回 1，在 set -e 下直接把整个脚本静默干掉，用户看到的是一片空白。
  local pkgspec="Python 3"
  [ "$OS_FAMILY" = windows ] && pkgspec="Python 3.13"
  if _pkg_install "$pkgspec" python; then
    detect_python
    if [ -n "$PY" ]; then ok "装好了：$PY_VER（$PY）"; return 0; fi
    warn "装是装完了，但这个终端里还看不到它 —— 重开一个终端再跑一次本脚本"
  fi
  # ③ 兜底：Windows 上直接下官方安装器。**只有显式 --allow-download 才做** ——
  #    「下载一段东西直接执行」正是本仓库警告过的攻击面，默认不能替用户决定。
  if [ "$OS_FAMILY" = windows ] && { [ "$ALLOW_DOWNLOAD" = 1 ] || [ "$YES" = 1 ]; }; then
    local tmp exe
    tmp="$(mktemp -d)"; exe="$tmp/python-setup.exe"
    local url="https://www.python.org/ftp/python/3.13.7/python-3.13.7-amd64.exe"
    warn "即将下载并静默安装官方安装器（用户级、会加到 PATH）：$url"
    if ask "确认下载并执行 python.org 官方安装器？" n; then
      mut curl -fL -o "$exe" "$url" && \
        mut "$exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 && \
        warn "装完了 —— 但 PATH 要新开一个终端才生效，请重开 Git Bash 再跑一次本脚本" || \
        warn "下载/安装失败：$url 打不开就手动去 python.org 下"
    else
      warn "跳过；手动装：$(_system_hint python)"
    fi
    rm -rf "$tmp" 2>/dev/null || true
  fi
  if [ -z "$PY" ]; then
    warn "还是没找到 Python 3.11+。手动装："
    note "$(_system_hint python)"
    die "缺少 Python，安装无法继续（装完重开终端再跑一遍本脚本）"
  fi
}

install_node() {
  [ -n "$NODE" ] && [ "$NODE_OK" = 1 ] && return 0
  # node 只有空间桥接要，纯聊天不用 —— 但我们不替你决定要不要桥接，
  # 所以这里只在「有 qzone-bridge 目录」且用户愿意装时才动手。
  [ -d "${ROOT:-}/qzone-bridge" ] || return 0
  say "补 Node.js（空间桥接需要，>=18）"
  if [ -n "$NODE" ] && [ "$NODE_OK" = 0 ]; then
    warn "现有 Node $NODE_VER 太旧（要 >=18），建议升级"
  else
    warn "没找到 Node"
  fi
  # 同样写成 if：`... && { command -v node && detect_node_again; }` 在装成功但
  # 当前终端仍看不到 node 时会让 && 链返回 1，set -e 直接把脚本干掉。
  if _pkg_install "Node.js" node; then
    command -v node >/dev/null 2>&1 && detect_node_again
  fi
  if ! command -v node >/dev/null 2>&1; then
    warn "没装成 —— 空间桥接会跳过。手动装："
    note "$(_system_hint node)"
  fi
}
detect_node_again() {
  NODE_VER="$(node --version 2>/dev/null | tr -d '\r' | head -1 || true)"
  local m="${NODE_VER#v}"; m="${m%%.*}"
  if [ -n "$m" ] && [ "$m" -ge 18 ] 2>/dev/null; then NODE_OK=1; NODE=node; ok "Node $NODE_VER 就绪"; else warn "Node $NODE_VER 仍不满足 >=18"; fi
}

# ── 2. 源码 ──────────────────────────────────────────────────────────
say "2/7 源码"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SELF_DIR/qqbot/bot.py" ]; then
  ROOT="$SELF_DIR"
  ok "就在当前目录：$ROOT"
elif [ -d "$TARGET_DIR/qqbot" ]; then
  ROOT="$TARGET_DIR"
  ok "已有源码：$ROOT"
elif [ "$CHECK" = 1 ] || [ "$DETECT" = 1 ]; then
  die "找不到源码（--check / --detect 模式不会去下载）"
else
  if ! command -v git >/dev/null 2>&1; then
    say "缺 git（clone 源码要用）"
    _pkg_install "git" git && command -v git >/dev/null && GIT_VER="$(git --version 2>/dev/null | head -1)"
  fi
  if command -v git >/dev/null 2>&1; then
    printf '  → git clone %s %s\n' "$REPO_URL" "$TARGET_DIR"
    mut git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
    ROOT="$TARGET_DIR"
    ok "已 clone 到 $ROOT"
  else
    die "没有 git，也没装成。请手动下载 Release 源码包解压，再在本目录里跑本脚本"
  fi
fi

QQBOT="$ROOT/qqbot"
[ -f "$QQBOT/requirements.txt" ] || die "缺 $QQBOT/requirements.txt，源码不完整"

install_python
install_node

# ── 3. venv + 依赖 ───────────────────────────────────────────────────
say "3/7 Python 依赖"
# venv 里的解释器按平台走：Windows（含 Git Bash 下的 Windows Python）是 Scripts/，POSIX 是 bin/
if [ "$PY_LAYOUT" = scripts ] || [ "$OS_FAMILY" = windows ]; then
  VENV_PY="$QQBOT/.venv/Scripts/python.exe"
  [ -x "$VENV_PY" ] || VENV_PY="$QQBOT/.venv/Scripts/python"
else
  VENV_PY="$QQBOT/.venv/bin/python"
fi
# 用 venv 里的 python 反查一次，免得平台判断错
_venv_probe() {
  for p in "$QQBOT/.venv/Scripts/python.exe" "$QQBOT/.venv/bin/python" "$QQBOT/.venv/bin/python3"; do
    [ -x "$p" ] && { VENV_PY="$p"; return 0; }
  done
  return 1
}
if _venv_probe; then
  ok "已有 .venv，跳过创建"
elif readonly_maybe; then
  warn "没有 .venv（--check / --detect / --dry-run 模式不创建）"
else
  # Debian/Ubuntu 上 python3-venv 是**单独一个包**，没有它 `python -m venv` 会失败，
  # 而且报错信息很难懂（"ensurepip is not available"）。所以先探再用，失败就装上重试一次。
  if [ "$PY_VENV_MOD" = 0 ]; then
    warn "$PY 缺 venv/ensurepip 模块（Debian/Ubuntu 上要单独装 python3-venv）"
    if _pkg_install "python3-venv" python3-venv; then
      detect_python
    fi
  fi
  mut "$PY" -m venv "$QQBOT/.venv" || {
    warn "创建 venv 失败，再试一次补装 venv 模块"
    _pkg_install "python3-venv" python3-venv >/dev/null 2>&1 || true
    detect_python
    "$PY" -m venv "$QQBOT/.venv" || die "建不出 venv。手动：$PY -m venv '$QQBOT/.venv'"
  }
  _venv_probe && ok "已创建 $VENV_PY"
fi

if [ -x "$VENV_PY" ] && ! readonly_maybe; then
  # ★ 必须用 venv 里的 python -m pip：用系统 pip 会装到全局，然后 import 失败
  PIP_ARGS=(install)
  [ -n "$MIRROR_PIP" ] && PIP_ARGS+=(-i "$MIRROR_PIP")
  mut "$VENV_PY" -m pip install --upgrade pip -q "${PIP_ARGS[@]:1}" || \
    warn "pip 升级失败（不影响后面的依赖安装，继续）"
  if mut "$VENV_PY" -m pip install -r "$QQBOT/requirements.txt" -q "${PIP_ARGS[@]:1}"; then
    ok "依赖装好${MIRROR_PIP:+（走 $MIRROR_PIP_NAME）}"
  elif [ -z "$MIRROR_PIP" ] && [ -n "$T_TUNA" ]; then
    warn "直连官方源装失败了，换清华源重试一次"
    mut "$VENV_PY" -m pip install -r "$QQBOT/requirements.txt" -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
      && ok "依赖装好（清华源）" || die "依赖还是装不上，手工：$VENV_PY -m pip install -r requirements.txt"
  else
    die "依赖装不上，手工：$VENV_PY -m pip install -r requirements.txt"
  fi
fi

# ── 4. config.json ───────────────────────────────────────────────────
say "4/7 配置"
if [ -f "$QQBOT/config.json" ]; then
  ok "config.json 已存在，**不覆盖**（这条最重要：覆盖会把你的 token/QQ 号弄丢）"
elif [ -f "$QQBOT/config.example.json" ] && ! readonly_maybe; then
  cp "$QQBOT/config.example.json" "$QQBOT/config.json"
  ok "已从模板生成 config.json —— 记得改 bot_qq / admin.user_ids / access_token"
else
  warn "没有 config.json"
fi

# ── 5. 空间桥接（可选） ──────────────────────────────────────────────
say "5/7 空间桥接（可选）"
BRIDGE="$ROOT/qzone-bridge"
if [ ! -d "$BRIDGE" ]; then
  warn "没有 qzone-bridge 目录，跳过"
elif ! command -v npm >/dev/null 2>&1; then
  warn "没有 npm，跳过（装完 node 后：cd qzone-bridge && npm ci && npm run build）"
  note "$(_system_hint node)"
elif readonly_maybe; then
  warn "跳过安装（--check / --detect / --dry-run）"
else
  # 只在 lock 变了或没装过时才重装 —— npm ci 会删掉整个 node_modules 重下几百 MB
  LOCK_HASH_FILE="$BRIDGE/node_modules/.lock-hash"
  WANT_HASH="$( (command -v shasum >/dev/null && shasum -a 256 "$BRIDGE/package-lock.json") \
                || sha256sum "$BRIDGE/package-lock.json" | awk '{print $1}')"
  NPM_ARGS=(ci --no-audit --no-fund)
  [ -n "$MIRROR_NPM" ] && NPM_ARGS+=(--registry="$MIRROR_NPM")
  if [ -f "$LOCK_HASH_FILE" ] && [ "$(cat "$LOCK_HASH_FILE" 2>/dev/null)" = "$WANT_HASH" ]; then
    ok "依赖已是最新（lock 未变），跳过 npm ci"
  else
    if ( cd "$BRIDGE" && npm "${NPM_ARGS[@]}" ); then
      printf '%s' "$WANT_HASH" > "$LOCK_HASH_FILE"
      ok "依赖装好${MIRROR_NPM:+（走 $MIRROR_NPM_NAME）}"
    elif [ -z "$MIRROR_NPM" ] && [ -n "$T_NPMMIRROR" ]; then
      warn "官方 npm 源装失败，换 npmmirror 重试一次"
      ( cd "$BRIDGE" && npm ci --no-audit --no-fund --registry=https://registry.npmmirror.com ) \
        && { printf '%s' "$WANT_HASH" > "$LOCK_HASH_FILE"; ok "依赖装好（npmmirror）"; } \
        || warn "npm 依赖装失败，稍后手工：cd qzone-bridge && npm ci"
    else
      warn "npm 依赖装失败，稍后手工：cd qzone-bridge && npm ci"
    fi
  fi
  if [ ! -f "$BRIDGE/dist/main.js" ] && [ ! -f "$BRIDGE/.env" ]; then
    warn "还没配 .env —— 复制 .env.example 改名并填 QQ 空间 Cookie"
  fi
  # Playwright 的 chromium 只在「刷新 Cookie」时才用得上，150 MB 左右，
  # 默认不装，避免纯聊天用户白等十分钟。
  if [ "$WITH_BROWSER" = 1 ]; then
    mut npm --prefix "$BRIDGE" exec -- playwright install chromium \
      || warn "chromium 没装成，手工：cd qzone-bridge && npx playwright install chromium"
  else
    note "需要浏览器刷 Cookie 时再加：bash install.sh --with-browser"
  fi
fi

# ── 6. 体检 ──────────────────────────────────────────────────────────
say "6/7 体检"
DOCTOR_RC=1
if [ -x "$VENV_PY" ] && ! readonly_maybe; then
  set +e
  "$VENV_PY" "$QQBOT/tools/doctor.py"
  DOCTOR_RC=$?
  set -e
else
  warn "没有 venv 或处于只读模式，跳过体检"
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
  ℹ️ 想知道本机环境长什么样：bash install.sh --detect（只读，不装东西）
EOF

exit $DOCTOR_RC
