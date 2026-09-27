<#
  许杏玖 · 一键安装（Windows）

  ⚠️ 本文件必须存为 **UTF-8 with BOM**。
     Windows PowerShell 5.1 对没有 BOM 的 .ps1 会按系统 ANSI 代码页
     （中文 Windows = GBK/936）解码，于是文件里所有中文都变成乱码，
     连报错信息都读不懂。这不是"显示问题"，是解码错误。
     改文件时如果你的编辑器默认存成"UTF-8 无 BOM"，请手动切到
     "UTF-8 with BOM"。同目录的 install.bat 则是**纯 ASCII**，
     两者规则相反，别互相拷贝风格。
     （忘了也没关系：python tools\normalize_scripts.py --fix 会自动补 BOM，
       CI 里也有一道守卫拦着。）

  做这些事（**幂等**，重复跑不会破坏已有配置）：
    0. 环境探测：系统 / 包管理器 / 权限 / Python / Node / git / 网络
    1. 缺什么补什么：能自动装的自己装，装不了的把命令摊在你面前
    2. 没有源码就取一份（git clone 优先）
    3. 建 .venv 并装依赖（官方源慢就自动切国内镜像）
    4. 生成 config.json（**已存在就跳过，绝不覆盖**）
    5. 空间桥接 npm ci（按 lock 哈希判断要不要重装）
    6. 跑体检 doctor.py

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -DetectOnly
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -DetectOnly -Json
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -CheckOnly
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Yes
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -NoSystem
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -DryRun

  关于「自动装系统依赖」的边界，和 install.sh 是同一套三条原则：
    1. **先找、再用现成工具、最后才动系统包管理器** —— 能不装就不装；
    2. 动系统包管理器一律先问一句（-Yes 才跳过），-NoSystem 直接禁止；
    3. **绝不「下载一段东西直接执行」**。这里下载 python.org 安装器属于这一类，
       所以只有你显式加 -AllowDownload 才会做。
#>
param(
  [switch]$CheckOnly,
  [switch]$DetectOnly,
  [switch]$Json,
  [switch]$Yes,
  [switch]$NoSystem,
  [switch]$DryRun,
  [switch]$WithBrowser,
  [switch]$AllowDownload,
  [ValidateSet("auto", "off", "tuna", "aliyun")]
  [string]$Mirror = "auto",
  [string]$RepoUrl = "https://github.com/your-name/xuxingjiu.git",
  [string]$TargetDir = ""
)

$ErrorActionPreference = "Stop"
$Schema = 1

function Say($m)  { Write-Host "`n$m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!!] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "  [xx] $m" -ForegroundColor Red; exit 1 }
function Note($m) { Write-Host "     $m" }

# 只读模式：一个文件都不许动
$ReadOnlyMode = $CheckOnly -or $DetectOnly -or $DryRun

function Mut([scriptblock]$Action, [string]$What) {
  if ($ReadOnlyMode) { Write-Host "  [dry] $What" }
  else { Write-Host "  [run] $What"; & $Action }
}

function Ask([string]$Question, [string]$Default = "n") {
  if ($Yes) { Write-Host "  -> $Question [自动 Yes]"; return $true }
  if ($Host.Name -ne "ConsoleHost" -or [Console]::IsInputRedirected) {
    Write-Host "  -> $Question [非交互，按 $Default 处理]"
    return ($Default -eq "y")
  }
  $hint = if ($Default -eq "y") { "Y/n" } else { "y/N" }
  $a = Read-Host "  ? $Question [$hint]"
  if ([string]::IsNullOrWhiteSpace($a)) { $a = $Default }
  return ($a -match "^(y|Y|yes|YES|是)$")
}

function HasCmd([string]$Name) { return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

# ── 0. 自保：确认这份脚本真被当作 UTF-8 读进去了 ──────────────────────
# 判据用**长度**而不是字符串比较 —— 字面量在被误解码时也是乱的，
# 拿它跟谁比都是自指。而长度骗不了人：「许杏玖」是 3 个字 / 9 个字节，
# 一旦被按 GBK 两字节一组错配，就会塌成 4~5 个字符。
# （BOM 缺失，或编辑器另存为"UTF-8 无 BOM"时命中）
if ("许杏玖".Length -ne 3) {
  Write-Host "  [!!] 本脚本的中文没被正确解码（很可能存成了 UTF-8 无 BOM）。" -ForegroundColor Yellow
  Write-Host "       安装逻辑照常会跑，但下面的中文提示会是乱码。" -ForegroundColor Yellow
  Write-Host "       修法：python tools\normalize_scripts.py --fix，或另存为 'UTF-8 with BOM'。" -ForegroundColor Yellow
}

# ── 0. 环境探测（整段只读） ──────────────────────────────────────────

$IsWin = $true
if ($null -ne $PSVersionTable.PSVersion -and $PSVersionTable.PSVersion.Major -ge 6) { $IsWin = $IsWindows }
elseif ($env:OS -ne "Windows_NT") { $IsWin = $false }

# 系统版本
$OsName = "Unknown"
if ($IsWin) {
  try {
    $v = [System.Environment]::OSVersion.Version
    # Win11 的 Build 号 >= 22000；只看 Major(10) 会把 11 说成 10
    if ($v.Build -ge 22000) { $OsName = "Windows 11（build $($v.Build)）" }
    else { $OsName = "Windows $($v.Major).$($v.Minor)（build $($v.Build)）" }
  } catch { $OsName = "Windows" }
} else {
  try { $OsName = "$([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)" } catch { $OsName = "Unix-like" }
}
$OsArch = "unknown"
try { $OsArch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() } catch { }
if (-not $OsArch -or $OsArch -eq "unknown") { $OsArch = $env:PROCESSOR_ARCHITECTURE }

# 包管理器
$Pkg = "none"; $PkgVer = ""; $NeedRoot = $true
if ($IsWin) {
  if (HasCmd "winget")     { $Pkg = "winget"; $NeedRoot = $false }
  elseif (HasCmd "choco")  { $Pkg = "choco";  $NeedRoot = $true }
  elseif (HasCmd "scoop")  { $Pkg = "scoop";  $NeedRoot = $false }
} else {
  foreach ($m in @("brew", "apt-get", "dnf", "pacman", "zypper", "apk")) {
    if (HasCmd $m) { $Pkg = $m; $NeedRoot = ($m -notin @("brew")); break }
  }
}
if ($Pkg -eq "winget") { $PkgVer = (& winget --version 2>$null | Select-Object -First 1) }
elseif ($Pkg -eq "brew") { $PkgVer = (& brew --version 2>$null | Select-Object -First 1) }
elseif ($Pkg -ne "none") { $PkgVer = (& $Pkg --version 2>$null | Select-Object -First 1) }

# 管理员权限（winget --scope user / 用户级安装不需要）
$IsAdmin = $false
if ($IsWin) {
  try {
    $IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  } catch { $IsAdmin = $false }
}
$CanRoot = $IsAdmin -or (-not $NeedRoot) -or (HasCmd "sudo")

# --- Python ---
# 候选里含 python3.14：系统只装了 3.14 且没把 python 设成默认时也能找到。
# 还要扫常见安装目录与 py 启动器 —— 装了 Python 但没勾 "Add to PATH" 是最常见的情况，
# 只查 PATH 会得出「没有 Python」的结论，然后让用户去装一个他其实已经有的东西。
$Py = $null; $PyVer = ""; $PyPath = ""; $PyLayout = "scripts"; $PyVenvModule = $false
$PyPreArgs = @()
$PyCandidates = New-Object System.Collections.ArrayList

function Add-Cand([string]$Cmd, [string[]]$PreArgs) {
  $null = $PyCandidates.Add([pscustomobject]@{ Cmd = $Cmd; Pre = $PreArgs })
}

# ① py 启动器：Windows 上最靠谱的一个，能列出所有已安装版本
if ($IsWin -and (HasCmd "py")) {
  foreach ($v in @("-3.14", "-3.13", "-3.12", "-3.11", "-3")) { Add-Cand "py" @($v) }
}
# ② PATH 里的名字（含 3.14 与 3.11~3.20 兜底，和 install.sh 保持一致）
foreach ($n in @("python3.14", "python3.13", "python3.12", "python3.11", "python3", "python")) { Add-Cand $n @() }
for ($i = 11; $i -le 20; $i++) { Add-Cand "python3.$i" @() }
# ③ 常见安装目录（没勾 Add to PATH 时 Python 只在这里）
if ($IsWin) {
  $dirs = @()
  foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "C:\", "${env:ProgramFiles}", "${env:ProgramFiles(x86)}")) {
    if ($root -and (Test-Path $root)) {
      $dirs += (Get-ChildItem -Path $root -Directory -Filter "Python3*" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
    }
  }
  foreach ($d in ($dirs | Sort-Object -Descending -Unique)) {
    $exe = Join-Path $d "python.exe"
    if (Test-Path $exe) { Add-Cand $exe @() }
  }
}
# ④ uv 管着的独立 CPython
if (HasCmd "uv") {
  try {
    $u = (& uv python find 2>$null | Select-Object -First 1)
    if ($u -and (Test-Path $u)) { Add-Cand $u @() }
  } catch { }
}

foreach ($cand in $PyCandidates) {
  $cmd = $cand.Cmd
  # Microsoft Store 的 python3 是「应用执行别名」假壳（在 WindowsApps 目录下）：
  # 它执行后不报版本，而是弹一句 "Python was not found; run without arguments to
  # install from the Microsoft Store..."。所以：① 先排除 WindowsApps 路径；
  # ② 再用版本号正则兜底 —— 假壳的输出谁也匹配不上。
  $resolved = $cmd
  if (-not (Test-Path $cmd)) {
    $g = Get-Command $cmd -ErrorAction SilentlyContinue
    if (-not $g) { continue }
    $resolved = $g.Source
  }
  if ($resolved -like "*\WindowsApps\*") { continue }
  try {
    $out = & $cmd @($cand.Pre) -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    $ver = ($out | Select-Object -First 1)
    if ($ver -notmatch '^(\d+)\.(\d+)\.(\d+)$') { continue }
    if ([int]$Matches[1] -ne 3 -or [int]$Matches[2] -lt 11) { continue }
    $Py = $cmd; $PyPreArgs = $cand.Pre; $PyVer = $ver; $PyPath = $resolved
    break
  } catch { }
}

if ($Py) {
  # Windows 的 venv 一律在 Scripts\ 下（install.sh 那边是 bin/，别照抄）
  $PyLayout = "scripts"
  try {
    & $Py @($PyPreArgs) -c "import venv, ensurepip" 2>$null
    $PyVenvModule = ($LASTEXITCODE -eq 0)
  } catch { $PyVenvModule = $false }
}

# --- Node / npm ---
$NodeVer = ""; $NpmVer = ""; $NodeOk = $false
if (HasCmd "node") {
  $NodeVer = (& node --version 2>$null | Select-Object -First 1)
  $maj = $NodeVer -replace '^v', '' -replace '\..*$', ''
  if ($maj -match '^\d+$' -and [int]$maj -ge 18) { $NodeOk = $true }   # package.json 要求 >=18
}
if (HasCmd "npm") { $NpmVer = (& npm --version 2>$null | Select-Object -First 1) }

# --- git ---
$GitVer = ""
if (HasCmd "git") { $GitVer = (& git --version 2>$null | Select-Object -First 1) }

# --- 现成的多版本工具链 ---
$Toolchains = @()
foreach ($t in @("uv", "pyenv", "conda")) { if (HasCmd $t) { $Toolchains += $t } }

# --- 网络 & 镜像 ---
# 只探 2 秒，探不到就当不可达 —— 探测失败绝不能把安装搞挂。
# 用 GET 而不是 HEAD：install.sh 那边的 curl 是 GET，两边方法一致结果才可比
# （实测 HEAD 到 pypi.org 0.2 秒、GET 要 2 秒，混着用会得出互相矛盾的结论）。
# 拿到响应头就 Close，不读 body，所以不会真的下一整个索引页。
function Probe([string]$Url) {
  try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $req = [System.Net.WebRequest]::Create($Url)
    $req.Method = "GET"
    $req.Timeout = 2000
    $resp = $req.GetResponse()
    $resp.Close()
    $sw.Stop()
    return [math]::Round($sw.Elapsed.TotalSeconds, 3)
  } catch { return $null }
}
$TPypi = Probe "https://pypi.org/simple/"
$TTuna = Probe "https://pypi.tuna.tsinghua.edu.cn/simple/"
$TNpm  = Probe "https://registry.npmjs.org/"
$TNpmm = Probe "https://registry.npmmirror.com/"

$MirrorPip = $null; $MirrorPipName = "官方 pypi.org"
$MirrorNpm = $null; $MirrorNpmName = "官方 registry.npmjs.org"
$TunaUrl = "https://pypi.tuna.tsinghua.edu.cn/simple"
$NpmmUrl = "https://registry.npmmirror.com"
function IsSlow($t, [double]$Threshold) { return ($null -eq $t) -or ($t -gt $Threshold) }
switch ($Mirror) {
  "off" { }
  "tuna" { $MirrorPip = $TunaUrl; $MirrorPipName = "清华 pypi.tuna"; $MirrorNpm = $NpmmUrl; $MirrorNpmName = "npmmirror" }
  "aliyun" { $MirrorPip = "https://mirrors.aliyun.com/pypi/simple/"; $MirrorPipName = "阿里 mirrors.aliyun"; $MirrorNpm = $NpmmUrl; $MirrorNpmName = "npmmirror" }
  "auto" {
    # 只在「官方不可达」或「官方明显比镜像慢」时切，避免无缘无故改下载源
    if ($null -ne $TTuna -and (IsSlow $TPypi 3) -and (-not (IsSlow $TTuna 1))) {
      $MirrorPip = $TunaUrl; $MirrorPipName = "清华 pypi.tuna（官方源慢/不通，已自动切换）"
    }
    if ($null -ne $TNpmm -and (IsSlow $TNpm 3) -and (-not (IsSlow $TNpmm 1))) {
      $MirrorNpm = $NpmmUrl; $MirrorNpmName = "npmmirror（官方源慢/不通，已自动切换）"
    }
  }
}

# --- 目标目录 ---
if (-not $TargetDir) { $TargetDir = Join-Path $PWD "xuxingjiu" }
$DirWritable = $false; $DirFreeMb = $null
try {
  $probeDir = $TargetDir
  if (-not (Test-Path $probeDir)) { $probeDir = (Split-Path -Parent $probeDir) }
  if (Test-Path $probeDir) {
    $DirWritable = $true   # 真要写的时候会报错，这里只需给个粗判断
    $drive = (Get-Item $probeDir).PSDrive
    if ($drive) { $DirFreeMb = [math]::Round($drive.Free / 1MB) }
  }
} catch { }

function Secs($t) { if ($null -eq $t) { return "不可达" } else { return "${t}s" } }

function Show-Env {
  Write-Host ("  操作系统      {0}（{1}）" -f $OsName, $OsArch)
  if ($IsWin -and -not $IsAdmin) { Write-Host "  权限          普通用户（够用：winget 走 --scope user，不碰系统 Python）" }
  elseif ($IsAdmin) { Write-Host "  权限          管理员" }
  Write-Host ("  包管理器      {0}{1}" -f $Pkg, $(if ($PkgVer) { "（$PkgVer）" } else { "" }))
  if ($Py) {
    # 显示的是**能直接粘去用的命令**（py 启动器要带上 -3.14 才有意义），
    # 而不是光秃秃一个 "py" —— 用户拿着 "py" 去敲是没法复现环境探测结果的。
    $pyCmd = "$Py"
    if ($PyPreArgs) { $pyCmd = "$Py $($PyPreArgs -join ' ')" }
    Write-Host ("  Python        {0}（{1}）" -f $PyVer, $pyCmd)
    Write-Host ("                解释器：{0}" -f $PyPath)
    if (-not $PyVenvModule) { Write-Host "                [!!] 缺 venv/ensurepip 模块，建虚拟环境会失败" }
  } else {
    Write-Host "  Python        未找到 3.11+"
  }
  if ($NodeVer) {
    Write-Host ("  Node / npm    {0} / {1}{2}" -f $NodeVer, $NpmVer, $(if ($NodeOk) { "" } else { "  [!!] Node 太旧（空间桥接要 >=18）" }))
  } else {
    Write-Host "  Node / npm    未找到（只有空间桥接需要，纯聊天不影响）"
  }
  Write-Host ("  git           {0}" -f $(if ($GitVer) { $GitVer } else { "未找到（要 clone 源码就得有）" }))
  Write-Host ("  工具链        {0}" -f $(if ($Toolchains.Count) { ($Toolchains -join " ") } else { "无 uv / pyenv / conda" }))
  Write-Host ("  网络          pypi.org {0} / pypi.tuna {1}" -f (Secs $TPypi), (Secs $TTuna))
  Write-Host ("                npmjs.org {0} / npmmirror {1}" -f (Secs $TNpm), (Secs $TNpmm))
  Write-Host ("  下载源        pip: {0}" -f $MirrorPipName)
  Write-Host ("                npm: {0}" -f $MirrorNpmName)
  Write-Host ("  目标目录      {0}{1}{2}" -f $TargetDir, $(if ($DirWritable) { "（可写）" } else { "（不可写！）" }), $(if ($DirFreeMb) { "，剩余 $DirFreeMb MB" } else { "" }))
}

function Show-Json {
  $report = [ordered]@{
    schema = $Schema
    os = [ordered]@{
      family = $(if ($IsWin) { "windows" } else { "unix" })
      name = $OsName
      arch = "$OsArch"
      msys = $false
      container = $false
      wsl = $false
    }
    shell = "powershell"
    toolchains = @($Toolchains)
    pkg = [ordered]@{
      name = $Pkg
      version = "$PkgVer"
      need_root = [bool]$NeedRoot
      can_root = [bool]$CanRoot
    }
    python = [ordered]@{
      found = [bool]$Py
      # cmd 是「能直接粘去用的命令」（py 启动器要带 -3.14），path 是真实解释器路径。
      # 两个 report 实现都必须给可直接执行的命令，比较器才有得比。
      cmd = $(if ($PyPreArgs) { "$Py $($PyPreArgs -join ' ')" } else { "$Py" })
      version = "$PyVer"
      path = "$PyPath"
      ok = [bool]$Py
      venv_module = [bool]$PyVenvModule
      layout = $PyLayout
    }
    node = [ordered]@{
      found = [bool]$NodeVer
      version = "$NodeVer"
      npm = "$NpmVer"
      ok = [bool]$NodeOk
    }
    git = [ordered]@{ found = [bool]$GitVer; version = "$GitVer" }
    net = [ordered]@{
      pypi = $TPypi; tuna = $TTuna; npm = $TNpm; npmmirror = $TNpmm
      mirror_pip = $MirrorPip; mirror_npm = $MirrorNpm
    }
    dir = [ordered]@{
      target = $TargetDir
      writable = [bool]$DirWritable
      free_mb = $DirFreeMb
    }
  }
  # PS 5.1 的 ConvertTo-Json 会把非 ASCII 转成 \uXXXX —— 那是合法 JSON，不影响解析。
  # 但控制台编码得先调成 UTF-8，否则 5.1 在 GBK 控制台里写管道会抛
  # "找不到 'utf8' 编码" 之类的错。
  try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
  $report | ConvertTo-Json -Depth 6
}

if ($DetectOnly) {
  if ($Json) { Show-Json } else {
    Say "环境探测（一个文件都没动）"
    Show-Env
    Write-Host "`n  提示：-DetectOnly 只报告。要装东西就跑不带参数的 install.ps1（或双击 install.bat）。"
  }
  exit 0
}

Say "0/7 环境探测"
Show-Env

# ── 系统依赖补齐 ─────────────────────────────────────────────────────

function System-Hint([string]$What) {
  switch ($What) {
    "python" { return "winget install --id Python.Python.3.13 -e --scope user   （或到 python.org 下，勾上 Add to PATH）" }
    "node"   { return "winget install --id OpenJS.NodeJS.LTS -e --scope user" }
    "git"    { return "winget install --id Git.Git -e" }
    default  { return "到官网下载安装包" }
  }
}

function Install-Pkg([string]$What, [string[]]$Names) {
  if ($NoSystem) { Warn "-NoSystem：不碰系统包管理器。手动装：$(System-Hint $What)"; return $false }
  if ($Pkg -eq "none") { Warn "这台机器上没有可用的包管理器（winget / choco / scoop）"; return $false }
  if (-not (Ask "用 $Pkg 装 $What？（不会动系统 Python；不想要可以以后用包管理器卸掉）" "n")) {
    Warn "跳过装 $What"; return $false
  }
  $okAll = $true
  foreach ($n in $Names) {
    if ($Pkg -eq "winget") {
      $id = switch ($n) {
        "python" { "Python.Python.3.13" }
        "node"   { "OpenJS.NodeJS.LTS" }
        "git"    { "Git.Git" }
        default  { $n }
      }
      # --scope user：装到用户目录，不要管理员，也不动系统里的 Python
      # Out-Host 是必须的：原生命令的 stdout 会被当成函数返回值的一部分，
      # 那样 `if (Install-Pkg ...)` 拿到的是「一堆输出 + $false」→ 永远为真。
      Mut { & winget install --id $id -e --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host } "winget install $id"
      if ($LASTEXITCODE -ne 0) { Warn "winget 装 $id 返回 $LASTEXITCODE（已经装过的话可以先忽略）" }
    } elseif ($Pkg -eq "choco") {
      Mut { & choco install -y $n | Out-Host } "choco install $n"
    } elseif ($Pkg -eq "scoop") {
      Mut { & scoop install $n | Out-Host } "scoop install $n"
    } else {
      Warn "这台机器用的 $Pkg 不在这里自动装，手动：$(System-Hint $What)"
      return $false
    }
  }
  return $okAll
}

# 重新探测 Python（装完要再看一次；PATH 变动需要新终端，所以也要扫安装目录）
function Try-Reload-Python {
  # 注意：这个函数要 return $true/$false，所以它内部**一个字节的输出都不能漏到管道里**
  # —— 原生命令的 stdout 会被当成函数返回值的一部分，`if (Try-Reload-Python)` 就永远为真。
  # 所有探测输出一律 Out-Null。
  $paths = @(
    "$env:LOCALAPPDATA\Programs\Python",
    "C:\",
    "${env:ProgramFiles}",
    "${env:ProgramFiles(x86)}"
  ) | Where-Object { $_ -and (Test-Path $_) }
  foreach ($root in $paths) {
    $found = Get-ChildItem -Path $root -Directory -Filter "Python3*" -ErrorAction SilentlyContinue |
             Sort-Object -Descending | ForEach-Object { Join-Path $_.FullName "python.exe" } |
             Where-Object { Test-Path $_ }
    foreach ($exe in $found) {
      try {
        $ver = (& $exe -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null | Select-Object -First 1)
        if ($ver -match '^3\.(\d+)\.\d+$' -and [int]$Matches[1] -ge 11) {
          $script:Py = $exe; $script:PyPreArgs = @(); $script:PyVer = $ver; $script:PyPath = $exe
          & $exe -c "import venv, ensurepip" 2>$null | Out-Null
          $script:PyVenvModule = ($LASTEXITCODE -eq 0)
          return $true
        }
      } catch { }
    }
  }
  return $false
}

function Install-Python {
  if ($Py) { return }
  Say "1/7 补 Python 3.11+"
  # ① uv：用户级下载一份独立 CPython，不动系统、不要管理员，最干净的一条路
  if ((HasCmd "uv") -and (-not $NoSystem)) {
    if (Ask "用 uv 装一份独立的 Python 3.12（用户级，不动系统 Python）？" "y") {
      Mut { & uv python install 3.12 } "uv python install 3.12"
      try {
        $u = (& uv python find 2>$null | Select-Object -First 1)
        if ($u -and (Test-Path $u)) {
          $ver = (& $u -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null | Select-Object -First 1)
          if ($ver -match '^3\.(\d+)\.\d+$' -and [int]$Matches[1] -ge 11) {
            $script:Py = $u; $script:PyPreArgs = @(); $script:PyVer = $ver; $script:PyPath = $u
            Ok "uv 装好了：$ver（$u）"
            return
          }
        }
      } catch { }
      Warn "uv 装完但没定位到解释器"
    }
  }
  # ② winget（用户级）
  if (Install-Pkg "Python 3.13" @("python")) {
    if (Try-Reload-Python) { Ok "装好了：$PyVer（$PyPath）"; return }
    Warn "装是装完了，但这个终端里还看不到它 —— 重开一个终端（或重开 install.bat）再跑一次"
  }
  # ③ 兜底：直接下官方安装器。**只有显式 -AllowDownload 才做** ——
  #    「下载一段东西直接执行」正是本仓库警告过的攻击面，默认不能替用户决定。
  if ($AllowDownload -and $IsWin) {
    $url = "https://www.python.org/ftp/python/3.13.7/python-3.13.7-amd64.exe"
    Warn "即将下载并静默安装官方安装器（用户级）：$url"
    if (Ask "确认下载并执行 python.org 官方安装器？" "n") {
      $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "python-setup-xuxingjiu.exe"
      Mut { Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing } "下载 $url"
      Mut { Start-Process -FilePath $tmp -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1" -Wait } "静默安装 Python"
      Remove-Item $tmp -ErrorAction SilentlyContinue
      Warn "装完了 —— PATH 要新开一个终端才生效，请重新跑一次本脚本"
    } else {
      Warn "跳过；手动装：$(System-Hint python)"
    }
  }
  if (-not $Py) {
    Warn "还是没找到 Python 3.11+。手动装："
    Note (System-Hint python)
    Die "缺少 Python，安装无法继续（装完重开终端再跑一遍本脚本）"
  }
}

function Install-Node {
  if ($NodeVer -and $NodeOk) { return }
  # node 只有空间桥接要，纯聊天不用 —— 所以只在有 qzone-bridge 目录时才问
  if (-not (Test-Path (Join-Path $Root "qzone-bridge"))) { return }
  Say "补 Node.js（空间桥接需要，>=18）"
  if ($NodeVer) { Warn "现有 Node $NodeVer 太旧（要 >=18），建议升级" } else { Warn "没找到 Node" }
  if (Install-Pkg "Node.js" @("node")) {
    if (HasCmd "node") {
      $script:NodeVer = (& node --version 2>$null | Select-Object -First 1)
      $maj = $NodeVer -replace '^v', '' -replace '\..*$', ''
      if ($maj -match '^\d+$' -and [int]$maj -ge 18) { $script:NodeOk = $true; Ok "Node $NodeVer 就绪" }
      else { Warn "Node $NodeVer 仍不满足 >=18" }
    }
  }
  if (-not (HasCmd "node")) {
    Warn "没装成 —— 空间桥接会跳过。手动装："
    Note (System-Hint node)
  }
}

# ── 2. 源码 ──────────────────────────────────────────────────────────
Say "2/7 源码"
$SelfDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $SelfDir "qqbot\bot.py")) {
  $Root = $SelfDir
  Ok "就在当前目录：$Root"
} elseif ($TargetDir -and (Test-Path (Join-Path $TargetDir "qqbot\bot.py"))) {
  $Root = $TargetDir
  Ok "已有源码：$Root"
} elseif ($CheckOnly -or $DetectOnly) {
  Die "找不到源码（-CheckOnly / -DetectOnly 模式不会去下载）"
} else {
  if (-not (HasCmd "git")) {
    Say "缺 git（clone 源码要用）"
    if (Install-Pkg "git" @("git")) { $script:GitVer = (& git --version 2>$null | Select-Object -First 1) }
  }
  if (HasCmd "git") {
    Write-Host "  -> git clone $RepoUrl $TargetDir"
    Mut { & git clone --depth 1 $RepoUrl $TargetDir } "git clone $RepoUrl $TargetDir"
    $Root = $TargetDir
    Ok "已 clone 到 $Root"
  } else {
    Die "没有 git。请手动下载 Release 源码包解压，再在本目录里跑本脚本"
  }
}
$QQBOT = Join-Path $Root "qqbot"
if (-not (Test-Path (Join-Path $QQBOT "requirements.txt"))) { Die "缺 requirements.txt，源码不完整" }

Install-Python
Install-Node

# ── 3. venv + 依赖 ───────────────────────────────────────────────────
Say "3/7 Python 依赖"
$VenvPy = Join-Path $QQBOT ".venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
  Ok "已有 .venv，跳过创建"
} elseif ($ReadOnlyMode) {
  Warn "没有 .venv（-CheckOnly / -DetectOnly / -DryRun 模式不创建）"
} else {
  # 缺 venv/ensurepip 时先补（Windows 的官方安装器一般自带，conda 环境里可能没有）
  if (-not $PyVenvModule) {
    Warn "$Py 缺 venv/ensurepip 模块"
    if ($Pkg -ne "none") { Warn "Windows 上建议直接用 python.org 的安装器重装一份，它会自带 venv" }
  }
  if (-not $PyPreArgs) { $PyPreArgs = @() }
  try {
    Mut { & $Py @($PyPreArgs) -m venv (Join-Path $QQBOT ".venv") } "$Py -m venv .venv"
  } catch {
    Die "建不出 venv：$($_.Exception.Message)。手动：$Py -m venv `"$QQBOT\.venv`""
  }
  if (Test-Path $VenvPy) { Ok "已创建 $QQBOT\.venv" }
}

if ((Test-Path $VenvPy) -and (-not $ReadOnlyMode)) {
  # ★ 必须用 venv 里的 python -m pip：用系统 pip 会装到全局，然后 import 失败
  $pipArgs = @("-m", "pip", "install")
  $pipMirror = @()
  if ($MirrorPip) { $pipMirror = @("-i", $MirrorPip) }
  Mut { & $VenvPy @pipArgs --upgrade pip -q @pipMirror } "pip install --upgrade pip"
  try {
    Mut { & $VenvPy @pipArgs -r (Join-Path $QQBOT "requirements.txt") -q @pipMirror } "pip install -r requirements.txt"
    if ($LASTEXITCODE -ne 0) { throw "pip 返回 $LASTEXITCODE" }
    Ok "依赖装好$(if ($MirrorPip) { "（走 $MirrorPipName）" })"
  } catch {
    if (-not $MirrorPip -and $null -ne $TTuna) {
      Warn "直连官方源装失败了，换清华源重试一次"
      Mut { & $VenvPy @pipArgs -r (Join-Path $QQBOT "requirements.txt") -q -i $TunaUrl } "pip install -r requirements.txt -i $TunaUrl"
      if ($LASTEXITCODE -ne 0) { Die "依赖还是装不上，手工：$VenvPy -m pip install -r requirements.txt" }
      Ok "依赖装好（清华源）"
    } else {
      Die "依赖装不上，手工：$VenvPy -m pip install -r requirements.txt"
    }
  }
}

# ── 4. config.json ───────────────────────────────────────────────────
Say "4/7 配置"
$Cfg = Join-Path $QQBOT "config.json"
$ExCfg = Join-Path $QQBOT "config.example.json"
if (Test-Path $Cfg) {
  Ok "config.json 已存在，**不覆盖**（覆盖会把你的 token / QQ 号弄丢）"
} elseif ((Test-Path $ExCfg) -and (-not $ReadOnlyMode)) {
  Copy-Item $ExCfg $Cfg
  Ok "已从模板生成 config.json —— 记得改 bot_qq / admin.user_ids / access_token"
} else {
  Warn "没有 config.json"
}

# ── 5. 空间桥接（可选） ──────────────────────────────────────────────
Say "5/7 空间桥接（可选）"
$Bridge = Join-Path $Root "qzone-bridge"
if (-not (Test-Path $Bridge)) {
  Warn "没有 qzone-bridge 目录，跳过"
} elseif (-not (HasCmd "npm")) {
  Warn "没有 npm，跳过（装完 node 后：cd qzone-bridge; npm ci; npm run build）"
  Note (System-Hint node)
} elseif ($ReadOnlyMode) {
  Warn "跳过安装（-CheckOnly / -DetectOnly / -DryRun）"
} else {
  # 只在 lock 变了或没装过时才重装 —— npm ci 会删掉整个 node_modules 重下几百 MB
  $LockHashFile = Join-Path $Bridge "node_modules\.lock-hash"
  $Want = (Get-FileHash (Join-Path $Bridge "package-lock.json") -Algorithm SHA256).Hash
  $npmArgs = @("ci", "--no-audit", "--no-fund")
  if ($MirrorNpm) { $npmArgs += "--registry=$MirrorNpm" }
  if ((Test-Path $LockHashFile) -and ((Get-Content $LockHashFile -Raw).Trim() -eq $Want)) {
    Ok "依赖已是最新（lock 未变），跳过 npm ci"
  } else {
    Push-Location $Bridge
    try {
      & npm @npmArgs
      if ($LASTEXITCODE -eq 0) {
        Set-Content -Path $LockHashFile -Value $Want -NoNewline
        Ok "依赖装好$(if ($MirrorNpm) { "（走 $MirrorNpmName）" })"
      } elseif ((-not $MirrorNpm) -and $null -ne $TNpmm) {
        Warn "官方 npm 源装失败，换 npmmirror 重试一次"
        & npm ci --no-audit --no-fund --registry=$NpmmUrl
        if ($LASTEXITCODE -eq 0) { Set-Content -Path $LockHashFile -Value $Want -NoNewline; Ok "依赖装好（npmmirror）" }
        else { Warn "npm 依赖装失败，稍后手工：cd qzone-bridge; npm ci" }
      } else {
        Warn "npm 依赖装失败，稍后手工：cd qzone-bridge; npm ci"
      }
    } finally { Pop-Location }
  }
  if ((-not (Test-Path (Join-Path $Bridge "dist\main.js"))) -and (-not (Test-Path (Join-Path $Bridge ".env")))) {
    Warn "还没配 .env —— 复制 .env.example 改名并填 QQ 空间 Cookie"
  }
  # Playwright 的 chromium 只在「刷新 Cookie」时才用得上，150 MB 左右，默认不装
  if ($WithBrowser) {
    Push-Location $Bridge
    try {
      Mut { & npm exec -- playwright install chromium } "playwright install chromium"
      if ($LASTEXITCODE -ne 0) { Warn "chromium 没装成，手工：cd qzone-bridge; npx playwright install chromium" }
    } finally { Pop-Location }
  } else {
    Note "需要浏览器刷 Cookie 时再加：install.ps1 -WithBrowser"
  }
}

# ── 6. 体检 ──────────────────────────────────────────────────────────
Say "6/7 体检"
$DoctorRc = 1
if ((Test-Path $VenvPy) -and (-not $ReadOnlyMode)) {
  & $VenvPy (Join-Path $QQBOT "tools\doctor.py")
  $DoctorRc = $LASTEXITCODE
} else {
  Warn "没有 venv 或处于只读模式，跳过体检"
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
  ℹ️ 想知道本机环境长什么样：install.ps1 -DetectOnly（只读，不装东西）
"@ | Write-Host

exit $DoctorRc
