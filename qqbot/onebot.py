"""QQ 接入端（OneBot v11 实现）的抽象。

**边界很重要**：`bot.py` 只实现 OneBot v11 的**反向 WS 服务端**（监听 ws_port，
等接入端连进来），它跟 NapCat 无关 —— 换成任何 OneBot 实现都能跑。
所以这一层只负责「**启动编排 + 平台守卫 + 给人看的说明**」，不动消息链路。

为什么需要它：原来 `launcher.py` 把 NapCat 的四个文件名、注入命令、注册表查找
全写死了，等于把"接入端"绑死在一个 Windows 专有实现上。现在：

  · 每个接入端是一份 `AdapterSpec`（探测 / 启动 / 平台 / 说明 / 已知坑）；
  · `pick_adapter()` 决定用哪个：配置指定 > 目录探测 > None（用户自己外挂）；
  · 非 Windows 上 NapCat 会被明确拒绝并给出替代方案，而不是静默失败。

⚠️ 各接入端的**配置字段名**不一样（NapCat 的 `websocketClients`、Lagrange 的
   `ReverseWebSocket`……），但对接口径是统一的：**反向连到 ws://host:port/ws，
   token 与本项目 config.json 的 access_token 一致**。文档里给了映射表。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# 接入端只在**编排层**被区别对待；消息链路永远只认 OneBot v11
PROTOCOL = "OneBot v11（反向 WebSocket）"

DEFAULT_PORT = 6199


@dataclass(frozen=True)
class AdapterSpec:
    key: str
    label: str
    platforms: tuple[str, ...]           # ("nt",) / ("linux", "darwin") / 全平台
    detect: Callable[[Path], bool]
    docs: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    requires_manual_login: bool = True

    def supported_here(self) -> bool:
        return ("nt" if os.name == "nt" else sys.platform) in self.platforms


def _has(dir_: Path, *names: str) -> bool:
    return all((dir_ / n).exists() for n in names)


# ── 内置接入端 ────────────────────────────────────────────────────────

NAPCAT = AdapterSpec(
    key="napcat",
    label="NapCatQQ（Windows 注入式）",
    platforms=("nt",),
    detect=lambda d: _has(d, "napcat.mjs", "NapCatWinBootMain.exe", "NapCatWinBootHook.dll"),
    docs=("从上游 Release 下载 Windows 一键包解压到 NapCat/，"
          "config/ 下的模板复制成正式文件（文件名里的 <QQ号> 换成机器人号）"),
    notes=(
        "**绝不要 taskkill /IM QQ.exe** —— 你的日常号和机器人号是同一个 QQ.exe，"
        "按镜像名杀会把大号一起带走",
        "首次注入后要在 QQ 窗口登录机器人号（这一步没有自动化）",
        "它的 launcher-user.bat 依赖 reg.exe 读注册表；受限环境里改用本项目的启动器",
    ),
)

LAGRANGE = AdapterSpec(
    key="lagrange",
    label="Lagrange.Core / Lagrange.OneBot",
    platforms=("linux", "darwin", "nt"),
    detect=lambda d: _has(d, "Lagrange.OneBot") or _has(d, "Lagrange.OneBot.exe"),
    docs=("独立进程，不是注入式 —— Linux/macOS 上常用它。"
          "在 appsettings.json 里配 Message/ReverseWebSocket 指向本项目的 ws 地址"),
    notes=("配置字段名与 NapCat 不同（见 docs/部署-Linux.md 的映射表）",),
    requires_manual_login=False,
)

LLONEBOT = AdapterSpec(
    key="llonebot",
    label="LLOneBot（QQNT 插件）",
    platforms=("nt", "linux", "darwin"),
    detect=lambda d: (d / "LLOneBot").is_dir() or (d / "llonebot").is_dir(),
    docs="作为 LiteLoaderQQNT 插件安装，在其 WebUI 里填反向 WS 地址与 token",
    requires_manual_login=True,
)

ADAPTERS: dict[str, AdapterSpec] = {a.key: a for a in (NAPCAT, LAGRANGE, LLONEBOT)}

# 用户自己挂的接入端（我们不启动它，只提示怎么对接）
EXTERNAL = AdapterSpec(
    key="external",
    label="自备 OneBot v11 实现",
    platforms=("nt", "linux", "darwin"),
    detect=lambda d: False,
    docs="任何实现了 OneBot v11 的端都可以反向连过来",
)


def pick_adapter(cfg: dict, root: Path) -> AdapterSpec | None:
    """决定用哪个接入端。

    优先级：配置显式指定 > 按目录探测（本平台支持的优先）> None（外部自备）。
    配置里写 `onebot.adapter = "none"` 表示"我自己管接入端"，启动器完全不碰它。
    """
    want = str((cfg.get("onebot") or {}).get("adapter") or "auto").strip().lower()
    if want == "none":
        return None
    if want != "auto":
        spec = ADAPTERS.get(want)
        if spec is None:
            return None
        if not spec.supported_here():
            return None
        return spec
    for spec in ADAPTERS.values():
        if spec.supported_here() and spec.detect(root / _dir_name(spec)):
            return spec
    return None


def _dir_name(spec: AdapterSpec, default: str = "NapCat") -> str:
    return {"lagrange": "Lagrange", "llonebot": "LLOneBot"}.get(spec.key, default)


def connect_hint(cfg: dict) -> str:
    """给所有接入端共用的对接说明（文档里那份表的浓缩版）。"""
    host = str(cfg.get("ws_host", "127.0.0.1"))
    port = int(cfg.get("ws_port", DEFAULT_PORT))
    token = str(cfg.get("access_token") or "")
    tok = (token[:4] + "***") if len(token) > 4 else ("（空）" if not token else "***")
    return (f"接入端要反向连到 ws://{host}:{port}/ws，token 用 config.json 的 "
            f"access_token（当前 {tok}）；两边不一致会一直连不上。")


def credential_hint(spec: AdapterSpec, cfg: dict) -> str:
    lines = [f"接入端：{spec.label}", f"  {spec.docs}", f"  {connect_hint(cfg)}"]
    for n in spec.notes:
        lines.append(f"  ⚠️ {n}")
    return "\n".join(lines)
