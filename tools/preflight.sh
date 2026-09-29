#!/usr/bin/env bash
# 发布前自查：确认仓库里没有密钥、没有真实 QQ 号、没有本机路径。
#
# 和 CI 里的 secret-scan 用同一套规则，但更快、能本地跑。
#
#   bash tools/preflight.sh          # 有问题退出码 1
#
# 分两段扫：
#   ① **工作区** —— 现在这份文件长什么样；
#   ② **git 历史** —— 以前提交过什么。
# 「提交过就等于泄露过」：工作区改干净了不算数，历史里还留着。
# 第二段以前只查密钥和 Cookie，漏掉了真实 QQ 号/昵称 —— 而真实数据恰恰
# 最容易残留在测试夹具里（本项目就踩过：两个真实 QQ 号 + 一个真实相册标识
# 混在 qzone-bridge 的测试夹具里，两段扫描都放过了它们）。
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

# 扫历史：把每次提交的补丁（-p）展开后 grep。
# 只能给出行、给不出文件名 —— 这里回答的是「有没有过」，不是「在哪儿」。
scan_history() {
  local title="$1" pattern="$2" allow="$3"
  local hits
  if ! command -v git >/dev/null 2>&1; then
    printf '\033[33m~ 没有 git，跳过「%s」\033[0m\n' "$title"; return
  fi
  hits=$(git log --all -p -- . ':!**/node_modules/**' ':!**/package-lock.json' 2>/dev/null \
         | grep -E -- "$pattern" | grep -vE "$allow" | head -20)
  if [ -n "$hits" ]; then
    printf '\n\033[31m✘ %s\033[0m\n%s\n' "$title" "$hits"
    FAIL=1
  else
    printf '\033[32m✔ %s\033[0m\n' "$title"
  fi
}

# 共用的白名单，分四类，每一类都注明了为什么可以放过：
#   · 合成占位值：10001 起的机器人号、11000000xx 的人造号、999000xxx 的测试群号、
#     重复数字与顺子的样例号（111111 / 888888888 / 876543210 这种一眼假）。
#   · 公开常量：QQ 空间的 **appid** 是公开的，谁抓包都是同一个数，不是个人标识。
#   · 示例时间戳：文档接口示例里的 Unix 时间戳，17 开头，不是号。
#   · GitHub noreply 邮箱：写成「用户 ID+用户名@users.noreply.github.com」。
#     那串 ID 必然撞上「9~11 位、2-9 开头」（第一次撞是在 SECURITY.md 的漏洞报告邮箱上，
#     preflight 直接飘红，但其实是个纯假阳性）。它是公开元数据 —— 每个提交里都有，
#     SECURITY.md 里也是公开给所有人看的 —— 不是谁的个人标识。
#     ⚠️ 所以**工作区扫描和历史扫描都要放过**：早先只在扫历史时放过，
#     结果 SECURITY.md 一加这行邮箱，工作区那一路就红了。
#     写成不含具体数字的通配式：写死那个 ID 的话，这段白名单自己会被工作区扫描扫出来
#     （顺带一提，它也不会自匹配 —— 文件里写的是 `users\.noreply\.github\.com`，
#     带反斜杠，对不上正则里那个没有反斜杠的形式）。
ALLOW_SYNTHETIC='1000[0-9]|1100000[0-9]{3}|999000[0-9]{3}|111111|222222|333333|123456789|888888888|999999999|876543210'
ALLOW_PUBLIC='549000912'
ALLOW_PLACEHOLDER='示例|example|placeholder|REDACTED|占位|xxx|yyy|XXX|zzz|你的QQ|oXXXX'
ALLOW_STAMPS='1709012345|1700000000|1700000001|1700000002|1770380359|1774000000|1774139725|1770[0-9]{6}|17[0-9]{8}'
ALLOW_META='[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com'
ALLOW_QQ="$ALLOW_SYNTHETIC|$ALLOW_PUBLIC|$ALLOW_PLACEHOLDER|$ALLOW_META"
ALLOW_HIST="$ALLOW_QQ"

echo "发布前自查（工作区）"
echo "----------------------------------------"

# 1) 真密钥：sk- 后面跟 20 位以上。示例值/占位符一律排除。
scan "没有明文 API Key" \
     'sk-[A-Za-z0-9_-]{20,}' \
     'REDACTED|xxxx|XXXX|your|example|占位|sk-\.\.\.|sk-xxx'

# 2) Cookie / 登录态。文档里的 uin=o你的QQ、skey=xxx 这类占位要放过。
scan "没有真实 Cookie / 登录态" \
     '(SESSDATA|bili_jct|superkey|p_skey)=[[:alnum:]%@_.-]{12,}' \
     "$ALLOW_PLACEHOLDER"

# 3) 真实 QQ 号。拆成三条查，因为「QQ 号」没有固定前缀，跟时间戳、端口、示例数字、
#    十六进制串重叠得很厉害：
#    3a 10 位、1[3-9] 开头（老规则，保留）
#    3b 9~11 位、以 2-9 开头 —— Unix 时间戳必然是 1 开头，所以这条不误报时间戳，
#       却能抓住 3a 完全漏掉的号（3a 只认 1 开头，而真实号里 2 开头的很多）
#    3c 带 QQ 语境（uin= / data-uin / nameCard_ / feed_）的短号，5 位以上，
#       覆盖 2000 年前发的 5~8 位老号；`feed_` 这种前缀最容易漏
#    边界统一写成 [^0-9A-Za-z_.]，只写 [^0-9.] 的话 pickey 那种十六进制串里
#    的连续数字段会被误报成 QQ 号（实测过）。
#    教训：真实号曾经躲过全部三条 —— 3a 因为它不是 1 开头，3b/3c 当时还不存在，
#    于是它安安稳稳通过了 CI，一路躺到第一个 tag 之后。
scan "没有真实 QQ 号（10 位 1[3-9] 开头）" \
     '(^|[^0-9A-Za-z_.])(1[3-9][0-9]{8})([^0-9A-Za-z_.]|$)' \
     "$ALLOW_QQ|$ALLOW_STAMPS"

scan "没有真实 QQ 号（9~11 位 2-9 开头）" \
     '(^|[^0-9A-Za-z_.])([2-9][0-9]{8,10})([^0-9A-Za-z_.]|$)' \
     "$ALLOW_QQ"

scan "没有 QQ 语境里的真实短号（uin= / nameCard_ / feed_）" \
     '(uin|UIN|nameCard|feed)[="_:]*o?([1-9][0-9]{4,10})' \
     "$ALLOW_QQ"

# 4) 本机路径 / 用户名。两类：
#    4a 用户目录（Windows 的 Users 目录、macOS 的 Users、Linux 的 home）。
#       文档里的占位写法（尖括号包起来的那种）不会命中，因为紧跟分隔符的必须
#       是字母数字。所以这条不需要白名单。
#    4b 开发机上自建 / 自定义的盘符目录。两个分支各有理由：
#       · 带 Users / GitHub / work 这些根名的，要求**后面真跟着一段目录名**才算数，
#         否则文档里那种尖括号占位会被误伤；
#       · 自建的 AI / QQ 目录树则**不要求**后面还有字符 —— 踩过的两次分别是
#         紧跟中文目录名（要求字母数字的那条抓不到）和用正斜杠写
#         （只认反斜杠的旧规则照样抓不到）。
#    白名单里**只留文档占位**，绝不能出现维护者自己的目录树 —— 旧版本就是因为把
#    几个本机根目录和 CI 的用户目录写进白名单，既泄露了自己的目录结构，
#    又让人误以为「这些前缀是安全的」。
scan "没有本机用户目录路径" \
     '(C:[\\/]{1,2}Users[\\/][A-Za-z0-9]|/home/[a-z][a-z0-9_-]*/|/Users/[a-z][a-z0-9_-]*/)' \
     "$ALLOW_PLACEHOLDER"

scan "没有开发机盘符路径" \
     '[A-Za-z]:[\\/]{1,2}(Users|GitHub|work|code|dev|projects|src)[\\/][A-Za-z0-9]|[A-Za-z]:[\\/]{1,2}(AI|QQ)[\\/]' \
     "$ALLOW_PLACEHOLDER"

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
echo "发布前自查（git 历史）"
echo "----------------------------------------"

# 8) 历史里的密钥 / Cookie。吊销优先于改写 —— 改历史不能把已经泄露的东西变回安全。
scan_history "历史里没有密钥 / Cookie" \
     'sk-[A-Za-z0-9_-]{20,}|(SESSDATA|bili_jct|superkey|p_skey)=[[:alnum:]%@_.-]{12,}' \
     "REDACTED|xxxx|XXX|example|placeholder|sk-\.\.\.|sk-xxx|$ALLOW_PLACEHOLDER"

# 9) 历史里的真实 QQ 号。
#    这是最容易漏的一类：测试夹具里的真实数据往往**先被提交过一次**，
#    后来在工作区里换成合成号，工作区扫描就绿了 —— 可历史里还留着。
#    本项目就撞上过：两个「2 开头、10 位」的真实号在第一个提交里，
#    一直躺到 v1.1.0。所以这段扫的两条规则必须和工作区那两条**完全一致**。
scan_history "历史里没有真实 QQ 号（10 位 1[3-9] 开头）" \
     '(^|[^0-9A-Za-z_.])(1[3-9][0-9]{8})([^0-9A-Za-z_.]|$)' \
     "$ALLOW_HIST|$ALLOW_STAMPS"

scan_history "历史里没有真实 QQ 号（9~11 位 2-9 开头）" \
     '(^|[^0-9A-Za-z_.])([2-9][0-9]{8,10})([^0-9A-Za-z_.]|$)' \
     "$ALLOW_HIST"

scan_history "历史里没有 QQ 语境里的真实短号" \
     '(uin|UIN|nameCard|feed)[="_:]*o?([1-9][0-9]{4,10})' \
     "$ALLOW_HIST"

echo "----------------------------------------"
if [ "$FAIL" = "0" ]; then
  echo "全部通过，可以推送。"
else
  echo "有问题，**先处理再推**（提交过就等于泄露过）。"
  echo
  echo "  工作区里的问题：直接改文件就行。"
  echo
  echo "  历史里也有（'历史里没有…' 那几条红了）：改工作区**没用**，"
  echo "  必须改写历史，而且要在**第一次 push 之前**做 —— 推上去之后再改，"
  echo "  别人的 fork 和 GitHub 的缓存里还留着。本仓库当前没有 remote，"
  echo "  改写历史是安全的（reflog 还能救回来）。两条路："
  echo
  echo "    # 推荐：git-filter-repo（把左边那串换成合成值，可以叠多条）"
  echo "    pip install git-filter-repo"
  echo "    printf '真实值==>合成值\\n' > /tmp/replacements.txt   # 必须写成真文件"
  echo "    git filter-repo --replace-text /tmp/replacements.txt"
  echo "    # 上面这个「写成真文件」不是讲究：Windows / Git Bash 下 --replace-text"
  echo "    # 接 <(echo ...) 会直接报 FileNotFoundError: /proc/.../fd/63（实测）。"
  echo "    # 另外三件实测过的后果：① filter-repo 会把 origin 远端删掉，之后要重新 add；"
  echo "    # ② 被改空的提交会被剪掉，提交数可能变少；"
  echo "    # ③ 清完历史后 push --force-with-lease 会报 stale info，只能 --force。"
  echo
  echo "    # 没有 filter-repo 时，用内置的 filter-branch。**别写 sed 替换表** ——"
  echo "    # 中文和多字节字符的转义很容易把替换写歪（实测把「用户己」的 . 转成了 &，"
  echo "    # 结果一条都没替换掉，还看不出来）。反过来做最稳：把「已经修好的那几份文件」"
  echo "    # 当作唯一事实源，让每个提交都拿副本覆盖过去。"
  echo
  echo "    #   ① 在仓库外放一份修好的副本，保持相对路径，例如 /tmp/histfix/"
  echo "    #   ② 逐个提交覆盖（--tag-name-filter cat 必须加，否则 tag 还指着旧提交）"
  echo "    git filter-branch -f --tag-name-filter cat \\"
  echo "      --tree-filter 'cp -r /tmp/histfix/. .' -- --all"
  echo "    #   ③ filter-branch 把旧提交备份在 refs/original/ 下，**不删就等于没清**："
  echo "    rm -rf .git/refs/original"
  echo "    git reflog expire --expire=now --all && git gc --prune=now"
  echo
  echo "  改完必须复核（这两条都要过关才算真清了）："
  echo "    git log --all -p | grep -nE '真实值' ; bash tools/preflight.sh"
  echo "  密钥类还要**先去吊销**，再清历史 —— 顺序反了等于没清。"
fi
exit $FAIL
