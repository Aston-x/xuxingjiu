"""环境体检 —— 逐项告诉你「哪里没配好、为什么、怎么修」。

为什么要有它：这个项目有一堆**本机依赖**（本地模型、生图后端、空间桥、QQ 接入端）。
配置不够的人不会翻日志，只会觉得"机器人是个哑巴"。启动时的自检只覆盖 5 项，
这个工具把能查的 15 项全查一遍。

用法：
    python tools/doctor.py            # 人看（✅ / ⚠️ / ❌ + 建议）
    python tools/doctor.py --json     # 给脚本/CI 消费
    python tools/doctor.py --deep     # 额外探测本地生成后端（可能耗时）

退出码：0 全绿 / 1 有警告 / 2 有错误。

**安全**：输出一律脱敏，只报"环境变量没设"，绝不打印密钥的值。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import socket
import sys

# --json 那份是要给 CI 读的：json.dumps(..., ensure_ascii=False) 会写出中文，
# 而 Windows 上 stdout 被重定向到文件时用的是本地代码页（中文机是 GBK，
# 英文机是 cp1252 —— 后者连中文都编不出来）。不重设这里，`--json > x.json`
# 出来的就不是 UTF-8，下游按 UTF-8 读要么抛异常要么乱码。
# 三段式和 qqbot/bot.py 里那份一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

BASE = pathlib.Path(__file__).resolve().parents[1]          # qqbot/
ROOT = BASE.parent                                          # 仓库根
CONFIG = BASE / "config.json"
EXAMPLE = BASE / "config.example.json"

OK, WARN, ERR, OFF = "ok", "warn", "error", "off"
ICON = {OK: "✅", WARN: "⚠️ ", ERR: "❌", OFF: "⏸️ "}

results: list[dict] = []


def add(cid: str, title: str, level: str, detail: str, hint: str = "") -> None:
    results.append({"id": cid, "title": title, "level": level,
                    "detail": detail, "hint": hint})


def _redact(text: str) -> str:
    """上屏前统一过一遍：把像密钥的东西换成 ***。"""
    try:
        sys.path.insert(0, str(BASE))
        import console_server as cs  # noqa: PLC0415
        return cs.redact_text(text)
    except Exception:  # noqa: BLE001
        return text


def _venv_python() -> pathlib.Path:
    if os.name == "nt":
        return BASE / ".venv" / "Scripts" / "python.exe"
    for n in ("python", "python3"):
        p = BASE / ".venv" / "bin" / n
        if p.exists():
            return p
    return BASE / ".venv" / "bin" / "python"


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.6):
            return True
    except OSError:
        return False


# ── 各项检查 ──────────────────────────────────────────────────────────

def check_python() -> None:
    v = sys.version_info
    if v >= (3, 11):
        add("D1", "Python 版本", OK, f"{v.major}.{v.minor}.{v.micro}")
    else:
        add("D1", "Python 版本", ERR, f"{v.major}.{v.minor}.{v.micro} 太旧（要 ≥3.11）",
            "装 Python 3.11+ 后用新解释器重建 venv")


def check_venv() -> None:
    py = _venv_python()
    if py.exists():
        add("D2", "虚拟环境", OK, str(py))
    else:
        add("D2", "虚拟环境", ERR, f"没有 {py}",
            "跑 install.sh / install.bat，或手工 python -m venv .venv")


def check_deps() -> None:
    need = {"httpx": "httpx", "websockets": "websockets", "PIL": "pillow", "qrcode": "qrcode"}
    miss = [pkg for mod, pkg in need.items() if importlib.util.find_spec(mod) is None]
    if miss:
        add("D3", "依赖", ERR, "缺：" + "、".join(miss),
            f"{_venv_python()} -m pip install -r requirements.txt"
            "（注意别用系统 pip，否则装到全局去了）")
    else:
        add("D3", "依赖", OK, "httpx / websockets / pillow / qrcode 都在")


def load_config() -> dict | None:
    if not CONFIG.exists():
        add("D4", "配置文件", ERR, "没有 config.json",
            "复制 config.example.json 改名成 config.json 再改")
        return None
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("顶层不是对象")
    except Exception as exc:  # noqa: BLE001
        add("D4", "配置文件", ERR, f"config.json 读不了：{_redact(str(exc)[:120])}",
            "常见原因：末尾多了逗号、用了 // 注释（JSON 不允许）")
        return None
    add("D4", "配置文件", OK, "config.json 是合法 JSON")
    return cfg


def check_required(cfg: dict) -> None:
    bad: list[str] = []
    qq = cfg.get("bot_qq")
    if not isinstance(qq, int) or qq in (0, 10001):
        bad.append(f"bot_qq={qq!r}（还是示例值或空的）")
    admins = (cfg.get("admin") or {}).get("user_ids") or []
    if not admins or 10002 in admins:
        bad.append("admin.user_ids 是空的或还是示例值 10002")
    if not (cfg.get("access_token") or "").strip():
        bad.append("access_token 是空的（接入端要用它握手）")
    if bad:
        add("D5", "必填项", ERR, "；".join(bad), "按 config.example.json 里的注释逐项填")
    else:
        add("D5", "必填项", OK, f"bot_qq={qq}，管理员 {len(admins)} 人，access_token 已设")


def check_adapter_token(cfg: dict) -> None:
    """接入端的 token 与 config 里的一致吗 —— 这是"连不上"的头号原因。"""
    try:
        sys.path.insert(0, str(BASE))
        import onebot  # noqa: PLC0415
        spec = onebot.pick_adapter(cfg, ROOT)
    except Exception as exc:  # noqa: BLE001
        add("D6", "接入端令牌", WARN, f"读不出接入端配置：{_redact(str(exc)[:100])}")
        return
    if spec is None:
        add("D6", "接入端令牌", WARN,
            "没探测到接入端（用 Docker / Lagrange 时这是正常的）",
            "自己确认它的 token 与 config.json 的 access_token 一致")
        return
    if spec.key != "napcat":
        add("D6", "接入端令牌", WARN, f"{spec.label} 的配置不在这里，无法自动核对",
            "手工比一下 access_token")
        return
    import glob
    files = sorted(glob.glob(str(ROOT / "NapCat" / "config" / "onebot11_*.json")))
    if not files:
        add("D6", "接入端令牌", WARN, "NapCat/config 下没有 onebot11_*.json",
            "把 onebot11.example.json 复制成 onebot11_<机器人QQ>.json")
        return
    want = str(cfg.get("access_token") or "")
    mism = []
    for f in files:
        try:
            got = (((json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
                     .get("network") or {}).get("websocketClients") or [{}])[0].get("token") or "")
        except Exception:  # noqa: BLE001
            continue
        if got != want:
            mism.append(pathlib.Path(f).name)
    if mism:
        add("D6", "接入端令牌", WARN,
            "token 对不上的文件：" + "、".join(mism),
            "两处必须完全一致，否则日志里只会说「鉴权失败」")
    else:
        add("D6", "接入端令牌", OK, "NapCat 的 token 与 config.json 一致")


def check_config_keys(cfg: dict) -> None:
    try:
        sys.path.insert(0, str(BASE / "tools"))
        import ensure_config  # noqa: PLC0415
        miss = [p for p in ensure_config.DEFAULTS
                if ensure_config.get_path(cfg, p)[1] is False]
    except Exception as exc:  # noqa: BLE001
        add("D7", "配置键完整", WARN, f"没法比对：{_redact(str(exc)[:80])}")
        return
    if miss:
        add("D7", "配置键完整", WARN, f"缺 {len(miss)} 项，例如 {miss[:3]}",
            f"{_venv_python()} tools/ensure_config.py（幂等，只补不改）")
    else:
        add("D7", "配置键完整", OK, "该有的键都在")


def check_models(cfg: dict) -> None:
    """能不能答话 —— 这是最要紧的一项。"""
    try:
        sys.path.insert(0, str(BASE))
        import bot  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        add("D8", "对话模型", ERR, f"import bot 失败：{_redact(str(exc)[:150])}",
            "先修上面几项；这一步会真的加载配置")
        return
    eps = bot.ROUTER.endpoints
    if not eps:
        add("D8", "对话模型", ERR, "一个端点都没配",
            "在 providers.endpoints 里配一个，或填顶层 local / cloud 段")
    else:
        usable = [e.id for e in eps if bot.ROUTER.usable(e)]
        if usable:
            add("D8", "对话模型", OK, "可用：" + "、".join(usable))
        else:
            why = "、".join(f"{e.id}({bot.ROUTER.skip_reason(e)})" for e in eps)
            add("D8", "对话模型", ERR, f"没有可用端点：{why}",
                "云端要配密钥；本地要先把模型服务开起来")
    # 环境变量有没有设（**只报有没有，不打印值**）
    missing = [e.api_key_env for e in eps if e.api_key_env and not os.environ.get(e.api_key_env)]
    if missing:
        add("D8b", "密钥环境变量", WARN, "没设：" + "、".join(sorted(set(missing))),
            f"set {missing[0]}=xxx（Windows）或 export {missing[0]}=xxx（POSIX）")
    else:
        add("D8b", "密钥环境变量", OK, "用到的都没问题")


def check_ports(cfg: dict) -> None:
    ws = int(cfg.get("ws_port", 6199))
    cport = int((cfg.get("console") or {}).get("port", 6200))
    info = []
    for name, p in (("ws", ws), ("控制台", cport), ("空间桥", 5700)):
        info.append(f"{name} {p} {'在跑' if _port_open(p) else '空闲'}")
    # 端口被占是正常的（bot 正在跑）；这里只提示，不算错误
    add("D9", "端口占用", OK if _port_open(ws) else WARN, "；".join(info),
        "" if _port_open(ws) else "bot 还没在跑（要启动就双击 启动.pyw / bash deploy/start-all.sh）")


def check_imagegen(cfg: dict, deep: bool) -> None:
    try:
        sys.path.insert(0, str(BASE))
        import bot  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        add("D10", "生图后端", WARN, "读不到（bot 加载失败，见 D8）")
        return
    if not bot.SDGEN.enable:
        add("D10", "生图后端", OFF, "生图关着（sd.enable / imagegen.enable=false）")
        return
    ok, reason, why = bot.SDGEN.check_ready("private")
    if ok:
        eps = [e.id for e in bot.IMAGE_ROUTER.endpoints if bot.IMAGE_ROUTER.usable(e)]
        add("D10", "生图后端", OK, "可用：" + "、".join(eps))
        if deep and any(e.tier == "local" for e in bot.IMAGE_ROUTER.endpoints):
            add("D10b", "生图探测", WARN, "已跳过（--deep 的在线探测没实装，避免误触发计费）")
    else:
        add("D10", "生图后端", WARN if reason in ("quota", "empty-intent") else ERR,
            f"不可用（{reason}）：{why}",
            "本地后端要先开 SD WebUI / ComfyUI；云端后端要配密钥")


def check_bridge() -> None:
    d = ROOT / "qzone-bridge"
    if not d.is_dir():
        add("D11", "空间桥接", OFF, "没有 qzone-bridge 目录（可选组件，不影响群聊）")
        return
    if not (d / "node_modules").is_dir():
        add("D11", "空间桥接", WARN, "存在但没装依赖",
            f"cd {d} && npm ci")
        return
    if (d / "dist" / "main.js").exists():
        add("D11", "空间桥接", OK, "dist/main.js 就绪（启动器优先跑它）")
    else:
        add("D11", "空间桥接", WARN, "没有 dist/main.js，得靠 tsx 跑源码",
            f"cd {d} && npm run build")


def check_adapter(cfg: dict) -> None:
    try:
        sys.path.insert(0, str(BASE))
        import onebot  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        add("D12", "QQ 接入端", WARN, f"读不到 onebot 模块：{_redact(str(exc)[:80])}")
        return
    spec = onebot.pick_adapter(cfg, ROOT)
    if spec is None:
        add("D12", "QQ 接入端", WARN, "没探测到（外部自备 / Docker 时正常）",
            onebot.connect_hint(cfg))
    else:
        add("D12", "QQ 接入端", OK if spec.supported_here() else WARN,
            f"{spec.label}；" + onebot.connect_hint(cfg))


def check_write() -> None:
    probe = BASE / "state" / ".doctor-probe"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("D13", "写权限", OK, f"{BASE / 'state'} 可写")
    except OSError as exc:
        add("D13", "写权限", ERR, f"写不了 {probe.parent}：{exc}",
            "换个目录放项目，或修一下目录权限")


def check_platform() -> None:
    if os.name == "nt":
        add("D14", "平台适配", OK, "Windows：注入式接入端 + .pyw 静默启动")
        return
    notes = []
    try:
        import fcntl  # noqa: F401,PLC0415
    except ImportError:
        notes.append("没有 fcntl（单实例锁会降级为放行）")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        notes.append("没有 $DISPLAY（无头环境，扫码类操作需要 headless）")
    add("D14", "平台适配", WARN if notes else OK,
        f"{sys.platform}；" + ("；".join(notes) if notes else "flock 与图形环境都正常"))


def check_psutil() -> None:
    if importlib.util.find_spec("psutil") is None:
        add("D15", "psutil（可选）", WARN, "没装 —— 进程识别会退回平台命令，精度差一些",
            "pip install psutil（可选，不装也能跑）")
    else:
        add("D15", "psutil（可选）", OK, "已装，进程/端口识别更准")


# ── 主流程 ────────────────────────────────────────────────────────────

def run(deep: bool = False) -> list[dict]:
    results.clear()
    check_python()
    check_venv()
    check_deps()
    cfg = load_config()
    if cfg is not None:
        check_required(cfg)
        check_adapter_token(cfg)
        check_config_keys(cfg)
        check_models(cfg)
        check_ports(cfg)
        check_imagegen(cfg, deep)
        check_adapter(cfg)
    check_bridge()
    check_write()
    check_platform()
    check_psutil()
    return list(results)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="许杏玖 · 环境体检")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给脚本/CI 用）")
    ap.add_argument("--deep", action="store_true", help="额外探测本地生成后端（可能耗时）")
    args = ap.parse_args(argv)

    items = run(deep=args.deep)
    n_err = sum(1 for x in items if x["level"] == ERR)
    n_warn = sum(1 for x in items if x["level"] == WARN)

    if args.json:
        print(json.dumps({
            "ok": n_err == 0,
            "platform": sys.platform,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "checks": items,
            "summary": {"error": n_err, "warn": n_warn,
                        "ok": sum(1 for x in items if x["level"] == OK)},
        }, ensure_ascii=False, indent=2))
    else:
        print("\n许杏玖 · 环境体检")
        print("-" * 56)
        for x in items:
            print(f"{ICON.get(x['level'], '•')} [{x['id']}] {x['title']}：{_redact(x['detail'])}")
            if x["hint"] and x["level"] in (WARN, ERR):
                print(f"      → {x['hint']}")
        print("-" * 56)
        if n_err:
            print(f"有 {n_err} 项**必须**修，{n_warn} 项建议关注。")
        elif n_warn:
            print(f"没有错误，{n_warn} 项建议关注（不影响跑起来）。")
        else:
            print("全绿，可以启动了。")

    return 2 if n_err else (1 if n_warn else 0)


if __name__ == "__main__":
    sys.exit(main())
