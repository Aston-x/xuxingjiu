"""识图策略 —— 「谁看图 / 缩到多宽 / 要不要先转述」的**唯一**判据来源。

重构前这四条判据散在 `Vision.cloud_ok` / `local_ok` / `first_answerer` / `max_side`
和 `_need_vision_relay` 里，任何一处走神就会复现「按云端分辨率缩图、实际本地模型收图」
的错配（源码 3340-3346 的注释专门记过这个坑）。现在统一收在这里。

⚠️ 两条纪律：

1. **「谁先答话」的判据必须与 Router 实际选路一致。** 父代码里 `Vision.first_answerer`
   写着「cloud 只看 api_key 不看 enable」，而 `Chat.answer` 在非中继模式下又看
   `cloud.enable` —— 两边对不上。这里按配置形态分别取一致的那一支：
   老配置（有 local/cloud 段）沿用 `Chat.answer` 的原始判据，新配置走端点的真实可用性。

2. **识图能力的口径是「严格」的**：端点必须显式 enable、且真的能收图、
   云端还要有密钥。不要偷换成 Router 内部那个还认 `require_enabled` 的可用性 ——
   那个是给「中继模式下 cloud 不看 cloud.enable」这条历史行为用的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .types import Endpoint

VISION_BACKENDS = ("auto", "cloud", "local", "off")


@dataclass(frozen=True)
class VisionPolicy:
    backend: str = "auto"
    usable: bool = False                   # 当前后端真的能看图吗（纯配置判断，不发请求）
    relay: bool = True                     # 要不要先把图转成文字再回答
    first_answerer: str = ""               # 第一个能答话的端点 tier（cloud/local/""）
    first_answerer_id: str = ""            # 同上，端点 id（日志/面板用）
    describe_endpoint_id: str = ""         # 谁来转述（原「本地识图」）
    image_max_side: int = 1568             # 该把图缩到多宽
    legacy: bool = True                    # 判据取自老配置的 local/cloud 段
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "usable": self.usable,
            "relay": self.relay,
            "first_answerer": self.first_answerer,
            "first_answerer_id": self.first_answerer_id,
            "describe_endpoint_id": self.describe_endpoint_id,
            "image_max_side": self.image_max_side,
            "legacy": self.legacy,
        }


def backend_of(cfg: dict) -> str:
    """当前配置选中的后端。vision.backend 缺失/脏值时回退看旧的 vision_relay。"""
    raw = str((cfg.get("vision") or {}).get("backend") or "").strip().lower()
    if raw in VISION_BACKENDS:
        return raw
    # 兼容老配置：vision_relay=true 是「图在本地看、话在云端说」的年代，
    # 落到 auto（云端优先、失败退本地）最接近当年想要的；false 就是只认本地
    return "auto" if cfg.get("vision_relay", True) else "local"


def is_legacy(cfg: dict) -> bool:
    """配置里还有顶层 local / cloud 段 → 判据走老口径。"""
    return bool(cfg.get("local") or cfg.get("cloud"))


def build(
    cfg: dict,
    *,
    endpoints: list[Endpoint],
    vision_capable: Callable[[Endpoint], bool],
    vision_of: Callable[[Endpoint], bool],
    answer_endpoint: Callable[[], Endpoint | None],
    describe_endpoint: Callable[[], Endpoint | None],
) -> VisionPolicy:
    """算出当前生效的识图策略。

    vision_capable —— 严格口径（显式 enable + 能收图 + 云端有密钥）
    vision_of      —— 该端点是否真的能收图（能力声明 + 端点覆盖）
    answer_endpoint / describe_endpoint —— Router 实际会选的端点（**必须与选路同源**）
    """
    total = bool(cfg.get("vision_enable", True))
    backend = backend_of(cfg)
    legacy = is_legacy(cfg)
    cloud_cfg = cfg.get("cloud") or {}
    local_cfg = cfg.get("local") or {}

    def cloud_ok() -> bool:
        if legacy:
            return bool(cloud_cfg.get("enable", True) and cloud_cfg.get("vision")
                        and cloud_cfg.get("api_key"))
        return any(vision_capable(e) for e in endpoints if e.tier == "cloud")

    def local_ok() -> bool:
        if legacy:
            return bool(local_cfg.get("enable") and local_cfg.get("vision"))
        return any(vision_capable(e) for e in endpoints if e.tier == "local")

    def first() -> tuple[str, str]:
        """第一个能答话的端点。老配置照抄 Chat.answer 的顺序判据。"""
        if legacy:
            relay_on = bool(cfg.get("vision_relay", True))
            if relay_on:
                if cloud_cfg.get("api_key"):
                    return "cloud", ""
                return ("local", "") if local_cfg.get("enable") else ("", "")
            if local_cfg.get("enable"):
                return "local", ""
            return ("cloud", "") if cloud_cfg.get("enable") else ("", "")
        ep = answer_endpoint()
        return (ep.tier, ep.id) if ep else ("", "")

    def answerer_can_see(tier: str) -> bool:
        if legacy:
            key = "cloud" if tier == "cloud" else "local"
            return bool((cloud_cfg if key == "cloud" else local_cfg).get("vision"))
        ep = answer_endpoint()
        return bool(ep and vision_of(ep))

    if backend == "off" or not total:
        usable = False
    elif backend == "cloud":
        usable = cloud_ok()
    elif backend == "local":
        usable = local_ok()
    else:
        usable = cloud_ok() or local_ok()

    who, who_id = first()

    # ── 要不要先转述 ──
    if backend == "off" or not total:
        relay = True
    elif backend == "local":
        relay = True                    # 图只许本地看，别把原图送去云端
    elif who in ("cloud", "local"):
        relay = not answerer_can_see(who)
    else:
        relay = True

    # ── 缩到多宽：谁消费这些图就给谁那一档 ──
    vision_cfg = cfg.get("vision") or {}
    cloud_side = int(vision_cfg.get("max_side_cloud", 1568))
    local_side = int(vision_cfg.get("max_side_local", 1024))

    if backend == "local":
        side = local_side
    elif backend == "cloud":
        side = cloud_side
    elif relay:
        target = describe_endpoint()
        side = int(target.image_max_side) if (target and target.image_max_side) else local_side
    else:
        target = answer_endpoint()
        if target and target.image_max_side:
            side = int(target.image_max_side)
        else:
            side = cloud_side if who == "cloud" else local_side

    desc = describe_endpoint()
    return VisionPolicy(
        backend=backend,
        usable=usable,
        relay=relay,
        first_answerer=who,
        first_answerer_id=who_id,
        describe_endpoint_id=desc.id if desc else "",
        image_max_side=side,
        legacy=legacy,
        detail={"vision_enable": total, "cloud_ok": cloud_ok(), "local_ok": local_ok()},
    )
