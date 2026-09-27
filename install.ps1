<#
  许杏玖 · 一键安装（Windows）

  ⚠️ 本文件必须存为 **UTF-8 with BOM**。
     Windows PowerShell 5.1 对没有 BOM 的 .ps1 会按系统 ANSI 代码页
     （中文 Windows = GBK/936）解码，于是文件里所有中文都变成乱码，
     连报错信息都读不懂。这不是"显示问题"，是解码错误。
     改文件时如果你的编辑器默认存成"UTF-8 无 BOM"，请手动切到
     "UTF-8 with BOM"。同目录的 install.bat 则是**纯 ASCII**，
     两者规则相反，别互相拷贝风格。

  做这些事（**幂等**，重复跑不会破坏已有配置）：
    1. 环境检查（python / node / git）
    2. 没有源码就取一份（git clone 优先）
    3. 建 .venv 并装依赖（bin/Scripts 按平台走，这里固定 Scripts）
    4. 生成 config.json（**已存在就跳过，绝不覆盖**）
    5. 空间桥接 npm ci（按 lock 哈希判断要不要重装）
    6. 跑体检 doctor.py

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -CheckOnly
#>
param(
  [switch]$CheckOnly,
  [string]$RepoUrl = "https://github.com/your-name/xuxingjiu.git",
  [string]$TargetDir = ""
)

$ErrorActionPreference = "Stop"

function Say($m)  { Write-Host "`n$m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!!] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "  [xx] $m" -ForegroundColor Red; exit 1 }

# ── 0. 自保：确认这份脚本真被当作 UTF-8 读进去了 ──────────────────────
# 判据用**长度**而不是字符串比较 —— 字面量在被误解码时也是乱的，
# 拿它跟谁比都是自指。而长度骗不了人：「许杏玖」是 3 个字 / 9 个字节，
# 一旦被按 GBK 两字节一组错配，就会塌成 4~5 个字符。
# （BOM 缺失，或编辑器另存为"UTF-8 无 BOM"时命中）
if ("许杏玖".Length -ne 3) {
  Write-Host "  [!!] 本脚本的中文没被正确解码（很可能存成了 UTF-8 无 BOM）。" -ForegroundColor Yellow
  Write-Host "       安装逻辑照常会跑，但下面的中文提示会是乱码。" -ForegroundColor Yellow
  Write-Host "       修法：把 install.ps1 另存为 'UTF-8 with BOM' 再重跑。" -ForegroundColor Yellow
}

# ── 1. 环境 ──────────────────────────────────────────────────────────
Say "1/6 环境检查"
$py = $null
foreach ($c in @("python", "py")) {
  try {
    $v = & $c -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.11") { $py = $c; break }
  } catch { }
}
if (-not $py) { Die "找不到 Python 3.11+。装完记得勾选 'Add to PATH'（或重开一个终端）" }
Ok "Python $(& $py -c 'import sys;print(sys.version.split()[0])') ($py)"

if (Get-Command node -ErrorAction SilentlyContinue) {
  Ok "Node $(& node --version)（空间桥接用，可选）"
} else {
  Warn "没有 node —— 空间桥接装不了；只聊天的话不影响"
}

# ── 2. 源码 ──────────────────────────────────────────────────────────
Say "2/6 源码"
$SelfDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $SelfDir "qqbot\bot.py")) {
  $Root = $SelfDir
  Ok "就在当前目录：$Root"
} else {
  if (-not $TargetDir) { $TargetDir = Join-Path $PWD "xuxingjiu" }
  if (Test-Path (Join-Path $TargetDir "qqbot\bot.py")) {
    $Root = $TargetDir
    Ok "已有源码：$Root"
  } elseif ($CheckOnly) {
    Die "找不到源码（-CheckOnly 模式不会去下载）"
  } elseif (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "  -> git clone $RepoUrl $TargetDir"
    git clone --depth 1 $RepoUrl $TargetDir
    $Root = $TargetDir
    Ok "已 clone 到 $Root"
  } else {
    Die "没有 git。请手动下载 Release 源码包解压，再在本目录里跑本脚本"
  }
}
$QQBOT = Join-Path $Root "qqbot"
if (-not (Test-Path (Join-Path $QQBOT "requirements.txt"))) { Die "缺 requirements.txt，源码不完整" }

# ── 3. venv + 依赖 ───────────────────────────────────────────────────
Say "3/6 Python 依赖"
$VenvPy = Join-Path $QQBOT ".venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
  Ok "已有 .venv，跳过创建"
} elseif ($CheckOnly) {
  Warn "没有 .venv（-CheckOnly 模式不创建）"
} else {
  & $py -m venv (Join-Path $QQBOT ".venv")
  Ok "已创建 $QQBOT\.venv"
}
if ((Test-Path $VenvPy) -and -not $CheckOnly) {
  # ★ 必须用 venv 里的 python -m pip：用系统 pip 会装到全局，然后 import 失败
  & $VenvPy -m pip install --upgrade pip -q
  & $VenvPy -m pip install -r (Join-Path $QQBOT "requirements.txt") -q
  Ok "依赖装好"
}

# ── 4. config.json ───────────────────────────────────────────────────
Say "4/6 配置"
$Cfg = Join-Path $QQBOT "config.json"
$ExCfg = Join-Path $QQBOT "config.example.json"
if (Test-Path $Cfg) {
  Ok "config.json 已存在，**不覆盖**（覆盖会把你的 token / QQ 号弄丢）"
} elseif ((Test-Path $ExCfg) -and -not $CheckOnly) {
  Copy-Item $ExCfg $Cfg
  Ok "已从模板生成 config.json —— 记得改 bot_qq / admin.user_ids / access_token"
} else {
  Warn "没有 config.json"
}

# ── 5. 空间桥接（可选） ──────────────────────────────────────────────
Say "5/6 空间桥接（可选）"
$Bridge = Join-Path $Root "qzone-bridge"
if (-not (Test-Path $Bridge)) {
  Warn "没有 qzone-bridge 目录，跳过"
} elseif (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  Warn "没有 npm，跳过（装完 node 后：cd qzone-bridge; npm ci; npm run build）"
} elseif ($CheckOnly) {
  Warn "跳过安装（-CheckOnly）"
} else {
  $LockHashFile = Join-Path $Bridge "node_modules\.lock-hash"
  $Want = (Get-FileHash (Join-Path $Bridge "package-lock.json") -Algorithm SHA256).Hash
  if ((Test-Path $LockHashFile) -and ((Get-Content $LockHashFile -Raw).Trim() -eq $Want)) {
    Ok "依赖已是最新（lock 未变），跳过 npm ci"
  } else {
    Push-Location $Bridge
    npm ci --no-audit --no-fund
    Pop-Location
    Set-Content -Path $LockHashFile -Value $Want -NoNewline
    Ok "依赖装好"
  }
}

# ── 6. 体检 ──────────────────────────────────────────────────────────
Say "6/6 体检"
$DoctorRc = 1
if (Test-Path $VenvPy) {
  & $VenvPy (Join-Path $QQBOT "tools\doctor.py")
  $DoctorRc = $LASTEXITCODE
} else {
  Warn "没有 venv，跳过体检"
}

Say "下一步"
@"
  1) 改 qqbot\config.json：bot_qq（机器人 QQ）、admin.user_ids（你的 QQ）、
     access_token（自己编一串随机字符，NapCat 那边要填一样的）
  2) 配模型：set DEEPSEEK_API_KEY=sk-...（新开的终端才生效），
     或改 providers.endpoints 指向本地模型
  3) 装 NapCat：见 NapCat\README.md（下载本体解压到 NapCat\ 目录）
  4) 启动：双击 deploy\start-all.pyw
  5) 看日志：控制台 http://127.0.0.1:6200/

  ⚠️ 绝不要 taskkill /IM QQ.exe —— 你的日常号和机器人号是同一个 QQ.exe。
"@ | Write-Host

exit $DoctorRc
