"""许杏玖 · 静默启动 / 停止器（不出现终端黑窗口）

为什么不用 .bat + VBS：
  - .bat 必然带一个控制台窗口；
  - VBS 的 Shell.Run(..., 0) 确实能隐藏窗口，但在部分受限环境（企业策略 / 安全软件 /
    沙箱）里 wscript/cscript 会被当成 LOLBin 拦掉，能不能用完全看运气。
于是改成：**双击 .pyw → Windows 用 pythonw.exe 跑它 → 本身无窗口**，
再由它用 subprocess 的 CREATE_NO_WINDOW 起子进程 → 子进程也没有窗口。

对外入口：
    启动.pyw      静默启动（缺失的组件才启，绝不杀任何进程）
    停止.pyw      只停 bot 与空间桥接，**绝不动 QQ / NapCat**
    状态.pyw      打印当前各端口与 PID 归属

命令行也可用：python launcher.py start|stop|status

⚠️ 重要：绝不用 `taskkill /IM QQ.exe`。
   你的日常 QQ 号与 NapCat 注入的机器人号用的是**同一个 QQ.exe**（同一份安装），
   按镜像名杀会把你的日常号一起带走 —— 这正是「每次都要重新登录」的根因。
   需要停的进程一律按**端口**定位，再核对镜像名，QQ 永不在候选里。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "state"
STOP_FLAG = STATE / "launcher.stop"
LAUNCH_LOCK = STATE / "launcher.lock"
MARKER = BASE / "launcher.running.json"

VENV_PY = BASE / ".venv" / "Scripts" / "python.exe"
BOT_LOG = BASE / "bot_boot.log"


def _sibling(name: str, fallback: str) -> Path:
    """取同级兄弟目录：<root>/{qqbot, qzone-bridge, NapCat} 三个平级。

    这样整体搬盘（换个目录、换个盘符）不用改任何代码。
    找不到才退回给定旧路径。
    """
    cand = BASE.parent / name
    return cand if cand.is_dir() else Path(fallback)


QZONE_DIR = _sibling("qzone-bridge", "qzone-bridge")
QZONE_LOG = QZONE_DIR / "qzone-bridge.log"
NAPCAT_DIR = _sibling("NapCat", "NapCat")
NAPCAT_LAUNCHER = NAPCAT_DIR / "launcher-user.bat"

# 各组件期望的镜像名（用于二次确认，避免误杀同名端口的其它程序）
EXPECT = {
    6199: ("python", "pythonw"),      # bot 主服务（反向 WS）
    6200: ("python", "pythonw"),      # bot 内嵌控制台
    5700: ("node",),                  # qzone-bridge
}


def _node_candidates() -> list[Path]:
    """node.exe 的候选位置：环境变量 NODE_EXE > PATH 里的 node > 常见安装路径。

    qzone-bridge 是 Node 项目，启动器要自己把 node 拉起来。
    """
    out: list[Path] = []
    env = os.environ.get("NODE_EXE")
    if env:
        out.append(Path(env))
    found = shutil.which("node")
    if found:
        out.append(Path(found))
    out += [Path(r"C:\Program Files\nodejs\node.exe"),
            Path(r"C:\Program Files (x86)\nodejs\node.exe")]
    return out


NODE_CANDIDATES = _node_candidates()


# ──────────────────────────────────────────────
# 基础工具
# ──────────────────────────────────────────────

def no_window() -> int:
    """Windows 上让子进程不分配控制台窗口。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, errors="replace",
                          creationflags=no_window(), **kw)


def port_pid(port: int) -> int | None:
    """返回监听该端口的 PID；没有则 None。"""
    out = _run(["netstat", "-ano"]).stdout or ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[-1] == "":
            continue
        if "LISTENING" not in line:
            continue
        local = parts[1]
        if local.endswith(":" + str(port)):
            try:
                return int(parts[-1])
            except ValueError:
                continue
    return None


def image_name(pid: int) -> str:
    """取进程镜像名（不含 .exe），失败返回空串。

    tasklist 的 CSV 形如：\"python.exe\",\"47796\",\"Console\",\"1\",\"43,656 K\"
    第二列是 PID —— 名字只在第一列。注意内存那列本身含逗号，所以不要按整行切。
    """
    out = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).stdout or ""
    for line in out.splitlines():
        cells = [c.strip().strip('"') for c in line.split(",")]
        if cells and cells[0].lower().endswith(".exe"):
            return cells[0].rsplit(".", 1)[0].lower()
    return ""


def is_up(port: int) -> bool:
    return port_pid(port) is not None


def owned_by(port: int) -> tuple[int | None, str]:
    """确认端口被期望的程序占用。返回 (pid, 说明)。"""
    pid = port_pid(port)
    if pid is None:
        return None, "未监听"
    name = image_name(pid)
    allow = EXPECT.get(port, ())
    if allow and name and name not in allow:
        return pid, f"被 {name}.exe(PID {pid}) 占用 —— 不是预期程序，不动它"
    return pid, f"{name}.exe(PID {pid})"


def kill_pid(pid: int) -> bool:
    r = _run(["taskkill", "/F", "/PID", str(pid)])
    return r.returncode == 0


def log(msg: str) -> None:
    line = f"[launcher] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    # 必须 flush：输出被重定向到文件/管道时是块缓冲的，进程被 kill 会丢掉全部日志
    # （曾因此误判成"卡住且没有任何输出"）
    print(line, flush=True)
    try:
        with (BASE / "launcher.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def ui_warn(title: str, text: str) -> None:
    """没有控制台时（双击 .pyw，stdout 为 None）用弹框把话说给用户。

    否则 pythonw 下所有日志都只写进文件，用户会对着"什么都没发生"发懵。
    有控制台时直接返回 —— 日志里已经能看到了。
    """
    if sys.stdout is not None:
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x30)  # MB_ICONWARNING
    except Exception:
        pass


# ──────────────────────────────────────────────
# 单击守护锁（防止同时跑两个启动器互相抢着重启）
# ──────────────────────────────────────────────

_lock_fh = None
# 本次进程是否拿到了启动器锁（用于决定要不要进守护循环）
_lock_held = False


def acquire_launcher_lock() -> bool:
    global _lock_fh
    try:
        import msvcrt
    except ImportError:
        return True
    STATE.mkdir(parents=True, exist_ok=True)
    fh = open(LAUNCH_LOCK, "a+", encoding="utf-8")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return False
    fh.seek(0)
    fh.write(f"{os.getpid()}\n")
    fh.truncate()
    fh.flush()
    _lock_fh = fh
    return True


# ──────────────────────────────────────────────
# 启动
# ──────────────────────────────────────────────

def _spawn(args, cwd: Path, logfile: Path):
    logfile.parent.mkdir(parents=True, exist_ok=True)
    fh = open(logfile, "a", encoding="utf-8", errors="replace")
    fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {args[0]} =====\n")
    fh.flush()
    return subprocess.Popen(
        args, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=no_window(), close_fds=True,
    )


def find_node() -> Path | None:
    for p in NODE_CANDIDATES:
        if p.exists():
            return p
    return None


def start_qzone() -> bool:
    pid, why = owned_by(5700)
    if pid:
        log(f"空间桥接已在运行（5700：{why}），跳过")
        return True
    node = find_node()
    if node is None:
        log("找不到 node.exe，跳过空间桥接")
        return False
    tsx = QZONE_DIR / "node_modules" / "tsx" / "dist" / "cli.mjs"
    if not tsx.exists():
        log(f"找不到 tsx：{tsx}，跳过空间桥接")
        return False
    _spawn([str(node), str(tsx), "src/main.ts"], QZONE_DIR, QZONE_LOG)
    log(f"已启动空间桥接（5700），日志 -> {QZONE_LOG}")
    return True


def start_bot() -> bool:
    pid, why = owned_by(6199)
    if pid:
        log(f"bot 已在运行（6199：{why}），跳过")
        return True
    # 用 pythonw.exe 起：彻底没有控制台（日志本来就落 bot.log）
    pyw = VENV_PY.with_name("pythonw.exe")
    exe = pyw if pyw.exists() else VENV_PY
    _spawn([str(exe), "bot.py"], BASE, BOT_LOG)
    log(f"已启动 bot（6199 + 6200），启动器日志 -> {BOT_LOG}")
    return True


def _qq_exe_path() -> Path | None:
    """从注册表找 QQ 安装路径。

    ⚠️ 用 Python 的 winreg，**不要**去调 reg.exe —— 部分受限环境（企业策略 / 安全软件 /
    沙箱）会把 reg.exe 拉黑（PROGRAM BLOCKED BY SECURITY POLICY），而 NapCat 自带的
    launcher-user.bat 正是靠 reg.exe 找 QQ 路径的，所以在这些环境里那条路走不通。
    """
    try:
        import winreg
    except ImportError:
        return None
    candidates = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
    ]
    for hive, sub in candidates:
        try:
            with winreg.OpenKey(hive, sub) as k:
                val = str(winreg.QueryValueEx(k, "UninstallString")[0])
        except OSError:
            continue
        exe = Path(val.strip('"')).parent / "QQ.exe"
        if exe.exists():
            return exe
    # 环境变量 > 常见安装位置兜底
    guesses = [os.environ.get("QQ_EXE"),
               r"C:\Program Files\Tencent\QQNT\QQ.exe",
               r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
               r"D:\Program Files\Tencent\QQNT\QQ.exe"]
    for guess in guesses:
        if not guess:
            continue
        p = Path(guess)
        if p.exists():
            return p
    return None


def _qq_running() -> bool:
    r = _run(["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/FO", "CSV", "/NH"])
    return "QQ.exe" in (r.stdout or "")


def _napcat_alive(wait_seconds: int = 10) -> bool:
    """等几秒看 QQ 有没有活下来。

    QQ 是 Electron 应用，可能「起来又自己退出」（需要登录、或上次被强制
    结束后残留了状态）。只报一句"已启动"会让人对着没反应的 QQ 发懵，
    所以这里确认一下再给结论。
    """
    for _ in range(wait_seconds):
        time.sleep(1)
        if _qq_running():
            return True
    return False


def napcat_connected() -> bool:
    """NapCat 是否已经接上了 bot。

    ⚠️ **不能**拿 `NapCatWinBootMain.exe` 是否存在来判断 —— 它只是个注入器，
    注入完成后自己就退出了（曾因此把"已连接"误报成"未运行"）。
    唯一可靠的判据是 bot 那边有没有活的 WebSocket 连接。
    """
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{console_port()}/api/bot/status",
            headers={"x-console-token": console_token()})
        with urllib.request.urlopen(req, timeout=5) as f:
            return bool(json.loads(f.read()).get("ws_connected"))
    except Exception:
        return False


def qq_process_count() -> int:
    r = _run(["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/FO", "CSV", "/NH"])
    return sum(1 for ln in (r.stdout or "").splitlines() if "QQ.exe" in ln)


def start_napcat() -> bool:
    """只在 NapCat 确实没接上时才启动它；**永不杀 QQ**。

    两道闸门，防止重复注入出多个 QQ 实例（踩过：变成 8 个 QQ 进程）：
      1. bot 已经报 ws_connected → 直接跳过
      2. 已经有 QQ.exe 在跑（但还没连上）→ 也不再注入，
         而是提示用户去那个 QQ 窗口完成登录
    """
    if napcat_connected():
        log("NapCat 已连接（bot 侧有活的 WS），跳过")
        return True

    if qq_process_count() > 0:
        log(f"已有 {qq_process_count()} 个 QQ 进程在跑但还没连上 bot，"
            f"不再注入第二个实例。")
        log("请到那个 QQ 窗口完成机器人号登录；若窗口已关，先手动关干净所有 QQ 再重试。")
        ui_warn("NapCat 还没连上",
                "检测到 QQ 已在运行，但没有连上机器人。\n\n"
                "为避免开出多个 QQ 实例，这次不再注入。请：\n"
                f"1. 看一下 QQ 窗口，是否停在登录界面？登录机器人号 {bot_qq() or '（见 config.json 的 bot_qq）'}\n"
                "2. 如果不确定，先把所有 QQ 窗口关干净，再双击 启动.pyw\n\n"
                "控制台：http://127.0.0.1:6200/")
        return False

    boot = NAPCAT_DIR / "NapCatWinBootMain.exe"
    hook = NAPCAT_DIR / "NapCatWinBootHook.dll"
    main_js = NAPCAT_DIR / "napcat.mjs"
    load_js = NAPCAT_DIR / "loadNapCat.js"

    launched = False
    if boot.exists() and hook.exists() and main_js.exists():
        qq = _qq_exe_path()
        if qq is not None:
            try:
                main_posix = str(main_js).replace("\\", "/")
                load_js.write_text(
                    f'(async () => {{await import("file:///{main_posix}")}})()',
                    encoding="utf-8")
                env = os.environ.copy()
                env.update({
                    "NAPCAT_PATCH_PACKAGE": str(NAPCAT_DIR / "qqnt.json"),
                    "NAPCAT_LOAD_PATH": str(load_js),
                    "NAPCAT_INJECT_PATH": str(hook),
                    "NAPCAT_LAUNCHER_PATH": str(boot),
                    "NAPCAT_MAIN_PATH": main_posix,
                    # 给下面那个 bat 用的（bat 本体保持纯 ASCII，避免中文路径的编码坑）
                    "QZBOT_NAPCAT_DIR": str(NAPCAT_DIR),
                    "QZBOT_QQ_PATH": str(qq),
                })
                # ⚠️ 必须先把控制台代码页设成 UTF-8（chcp 65001）再启动。
                # 直接 Popen NapCatWinBootMain.exe 的话，新控制台是系统默认的 GBK，
                # 而 NapCat 往 stdout 写的是 UTF-8 —— 中文全成乱码。
                # NapCat 自带的 launcher-user.bat 正是靠 chcp 65001 解决的，
                # 我早先"复刻"时漏了这一步。
                # 用 bat 而不是把命令拼在 cmd 命令行上：bat 内容可以保持纯 ASCII，
                # 真正的路径通过环境变量传进来（Python 传的是 UTF-16，不会有编码问题）。
                helper = NAPCAT_DIR / "_qqbot_launch.bat"
                helper.write_text(
                    "@echo off\r\n"
                    "rem 本文件由 qqbot\\launcher.py 自动生成，请勿手动修改\r\n"
                    "rem 作用：把控制台代码页切成 UTF-8，否则 NapCat 的中文输出会是乱码\r\n"
                    "chcp 65001 >nul\r\n"
                    "set NAPCAT_PATCH_PACKAGE=%QZBOT_NAPCAT_DIR%\\qqnt.json\r\n"
                    "set NAPCAT_LOAD_PATH=%QZBOT_NAPCAT_DIR%\\loadNapCat.js\r\n"
                    "set NAPCAT_INJECT_PATH=%QZBOT_NAPCAT_DIR%\\NapCatWinBootHook.dll\r\n"
                    "set NAPCAT_LAUNCHER_PATH=%QZBOT_NAPCAT_DIR%\\NapCatWinBootMain.exe\r\n"
                    "\"%NAPCAT_LAUNCHER_PATH%\" \"%QZBOT_QQ_PATH%\" \"%NAPCAT_INJECT_PATH%\"\r\n",
                    encoding="ascii")
                subprocess.Popen(
                    ["cmd", "/c", str(helper)],
                    cwd=str(NAPCAT_DIR), env=env,
                    creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
                )
                log(f"已拉起 NapCat 注入器（目标 {qq}），控制台已设为 UTF-8")
                launched = True
            except Exception as exc:
                log(f"Python 方式启动 NapCat 失败（{type(exc).__name__}: {exc}），回退到 .bat")

    if not launched:
        if not NAPCAT_LAUNCHER.exists():
            log(f"找不到 NapCat 启动脚本：{NAPCAT_LAUNCHER}，跳过")
            return False
        subprocess.Popen(
            ["cmd", "/c", str(NAPCAT_LAUNCHER)],
            cwd=str(NAPCAT_DIR),
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
        )
        log("已通过 launcher-user.bat 拉起 NapCat，正在等 QQ 起来…")

    if _napcat_alive(15):
        log("QQ 进程已存活；等它连上 bot（登录后通常几秒内）")
        return True

    log("⚠ QQ 没有存活下来 —— NapCat 注入器起来了，但 QQ 自己退出了。")
    log("   常见原因：① QQ 需要登录（机器人号未登录）② 上次被强制结束后有残留状态")
    log(f"   请手动双击：{NAPCAT_LAUNCHER}")
    log("   （那个窗口是有界面的，QQ 的具体提示能看到；启动器这边隐藏了窗口所以看不到）")
    ui_warn("QQ 没起来（NapCat）",
            "NapCat 的注入器已经拉起，但 QQ 进程随后自己退出了。\n\n"
            "结果：机器人现在收不到 QQ 消息（bot 与空间桥接都正常）。\n\n"
            "请手动双击这个文件，它是有窗口的，能看到 QQ 的具体提示：\n"
            f"{NAPCAT_LAUNCHER}\n\n"
            "常见原因：\n"
            f"① QQ 需要登录（机器人号 {bot_qq() or '（见 config.json 的 bot_qq）'} 未登录）\n"
            "② 上一次 QQ 被强制结束，残留了状态 —— 先手动关掉所有 QQ 窗口再试\n\n"
            "日志都在控制台的「日志」页：http://127.0.0.1:6200/")
    return False


def wait_port(port: int, seconds: int) -> bool:
    for _ in range(seconds * 2):
        if is_up(port):
            return True
        time.sleep(0.5)
    return False


def wait_port_free(port: int, seconds: int) -> bool:
    """等端口**真正没人监听**了（等旧进程退出）。返回 False 表示超时仍未释放。

    ⚠️ 为什么不能用固定 sleep 代替：下一步要删 `state/bot.lock`。
    那个锁是 bot 自己的单实例保护（bot.py 里 msvcrt 加锁，被占用就退出）。
    taskkill 之后旧进程未必立刻死透 —— 此时删锁，新进程就能成功加锁，
    于是**两个 bot 同时写同一批状态文件**（mood.json / memory.json / tokens.json…）。
    所以必须先确认端口空了，再动锁。
    """
    for _ in range(seconds * 2):
        if port_pid(port) is None:
            return True
        time.sleep(0.5)
    return False


# ──────────────────────────────────────────────
# 打开控制台（自动带令牌，用户不用手输）
# ──────────────────────────────────────────────

def console_port() -> int:
    """控制台端口。以 config.json 为准，读不到就 6200。"""
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        return int((cfg.get("console") or {}).get("port") or 6200)
    except Exception:
        return 6200


def bot_qq() -> str:
    """机器人 QQ 号，只用于提示文案。读不到就返回空串。"""
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        return str(cfg.get("bot_qq") or "")
    except Exception:
        return ""


def console_token() -> str:
    try:
        return (STATE / "console-token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def console_url() -> str:
    url = f"http://127.0.0.1:{console_port()}/"
    tok = console_token()
    return url + ("?token=" + tok if tok else "")


def open_console() -> bool:
    """用默认浏览器打开控制台。带令牌，省得用户去找 state/console-token。"""
    port = console_port()
    url = console_url()
    if not is_up(port):
        log(f"控制台（{port}）还没在监听，先不打开；请先跑一次启动")
        return False
    try:
        os.startfile(url)          # Windows 原生，直接用默认浏览器
        log(f"已在浏览器打开控制台：http://127.0.0.1:{port}/")
        return True
    except Exception as exc:
        log(f"打开浏览器失败（{type(exc).__name__}: {exc}）；请手动访问 {url}")
        return False


def start_all() -> int:
    global _lock_held
    STATE.mkdir(parents=True, exist_ok=True)
    if STOP_FLAG.exists():
        STOP_FLAG.unlink()

    _lock_held = acquire_launcher_lock()
    if not _lock_held:
        # 已有守护在跑。**不要直接退出** —— 用户双击 启动.pyw 时期望的是
        # "面板打开"，什么都不发生会让人以为坏了。所以照常补齐组件，
        # 由 main() 负责把面板打开，然后本进程就结束（不做第二个守护）。
        log("已有守护进程在运行：本次只补齐缺失的组件，不再起第二个守护")

    log("开始启动（只会补启缺失的组件，不杀任何进程）")
    start_qzone()
    start_bot()
    wait_port(6199, 40)
    wait_port(6200, 20)
    start_napcat()

    log(f"6199/6200/5700 -> {is_up(6199)}/{is_up(6200)}/{is_up(5700)}")
    log("控制台：http://127.0.0.1:6200/   停止：双击 停止.pyw")
    write_marker()
    return 0


def write_marker() -> None:
    try:
        MARKER.write_text(json.dumps({
            "launcher_pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ports": {str(p): port_pid(p) for p in (6199, 6200, 5700)},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def supervise() -> int:
    """看住 bot 与空间桥接：意外退出就拉起来；看到停止标记就收工。

    用端口而非 Popen 句柄判断存活 —— 这样即使 bot 是被别的途径拉起来的也认。
    重复启动不会出事：bot 自己有单实例锁，第二个实例会以退出码 2 退出。
    """
    recent: list[float] = []
    while True:
        if STOP_FLAG.exists():
            log("检测到停止标记，守护退出")
            return 0
        try:
            if not is_up(6199):
                now = time.time()
                recent = [t for t in recent if now - t < 120]
                if len(recent) >= 5:
                    log("bot 两分钟内反复退出 5 次，停止重试；请到控制台看日志")
                    return 1
                log("bot 不在监听，重新拉起")
                start_bot()
                recent.append(now)
            if not is_up(5700):
                log("空间桥接不在监听，重新拉起")
                start_qzone()
            write_marker()
        except Exception as exc:  # 守护进程自己绝不能死
            log(f"守护循环异常（已忽略）：{type(exc).__name__}: {exc}")
        time.sleep(5)


# ──────────────────────────────────────────────
# 停止（只碰 bot 与空间桥接）
# ──────────────────────────────────────────────

def stop_all() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        STOP_FLAG.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass

    log("开始停止（只停 bot 与空间桥接，不动 QQ / NapCat）")
    stopped = []
    for port in (6199, 6200, 5700):
        pid, why = owned_by(port)
        if pid is None:
            log(f"  {port}：本来就没人监听")
            continue
        name = image_name(pid)
        if EXPECT.get(port) and name and name not in EXPECT[port]:
            log(f"  {port}：{why} —— 跳过不杀")
            continue
        if kill_pid(pid):
            stopped.append(f"{port}/{name}({pid})")
            log(f"  {port}：已停止 {why}")
        else:
            log(f"  {port}：停止失败（{why}），可能需要管理员权限")
        time.sleep(0.6)

    # 清掉可能残留的锁。**必须等端口真空了再清** ——
    # 否则旧进程还活着时锁就没了，新进程会一起跑起来，两个 bot 写同一批状态文件。
    for port in (6199, 6200):
        if not wait_port_free(port, 15):
            log(f"  {port}：15 秒仍未释放，先不清理 bot.lock（避免双实例）")
            return 0
    for f in (STATE / "bot.lock",):
        try:
            f.unlink()
        except OSError:
            pass
    log(f"已停止：{', '.join(stopped) if stopped else '（没有需要停的）'}")
    log("QQ 与 NapCat 未受影响")
    return 0


# ──────────────────────────────────────────────
# 状态
# ──────────────────────────────────────────────

def status() -> int:
    print("许杏玖 · 运行状态")
    print("-" * 46)
    for port, label in ((6199, "bot 主服务"), (6200, "控制台"), (5700, "空间桥接")):
        pid, why = owned_by(port)
        mark = "✔" if pid else "✘"
        print(f"  {mark} {port:>4}  {label:<10} {why}")
    conn = napcat_connected()
    print(f"  {'✔' if conn else '✘'} NapCat          "
          f"{'已连接 bot' if conn else '未连接（QQ 没启动或还没登录）'}")
    n = qq_process_count()
    # 一个 QQ 实例（QQNT）本身就有 4 个左右进程，明显多于此说明开了多个实例
    hint = "   ← 可能开了多个 QQ 实例，建议全部关掉重开" if n > 5 else ""
    print(f"  · QQ 进程       {n} 个（本工具不会杀它）{hint}")
    if MARKER.exists():
        print("-" * 46)
        print("  上次启动：" + MARKER.read_text(encoding="utf-8").strip().replace("\n", " "))
    print("-" * 46)
    print("  控制台 http://127.0.0.1:6200/")
    return 0


# ──────────────────────────────────────────────

def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "start"
    if cmd == "start":
        rc = start_all()
        if rc != 0:
            return rc
        # 启动完直接把面板打开（带令牌），用户不用自己找地址
        open_console()
        if not _lock_held:
            # 已有守护在跑，本进程只负责"补齐 + 开面板"，然后收工
            log("已有守护在运行，本进程退出（面板已打开）")
            return 0
        # 之后进入守护：崩了自动拉起。停止.pyw 会写 stop 标记让它收工。
        return supervise()
    if cmd == "start-only":
        return start_all()
    if cmd == "open":
        return 0 if open_console() else 1
    if cmd == "watch":
        return supervise()
    if cmd == "stop":
        return stop_all()
    if cmd == "status":
        return status()
    if cmd == "restart-bot":
        # 只重启 bot：先按端口停掉 6199/6200，再起
        for port in (6199, 6200):
            pid, why = owned_by(port)
            if pid:
                name = image_name(pid)
                if not EXPECT.get(port) or not name or name in EXPECT[port]:
                    kill_pid(pid)
                    log(f"已停止 {port}（{why}）")
                    time.sleep(0.6)
        # 同样先确认端口空了再清锁（理由见 wait_port_free 的注释）：
        # 锁是 bot 的单实例保护，旧进程没死透就删锁会放出双实例
        for port in (6199, 6200):
            if not wait_port_free(port, 15):
                log(f"{port}：15 秒仍未释放，保留 bot.lock 不动（避免双实例）")
                return 0
        try:
            (STATE / "bot.lock").unlink()
        except OSError:
            pass
        start_bot()
        wait_port(6199, 40)
        write_marker()
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
