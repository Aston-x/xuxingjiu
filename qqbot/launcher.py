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

def _venv_python() -> Path:
    """虚拟环境里的解释器。Windows 是 `.venv/Scripts/python.exe`，
    POSIX 是 `.venv/bin/python`（另兜一个 python3，有些发行版只有后者）。"""
    if os.name == "nt":
        return BASE / ".venv" / "Scripts" / "python.exe"
    for name in ("python", "python3"):
        p = BASE / ".venv" / "bin" / name
        if p.exists():
            return p
    return BASE / ".venv" / "bin" / "python"


VENV_PY = _venv_python()
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

# 端口 -> 期望的**命令行关键词**（跨平台首选判据）
# 镜像名在各平台不一样（python / python3 / Python），在 venv 里还可能是软链，
# 所以"这个名字对不对"远不如"命令行里有没有 bot.py"可靠。
EXPECT_CMD = {
    6199: ("bot.py", "launcher.py"),          # bot 主服务（反向 WS）
    6200: ("bot.py",),                        # bot 内嵌控制台
    5700: ("main.ts", "dist/main.js", "qzone-bridge"),
}
# 端口 -> 期望的工作目录末段（命令行拿不到时的次选判据）
EXPECT_CWD = {6199: ("qqbot",), 6200: ("qqbot",), 5700: ("qzone-bridge",)}
# 端口 -> 镜像名白名单（最后的兜底，等价于旧行为）
EXPECT = {
    6199: ("python", "python3", "Python", "pythonw"),
    6200: ("python", "python3", "Python", "pythonw"),
    5700: ("node", "nodejs"),
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


def _psutil():
    """有 psutil 就用它（三平台一套 API，还能直接读 cmdline/cwd）。

    没有也完全能跑 —— 只是探测要靠平台命令，且拿不到命令行特征。**不强制依赖**。
    """
    try:
        import psutil  # noqa: PLC0415
        return psutil
    except ImportError:
        return None


def port_pid(port: int) -> int | None:
    """返回监听该端口的 PID；拿不到就 None。

    顺序：psutil → 平台命令（Windows netstat / Linux ss / macOS lsof）→ None。
    **拿不到 PID 时上层必须退化成"只认有人在监听"，绝不能盲杀。**
    """
    ps = _psutil()
    if ps is not None:
        try:
            for conn in ps.net_connections(kind="inet"):
                if not conn.laddr or conn.laddr.port != int(port):
                    continue
                if getattr(conn, "status", "") in ("LISTEN", "LISTENING") or conn.pid:
                    if conn.pid:
                        return int(conn.pid)
        except Exception:  # noqa: BLE001  权限不足 / 平台差异，退回命令行
            pass

    if os.name == "nt":
        out = _run(["netstat", "-ano"]).stdout or ""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5 or "LISTENING" not in line:
                continue
            if parts[1].endswith(":" + str(port)):
                try:
                    return int(parts[-1])
                except ValueError:
                    continue
        return None

    if sys.platform == "darwin":
        out = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).stdout or ""
        for line in out.splitlines():
            try:
                return int(line.strip())
            except ValueError:
                continue
        return None

    # Linux：ss 优先，没有就退 /proc
    out = _run(["ss", "-ltnpH", f"sport = :{port}"]).stdout or ""
    marker = "pid="
    for line in out.splitlines():
        idx = line.find(marker)
        if idx < 0:
            continue
        rest = line[idx + len(marker):]
        num = ""
        for ch in rest:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            return int(num)
    return None


def proc_info(pid: int) -> dict:
    """取进程的 {name, cmdline, cwd}，拿不到就返回空 dict。

    `cmdline` 是跨平台判断"这个端口是不是我的进程"的**唯一可靠依据** ——
    镜像名在三平台各不相同（python / python3 / Python），而且在 venv 里还可能是软链。
    """
    if not pid:
        return {}
    ps = _psutil()
    if ps is not None:
        try:
            p = ps.Process(pid)
            return {"name": (p.name() or "").lower(),
                    "cmdline": " ".join(p.cmdline() or []),
                    "cwd": (p.cwd() or "")}
        except Exception:  # noqa: BLE001
            pass
    if os.name == "nt":
        # ⚠️ 这里**不能**调 image_name()：那个函数是 proc_info() 的包装，
        #    会变成无限递归（踩过）。
        return {"name": _win_image_name(pid)}
    try:
        if sys.platform == "darwin":
            name = (_run(["ps", "-p", str(pid), "-o", "comm="]).stdout or "").strip()
            cmd = (_run(["ps", "-p", str(pid), "-o", "command="]).stdout or "").strip()
            return {"name": Path(name).name.lower() if name else "", "cmdline": cmd}
        base = Path(f"/proc/{pid}")
        name = (base / "comm").read_text(encoding="utf-8", errors="replace").strip().lower()
        raw = (base / "cmdline").read_bytes().split(b"\x00")
        cmd = " ".join(x.decode("utf-8", "replace") for x in raw if x)
        cwd = ""
        try:
            cwd = str((base / "cwd").resolve())
        except OSError:
            pass
        return {"name": name, "cmdline": cmd, "cwd": cwd}
    except OSError:
        return {}


def _win_image_name(pid: int) -> str:
    """Windows：用 tasklist 取镜像名（不含 .exe）。

    tasklist 的 CSV 形如：\"python.exe\",\"47796\",\"Console\",\"1\",\"43,656 K\"
    第二列是 PID —— 名字只在第一列。注意内存那列本身含逗号，所以不要按整行切。
    """
    out = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).stdout or ""
    for line in out.splitlines():
        cells = [c.strip().strip('"') for c in line.split(",")]
        if cells and cells[0].lower().endswith(".exe"):
            return cells[0].rsplit(".", 1)[0].lower()
    return ""


def image_name(pid: int) -> str:
    """进程镜像名（不含 .exe）。拿不到返回空串。"""
    return str(proc_info(pid).get("name") or "")


def is_up(port: int) -> bool:
    """端口有没有人在监听。

    ★ 这是**最终兜底**：即使拿不到 PID（权限不足 / 没有 ss / 没有 psutil），
      也必须能回答"这个端口是不是被占着"。纯 socket 探活，三平台一致。
    """
    if port_pid(port) is not None:
        return True
    import socket  # noqa: PLC0415
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.6):
            return True
    except OSError:
        return False


def owns_port(port: int) -> tuple[int | None, str]:
    """这个端口是不是被**我们自己的进程**占着。返回 (pid, 说明)。

    判定顺序（越靠前越可靠）：
      1. 命令行特征：cmdline 里出现了 EXPECT_CMD[port] 里的任一关键词；
      2. 工作目录特征：cwd 末段落在 EXPECT_CWD[port] 里；
      3. 镜像名兜底：旧行为（python / pythonw / node）。

    🔴 拿不到 PID 时返回 (None, 说明)，调用方**绝不能 kill** ——
       这条保护的是"绝不误杀 QQ"。
    """
    pid = port_pid(port)
    if pid is None:
        if is_up(port):
            return None, f"端口被占用，但拿不到 PID（没 psutil / 没权限）"
        return None, "未监听"
    info = proc_info(pid)
    cmd = str(info.get("cmdline") or "")
    cwd = str(info.get("cwd") or "")
    name = str(info.get("name") or "")

    for want in EXPECT_CMD.get(port, ()):
        if want and want in cmd:
            return pid, f"{name or '进程'}(PID {pid}，命令行匹配 {want!r})"
    for want in EXPECT_CWD.get(port, ()):
        if want and Path(cwd).name == want:
            return pid, f"{name or '进程'}(PID {pid}，工作目录 {want})"
    allow = EXPECT.get(port, ())
    if allow and name and name not in allow:
        return pid, f"被 {name}(PID {pid}) 占用 —— 不是预期程序，不动它"
    if name:
        return pid, f"{name}(PID {pid})"
    return pid, f"PID {pid}（拿不到进程名）"


def owned_by(port: int) -> tuple[int | None, str]:
    """旧名字，保留给既有调用点。语义同 owns_port。"""
    return owns_port(port)


def kill_pid(pid: int, *, expect_port: int | None = None) -> bool:
    """结束进程。**kill 前会再核对一次身份**，防 PID 复用（TOCTOU）。

    `port_pid()` 拿到 PID 到这个函数执行之间，目标进程可能已经退出、PID 被系统
    分配给别的程序 —— 不复核就会杀掉一个无辜的进程。
    """
    if not pid:
        return False
    if expect_port is not None:
        who, why = owns_port(expect_port)
        if who != pid or "不是预期程序" in why:
            log(f"拒绝结束 PID {pid}：复核时端口 {expect_port} 的归属已变（{why}）")
            return False
    if os.name == "nt":
        return _run(["taskkill", "/F", "/PID", str(pid)]).returncode == 0
    import signal  # noqa: PLC0415
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        return False
    for _ in range(10):
        time.sleep(0.5)
        if not proc_info(pid):
            return True
    try:
        os.kill(int(pid), signal.SIGKILL)
    except OSError:
        return False
    return True


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
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x30)  # MB_ICONWARNING
            return
        except Exception:  # noqa: BLE001
            pass
    elif sys.platform == "darwin":
        try:
            script = f'display notification {json.dumps(text)} with title {json.dumps(title)}'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
            return
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            subprocess.run(["notify-send", title, text], capture_output=True, timeout=5)
            return
        except Exception:  # noqa: BLE001
            pass
    # 三个平台都弹不出来（无 GUI 服务器）：至少别让它静默消失
    sys.stderr.write(f"\n[{title}] {text}\n")
    log(f"[提示] {title}：{text.splitlines()[0] if text else ''}")


# ──────────────────────────────────────────────
# 单击守护锁（防止同时跑两个启动器互相抢着重启）
# ──────────────────────────────────────────────

_lock_fh = None
# 本次进程是否拿到了启动器锁（用于决定要不要进守护循环）
_lock_held = False


def acquire_launcher_lock() -> bool:
    """拿启动器单实例锁。跨平台（Windows msvcrt / POSIX flock），见 filelock.py。

    以前非 Windows 直接 `return True` —— 等于**静默没有保护**，两个启动器
    可以同时跑并互相抢着重启。现在两边语义一致。
    """
    global _lock_fh
    import filelock  # noqa: PLC0415  同目录模块
    fh, holder = filelock.acquire(LAUNCH_LOCK)
    if fh is None:
        log(f"已有启动器在跑（PID {holder or '?'}），本次不再动手")
        return False
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
        stdin=subprocess.DEVNULL, creationflags=no_window(),
        # POSIX 上加 start_new_session 等价 setsid：脱离当前终端，
        # 关掉终端 / SSH 断开都不会把 bot 一起带走
        start_new_session=(os.name != "nt"),
        close_fds=True,
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
    # 优先跑构建产物：省掉 tsx 这个运行时依赖，Linux 上按 systemd 模板部署时
    # 通常只有 dist/（没有 devDependencies）。都没有才提示去 build。
    dist_main = QZONE_DIR / "dist" / "main.js"
    tsx = QZONE_DIR / "node_modules" / "tsx" / "dist" / "cli.mjs"
    src_main = QZONE_DIR / "src" / "main.ts"
    if dist_main.exists():
        _spawn([str(node), str(dist_main)], QZONE_DIR, QZONE_LOG)
    elif tsx.exists() and src_main.exists():
        _spawn([str(node), str(tsx), "src/main.ts"], QZONE_DIR, QZONE_LOG)
    else:
        log(f"空间桥接既没有 dist/main.js 也没有 tsx+src，跳过"
            f"（先在 {QZONE_DIR} 里 npm install && npm run build）")
        return False
    log(f"已启动空间桥接（5700），日志 -> {QZONE_LOG}")
    return True


def start_bot() -> bool:
    pid, why = owned_by(6199)
    if pid:
        log(f"bot 已在运行（6199：{why}），跳过")
        return True
    # Windows：用 pythonw.exe 起，彻底没有控制台（日志本来就落 bot.log）
    # POSIX：没有 pythonw 这种东西，"无窗口"等于"不占终端" —— 靠 _spawn 里的
    #        start_new_session=True（等价 setsid）实现，所以直接用 venv 里的 python。
    if os.name == "nt":
        pyw = VENV_PY.with_name("pythonw.exe")
        exe = pyw if pyw.exists() else VENV_PY
    else:
        exe = VENV_PY
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
    """只在本平台的接入端确实没接上时才启动它；**永不杀 QQ**。

    两道闸门，防止重复注入出多个 QQ 实例（踩过：变成 8 个 QQ 进程）：
      1. bot 已经报 ws_connected → 直接跳过
      2. 已经有 QQ.exe 在跑（但还没连上）→ 也不再注入，
         而是提示用户去那个 QQ 窗口完成登录
    """
    # 先问「本平台该用哪个接入端」。这一层让"QQ 接入端"不再是 NapCat 专属：
    # 换成 Lagrange 之类时，这里只负责提示怎么对接，不代它启动。
    import onebot  # noqa: PLC0415  同目录模块
    want = str((CFG.get("onebot") or {}).get("adapter") or "auto").strip().lower()
    spec = onebot.pick_adapter(CFG, BASE.parent)
    if spec is None:
        if want == "none":
            log("接入端由你自己管（onebot.adapter=none），启动器不碰它。")
        else:
            log("没探测到可用的 QQ 接入端，跳过启动。")
        log("  " + onebot.connect_hint(CFG))
        return True
    if spec.key != "napcat":
        log(f"当前接入端是「{spec.label}」，本启动器不代为启动它。")
        for line in onebot.credential_hint(spec, CFG).splitlines():
            log("  " + line)
        return True

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

    if os.name != "nt":
        log("QQ 接入端：NapCat 是 Windows 注入式的，当前系统上跑不了。")
        log("  请自备 OneBot v11 实现（Linux/macOS 常见做法：NapCat 的 Docker 镜像，"
            "或 Lagrange.Core / LLOneBot），让它反向连到 "
            f"ws://{CFG.get('ws_host', '127.0.0.1')}:{CFG.get('ws_port', 6199)}/ws，"
            "token 与本配置的 access_token 保持一致。")
        log("  详见 docs/部署-Linux.md 的「QQ 接入端」一节。")
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
        # webbrowser 三平台通用；Windows 上 os.startfile 更"原生"所以先试它
        if os.name == "nt":
            os.startfile(url)
        else:
            import webbrowser
            if not webbrowser.open(url):
                raise RuntimeError("没有可用的浏览器")
    except Exception:
        log(f"控制台地址（请手动打开）：{url}")          # Windows 原生，直接用默认浏览器
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
