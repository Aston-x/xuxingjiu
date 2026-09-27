#!/usr/bin/env bash
# 发布前自查：确认仓库里没有密钥、没有真实 QQ 号、没有本机路径。
#
# 和 CI 里的 secret-scan 用同一套规则，但更快、能本地跑。
#
#   bash tools/preflight.sh          # 有问题退出码 1
#
# 注意：这里只 grep **工作区**。git 历史要另外查（本仓库历史从 v1.0.0 起是干净的）：
#   git log --all -p | grep -nE "sk-[A-Za-z0-9]{20,}|skey=|SESSDATA=" | head
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
FAIL=0

EXCL='--exclude-dir=node_modules --exclude-dir=.git --exclude-dir=.venv
      --exclude-dir=__pycache__ --exclude-dir=dist --exclude-dir=test_cache
      --exclude-dir=state --exclude-dir=generated
      --exclude=package-lock.json --exclude=*.log --exclude=_tmp_* --exclude=*_state.json'

scan() {
  local title="$1" pattern="$2" allow="$3"
  local hits
  hits=$(grep -rInE $EXCL -- "$pattern" . 2>/dev/null | grep -vE "$allow" | head -20)
  if [ -n "$hits" ]; then
    printf '\n\033[31m✘ %s\033[0m\n%s\n' "$title" "$hits"
    FAIL=1
  else
    printf '\033[32m✔ %s\033[0m\n' "$title"
  fi
}

echo "发布前自查（工作区）"
echo "----------------------------------------"

# 1) 真密钥：sk- 后面跟 20 位以上。示例值/占位符一律排除。
scan "没有明文 API Key" \
     'sk-[A-Za-z0-9_-]{20,}' \
     'REDACTED|xxxx|XXXX|your|example|占位|sk-\.\.\.|sk-xxx'

# 2) Cookie / 登录态。文档里的 uin=o你的QQ、skey=xxx 这类占位要放过。
scan "没有真实 Cookie / 登录态" \
     '(SESSDATA|bili_jct|superkey|p_skey)=[[:alnum:]%@_.-]{12,}' \
     'xxx|yyy|XXX|zzz|example|demo|placeholder'

# 3) 疑似真实 QQ 号（10 位、以 13-19 开头）。
#    测试夹具里的号已经全换成 10001 起的合成号，所以这里命中基本就是真货。
#    但**时间戳也是 10 位数字**（1709012345 这种），文档里的接口示例用的是它，
#    所以把已知的示例时间戳加进白名单 —— 否则每次都会误报一堆。
scan "没有真实 QQ 号" \
     '(^|[^0-9.])(1[3-9][0-9]{8})([^0-9.]|$)' \
     '10001|10002|10003|10004|10005|示例|example|1709012345|1700000000|1700000001|1700000002|1770380359|1774000000|1774139725|1770[0-9]{6}|17[0-9]{8}'

# 4) 本机路径 / 用户名
scan "没有本机绝对路径" \
     '(C:\\\\Users\\\\[A-Za-z]+|/home/[a-z]+/|D:\\\\AI|B:\\\\QQ|G:\\\\AI)' \
     'example|Users\\\\Oracle\\\\WorkBuddy' # 允许 CI 自己的路径

# 5) 不该被跟踪的文件
BAD=$(git ls-files 2>/dev/null | grep -E '(^|/)(config\.json|\.env)$|_state\.json$|memory\.json$|bili_state\.json$|\.log$' || true)
if [ -n "$BAD" ]; then
  printf '\n\033[31m✘ 有不该进版本库的文件被跟踪了\033[0m\n%s\n' "$BAD"
  FAIL=1
else
  printf '\033[32m✔ 没有敏感的配置文件被跟踪\033[0m\n'
fi

# 6) 脚本编码与换行符。
#    这条是用血换来的：install.bat 曾经整篇中文注释、而 chcp 65001 压在第 11 行，
#    cmd.exe 按 GBK 逐行解码批处理时把中文的半个字节错配出去，
#    整个脚本散架成一片「'xxx' 不是内部或外部命令」，连 install.ps1 都变成
#    'nstall.ps1'。这种问题本地跑测试抓不到 —— 只能在这里守。
PY_BIN=""
for _c in python python3 py; do
  if command -v "$_c" >/dev/null 2>&1; then PY_BIN="$_c"; break; fi
done
if [ -z "$PY_BIN" ]; then
  printf '\033[33m~ 环境里没有 python，跳过脚本编码检查\033[0m\n'
elif [ ! -f tools/normalize_scripts.py ]; then
  printf '\033[33m~ 找不到 tools/normalize_scripts.py，跳过\033[0m\n'
elif "$PY_BIN" tools/normalize_scripts.py --quiet; then
  printf '\033[32m✔ 脚本编码/换行符合规（.bat 纯 ASCII、.ps1 带 BOM、.sh 是 LF）\033[0m\n'
else
  printf '\n\033[31m✘ 脚本编码/换行符不合规\033[0m\n'
  echo "  修：$PY_BIN tools/normalize_scripts.py --fix（能自动修的大部分会自动修）"
  FAIL=1
fi

# 7) 版本兼容性守卫。
#    防止「在新 Python 上一跑又炸」这类问题复发：requirements.txt 被钉回 ==
#    （导致 3.14 上 pillow/websockets 没 wheel）、安装脚本漏掉 python3.14、
#    或 CI 矩阵把 3.14 摘掉。每一项都对应一个已经踩过的坑。
if [ -z "$PY_BIN" ]; then
  printf '\033[33m~ 环境里没有 python，跳过兼容性守卫\033[0m\n'
elif [ ! -f tools/compat_check.py ]; then
  printf '\033[33m~ 找不到 tools/compat_check.py，跳过\033[0m\n'
elif "$PY_BIN" tools/compat_check.py; then
  printf '\033[32m✔ 版本兼容性守卫通过\033[0m\n'
else
  printf '\n\033[31m✘ 版本兼容性守卫未通过\033[0m\n'
  FAIL=1
fi

echo "----------------------------------------"
if [ "$FAIL" = "0" ]; then
  echo "全部通过，可以推送。"
else
  echo "有问题，**先处理再推**（提交过就等于泄露过）。"
  echo "如果历史里也有：先去吊销密钥，再用 git filter-repo 清历史。"
fi
exit $FAIL
