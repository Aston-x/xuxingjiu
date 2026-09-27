"""QQ 机器人最小服务：NapCat(OneBot v11 反向 WS) -> 本地模型为主，失败降级 DeepSeek。

链路：NapCat --反向WS--> 本服务(6199) --HTTP--> LM Studio(1234) / DeepSeek API
"""

import asyncio
import atexit
import base64
import hashlib
import html as ihtml
import json
import logging
import math
import os
import random
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

# 模型接入层：本地 / 云端 / 任意第三方都走它（见 qqbot/providers/README.md）
from providers import ConcurrencyGate, Endpoint, Provider, build_router

# 生图层：把「图弄回来」从 SD 类里抽出来（本地 4 家 + 云端 5 家，见 qqbot/imagegen/README.md）
from imagegen import (
    GenOutcome,
    GenRequest,
    build_image_router,
)
from imagegen.types import (
    REASON_DISABLED,
    REASON_EMPTY,
    REASON_FAILED,
    REASON_QUOTA,
    REASON_TIMEOUT,
)

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"\nconfig.json 格式错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}\n"
            "常见原因：末尾多了逗号，或者用了 // 注释 —— JSON 不允许注释。\n"
            "要加说明请用 \"_说明\": \"...\" 这种合法字段。\n"
        )
        # 抛 ValueError 而不是 SystemExit：SystemExit 继承自 BaseException，
        # HTTP 线程里的 except Exception 拦不住，会连带掀掉整个进程。
        raise ValueError(
            f"config.json 格式错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
        ) from exc


try:
    CFG = load_config()
except ValueError as exc:
    # 命令行启动时友好退出；程序化调用方请自行捕获 ValueError
    # （用 pythonw 无控制台启动时 stderr 为 None，所以要判一下）
    if sys.stderr is not None:
        sys.stderr.write(f"\n{exc}\n请修正 config.json 后重新启动。\n")
    raise SystemExit(1)

logger = logging.getLogger("qqbot")
logger.setLevel(getattr(logging, CFG.get("log_level", "INFO")))
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh = RotatingFileHandler(BASE / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)
# 只有存在 stdout 时才加流处理器：用 pythonw.exe 无控制台启动时 sys.stdout 是 None，
# 硬加会得到一个永远写不出去的 handler。日志本身已落 bot.log，控制台面板能看。
if sys.stdout is not None:
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    logger.addHandler(_sh)

# ── 人设层 ────────────────────────────────────────────────────────────
# 人设字段（persona / world / self_image / style_* / 生图形象串…）统一从角色卡取，
# 见 qqbot/persona.py。config.json 里的老键仍然**优先**（迁移期的桥），所以老安装
# 升级上来行为一字不变；按会话/按群换人设走 persona_bindings.json。
# 必须放在 CFG 之后：卡库要读 CFG 判 lock、做老键覆盖。
from persona import (  # noqa: E402
    ART_FIELDS,
    PROMPT_FIELDS,
    PROMPT_LIST_FIELDS,
    PersonaLibrary,
)

# 迁移期那些"和角色卡重叠"的老键路径。退位时按它删 —— 全删，不是只删和卡不一样的：
# 留着值相同的那几个，等哪天卡改了它们就会跳出来盖住新值，那是最难查的一类问题。
PERSONA_LEGACY_PATHS = list(PROMPT_FIELDS + PROMPT_LIST_FIELDS) + \
    [f"sd.{k}" for k in ART_FIELDS]

PERSONA = PersonaLibrary(BASE, CFG)


def _warn_persona_legacy() -> None:
    """config.json 里的老键还在盖角色卡时，启动说一声。

    不说的话，换卡只换提示词、不换长相，看起来像"这功能没生效"。
    只报和卡**不一样**的键 —— 全新安装两边逐字相同，不该天天响。
    """
    conf = PERSONA.conflicts()
    if not conf:
        return
    logger.warning(
        "config.json 里这些老键正在覆盖角色卡：%s。"
        "想让角色卡完全说了算，把这几项从 config.json 删掉即可"
        "（留着也不会出别的问题，只是卡改不动它们）。",
        "、".join(k for k, _, _ in conf))


_warn_persona_legacy()


def session_key(is_group: bool, group_id=None, user_id=None) -> str:
    """会话键：群聊 g<群号>，私聊 p<QQ号>。

    ★ 必须和 handle_message 里原来那行算得一模一样（人设绑定、冷却、上下文都按它
    分桶）。统一从这里出，免得哪天只改了一处 —— 那会变成"提示词按 A 算、冷却按 B 算"，
    这种错极难查。拿不到 id 时返回空串，下游会退回默认卡。
    """
    if is_group:
        return f"g{group_id}" if group_id not in (None, "") else ""
    return f"p{user_id}" if user_id not in (None, "") else ""


PERSONA_HELP = (
    "/人设              看这个会话用的哪张卡、卡库里都有什么\n"
    "/人设 换 <卡id>     这个会话换成那张卡（群聊里就是给这个群换）\n"
    "/人设 默认          这个会话改回默认卡\n"
    "/人设 全局 <卡id>   改默认卡（没单独设过的地方都跟着变）\n"
    "/人设 退位          把 config.json 里还压着角色卡的老键删掉，改成以卡为准"
)


async def handle_persona_command(text: str, is_group: bool, group_id, user_id,
                                 say) -> bool:
    """人设管理命令（`/人设 …`）。返回 True = 这条被吃掉了，别再走正常回复。

    ⚠️ 管理员专用：换人设等于换一个人跟你说话，不该谁都能改。
    群聊里绑的是**这个群**（会话键 g<群号>），私聊里绑的是这段私聊 —— 同一个机制，
    这就是"以后进多群可以按群设人设"的入口。
    """
    raw = (text or "").strip()
    if not raw.startswith("/人设"):
        return False
    if not allowed("persona", user_id):
        logger.info("非管理员 %s 想改人设，静默忽略", user_id)
        return True                     # 装糊涂：不执行也不解释（和 /clear 一个态度）
    parts = raw[len("/人设"):].strip().split()
    sub = parts[0] if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    skey = session_key(is_group, group_id, user_id)

    if sub in ("", "列表", "list"):
        cur = PERSONA.resolve(skey)
        where = "单独绑过" if PERSONA.binding_of(skey) else "默认卡"
        lines = [f"这个会话用的是 {cur or '（一张卡都没有）'}（{where}）"]
        if PERSONA.cards:
            lines.append("卡库里：")
            for cid in sorted(PERSONA.cards):
                tags = [t for t, hit in (("默认", cid == PERSONA.default_id()),
                                         ("当前", cid == cur)) if hit]
                name = PERSONA.cards[cid].get("name") or cid
                lines.append(f"  {cid} —— {name}" + (f"（{'、'.join(tags)}）" if tags else ""))
        lines += ["", PERSONA_HELP]
        await say("\n".join(lines))
        return True

    if sub in ("换", "换人设", "set"):
        if not arg:
            await say("要换成哪张卡？发 /人设 看列表。")
            return True
        ok, msg = PERSONA.bind(skey, arg)
        await say(("换好了：" if ok else "没换成：") + msg
                  + ("\n下一条回复就按新卡说话。" if ok else ""))
        return True

    if sub in ("默认", "回默认"):
        if not PERSONA.binding_of(skey):
            await say("这个会话本来就是默认卡。")
            return True
        ok, msg = PERSONA.unbind(skey)
        await say(msg if ok else f"没改成：{msg}")
        return True

    if sub in ("全局", "默认卡"):
        if not arg:
            await say("要把哪张卡设成默认？发 /人设 看列表。")
            return True
        ok, msg = PERSONA.set_default(arg)
        await say(msg if ok else f"没改成：{msg}")
        return True

    if sub in ("退位", "以卡为准"):
        conflicted = {k for k, _, _ in PERSONA.conflicts()}
        gone = drop_config(PERSONA_LEGACY_PATHS)
        if not gone:
            await say("config.json 里没有跟角色卡重叠的老键，本来就是以卡为准。")
            return True
        out = "已从 config.json 删掉这些老键，从现在起以角色卡为准：\n" + "、".join(gone)
        if conflicted:
            out += ("\n其中 " + "、".join(sorted(conflicted)) +
                    " 原本和卡里的值不一样，现在以卡为准 —— 想找回旧值看 git diff。")
        await say(out)
        return True

    await say("不认识的用法。\n" + PERSONA_HELP)
    return True


CQ_PATTERN = re.compile(r"\[CQ:[^\]]+\]")


CALL_LOG: dict[str, deque] = {}

# 控制台用：进程起始时间，以及 bot 事件循环的引用
# （LOOP 在 main() 内、拿到 running loop 之后才填，故初始为 None）
START_TS: float = time.time()
LOOP: "asyncio.AbstractEventLoop | None" = None
LAST_SPEAKER: dict[str, int] = {}


# 配置里没有 admin 段时的兜底：**空名单**。
# 注意空名单的语义是「不限制」（见 is_admin），也就是所有人都算管理员 ——
# 所以 config.json 里应当显式写 admin.user_ids；没写的话启动时会打一条醒目警告
# （见 _warn_admin_unset）。
_DEFAULT_ADMIN = {"user_ids": []}


def is_admin(user_id) -> bool:
    """管理员名单里的人才能触发受限功能。"""
    ids = CFG.get("admin", _DEFAULT_ADMIN).get("user_ids") or []
    if not ids:
        return True  # 名单为空 = 不限制
    return str(user_id) in {str(i) for i in ids}


def _warn_admin_unset() -> None:
    """admin.user_ids 为空时喊一嗓子。

    空名单 = 所有人都能触发 [禁言:]/[说说:] 这些受限功能。这是个**静默的危险默认值**，
    而这个开源版的兜底名单故意是空的（不再硬编码号主 QQ），所以必须在启动日志里说清楚，
    否则用户会以为限制生效了，实际上谁都能管。
    """
    ids = (CFG.get("admin") or {}).get("user_ids") or []
    if ids:
        return
    logger.warning("=" * 62)
    logger.warning("admin.user_ids 是空的 —— **所有用户都会被当成管理员**。")
    logger.warning('  想限制的话，在 config.json 里填上你自己的 QQ 号，例如')
    logger.warning('      "admin": { "user_ids": [123456789] }')
    logger.warning("  不打算用管理限制的话，忽略这条即可。")
    logger.warning("=" * 62)


def restricted(feature: str) -> bool:
    """这个功能是否被设为"仅管理员"。"""
    return bool((CFG.get("admin", _DEFAULT_ADMIN).get("restrict") or {}).get(feature, False))


def allowed(feature: str, user_id) -> bool:
    return (not restricted(feature)) or is_admin(user_id)


def feat_ok(feature: str, speaker_id) -> bool:
    """speaker_id 为 None 表示"她自己想做"，不受用户权限限制。"""
    return True if speaker_id is None else allowed(feature, speaker_id)


RECENT_SPEAKERS: dict[str, deque] = {}   # key=群号，值=(时间戳, QQ号)


def note_speaker_activity(group_id, user_id) -> None:
    """记下"谁在这个群说过话"，用来判断是否有多人在同时聊。"""
    RECENT_SPEAKERS.setdefault(f"g{group_id}", deque(maxlen=80)).append((time.time(), user_id))


def active_speaker_count(group_id, window: float) -> int:
    """最近 window 秒内有几个人发过言（去重）。"""
    dq = RECENT_SPEAKERS.get(f"g{group_id}")
    if not dq:
        return 0
    cutoff = time.time() - window
    return len({uid for ts, uid in dq if ts >= cutoff})


# 「他最后一次跟我说话」——戳一戳要用它判断这一下是不是"在不在？看我一眼"
LAST_TALK_AT: dict[str, float] = {}


def note_talker(user_id) -> None:
    """记下他刚跟我说过话（群聊私聊都算）。"""
    if user_id:
        LAST_TALK_AT[str(user_id)] = time.time()


def is_recent_talker(user_id, seconds: float) -> bool:
    t = LAST_TALK_AT.get(str(user_id))
    return bool(t) and (time.time() - t) <= seconds


def read_json_dict(path: Path, what: str) -> dict:
    """读一个 JSON 状态文件。文件不存在或内容坏了都返回 {}，只告警不抛。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        logger.warning("%s 内容不是对象，忽略", what)
    except Exception as exc:
        logger.warning("%s 读取失败：%s", what, exc)
    return {}


def atomic_write_json(path: Path, obj: dict, retries: int = 5) -> None:
    """原子写 JSON：同目录临时文件 -> fsync -> os.replace。

    必须用 os.replace 而非 os.rename：Windows 上 os.rename 覆盖已存在文件会抛错，
    而 os.replace 走 MoveFileExW(MOVEFILE_REPLACE_EXISTING)，同卷下是原子的。
    临时文件必须落在目标同目录（同卷）才能保证原子性，不能放系统 temp。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # 目标被独占打开（编辑器 / 杀软扫描 / 同步盘），退避重试
                if attempt == retries - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def write_json_dict(path: Path, obj: dict, what: str) -> None:
    """把状态写回 JSON 文件（原子写：tmp + os.replace），失败只告警。"""
    try:
        atomic_write_json(path, obj)
    except Exception as exc:
        logger.warning("%s 写入失败：%s", what, exc)


LOCK_PATH = BASE / "state" / "bot.lock"
_lock_fh = None


def acquire_lock() -> None:
    """单实例锁：用 Windows 原生文件锁占住 state/bot.lock。

    为什么不用「写 PID + os.kill(pid, 0) 探活」：2026-09-26 在本机实测，
    os.kill(pid, 0) 对**刚死掉的 PID 不抛异常**，会被误判成「还活着」，
    于是进程崩溃后陈旧锁永远清不掉、之后再也起不来。
    文件锁则由操作系统在进程结束时自动释放 —— 陈旧锁自愈，不需要猜。

    退出码 2 = 已有实例在跑（守护脚本据此区分「不该重启」与「崩溃需拉起」）。
    """
    global _lock_fh
    import filelock  # noqa: PLC0415  同目录模块
    # 跨平台：Windows 走 msvcrt，POSIX 走 flock（以前非 Windows 是**静默跳过**的，
    # 等于没有保护，两个 bot 一起写 memory.json 会把数据写坏）
    fh, holder = filelock.acquire(LOCK_PATH)
    if fh is None:
        logger.error("已有实例在运行（%s 被 PID %s 占用）", LOCK_PATH, holder or "?")
        logger.error("先停掉那个实例（双击 停止.pyw），确认没有别的窗口在跑再启动。")
        sys.exit(2)
    _lock_fh = fh
    atexit.register(release_lock)


def release_lock() -> None:
    """释放锁并删掉锁文件（只在确实持有句柄时动手）。"""
    global _lock_fh
    fh, _lock_fh = _lock_fh, None
    if fh is None:
        return
    try:
        import filelock  # noqa: PLC0415
        filelock.release(fh)
    except Exception:  # noqa: BLE001
        pass
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def nickname_to_uin(nick: str) -> int | None:
    """从群里见过的昵称反查 QQ 号（好友动态翻不到时，用它去对方空间主页拉）。"""
    nick = (nick or "").strip()
    if not nick:
        return None
    for members in SPEAKERS.values():
        if nick in members:
            return members[nick]
    for members in SPEAKERS.values():
        for name, uid in members.items():
            if nick in name or name in nick:
                return uid
    return None


def allow_call(key: str) -> bool:
    """每群每分钟的调用上限，防刷屏把 token 烧穿。"""
    limit = int(CFG.get("rate_limit", {}).get("max_calls_per_minute", 0))
    if limit <= 0:
        return True
    d = CALL_LOG.setdefault(key, deque())
    now = time.time()
    while d and now - d[0] > 60:
        d.popleft()
    if len(d) >= limit:
        return False
    d.append(now)
    return True


def trim_context(system: dict, history, content) -> list:
    """上下文按字符预算从后往前保留，超了就丢最早的。"""
    budget = int(CFG.get("context", {}).get("max_chars", 0))
    if budget <= 0:
        msgs = [system, *history]
    else:
        kept: list[dict] = []
        total = 0
        for m in reversed(list(history)):
            c = m.get("content")
            size = len(c) if isinstance(c, str) else 300
            if total + size > budget:
                break
            kept.append(m)
            total += size
        kept.reverse()
        if len(kept) < len(history):
            logger.info("上下文超预算，截断 %d -> %d 条", len(history), len(kept))
        msgs = [system, *kept]
    if content is not None:
        msgs.append({"role": "user", "content": content})
    return msgs


async def _safe(coro) -> None:
    """后台任务的兜底：出错只记日志，绝不让它冒泡把事件循环搞崩。"""
    try:
        await coro
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.exception("后台任务出错：%s", exc)


class OneBot:
    """通过反向 WS 连接调用 OneBot v11 API（NapCat 没开 HTTP，只能走这条）。"""

    def __init__(self) -> None:
        self.ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self.self_id = int(CFG.get("bot_qq") or 0)

    def bind(self, ws) -> None:
        self.ws = ws

    def resolve(self, data: dict) -> None:
        echo = data.get("echo")
        fut = self._pending.pop(echo, None)
        if fut and not fut.done():
            fut.set_result(data)

    async def call(self, action: str, params: dict, timeout: float = 20.0) -> dict:
        if self.ws is None:
            raise RuntimeError("OneBot 连接未就绪")
        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[echo] = fut
        await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
        try:
            r = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(echo, None)
        if isinstance(r, dict) and r.get("status") == "failed":
            raise RuntimeError(
                f"retcode={r.get('retcode')} {r.get('wording') or r.get('message') or ''}".strip()
            )
        return r


OB = OneBot()


# ══════════════════════ 模型接入层（providers） ══════════════════════
#
# 端点在**导入期**就建好：老配置由 local/cloud 推导，新配置读 providers 段，
# 两种都能跑（见 providers/registry.py 的 _legacy_chains）。任何装载失败只记日志。

def _adhoc_endpoint(base_url: str) -> Endpoint:
    """给非 provider 的 HTTP 链路（天气 / 搜索 / B 站 / 生图 / 探针）造个临时端点。

    只为复用 `providers.transport` 的「本机不走代理」判定 —— 这个判定只能有一份，
    散出去就一定会有一处忘了绕过代理，然后卡在 127.0.0.1 上查半天。
    """
    return Endpoint(id="_adhoc", provider="openai", base_url=base_url, model="-",
                    tier="cloud")


def _endpoint_from_dict(d: dict, timeout: float = 90.0) -> Endpoint:
    """把老式的「模型端点 dict」（config.local / config.cloud 那种）转成 Endpoint。

    `Chat._post` 的兼容入口用它 —— 老配置里的字段名与 Endpoint 不完全一样，
    这里做一次映射，别让两套命名到处渗。
    """
    base = str(d.get("base_url") or "")
    provider = str(d.get("provider") or ("lmstudio" if "1234" in base else "openai"))
    params = {}
    if d.get("thinking"):
        params["thinking"] = {"type": d.get("thinking")}
    return Endpoint(
        id=str(d.get("id") or "_legacy"),
        provider=provider,
        base_url=base,
        model=str(d.get("model") or ""),
        api_key=str(d.get("api_key") or ""),
        api_key_env=str(d.get("api_key_env") or ""),
        tier="local" if ("127.0.0.1" in base or "localhost" in base) else "cloud",
        timeout=float(d.get("timeout_seconds", timeout) or timeout),
        max_tokens=int(d.get("max_tokens", 1024) or 1024),
        vision=bool(d.get("vision")) if d.get("vision") is not None else None,
        params=params,
    )


def _usage_sink(usage: dict) -> None:
    """Router 每完成一次模型调用就回调一次（字段已在 providers 里归一化）。"""
    tokens_add({"in": usage.get("in", 0), "out": usage.get("out", 0),
                "hit": usage.get("hit", 0), "miss": usage.get("miss", 0),
                "calls": int(usage.get("calls", 1) or 1)})
    total = TOKENS["hit"] + TOKENS["miss"]
    logger.info("用量 入%d 出%d｜缓存命中%d 未命中%d｜累计 入%d 出%d 命中率%.0f%%",
                usage.get("in", 0), usage.get("out", 0),
                usage.get("hit", 0), usage.get("miss", 0),
                TOKENS["in"], TOKENS["out"],
                100.0 * TOKENS["hit"] / total if total else 0.0)


ROUTER = build_router(CFG, base_dir=BASE, on_usage=_usage_sink, logger_=logger)

# 生图路由：端点/回退链/云端每日上限/负缓存 都在它里面。
# 旧 sd.* 会被自动迁移成一个本地端点（provider=sd_webui），所以老配置不改也能画。
IMAGE_ROUTER = build_image_router(CFG, base_dir=BASE, logger_=logger,
                                  state_path=BASE / "state" / "imagegen_state.json")


class Chat:
    """对话门面：上下文 / 防复读在这里，**模型调用一律交给 providers.Router**。

    重构前这个类自己拼 URL、拼 body、定代理、管并发、算用量；现在那些都搬进
    `qqbot/providers/`，这里只剩「跟这次对话有关的记忆」和两个转调。
    """

    def __init__(self) -> None:
        self.sessions: dict[str, deque] = {}
        self.recent_replies: dict[str, deque] = {}
        self.last_reply_at: dict[str, float] = {}

    @property
    def gate(self) -> ConcurrencyGate:
        """按端点限流的闸门 —— 原来那个只盯 local 的 `local_sem` 的泛化版。"""
        return ROUTER.gate

    def client_for(self, base_url: str) -> httpx.AsyncClient:
        """本机端点强制不走代理（环境变量里的 HTTP_PROXY 会打死 127.0.0.1）。

        模型链路现在走 Router，但天气 / 搜索 / B 站 / 生图这些非 provider 的 HTTP
        还在用这里，所以保留。判定逻辑已搬进 `providers.transport`。
        """
        return ROUTER.http.client_for(_adhoc_endpoint(base_url))

    def history(self, key: str) -> deque:
        if key not in self.sessions:
            self.sessions[key] = deque(maxlen=int(CFG.get("max_context_turns", 16)) * 2)
        return self.sessions[key]

    @staticmethod
    def _check_messages(messages: list) -> list:
        """拦掉结构不对的消息项。

        典型错误是把 build_system() 返回的字符串直接当消息塞进列表 —— 服务端会以 422
        拒收，报错只说 "invalid type: string"，不打印响应体根本查不出是哪条调用写的。
        """
        ok: list = []
        for i, m in enumerate(messages):
            if not isinstance(m, dict) or not isinstance(m.get("content"), (str, list)):
                logger.error("消息结构非法，已丢弃 messages[%d]：%r", i, m)
                continue
            ok.append(m)
        if not ok:
            raise ValueError("messages 里没有一条合法消息，放弃本次调用")
        return ok

    # ── 兼容入口 ──
    # 真实实现已搬进 providers/，但 `_post` / `_fit_vision` 这两个签名被老代码
    # 和回归测试的桩依赖着，所以保留成薄转调。
    #   _note_usage   -> providers.usage.normalize_usage + 本文件的 _usage_sink
    #   并发闸        -> ConcurrencyGate（见上面的 gate 属性）

    @staticmethod
    def _fit_vision(messages: list, ep: dict) -> list:
        """按接口能力处理图片段（兼容入口）。

        真实实现在 `providers/base.py` 的 `Provider.prepare_messages` /
        `vision_placeholder` —— 那两套占位措辞就是从这里原样搬过去的，
        改动前请先看 test_regression.py 【31】段的逐字断言。
        """
        target = _endpoint_from_dict(ep)
        can_see = VISION.usable()
        prov = ROUTER.registry.get(target.provider)
        if prov is None:
            # 适配器都没注册（理论上不会发生）：至少把占位措辞保住
            if target.vision:
                return messages
            return Provider.vision_placeholder(messages, target, backend_can_see=can_see)
        return prov.prepare_messages(messages, target, prov.capability(target),
                                     backend_can_see=can_see)

    async def _post(self, ep: dict, messages: list, timeout: float,
                    extra: dict | None = None) -> str:
        """按「端点配置 dict」发一次对话请求，返回纯文本。"""
        target = _endpoint_from_dict(ep, timeout)
        try:
            prepared = self._check_messages(messages)
        except ValueError as exc:
            logger.error("消息全非法，放弃本次调用：%s", exc)
            return ""
        result = await ROUTER.call(target, prepared, extra=extra)
        return result.text

    async def avoid_repeat(self, key: str, messages: list, reply: str) -> str:
        """和最近说过的话撞车就让她换个说法，最多重来两次。"""
        rec = self.recent_replies.setdefault(key, deque(maxlen=10))
        used = {dedupe_key(x) for x in rec}
        if not dedupe_key(reply) or dedupe_key(reply) not in used:
            rec.append(reply)
            return reply

        logger.info("回复与最近内容重复，要求换个说法")
        for _ in range(2):
            said = " / ".join(list(rec)[-3:])
            nudge = f"（你刚才说过几乎一模一样的话。你最近说过：{said}。换句新的，别复读自己。）" if said \
                else "（你刚才说过一模一样的话，换种说法再答一次，别复读自己。）"
            messages = messages + [{"role": "user", "content": nudge}]
            try:
                new, _ = await self.answer(key, messages)
            except Exception:
                break
            if new:
                new = new.strip()
                # 既要跟这次的原话不同，也不能撞上历史里任何一句
                if dedupe_key(new) not in used and dedupe_key(new) != dedupe_key(reply):
                    rec.append(new)
                    return new
                logger.info("换出来的还是撞车，再试")
        rec.append(reply)
        return reply

    async def describe_images(self, image_urls: list) -> str | None:
        """本地识图：用本地 VL 模型把图片转成文字描述，供云端文本模型基于描述回复。

        仅当 local.enable 且 local.vision 时有效；失败返回 None（调用方退化为看不到图）。
        挑引擎是 VISION.describe 的事，这里只管"本地"这一档。
        """
        local = CFG.get("local", {})
        if not (local.get("enable") and local.get("vision")):
            return None
        content = [{"type": "text", "text": VISION_DESCRIBE_PROMPT}]
        for u in image_urls:
            content.append({"type": "image_url", "image_url": {"url": u}})
        msgs = [{"role": "user", "content": content}]
        target = _endpoint_from_dict(local, 120.0)
        try:
            # 本地并发闸：原来写死盯 local 的 local_sem，现在按端点配置走
            async with self.gate.hold(target):
                desc = await self._post(local, msgs, float(local.get("timeout_seconds", 120)),
                                        {"max_tokens": VISION.describe_max_tokens()})
            return (desc or "").strip() or None
        except Exception as exc:
            logger.warning("本地识图失败，退化为看不到图：%s", exc)
            return None

    async def answer(self, key: str, messages: list) -> tuple[str | None, str]:
        """沿回退链要一句回复。返回 (文本, source)。

        source 只可能是 `local` / `cloud` / `no-key` / `failed` ——
        `_reply_model_failure` 按它挑中文说法，别在这里改成端点 id。
        链的顺序由配置推导（见 providers/registry.py 的 _legacy_chains）。
        """
        try:
            messages = self._check_messages(messages)
        except ValueError as exc:
            logger.error("消息全非法，放弃本次调用：%s", exc)
            return None, "failed"
        return await ROUTER.answer(messages, chain="default")


CHAT = Chat()

# ══════════════════════ token 用量：永久记录 ══════════════════════
#
# 以前这份累计是**进程内**的（重启清零），所以面板上永远只看到一个"本次运行"的量，
# 看不出长期趋势。现在落盘到 state/tokens.json，**永久保留**。
#
# 记的内容一个字段都没变（in / out / hit / miss / calls），
# 只是从"内存里攒着"改成"攒着并且写盘"；额外多了 since / updated 两个时间戳，
# 因为"永久记录"总得知道是从哪天开始记的、最后一次写是什么时候。
#
# 为什么节流写盘：每次 LLM 调用都写一次 JSON 太浪费（这条链路本来就慢，
# 别再加磁盘抖动）。改动先记在内存里，攒够 TOKEN_SAVE_MIN_GAP 秒再落一次盘，
# 另外 atexit 与 idle_loop 会补一次强制落盘 —— 崩溃最多丢几秒的量。
TOKENS: dict = {"in": 0, "out": 0, "hit": 0, "miss": 0, "calls": 0}
TOKENS_PATH = BASE / "state" / "tokens.json"
TOKENS_SINCE = ""          # 第一次落盘的时间，之后不再改
TOKENS_UPDATED = ""        # 最后一次落盘的时间
TOKEN_SAVE_MIN_GAP = 5.0   # 两次落盘之间至少隔几秒
_token_dirty = False
_token_saved_at = 0.0


def tokens_load() -> None:
    """启动时把历史累计读回来；文件不在就新建一份全零的。

    读坏了也当"从零开始"：token 统计只是观察用，绝不允许它拦住启动。
    """
    global TOKENS_SINCE, TOKENS_UPDATED, _token_dirty
    data = read_json_dict(TOKENS_PATH, "token 用量")
    for k in TOKENS:
        try:
            TOKENS[k] = int(data.get(k) or 0)
        except (TypeError, ValueError):
            TOKENS[k] = 0
    TOKENS_SINCE = str(data.get("since") or "")
    TOKENS_UPDATED = str(data.get("updated") or "")
    if data:
        logger.info("token 累计已读回（自 %s）：入%d 出%d 调用%d",
                    TOKENS_SINCE or "未知", TOKENS["in"], TOKENS["out"], TOKENS["calls"])
    else:
        # 没有文件就立刻建一份，让"永久记录"从这一刻起就有据可查
        _token_dirty = True
        tokens_save(force=True)


def tokens_save(force: bool = False) -> None:
    """把累计写回 state/tokens.json。

    force=True 只是**跳过节流等待**，没脏照样不写（免得 idle 巡检每次空刷一遍盘）。
    """
    global _token_dirty, _token_saved_at, TOKENS_SINCE, TOKENS_UPDATED
    if not _token_dirty:
        return
    now = time.time()
    if not force and now - _token_saved_at < TOKEN_SAVE_MIN_GAP:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not TOKENS_SINCE:
        TOKENS_SINCE = stamp
    TOKENS_UPDATED = stamp
    payload = dict(TOKENS)
    payload["since"] = TOKENS_SINCE
    payload["updated"] = TOKENS_UPDATED
    write_json_dict(TOKENS_PATH, payload, "token 用量")
    _token_dirty = False
    _token_saved_at = now


def tokens_add(delta: dict) -> None:
    """累加一次调用的用量，并（节流地）落盘。"""
    global _token_dirty
    for k, v in delta.items():
        if k in TOKENS:
            TOKENS[k] += int(v)
    _token_dirty = True
    tokens_save()


def tokens_meta() -> dict:
    """给控制台看的元信息：永久记录从哪天开始、最后写到什么时候。"""
    return {"since": TOKENS_SINCE, "updated": TOKENS_UPDATED, "path": str(TOKENS_PATH)}


atexit.register(lambda: tokens_save(force=True))

SEEN_MSG_IDS: set = set()
SEEN_ORDER: deque = deque(maxlen=500)


def mark_seen(message_id) -> bool:
    """NapCat 偶尔重发同一条，返回 False 表示这条已经处理过了。"""
    if not message_id:
        return True
    if message_id in SEEN_MSG_IDS:
        return False
    SEEN_MSG_IDS.add(message_id)
    SEEN_ORDER.append(message_id)
    if len(SEEN_ORDER) > 500:
        SEEN_MSG_IDS.discard(SEEN_ORDER.popleft())
    return True


def dedupe_key(t: str) -> str:
    """复读比对用的指纹：去掉标点和空白。只用于判断"是否重复"，**不用于输出**，别和 normalize_reply 搞混。"""
    return re.sub(r"[\s，。！？、,.!?~\-…\"'“”‘’]+", "", t or "")[:120]


# 名字不再写死在代码里：每张角色卡自带 name + aliases，多套人设下"她叫什么"跟着会话走。
# 判断"有没有在喊她"用 self_names(会话键)；判断"她自报家门"用 strip_name_prefix()。
def self_names(sk: str = "") -> tuple[str, ...]:
    """这个会话那张卡的名字（本名 + 别名）。一张卡都没有时返回空 —— 她也就不认名字了，
    所以 personas/ 里必须至少有一张卡（随仓库发的那张就是了）。"""
    try:
        return PERSONA.names(sk)
    except Exception:  # noqa: BLE001
        return ()


def her_name(skey: str = "") -> str:
    """她叫什么，用来拼日志和通知里的抬头。

    以前这些地方直接写死「许杏玖」—— 开源版换张卡就全都对不上了（日志标题还写着上一个
    角色的名字）。一张卡都没有时给个中性称呼，别让日志里出现 "None"。
    """
    return (self_names(skey) or ("她",))[0]


def _name_prefix_re() -> re.Pattern:
    """把**所有**卡的名字拼成一个前缀正则：任何一张卡的名字开头都会被去掉。

    故意取并集而不是按会话取：clean_reply 有十来处调用点，多数手里没有会话键；
    而"她自报家门"本来就是模型的偶发行为，宁可多去一点也别漏。
    """
    names: set[str] = set()
    try:
        for c in PERSONA.cards.values():
            names.add(str(c.get("name") or "").strip())
            names.update(str(a).strip() for a in (c.get("aliases") or []))
    except Exception:  # noqa: BLE001
        pass
    names = {n for n in names if n}
    if not names:
        # 没有卡时给一个永不匹配的正则，别让 re.compile("") 把每句话开头都吃掉
        return re.compile(r"(?!)")
    joined = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(r"^\s*(?:" + joined + r")\s*[：:]\s*")


NAME_PREFIX = _name_prefix_re()


def strip_name_prefix(text: str) -> str:
    """去掉她自报家门的前缀（「名字：」这种）。"""
    return NAME_PREFIX.sub("", text)
INTEREST_WORDS = ("哈哈", "笑死", "离谱", "绝了", "无语", "服了", "真的假的", "真的吗",
                  "牛", "卧槽", "我靠", "为什么", "咋", "怎么", "谁啊", "啊？")


ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
WEAK_PUNCT = "，,、；;：: "   # 句中停顿：长句退而求其次的断点


def normalize_reply(t: str) -> str:
    """清理不可见字符和多余空白。

    换行**保留**：它是「她主动分条发送」的分隔符，不再当垃圾清掉。
    """
    t = ZERO_WIDTH.sub("", t or "")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]*\n[ \t]*", "\n", t)   # 行首行尾空白
    t = re.sub(r"\n{2,}", "\n", t)           # 连续空行并成一个分隔
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def _break_weak(s: str, soft: int) -> list[str]:
    """句子太长又没句末标点时，退到逗号/顿号处断，尽量在 soft 附近找落点。"""
    out: list[str] = []
    buf = ""
    for ch in s:
        buf += ch
        if len(buf) >= soft and ch in WEAK_PUNCT:
            out.append(buf[:-1].strip())  # 分隔用的逗号本身不留
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [p for p in out if p]


def _break_line(line: str, soft: int) -> list[str]:
    """按语义切一行。

    句末标点是**语义边界**，不管长短一律切（"下雨了。哪儿也去不了" 就是两句）；
    只有切完仍过长的句子（那种通篇逗号的流水句）才退到逗号处再断。
    """
    sentences = [p.strip() for p in re.findall(r"[^。！？!?…～]+[。！？!?…～]*", line)]
    sentences = [p for p in sentences if p] or [line.strip()]
    out: list[str] = []
    for s in sentences:
        out.extend(_break_weak(s, soft) if len(s) > soft else [s])
    return [p for p in out if p]


def split_bubbles(text: str) -> list[str]:
    """把一条回复拆成多条气泡。

    不只看换行：她写成一整行时，按句意（句末标点 / 逗号停顿）继续拆。
    """
    cfg = CFG.get("split", {})
    clean = normalize_reply(text)
    if not clean:
        return []
    if not cfg.get("enable", True):
        return [clean]

    soft = max(8, int(cfg.get("soft_limit_chars", 26)))
    parts: list[str] = []
    for line in clean.split("\n"):
        line = line.strip()
        if not line:
            continue
        got = _break_line(line, soft)
        if len(got) > 1:
            # 只有**自动拆出来的**碎片才做合并；换行是她明说的分条，必须尊重
            m: list[str] = []
            for p in got:
                if m and len(p) <= 2 and not re.search(r"[^\w\s]", p):
                    m[-1] = m[-1] + p
                else:
                    m.append(p)
            got = m
        parts.extend(got)
    if not parts:
        return []

    cap = max(1, int(cfg.get("max_bubbles", 3)))
    if len(parts) > cap:
        parts = parts[:cap - 1] + ["".join(parts[cap - 1:])]
    return parts


def drop_tags(text: str, *patterns) -> str:
    """删掉标记，只合并标记留下的重复标点，**不碰她自己写的标点**。

    旧写法 `.strip("，,。. ")` 会把正文两端的标点一并削掉（"不去。" -> "不去"），
    看起来就像她"少说了一两个字"，所以换成这个。
    """
    for p in patterns:
        text = p.sub("\x00", text)
    text = re.sub(r"[ \t]*\x00+[ \t]*", "", text)
    text = normalize_reply(text)
    text = re.sub(r"([，,。.、；;:：!！?？])\1+", r"\1", text)  # 标记留下的 。。 -> 。
    if not re.search(r"\w", text, re.UNICODE):
        return ""
    return text


def clean_reply(text: str) -> str:
    """模型偶尔会自报家门，去掉名字前缀；顺手把换行和不可见字符规范化。"""
    return normalize_reply(strip_name_prefix(text)) or normalize_reply(text)


class Attention:
    """注意力：群里越热闹/越提到她，兴趣值越高；攒够阈值她就自己冒出来说一句。"""

    def __init__(self) -> None:
        cfg = CFG.get("attention", {})
        self.enable = bool(cfg.get("enable", True))
        self.threshold = float(cfg.get("threshold", 1.0))
        self.decay = float(cfg.get("decay_seconds", 300))
        self.cooldown = float(cfg.get("cooldown_seconds", 900))
        self.idle_minutes = float(cfg.get("idle_trigger_minutes", 30))
        self.idle_prob = float(cfg.get("idle_probability", 0.2))
        # 她刚说过话、别人又接上了，就算"正在这个话题里"。这种时候别随机沉默
        self.engage_window = float(cfg.get("engage_window_seconds", 150))
        self.engaged_reply_prob = float(cfg.get("engaged_reply_probability", 0.95))
        # 没被 @ 但被喊了她的名字时，把**有效阈值**临时调低。
        # 注意这和"加兴趣"是两回事：加兴趣是一次性推一把、会被指数衰减吃掉；
        # 降阈值是**持续一个窗口**地把门槛降下来，效果是"她更容易接下一句"。
        self.alias_discount = float(cfg.get("alias_threshold_discount", 0.35))
        self.alias_window = float(cfg.get("alias_window_seconds", 120))
        self.groups: dict[str, dict] = {}

    def state(self, group_id) -> dict:
        key = f"g{group_id}"
        st = self.groups.get(key)
        if st is None:
            st = {"interest": 0.0, "last_ts": time.time(), "last_proactive": 0.0,
                  "last_msg_at": time.time(), "my_last_reply_at": 0.0,
                  "recent": deque(maxlen=10)}
            self.groups[key] = st
        return st

    def note(self, group_id, text: str, nickname: str, at_me: bool, mentioned: bool) -> None:
        st = self.state(group_id)
        now = time.time()
        st["interest"] *= math.exp(-max(now - st["last_ts"], 0.0) / self.decay) if self.decay > 0 else 0.0
        st["last_ts"] = now
        st["last_msg_at"] = now

        delta = 0.05
        if at_me:
            delta += 0.9
        elif mentioned:
            delta += 0.5
            # 没被 @、只是喊了她的名字 —— 除了加兴趣，还把**阈值**降下来一段时间。
            # 只喊名字不加 @ 往往是"在跟她说"，这种时候她该更容易接话。
            st["alias_until"] = now + self.alias_window
        if "？" in text or "?" in text:
            delta += 0.1
        if any(w in text for w in INTEREST_WORDS):
            delta += 0.12
        st["interest"] += delta
        if text:
            st["recent"].append(f"{nickname}：{text[:60]}")

    def effective_threshold(self, group_id) -> float:
        """当前生效的触发阈值。

        没被 @ 但最近被喊过名字时会把阈值下调 alias_discount（持续 alias_window 秒）。
        用户要的效果：喊她别名比 @ 更"灵敏"，她更容易接下一句。
        """
        st = self.state(group_id)
        if float(st.get("alias_until") or 0.0) > time.time():
            return max(0.05, self.threshold * (1.0 - self.alias_discount))
        return self.threshold

    def ready(self, group_id) -> bool:
        if not self.enable:
            return False
        st = self.state(group_id)
        if st["interest"] < self.effective_threshold(group_id):
            return False
        return time.time() - st["last_proactive"] >= self.cooldown

    def mark_replied(self, group_id) -> None:
        """她在群里说了话 —— 记一下，用来判断她是不是正聊着。"""
        self.state(group_id)["my_last_reply_at"] = time.time()

    def engaged(self, group_id) -> bool:
        """她是不是正在这个话题里：刚说过话，而且之后还有人接着说。"""
        st = self.state(group_id)
        mine = float(st.get("my_last_reply_at") or 0.0)
        if mine <= 0:
            return False
        # 用 >= ：Windows 下 time.time() 精度约 15ms，
        # 她刚回完、对方立刻接话时两个时间戳可能完全相同，严格大于会漏判
        return (time.time() - mine <= self.engage_window
                and float(st.get("last_msg_at") or 0.0) >= mine)

    def describe(self, group_id) -> str:
        """给日志用的一句话状态。"""
        st = self.state(group_id)
        thr = self.effective_threshold(group_id)
        bits = [f"兴趣={float(st.get('interest') or 0):.2f}/{thr:.2f}"]
        if thr < self.threshold:
            left = int(float(st.get("alias_until") or 0) - time.time())
            bits.append(f"别名降阈值中(还剩{max(0, left)}s)")
        if self.engaged(group_id):
            bits.append(f"正在聊({int(time.time() - float(st.get('my_last_reply_at') or 0))}s前说过)")
        else:
            bits.append(f"静默{int(time.time() - float(st.get('last_msg_at') or time.time()))}s")
        bits.append(f"在场{active_speaker_count(group_id, 120)}人")
        return " ".join(bits)

    def consume(self, group_id) -> None:
        st = self.state(group_id)
        st["interest"] = 0.0
        st["last_proactive"] = time.time()
        st["last_msg_at"] = time.time()


ATTENTION = Attention()


class Life:
    """她自己的作息：几点在干什么、忙不忙、愿不愿搭理人。忙的时候真的会不回消息。"""

    def __init__(self) -> None:
        cfg = CFG.get("life", {})
        self.enable = bool(cfg.get("enable", True))
        self.schedule = cfg.get("schedule", [])
        self.events = cfg.get("mood_events", [])
        # 一个小事件一天只选一个，如果每个提示词都塞，她会全天反复说同一件事。
        # 所以按概率给，并且全天有次数上限。
        self.event_max_per_day = int(cfg.get("event_max_per_day", 4))
        self.event_chance = float(cfg.get("event_chance", 0.25))
        self.event_chance_draw = float(cfg.get("event_chance_draw", 0.12))
        self.event_chance_report = float(cfg.get("event_chance_report", 0.3))
        self.state_path = BASE / str(cfg.get("state_path", "life_state.json"))
        self._uses: dict = read_json_dict(self.state_path, "日常事件计数") or {}
        self.busy_silent_chance = float(cfg.get("busy_silent_chance", 0.5))
        # 忙分两档：睡觉这类不可打断的，@ 她也可能装死；
        # 上班/吃饭这类"忙但能被叫住"的，@ 她基本都会答
        self.busy_at_silent_chance = float(cfg.get("busy_at_silent_chance", 0.05))
        # 忙的时候少接闲话：非指向性消息的回复概率再打这个折
        self.busy_non_directed_factor = float(cfg.get("busy_non_directed_factor", 0.15))
        self.delay = (float(cfg.get("delay_min_seconds", 15)), float(cfg.get("delay_max_seconds", 60)))
        self._day = ""
        self._event = ""
        # ── 自主活动：她平时自己就在做点什么，**与群冷热无关** ──
        # （以前"上网/看视频"只挂在冷场触发的 idle_thought 里，群一热闹她就只会干等）
        sa = CFG.get("self_activity", {})
        self.act_enable = bool(sa.get("enable", True))
        self.act_pool = list(sa.get("acts") or [])
        self.places = list(sa.get("places") or [])
        self.act_tick = float(sa.get("tick_seconds", 120))
        self.act_gap = (float(sa.get("min_gap_minutes", 20)),
                        float(sa.get("max_gap_minutes", 75)))
        _dur = sa.get("duration_minutes") or [15, 70]
        self.act_duration = (float(_dur[0]), float(_dur[-1]))
        self.act_skip = float(sa.get("skip_chance", 0.25))
        self.material_keep = float(sa.get("material_keep_minutes", 90)) * 60
        # ── 忙的时候（上班/吃饭/睡觉）：长活动要被打断，只留"忙里偷闲"的小动作 ──
        # 明确要求：忙起来就把手头那件长活动停掉；忙的整段时间里都不再起长活动，
        # 改成**较低概率**的**偶发短动作**（刷一两个小视频这种），做完就完、不占她一整段。
        self.busy_stop_long = bool(sa.get("busy_stop_long", True))
        self.busy_act_chance = float(sa.get("busy_act_chance", 0.10))
        self.busy_short_acts = list(sa.get("busy_short") or [])
        # 时长按 codebase 既有写法拆成 min/max 两个键（成对的值挂不上一条滑块）
        _lo = sa.get("busy_short_min_minutes")
        _hi = sa.get("busy_short_max_minutes")
        if _lo is None or _hi is None:
            _pair = sa.get("busy_short_minutes") or [1, 8]
            _lo, _hi = _lo if _lo is not None else _pair[0], \
                       _hi if _hi is not None else _pair[-1]
        self.busy_short_duration = (float(_lo), max(float(_lo), float(_hi)))
        self.activity: dict | None = None     # 手头正在做的事
        self.activity_until = 0.0             # 这件事做到什么时候
        self.activity_next = 0.0              # 下次可以换事的时间
        self.material = ""                    # 攒下的话题素材
        self.material_kind = ""               # 素材的来源类型（跳过重复注入用）
        self.material_ts = 0.0
        self._place_day = ""
        self._place = ""
        self.events_per_day = int(cfg.get("events_per_day", 3))
        self._day_events: list[str] = []
        # "这一小时的小事"缓存：同一小时所有用途共用同一份结论（见 event_for）
        self._slot_key = ""
        self._slot_pick: dict[str, str] = {}

    def _time_str(self, now) -> str:
        """当前时间的说法。

        精度刻意做成**可配置且默认到小时**：提示词里带分钟的话，每分钟系统提示词都变一次，
        云端的前缀缓存（DeepSeek 的 context caching）就永远命中不了。
        """
        prec = str(CFG.get("life", {}).get("time_precision", "hour")).lower()
        wd = "星期" + "一二三四五六日"[now.weekday()]
        if prec == "minute":
            return f"{wd} {now:%H:%M}"
        if prec == "half_hour":
            minute = 30 if now.minute >= 30 else 0
            return f"{wd} {now:%H}:{minute:02d}"
        return f"{wd} {now:%H}点"

    def _events_today(self, day: str) -> list[str]:
        """今天随机挑**几条**小事。

        以前固定只挑 1 条，一整天来来回回就那一件事，太单调。
        条数由 life.events_per_day 控制。同一个 seed 保证当天不变。
        """
        if not self.events:
            return []
        if self._day != day:
            self._day = day
            rnd = random.Random(day)
            n = max(1, min(self.events_per_day, len(self.events)))
            self._day_events = rnd.sample(self.events, n)
        return self._day_events

    def _event_today(self, day: str) -> str:
        evs = self._events_today(day)
        return evs[0] if evs else ""

    def place_today(self, day: str) -> str:
        """每天随机一个"她现在待的地方"。

        用户要"别老在一个地方呆着" —— 日程段只说了在做什么，
        具体在哪儿每天换一个，动线才不会千篇一律。
        """
        if not self.places:
            return ""
        if self._place_day != day:
            self._place_day = day
            self._place = random.Random(f"place-{day}").choice(self.places)
        return self._place

    def _uses_today(self) -> dict:
        """今天各件小事分别用过几次。旧格式是 {"date","count"}（一个总数），
        读到了就当它全是"第一条小事"用掉的，不至于把计数清零重来。"""
        day = datetime.now().strftime("%Y-%m-%d")
        if self._uses.get("date") != day:
            return {}
        per = self._uses.get("per")
        if isinstance(per, dict):
            return {k: int(v) for k, v in per.items() if isinstance(v, (int, float))}
        # 旧格式迁移
        evs = self._events_today(day)
        first = evs[0] if evs else ""
        return {first: int(self._uses.get("count") or 0)} if first else {}

    def _bump_uses(self, ev: str) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        per = self._uses_today()
        per[ev] = int(per.get(ev) or 0) + 1
        self._uses = {"date": day, "per": per}
        write_json_dict(self.state_path, self._uses, "日常事件计数")

    def _running_activity(self) -> dict | None:
        """手头正在做的自主活动（没做完才算；忙里偷闲的短动作不算）。"""
        running = self.activity if (self.activity and time.time() < self.activity_until) else None
        if running and running.get("short"):
            return None
        return running

    # 今日小事与"她这会儿在做什么"打架的几种情形：
    # 作息 hint 说在外面活动，小事却说被雨困住/不想出门 —— 两句都在定义当下，直接矛盾
    _EVENT_CLASH = ("去不了", "困在", "不出门", "不想出门", "躲雨", "窝在")
    _HINT_OUTSIDE = ("晃悠", "外面", "附近", "门口", "逛街", "溜达", "晒太阳")

    def _clash_with_hint(self, ev: str, hint: str) -> bool:
        """这件小事跟"她这会儿在做什么"是不是互相打脸。"""
        if not ev or not hint:
            return False
        if any(w in ev for w in self._EVENT_CLASH) and any(w in hint for w in self._HINT_OUTSIDE):
            return True
        return False

    # 事件池里既有"今天阳光特别好"又有"外面在下雨" —— 小事是随机挑的，天气是实时的，
    # 挑到"阳光好"而外面正下雨，她就会一边说太阳好一边说外面在下雨。
    _EVENT_SUNNY = ("阳光", "晴天", "晒", "太阳", "好天气")
    _EVENT_WET = ("下雨", "雨", "潮湿", "淋")

    def _clash_with_weather(self, ev: str) -> bool:
        """这件小事跟**外面真实的天气**是不是互相打脸。"""
        if not ev or not WEATHER.enable:
            return False
        snap = WEATHER._data or {}
        code = snap.get("code")
        desc = str(snap.get("desc") or "")
        if code is None and not desc:
            return False                       # 还没取到天气，就当不冲突
        wet = (isinstance(code, int) and (code in RAINY or code in SNOWY or code in STORM)) \
            or ("雨" in desc) or ("雪" in desc)
        dry = (isinstance(code, int) and code in (0, 1)) or ("晴" in desc)
        if wet and any(w in ev for w in self._EVENT_SUNNY):
            return True
        if dry and any(w in ev for w in self._EVENT_WET):
            return True
        return False

    def event_for(self, kind: str = "chat") -> str:
        """取"今天的小事"，但不保证给。

        ⚠️ 这里踩过三个坑，改动前先看清楚：

        1. **和"她在做什么"打架**：以前每次调用都重新掷骰子，于是同一时刻
           chat 给了、draw/report 不给，三种用途说法不一致；而且作息 hint 说
           "在附近晃悠"、小事说"下雨哪儿也去不了"，一句话里自相矛盾。
           现在**按小时定一次**（同一小时所有用途看到同一件小事），
           并且跟当下动作冲突时直接不给。

        2. **永远只用第一条**：`events_per_day` 挑了好几条，却只取 `evs[0]`，
           挑的其余几条白挑了。现在按小时在当天那几条里**轮着换**。

        3. **她在做自主活动时不给**：活动的 hint 已经在说"这会儿在干嘛"了，
           再塞一件小事就成了两个并列的当下状态，她会前言不搭后语。
        """
        if not self.events:
            return ""
        # ① 她正做着一件事 —— 当下已由活动定义，不再叠加小事
        if self._running_activity():
            return ""
        day = datetime.now().strftime("%Y-%m-%d")
        hour = datetime.now().hour
        slot_key = f"{day}-{hour}"
        if self._slot_key != slot_key:
            self._slot_key = slot_key
            self._slot_pick = self._decide_slot(day, hour)
        ev = self._slot_pick.get(kind) or ""
        if not ev:
            return ""
        # ② 跟"这会儿在做什么"矛盾就不说（作息 hint 也随活动/时段变，得每次判）
        if self._clash_with_hint(ev, self.schedule_now().get("hint", "")):
            return ""
        # ②' 跟外面真实的天气矛盾也不说（"今天阳光特别好" vs 外面在下雨）
        if self._clash_with_weather(ev):
            return ""
        # ③ 同一件小事每天有次数上限，别一整天念叨同一件
        used = int(self._uses_today().get(ev) or 0)
        if 0 < self.event_max_per_day <= used:
            logger.debug("这件小事今天已经说过 %d 次，这次不给：%s", used, ev[:20])
            return ""
        self._bump_uses(ev)
        return ev

    def _decide_slot(self, day: str, hour: int) -> dict:
        """把"这一小时用哪件小事、各用途给不给"一次性定下来。

        用 `random.Random(f"{day}-{hour}")` 而不是全局 random：
        同一个小时里无论被问多少次（也不管是对话/画图/报告），拿到的都是同一份结论，
        换个进程、重启也一致 —— 否则同一时刻三种用途给她三件不同的小事。
        """
        evs = self._events_today(day)
        if not evs:
            return {}
        # 按小时轮着换，别一整天只有第一条
        ev = evs[hour % len(evs)]
        rnd = random.Random(f"evslot-{day}-{hour}")
        out = {}
        for k, chance in (("chat", self.event_chance),
                          ("draw", self.event_chance_draw),
                          ("report", self.event_chance_report)):
            out[k] = ev if rnd.random() < chance else ""
        return out

    @staticmethod
    def _in_range(item: dict, minutes: int) -> bool:
        try:
            sh, sm = (int(x) for x in item["from"].split(":"))
            eh, em = (int(x) for x in item["to"].split(":"))
        except Exception:
            return False
        start, end = sh * 60 + sm, eh * 60 + em
        return start <= minutes < end if start <= end else (minutes >= start or minutes < end)

    def _pick_item(self, weekday: int, minutes: int) -> dict:
        """按"星期 + 分钟数"从作息表里挑一段。

        带 weekdays 的条目（比如工作日上班）优先，命中就用它；
        否则退回不限星期的那条。抽出来是因为 `current()` 与 `schedule_now()`
        必须用**同一套**挑法，否则"她算不算在忙"两个入口会给出不同答案。
        """
        chosen, fallback = None, None
        for item in self.schedule:
            if not self._in_range(item, minutes):
                continue
            wd = item.get("weekdays")
            if wd and weekday in wd:
                chosen = item
                break
            if not wd and fallback is None:
                fallback = item
        return chosen or fallback or {}

    def schedule_now(self) -> dict:
        """**只看作息表**的当前状态 —— 不受她自己起的活动影响。

        为什么要单独有这么个东西：`current()` 会让"手头正做的事"盖过日程，
        于是"她到底算不算在忙"会被她自己起的活动污染。
        而"忙的时候要打断活动"这个判断，恰恰必须看作息表本身说了什么。
        """
        now = datetime.now()
        item = self._pick_item(now.weekday(), now.hour * 60 + now.minute)
        busy = bool(item.get("busy", False))
        hint = item.get("hint", "")
        if hint and not busy:
            # 日程段配一个"今天待的地方"，每天换一个（动线才不会千篇一律）
            place = self.place_today(now.strftime("%Y-%m-%d"))
            if place and place not in hint:
                hint = f"{hint}（今天在{place}）"
        return {
            "act": item.get("act", ""),
            "hint": hint,
            "chance": float(item.get("reply_chance", 1.0)),
            "busy": busy,
            "interruptible": bool(item.get("interruptible", False)),
        }

    def current(self) -> dict:
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        sch = self.schedule_now()
        act, hint, chance, busy = sch["act"], sch["hint"], sch["chance"], sch["busy"]
        self.note_act_change(act)

        # 自主活动优先：她手头正做的事盖过日程段。
        # 这就是"不在现有的地方一直呆着" —— 日程只是底子，她还会自己找事做。
        # 但有三个例外：
        #   1) 忙里偷闲的**短动作**（short）不顶班 —— 她还是"在上班"，只是抽空刷了个视频；
        #   2) 日程说她"忙"时，一个**不忙**的长活动也不顶班（那种情况会被 busy_tick 打断）；
        #   3) 反过来，日程不忙、长活动是"忙"的（比如蜷着打盹），照旧由活动说了算。
        running = self.activity if (self.activity and time.time() < self.activity_until) else None
        if self.activity and not running:
            self.activity = None          # 做完了
        short_now = bool(running and running.get("short"))
        if running and not short_now and not (busy and not running.get("busy")):
            act = running.get("name") or act
            hint = running.get("hint") or hint
            chance = float(running.get("reply_chance", chance))
            busy = bool(running.get("busy", busy))

        return {
            "act": act,
            "hint": hint,
            "chance": chance,
            "busy": busy,
            "interruptible": sch["interruptible"],
            "time_str": self._time_str(now),
            "event": self._event_today(day),
            "events": self._events_today(day),
            "self_activity": (running or {}).get("name", ""),
            "self_activity_short": short_now,
            "self_mood": self.self_mood(chance)[1],
            # 作息表那一段的名字 —— 跟 act 不是一回事（act 会被自主活动盖掉）。
            # 面板要靠它高亮"日程表现在走到哪一段"，用 act 的话她一做别的事
            # 高亮就没了。
            "schedule_act": sch["act"],
        }

    # ──────────────────── 自主活动 ────────────────────

    def self_mood(self, chance: float | None = None) -> tuple[float, str]:
        """她**整体**的心情，返回 (0-1 分, 档位 good/calm/bad)。

        两处合成：
          · 当前时段的精神头（日程里的 reply_chance）—— 困了忙了当然什么都不想干
          · 她对所有人的平均心情（跨群，来自 Mood）—— 刚被人气过就懒得动
        注意这是"她自己的心情"，跟"对某个人的好感/心情"不是一回事。
        """
        if chance is None:
            chance = float(self.current().get("chance") or 0.5)
        try:
            social = MOOD.overall_mood()
        except Exception:
            social = 0.5
        score = 0.55 * float(chance) + 0.45 * social
        label = "good" if score >= 0.6 else "bad" if score < 0.35 else "calm"
        return score, label

    def pick_activity(self, label: str) -> dict | None:
        """按心情档位在活动池里加权抽一个。"""
        if not self.act_enable or not self.act_pool:
            return None
        pool = [a for a in self.act_pool if a.get("mood", "any") in ("any", label)]
        if not pool:
            pool = [a for a in self.act_pool if a.get("mood", "any") == "any"]
        if not pool:
            pool = list(self.act_pool)
        weights = [max(0.01, float(a.get("weight", 1))) for a in pool]
        return random.choices(pool, weights=weights, k=1)[0]

    def set_activity(self, act: dict) -> None:
        """开始做一件事，持续一个**随机**时长。"""
        lo, hi = self.act_duration
        minutes = random.uniform(lo, max(lo, hi))
        now = time.time()
        self.activity = act
        self.activity_until = now + minutes * 60
        self.activity_next = self.activity_until
        place = self.place_today(datetime.now().strftime("%Y-%m-%d"))
        where = f"（在{place}）" if place else ""
        ACTIVITY.note("动作", f"{act.get('name', '做点什么')}{where}　{act.get('hint', '')}")
        logger.info("自主活动：%s%s，约 %.0f 分钟", act.get("name", "?"), where, minutes)

    def schedule_next(self) -> None:
        """安排下一次"可以考虑换件事"的时间 —— 间隔是随机的，别像按表走的。"""
        lo, hi = self.act_gap
        self.activity_next = time.time() + random.uniform(lo, max(lo, hi)) * 60

    # ──────────────────── 忙的时候：打断 + 小动作 ────────────────────

    def stop_activity(self, why: str) -> str:
        """把手上正在做的事**立刻结束**，返回刚停掉的名字（本来没事就返回空串）。

        用法就一个场景：**她忙起来了**。作息表翻到上班/吃饭/睡觉那一段时，
        手里那件长活动不该继续挂着 —— 挂着的直接后果是 `current()` 一直拿活动去盖作息，
        于是"该忙"的她看起来还在闲逛，接话态度也就不像在忙。
        """
        running = self.activity if (self.activity and time.time() < self.activity_until) else None
        self.activity = None
        self.activity_until = 0.0
        if not running:
            return ""
        name = str(running.get("name") or "")
        self.schedule_next()        # 停掉之后按正常间隔再考虑下一件，不要立刻又起一个
        # 连带清掉这件事攒下的话题素材：素材是"她刚做了什么"的谈资，
        # 事情既然没做完，留着就会让她开口说"我刚看完视频…" —— 可她其实没看完。
        if self.material_kind == "活动":
            self.material = ""
            self.material_kind = ""
            self.material_ts = 0.0
        ACTIVITY.note("动作", f"（{why}）{name} 中断了")
        logger.info("自主活动被打断：%s（%s）", name, why)
        return name

    def busy_tick(self) -> str:
        """忙的时候每个巡检周期调一次：**长活动一律打断**。

        跳过"短动作"（那个本来就是忙里偷闲，不该被自己打断）。
        `self_activity.busy_stop_long` 关掉时整个不动手（保留旧行为）。
        """
        running = self.activity if (self.activity and time.time() < self.activity_until) else None
        if running and running.get("short"):
            return ""
        if not (self.busy_stop_long and self.schedule_now().get("busy")):
            return ""
        return self.stop_activity("忙起来了")

    def pick_short_activity(self) -> dict | None:
        """忙里偷闲的小动作池（刷一两个视频这种）；池子为空返回 None。"""
        if not self.busy_short_acts:
            return None
        weights = [max(0.01, float(a.get("weight", 1))) for a in self.busy_short_acts]
        act = dict(random.choices(self.busy_short_acts, weights=weights, k=1)[0])
        act["short"] = True         # 标记：不顶作息、不会被 busy_tick 打断
        return act

    def set_short_activity(self, act: dict) -> None:
        """起一个小动作，持续**几分钟**就完 —— 忙的人只偷得起这点空。"""
        lo, hi = self.busy_short_duration
        minutes = random.uniform(lo, max(lo, hi))
        now = time.time()
        act = dict(act)
        act["short"] = True
        self.activity = act
        self.activity_until = now + minutes * 60
        self.activity_next = self.activity_until
        ACTIVITY.note("动作", f"（忙里偷闲）{act.get('name', '做点什么')}　{act.get('hint', '')}")
        logger.info("忙里偷闲：%s，约 %.1f 分钟", act.get("name", "?"), minutes)

    def note_material(self, text: str, kind: str = "") -> None:
        """把活动里看到的、能当话题的东西存下来。

        `kind` 是这份素材的**来源类型**（"天气" / "好感" / ""）。
        为什么要分类型：同一件事可能被两条路同时送进提示词 ——
        "外面开始下雨了"既会经 `WEATHER.phrase()` 进 system 的天气那句，
        又会经这里攒成素材进 user 那句，模型就看见两遍（而且两遍说法不同，
        一边是实况、一边是变化，她还以为下了两场雨）。
        所以取用的时候可以按类型跳过（见 `take_material`）。
        """
        text = (text or "").strip()
        if not text:
            return
        self.material = text[:200]
        self.material_kind = kind or ""
        self.material_ts = time.time()

    def take_material(self, skip_kinds=()) -> str:
        """取手头的话题素材；太旧就不要了。取走不清空 —— 同一件事可以聊几次。

        `skip_kinds`：调用方这次**已经从别处说过**的素材类型，就别再从素材里说一遍。
        天气/好感这类"system 里本来就有"的信息，冷场自语时应当跳过，
        否则同一句话会在 system 和 user 两侧各出现一次。
        """
        if not self.material:
            return ""
        if time.time() - self.material_ts > self.material_keep:
            return ""
        if self.material_kind and self.material_kind in (skip_kinds or ()):
            return ""
        return self.material

    def snapshot(self) -> dict:
        """只读快照，给控制台看她在干什么。"""
        running = self.activity if (self.activity and time.time() < self.activity_until) else None
        sch = self.schedule_now()
        return {
            "enable": self.act_enable,
            "pool_size": len(self.act_pool),
            "places": self.places,
            "gap_minutes": list(self.act_gap),
            "duration_minutes": list(self.act_duration),
            "skip_chance": self.act_skip,
            "material_keep_minutes": self.material_keep / 60,
            # 忙不忙由**作息**说了算（不是由手头活动）：面板上那行提示要能对上这个判断
            "busy": bool(sch.get("busy")),
            "schedule_act": sch.get("act", ""),
            "busy_stop_long": self.busy_stop_long,
            "busy_act_chance": self.busy_act_chance,
            # 叫 range 不叫 minutes：它是一对 [最短,最长]，而可编辑的路径是
            # busy_short_min_minutes / busy_short_max_minutes 两个键 ——
            # 叫 minutes 会让人以为存在 self_activity.busy_short_minutes 这个键
            "busy_short_range": list(self.busy_short_duration),
            "busy_short": [{"id": a.get("id"), "name": a.get("name"),
                            "weight": a.get("weight"), "hint": a.get("hint")}
                           for a in self.busy_short_acts],
            "activity": ({"name": running.get("name", ""), "hint": running.get("hint", ""),
                          "id": running.get("id", ""), "short": bool(running.get("short")),
                          "left_seconds": max(0, int(self.activity_until - time.time()))}
                         if running else None),
            "next_in_seconds": max(0, int(self.activity_next - time.time()))
                               if self.activity_next else None,
            "material": self.take_material(),
            "material_age_seconds": (time.time() - self.material_ts) if self.material_ts else None,
            "today_events": self._events_today(datetime.now().strftime("%Y-%m-%d")),
            "events_per_day": self.events_per_day,
            "place_today": self.place_today(datetime.now().strftime("%Y-%m-%d")),
            "acts": [{"id": a.get("id"), "name": a.get("name"), "mood": a.get("mood"),
                      "weight": a.get("weight"), "busy": a.get("busy")}
                     for a in self.act_pool],
        }

    def note_act_change(self, act: str) -> None:
        """作息换段了（比如"晒太阳"->"书店看店"）就记一笔，报告里能看出她一天的动线。"""
        if not act or act == getattr(self, "_last_act", ""):
            return
        if getattr(self, "_last_act", ""):
            ACTIVITY.note("作息", f"换到了「{act}」")
        self._last_act = act


LIFE = Life()

WMO = {0: "晴", 1: "大致晴", 2: "多云", 3: "阴", 45: "起雾", 48: "雾凇", 51: "毛毛雨", 53: "小雨",
       55: "细雨", 56: "冻毛毛雨", 57: "冻雨", 61: "下雨", 63: "中雨", 65: "大雨", 66: "冻雨",
       67: "冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪", 80: "阵雨", 81: "阵雨",
       82: "暴雨", 85: "阵雪", 86: "阵雪", 95: "雷阵雨", 96: "雷暴", 99: "雷暴"}


RAINY = frozenset({51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82})
SNOWY = frozenset({71, 73, 75, 77, 85, 86})
STORM = frozenset({95, 96, 99})


class Weather:
    """外面的天气 —— open-meteo 免费接口（无需 key），失败就静默跳过，不影响对话。

    2026-09-27 为了"她对当地天气更敏锐"重做了三件事：

      1. **看得更细**：除天气码与气温，还取体感温度、湿度、风、降水量、紫外线、
         当天最高/最低、日出日落；提示词里给的是"人身上能感觉到"的那几句。
      2. **感知变化**：每次刷新跟**上一次的快照**比，温度骤变 / 下起雨来 / 风大起来
         都算一次变化，记进她的行为流水，并攒成她开口时的话题素材。
         快照落盘（state/weather_state.json），重启也还记得"刚才是晴的"。
      3. **接上网**：出现明显变化时（或定时的巡检里）用 Web 搜一眼本地的
         天气预警/实况，把标题也攒进素材 —— 不再只有一个孤零零的温度数字。
    """

    def __init__(self) -> None:
        cfg = CFG.get("weather", {})
        self.enable = bool(cfg.get("enable", False))
        self.city = cfg.get("city", "")
        self.cache_hours = float(cfg.get("cache_hours", 3))
        self.timezone = str(cfg.get("timezone", "Asia/Shanghai"))
        # 变化感知
        self.change_keep_minutes = float(cfg.get("change_keep_minutes", 45))
        self.change_temp_delta = float(cfg.get("change_temp_delta", 5.0))
        # 联网查预警
        self.alert_enable = bool(cfg.get("alert_enable", True))
        self.alert_cooldown = float(cfg.get("alert_cooldown_minutes", 120)) * 60
        self.tick_minutes = float(cfg.get("tick_minutes", 15))
        self.state_path = BASE / str(cfg.get("state_path", "state/weather_state.json"))
        self._ts = 0.0
        self._text = ""
        self._coord = None
        self._data: dict = {}          # 最近一次拿到的全部字段（控制台用）
        self._change = ""              # 最近一次"明显变化"的说法
        self._change_ts = 0.0          # 什么时候发现的变化
        self._alert_ts = 0.0           # 上次联网查预警的时间
        self._alert = ""               # 最近一次网上查到的（预警/实况标题）
        self._prev: dict = {}          # 上一次刷新时的关键字段，用来比变化
        self._prev_ts = 0.0
        self.load()

    # ──────────────────────── 状态存取 ────────────────────────

    def load(self) -> None:
        """把"上一次的天气快照"读回来 —— 重启后仍能比出变化。"""
        data = read_json_dict(self.state_path, "天气快照")
        if not data:
            return
        prev = data.get("prev")
        self._prev = dict(prev) if isinstance(prev, dict) else {}
        try:
            self._prev_ts = float(data.get("prev_ts") or 0)
            self._change_ts = float(data.get("change_ts") or 0)
            self._alert_ts = float(data.get("alert_ts") or 0)
        except (TypeError, ValueError):
            self._prev_ts = self._change_ts = self._alert_ts = 0.0
        self._change = str(data.get("change") or "")
        self._alert = str(data.get("alert") or "")

    def save(self) -> None:
        write_json_dict(self.state_path, {
            "prev": self._prev, "prev_ts": self._prev_ts,
            "change": self._change, "change_ts": self._change_ts,
            "alert": self._alert, "alert_ts": self._alert_ts,
            "_说明": ("prev 是上一次刷新时的关键天气字段（用来比出变化，重启不清）；"
                    "change 是最近一次明显变化的说法；alert 是联网查到的预警标题。"),
        }, "天气快照")

    # ──────────────────────── 取数 ────────────────────────

    async def refresh(self) -> dict:
        """拉一次天气并更新内部状态（含变化判定）。失败返回 {} 且不动旧值。"""
        if not self.enable or not self.city:
            return {}
        try:
            cli = CHAT.client_for("https://api.open-meteo.com")
            if self._coord is None:
                g = await cli.get("https://geocoding-api.open-meteo.com/v1/search",
                                  params={"name": self.city, "count": 1, "language": "zh"}, timeout=10)
                res = g.json().get("results") or []
                if not res:
                    logger.info("天气：城市「%s」没查到坐标", self.city)
                    return {}
                self._coord = (res[0]["latitude"], res[0]["longitude"])
                # 记下解析到的官方名字（"长沙" -> "长沙市"），面板上能看出查的是哪个地方
                self._data["place"] = str(res[0].get("name") or self.city)
            w = await cli.get("https://api.open-meteo.com/v1/forecast",
                              params={
                                  "latitude": self._coord[0], "longitude": self._coord[1],
                                  "current": ("weather_code,temperature_2m,apparent_temperature,"
                                              "relative_humidity_2m,precipitation,wind_speed_10m,is_day"),
                                  "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                                            "precipitation_probability_max,uv_index_max,sunrise,sunset"),
                                  "timezone": self.timezone,
                                  "forecast_days": 1,
                              }, timeout=10)
            body = w.json() or {}
        except Exception as exc:
            logger.debug("天气获取失败（忽略）：%s", exc)
            return {}

        cur = body.get("current") or {}
        daily = body.get("daily") or {}

        def _d(key, idx=0):
            arr = daily.get(key) or []
            return arr[idx] if len(arr) > idx else None

        code = cur.get("weather_code")
        snap = {
            "code": code,
            "desc": WMO.get(code, ""),
            "temp": cur.get("temperature_2m"),
            "feels": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"),
            "precip": cur.get("precipitation"),
            "wind": cur.get("wind_speed_10m"),
            "is_day": cur.get("is_day"),
            "tmax": _d("temperature_2m_max"),
            "tmin": _d("temperature_2m_min"),
            "rain_p": _d("precipitation_probability_max"),
            "uv": _d("uv_index_max"),
            "sunrise": str(_d("sunrise") or ""),
            "sunset": str(_d("sunset") or ""),
            "place": self._data.get("place") or self.city,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        snap["text"] = self._short(snap)
        self._data = snap
        self._text = snap["text"]
        self._ts = time.time()
        self._note_change(snap)
        return snap

    @staticmethod
    def _short(s: dict) -> str:
        """一句话的天气（提示词与顶栏都看这句）。"""
        desc = s.get("desc") or "天气不明"
        temp = s.get("temp")
        feels = s.get("feels")
        out = f"{desc}{f' {temp:g}°C' if temp is not None else ''}"
        if temp is not None and feels is not None and abs(float(feels) - float(temp)) >= 3:
            out += f"（体感 {feels:g}°）"
        return out

    def _note_change(self, snap: dict) -> None:
        """跟上一份快照比，挑**一条**最值得说的变化。

        只挑一条是刻意的：全说出去她会像在念天气播报，一句"外面下起雨了"才像人。
        """
        old = self._prev
        fresh = bool(old) and (time.time() - self._prev_ts) < 12 * 3600
        self._prev = {k: snap.get(k) for k in ("code", "desc", "temp", "wind", "humidity",
                                               "precip", "place")}
        self._prev_ts = time.time()

        line = ""
        if fresh:
            ocode, ncode = old.get("code"), snap.get("code")
            if isinstance(ocode, int) and isinstance(ncode, int):
                if ncode in STORM and ocode not in STORM:
                    line = "外面开始打雷了（她怕打雷，心里发毛）"
                elif ncode in SNOWY and ocode not in SNOWY:
                    line = f"外面下起雪来了（{snap['desc']}）"
                elif ncode in RAINY and ocode not in RAINY:
                    line = f"外面开始下雨了（{snap['desc']}）"
                elif ocode in RAINY and ncode not in RAINY:
                    line = "雨停了"
                elif ocode in SNOWY and ncode not in SNOWY:
                    line = "雪停了"
            o_t, n_t = old.get("temp"), snap.get("temp")
            if not line and o_t is not None and n_t is not None:
                delta = float(n_t) - float(o_t)
                if abs(delta) >= self.change_temp_delta:
                    line = (f"这一会儿工夫{'升' if delta > 0 else '降'}了 "
                            f"{abs(delta):.0f} 度，现在 {float(n_t):g}°C")
            o_w, n_w = old.get("wind"), snap.get("wind")
            if not line and o_w is not None and n_w is not None and float(n_w) >= 8:
                if float(n_w) - float(o_w) >= 4:
                    line = "外面风大起来了"
        if not line:
            # 没有"变化"，也留一句当下的极端感受 —— 这也是"敏锐"的一部分
            t = snap.get("temp")
            code = snap.get("code")
            if t is not None and float(t) >= 35:
                line = f"外面热得离谱（{float(t):g}°C）"
            elif t is not None and float(t) <= 2:
                line = f"外面冷得手都僵了（{float(t):g}°C）"
            elif code in STORM:
                line = "外面在打雷（她怕打雷）"
            elif snap.get("rain_p") is not None and float(snap["rain_p"]) >= 80:
                line = f"今天大概率要下雨（降水概率 {float(snap['rain_p']):.0f}%）"

        if not line:
            return
        if line == self._change and (time.time() - self._change_ts) < 3600:
            return                        # 同一句话一小时内别重复记
        self._change = line
        self._change_ts = time.time()
        ACTIVITY.note("天气", line)
        logger.info("天气变化：%s（%s）", line, snap.get("text"))
        self.save()

    # ──────────────────────── 对外 ────────────────────────

    async def text(self) -> str:
        """一句话天气（带缓存）。"""
        if not self.enable or not self.city:
            return ""
        if self._text and time.time() - self._ts < self.cache_hours * 3600:
            return self._text
        snap = await self.refresh()
        return snap.get("text") if snap else ""

    def change(self) -> str:
        """最近一次明显变化的说法；过了一阵（change_keep_minutes）就不算"新鲜"了。"""
        if not self._change:
            return ""
        if time.time() - self._change_ts > self.change_keep_minutes * 60:
            return ""
        return self._change

    async def phrase(self) -> str:
        """提示词里用的那一句：天气 + （刚有变化时）变化。"""
        base = await self.text()
        if not base:
            return ""
        ch = self.change()
        return f"{base}，{ch}" if ch else base

    async def sense(self) -> str:
        """定期巡检：刷一次天气；有变化就上网查一眼预警，把结果攒成她的素材。

        返回这次有没有"值得她开口"的东西（变化或预警标题），空串表示无事发生。
        """
        snap = await self.refresh()
        if not snap:
            return ""
        bits: list[str] = []
        ch = self.change()
        if ch:
            bits.append(ch)
        # 有变化、或者距上次查预警够久了，就上网看一眼（不联网就跳过，不影响天气本身）
        if (ch or time.time() - self._alert_ts > self.alert_cooldown) and self.alert_enable:
            self._alert_ts = time.time()
            if WEB.enable:
                try:
                    hits = await WEB.search(f"{self.city} 天气 预警")
                    for r in hits or []:
                        title = str(r.get("title") or "").strip()
                        if title and (self.city in title or "天气" in title or "预警" in title):
                            self._alert = title[:80]
                            bits.append(f"网上说：{self._alert}")
                            break
                except Exception as exc:
                    logger.debug("天气预警联网查询失败（忽略）：%s", exc)
            self.save()
        if not bits:
            return ""
        line = "；".join(bits)
        # 标成"天气"：这条变化同时也会经 WEATHER.phrase() 进 system 的天气那句，
        # 冷场自语取素材时得能跳过它（见 take_material 的 skip_kinds）
        LIFE.note_material(f"（天气）{line}", kind="天气")
        logger.info("天气巡检：%s", line[:120])
        return line

    def snapshot(self) -> dict:
        """给控制台看的全量快照。"""
        return {
            "enable": self.enable,
            "city": self.city,
            "timezone": self.timezone,
            "coords": list(self._coord) if self._coord else None,
            "cache_hours": self.cache_hours,
            "age_seconds": (time.time() - self._ts) if self._ts else None,
            "data": dict(self._data),
            "change": self.change(),
            "change_any": self._change,
            "change_age_seconds": (time.time() - self._change_ts) if self._change_ts else None,
            "change_keep_minutes": self.change_keep_minutes,
            "change_temp_delta": self.change_temp_delta,
            "alert_enable": self.alert_enable,
            "alert": self._alert,
            "alert_age_seconds": (time.time() - self._alert_ts) if self._alert_ts else None,
            "alert_cooldown_minutes": self.alert_cooldown / 60,
            "tick_minutes": self.tick_minutes,
            "state_path": str(self.state_path),
            "web_enable": bool(WEB.enable),
        }


WEATHER = Weather()


class Memory:
    """关于"具体某几个人"的长期记忆 —— **人物记忆**。

    按 **QQ 号** 存，所以跨群和私聊都看得见 —— 不像以前按群隔离，
    同一个人换个群就变陌生人了。

    **留不留由管理员决定**：她先 propose 挂起，管理员 approve 才落盘，
    decline 过的不再问。（面板 B04 上有批准/拒绝/忘掉。）

    和另两套东西的分工（三者的边界别糊）：
      · **这里** 记"某个人是什么样的人"—— 管理员把关
      · **GroupMemory** 记"这个群是个什么群"—— 跟群走，不跟人走，另一个文件
      · **闲聊 / 当时的话题 / 气氛** 这类不重要的 —— 两边都不落盘，
        改用 `ephemeral()` 每轮现算，永远新鲜
    只记两类：这个人的特点、以及答应过/要做的事。闲聊不记，记不记由她自己拿捏。
    新人要先问管理员；管理员自己默认就记着，不用问。
    """

    def __init__(self) -> None:
        cfg = CFG.get("memory", {})
        self.enable = bool(cfg.get("enable", True))
        self.extract = bool(cfg.get("extract_enable", True))
        self.every = int(cfg.get("extract_every_messages", 30))
        self.reset_hour = int(cfg.get("reset_hour", 4))
        self.every_days = max(1, int(cfg.get("reset_every_days", 1)))
        self.max_traits = int(cfg.get("max_traits_per_person", 4))
        self.max_todos = int(cfg.get("max_todos_per_person", 3))
        self.max_people = int(cfg.get("max_people", 12))
        self.path = BASE / str(cfg.get("path", "memory.json"))
        self.people: dict[str, dict] = {}     # QQ号 -> {name, traits[], todos[]}
        self.pending: dict[str, dict] = {}    # 想记住、在等管理员点头的
        self.declined: list[str] = []         # 管理员拒绝过的，别再问
        self.legacy: list[str] = []           # 旧格式（按昵称）迁过来的
        self.since: dict[str, int] = {}
        self._last_reset = ""
        self.load()
        self.ensure_admins()

    # ── 存取 ────────────────────────────────
    def load(self) -> None:
        data = read_json_dict(self.path, "长期记忆")
        self.people = {str(k): v for k, v in (data.get("people") or {}).items()
                       if isinstance(v, dict)}
        self.pending = {str(k): v for k, v in (data.get("pending") or {}).items()
                        if isinstance(v, dict)}
        self.declined = [str(x) for x in (data.get("declined") or [])]
        old = data.get("facts") or []
        for f in old if isinstance(old, list) else []:
            who = str(f.get("who") or "").strip()
            fact = str(f.get("fact") or "").strip()
            if who and fact and len(self.legacy) < 20:
                self.legacy.append(f"{who}：{fact}")
        self._last_reset = str(data.get("last_reset", ""))
        if old:
            self.save()          # 落一次新格式，旧 facts 从此作废

    def save(self) -> None:
        write_json_dict(self.path, {
            "people": self.people, "pending": self.pending,
            "declined": self.declined, "legacy": self.legacy,
            "last_reset": self._last_reset,
        }, "长期记忆")

    def ensure_admins(self) -> None:
        """管理员默认就记着，不用走审批；而且 pin 住，**一直保留、不会被裁剪掉**。"""
        changed = False
        for uid in CFG.get("admin", {}).get("user_ids") or []:
            uid = str(uid)
            if uid not in self.people:
                self.people[uid] = {"name": "", "traits": [], "todos": [], "updated": 0.0}
                changed = True
            if not self.people[uid].get("pinned"):
                self.people[uid]["pinned"] = True
                changed = True
        if changed:
            self.save()

    # ── 查询 ────────────────────────────────
    def knows(self, uid) -> bool:
        return str(uid) in self.people

    def as_dict(self) -> dict:
        """只读快照，给控制台用。"""
        return {
            "enable": self.enable,
            "reset_hour": self.reset_hour,
            "every_days": self.every_days,
            "extract": self.extract,
            "extract_every": self.every,
            "last_reset": self._last_reset,
            "path": str(self.path),
            "counts": {"people": len(self.people), "pending": len(self.pending),
                       "declined": len(self.declined), "legacy": len(self.legacy)},
            "people": self.people,
            "pending": self.pending,
            "declined": self.declined,
            "legacy": self.legacy,
            # 每轮现算的临时块（不落盘）：抽几条当前活跃会话给面板看
            "ephemeral": [
                {"key": k, "text": self.ephemeral(k)}
                for k in sorted(CHAT.sessions, key=lambda k: -float(CHAT.last_reply_at.get(k) or 0))[:3]
            ],
            "ephemeral_note": "这些不落盘，每轮从最近几条消息现算 —— 不重要的信息不进长期记忆。",
        }

    def render(self, speaker_id=None) -> str:
        """给系统提示词用的记忆块。当前说话的人排前面，方便她想起来是谁。"""
        if not self.enable:
            return ""
        uid = str(speaker_id) if speaker_id else ""
        items = sorted(self.people.items(),
                       key=lambda kv: (str(kv[0]) != uid, -float(kv[1].get("updated") or 0)))
        lines: list[str] = []
        for q, p in items[:8]:
            bits: list[str] = []
            if p.get("traits"):
                bits.append("、".join(p["traits"][:3]))
            if p.get("todos"):
                bits.append("待办：" + "、".join(p["todos"][:3]))
            if bits:
                lines.append(f"{p.get('name') or '某人'}（{q}）：" + "；".join(bits))
        if self.legacy:
            lines.append("（早年零碎记的：" + "；".join(self.legacy[-5:]) + "）")
        return "\n".join(lines)

    def fact_texts(self) -> list[str]:
        """人物记忆里**每一条**说法（给群记忆去重用）。

        两套记忆可能记到同一件事上，提示词里就会各说一遍；
        这里把人物记忆的条目摊平，交给 `GroupMemory.render(skip=...)` 比对。
        """
        out: list[str] = []
        for _q, p in self.people.items():
            for t in (p.get("traits") or []):
                out.append(str(t))
            for t in (p.get("todos") or []):
                out.append(str(t))
        out.extend(self.legacy)
        return [x for x in out if x]

    def ephemeral(self, key: str) -> str:
        """**每轮现算**的临时块 —— 不落盘、不进长期记忆。

        抽取时会滤掉"闲聊、当时的话题、气氛"这类不重要的东西（它们不该占长期记忆的名额），
        但也不能当她没看见。所以改成每轮从最近几条里现算一遍：谁在说、在说什么、几个人。
        永远是当下的 —— 不像缓存下来的摘要那样会过期变味。
        """
        hist = list(CHAT.sessions.get(key) or [])
        if not hist:
            return ""
        recent = [m for m in hist
                  if m.get("role") == "user" and isinstance(m.get("content"), str)][-5:]
        if not recent:
            return ""
        people: list[str] = []
        said: list[str] = []
        for m in recent:
            c = str(m.get("content") or "").strip()
            mm = re.match(r"^(.{1,12}?)\((\d+)\)[：:]\s*(.*)$", c)
            if mm:
                who, body = mm.group(1), mm.group(3).strip()
                if who not in people:
                    people.append(who)
            else:
                body = c
            if body:
                said.append(body[:18])
        if not (people or said):
            return ""
        bits = []
        if people:
            bits.append("／".join(people[:4]) + " 最近在说话")
        if said:
            bits.append("刚聊到：" + " / ".join(said[-3:]))
        return ("（眼下：" + "；".join(bits) +
                "。这些是当下的事，别当成长期记忆反复提。）")

    # ── 增删 ────────────────────────────────
    def add_person(self, uid, name: str = "") -> None:
        uid = str(uid)
        p = self.people.setdefault(uid, {"name": "", "traits": [], "todos": [], "updated": 0.0})
        if name and not p.get("name"):
            p["name"] = name
        p["updated"] = time.time()

    def propose(self, uid, name: str, why: str) -> bool:
        """想记住一个还不在记忆里的人 —— 先挂起，等管理员点头。"""
        uid = str(uid)
        if uid in self.people or uid in self.pending or uid in self.declined:
            return False
        self.pending[uid] = {"name": name, "why": (why or "")[:40], "ts": time.time()}
        self.save()
        return True

    def approve(self, uid) -> str:
        """管理员同意了：把当初的理由顺手留成第一条特点。"""
        p = self.pending.pop(str(uid), None) or {}
        name = str(p.get("name") or "")
        self.add_person(uid, name)
        if p.get("why"):
            self.add_trait(uid, name, str(p["why"]))
        self.save()
        return name or str(uid)

    def decline(self, uid) -> None:
        uid = str(uid)
        self.pending.pop(uid, None)
        if uid not in self.declined:
            self.declined.append(uid)
        self.save()

    def _clip(self, p: dict, key: str, cap: int) -> None:
        """裁剪。被 pin 住的人（管理员）一条都不删 —— 他的记忆一直留着。"""
        if p.get("pinned"):
            return
        lst = p.get(key) or []
        if len(lst) > cap:
            del lst[cap:]      # 丢最新的：既然以最开始的为准，就别把最早那条挤掉

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", s or "")

    @classmethod
    def _conflict_at(cls, new: str, old: list[str]) -> int:
        """粗判"同一件事冒出两种说法"：字面有重合，但一边是否定、另一边不是。

        返回冲突项的下标，没有则 -1。真正的语义判断交给抽记忆时的模型，
        这里只是兜底 —— 万一模型漏判了，至少不会把最早那条冲掉。
        """
        n = cls._norm(new)
        if not n:
            return -1
        neg_new = any(w in n for w in ("不", "没", "讨厌", "拒绝", "别", "戒"))
        for i, o in enumerate(old):
            m = cls._norm(o)
            if not m or not (set(n) & set(m)):
                continue
            common = len(set(n) & set(m))
            if common < 2:
                continue
            neg_old = any(w in m for w in ("不", "没", "讨厌", "拒绝", "别", "戒"))
            # 同一件事（字面有重合）却一个肯定一个否定 —— 就算冲突。
            # 别再拿比例卡：卡太严会漏掉"不吃香菜 / 其实吃香菜"这种最典型的
            if neg_new != neg_old:
                return i
        return -1

    def add_trait(self, uid, name: str, trait: str, by_self: bool = False) -> bool:
        """记一条特点。

        冲突时**以最开始记的为准** —— 道听途说的新说法不改旧账；
        除非是**他本人亲口说的**（by_self），那才按他说的改。
        """
        trait = (trait or "").strip()
        if not trait or len(trait) > 40:
            return False
        uid = str(uid)
        self.add_person(uid, name)
        p = self.people[uid]
        old = p["traits"]
        if trait in old:
            return False

        idx = self._conflict_at(trait, old) if by_self else -1
        if by_self and idx >= 0:
            logger.info("本人亲口纠正，改掉旧记忆：%s -> %s", old[idx], trait)
            old[idx] = trait
        elif not by_self and self._conflict_at(trait, old) >= 0:
            # 别人说的、又和最早记的那条打架 —— 以最早为准，这条不记
            logger.info("记忆冲突，以最早那条为准，跳过：%s", trait)
            return False
        else:
            old.append(trait)
        self._clip(p, "traits", self.max_traits)
        p["updated"] = time.time()
        self.save()
        return True

    def add_todo(self, uid, name: str, todo: str) -> bool:
        todo = (todo or "").strip()
        if not todo or len(todo) > 40:
            return False
        uid = str(uid)
        self.add_person(uid, name)
        p = self.people[uid]
        if todo in p["todos"]:
            return False
        p["todos"].append(todo)
        self._clip(p, "todos", self.max_todos)
        p["updated"] = time.time()
        self.save()
        return True

    def finish_todo(self, uid, keyword: str) -> None:
        """办完就划掉，别一直挂着显得很啰嗦。"""
        p = self.people.get(str(uid))
        if not p:
            return
        left = [t for t in p["todos"] if keyword not in t]
        if len(left) != len(p["todos"]):
            p["todos"] = left
            p["updated"] = time.time()
            self.save()

    # ── 抽取 ────────────────────────────────
    def note_activity(self, key: str) -> bool:
        if not self.enable or not self.extract:
            return False
        self.since[key] = self.since.get(key, 0) + 1
        return self.since[key] >= self.every

    def mark_extracted(self, key: str) -> None:
        self.since[key] = 0

    async def ask_admin(self, items: list[str]) -> None:
        """想记住新人，先问管理员一句。"""
        admins = CFG.get("admin", {}).get("user_ids") or []
        if not admins:
            return
        text = ("这几个人我想记住，行吗？\n" + "\n".join(f"  · {x}" for x in items)
                + "\n回我一句「可以」我就记，说「不行」我就忘了。")
        try:
            await send(False, None, int(admins[0]), None, text)
            logger.info("已问管理员要不要记住 %d 个人", len(items))
        except Exception as exc:
            logger.debug("问管理员失败：%s", exc)

    async def extract_from(self, key: str, history) -> None:
        """让她自己拿捏：这个人值不值得记、记他什么。"""
        convo = "\n".join(
            f"{m['role']}: {m['content']}" for m in history
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
        )
        if len(convo) < 40:
            self.mark_extracted(key)
            return
        known = self.render()
        is_group = str(key).startswith("g")
        gid = str(key)[1:] if is_group else ""
        # 群聊记忆和人物记忆是两套：这里额外问她"这个群本身"有没有值得长期记的
        gpart = ""
        if is_group and GMEM.enable:
            gpart = ("另外再问你一次：**这个群本身**有没有值得长期记住的事？"
                     "（群里怎么称呼彼此、有什么固定说法或梗、常聊什么、有什么不成文的规矩）"
                     "只跟这个群有关、跟某个人无关的才算；没有就留空。\n")
        prompt = (
            "你是" + her_name(key) + "。下面是刚才的聊天，你自己在心里过一遍：这些人里有谁值得你记住？\n"
            "只记两类，别的都别记：\n"
            "1) 这个人的特点 —— 脾气、习惯、喜欢什么讨厌什么、是干什么的。"
            "得是以后还能用得上的，随口一句不算；\n"
            "2) 你答应过他、或者他托你办的事 —— 只记还没做的。\n"
            "闲聊、吐槽、当时聊的话题一律不记 —— 那些是当下的事，不用你存。\n"
            + (f"\n（你目前已经记着的：\n{known}\n"
               "**已经记下的以最开始那条为准**，别人随口说的别拿来改旧账；\n"
               "但如果是**他本人亲口说的**，那就按他说的改 —— 这种条目标 self=true。）\n"
               if known else "")
            + gpart
            + '\n只输出 JSON：{"people":[{"qq":"QQ号","name":"昵称",'
              '"traits":[{"t":"一条","self":true}],"todos":["一条"],"done":["已经办完的事"]}],'
              '"group":["一条群记忆"]}\n'
            "traits 也允许直接写字符串（那就当作不是本人说的）；"
            "people 可以是空数组，group 也可以，都没什么就两个都留空。\n"
            "最多 3 个人；每人 traits 最多 2 条、todos 最多 2 条；group 最多 2 条；"
            "每条不超过 20 字。\n\n"
            + convo[-4000:]
        )
        try:
            reply, _ = await CHAT.answer("#mem", [{"role": "user", "content": prompt}])
            raw = re.sub(r"^```(?:json)?|```$", "", (reply or "").strip(), flags=re.M).strip()
            obj = json.loads(raw) if raw.startswith("{") else {}
            if isinstance(obj, list):
                obj = {"people": obj}          # 兼容旧格式（直接给数组）
            items = obj.get("people") or []
            gnotes = [str(x).strip() for x in (obj.get("group") or [])
                      if str(x).strip()][:2]
        except Exception as exc:
            logger.debug("抽记忆失败：%s", exc)
            items, gnotes = [], []

        asked: list[str] = []
        for it in (items or [])[:3]:
            if not isinstance(it, dict):
                continue
            qq = str(it.get("qq") or "").strip()
            name = str(it.get("name") or "").strip()
            if not qq.isdigit():
                continue
            # traits 允许两种写法：字符串（当别人说的）或 {"t":..., "self":true}（本人亲口）
            pairs: list[tuple[str, bool]] = []
            for x in (it.get("traits") or [])[:2]:
                if isinstance(x, dict):
                    t = str(x.get("t") or x.get("text") or "").strip()
                    if t:
                        pairs.append((t, bool(x.get("self"))))
                else:
                    t = str(x or "").strip()
                    if t:
                        pairs.append((t, False))
            todos = [str(x).strip() for x in (it.get("todos") or []) if str(x).strip()][:2]
            dones = [str(x).strip() for x in (it.get("done") or []) if str(x).strip()][:3]
            if not pairs and not todos and not dones:
                continue
            if self.knows(qq):
                for t, by_self in pairs:
                    self.add_trait(qq, name, t, by_self=by_self)
                for t in todos:
                    self.add_todo(qq, name, t)
                for d in dones:      # 他本人说办完了，就划掉
                    self.finish_todo(qq, d)
            else:
                why = pairs[0][0] if pairs else (todos[0] if todos else "")
                if why and self.propose(qq, name, why):
                    asked.append(f"{name or qq}（{qq}）—— {why}")
        # 群聊记忆不走管理员 —— 它记的是"这个群"，不是某个人，
        # 所以不需要人点头；留不留由条数上限和重置周期管。
        if gid and gnotes and GMEM.enable:
            for t in gnotes:
                GMEM.add(gid, t)
        if asked:
            await self.ask_admin(asked)
        self.mark_extracted(key)

    async def tick(self) -> None:
        if not self.enable:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self._last_reset == today:
            return
        # 不要求 now.hour 恰好等于 reset_hour：判定窗口只有一小时宽，巡检间隔
        # 一旦抖动、或进程在重置点之后才启动，就会整天错过，导致永不重置。
        # 改为「今天已过重置点、且今天还没重置过」即触发，可自然补做。
        if now.hour < self.reset_hour:
            return
        if self._last_reset:
            try:
                gap = (now.date() - datetime.strptime(self._last_reset, "%Y-%m-%d").date()).days
            except ValueError:
                gap = self.every_days
            if gap < self.every_days:
                return
        self._last_reset = today
        # ⚠️ 顺序很要紧：**先快照并清空，再慢慢抽取**。
        # 抽取是逐个会话发请求（每个都要 await 一次模型），几十个会话能跑几十秒；
        # 这段时间里 handle_message 还在往 CHAT.sessions 里塞新消息。
        # 以前是"先抽完再清"，结果这几十秒内进来的会话既没被抽取、又被 clear 掉，
        # 等于白聊了 —— 现在先取快照、立刻清空，新消息落到干净的 sessions 里等下一轮。
        pending = {}
        if self.extract:
            for key, hist in list(CHAT.sessions.items()):
                if len(hist) >= 4:
                    pending[key] = list(hist)
        CHAT.sessions.clear()
        CHAT.last_reply_at.clear()
        ATTENTION.groups.clear()
        for key, hist in pending.items():
            try:
                await self.extract_from(key, hist)
            except Exception as exc:
                logger.debug("重置前抽取 %s 失败（忽略）：%s", key, exc)
        # 群聊记忆跟人物记忆同一个周期一起清
        GMEM.reset()
        GMEM._last_reset = today
        GMEM.save()
        self.save()


def _text_overlap(a: str, b: str) -> bool:
    """两句话是不是在说同一件事（给记忆去重用）。

    不去分词，用"互相包含 + 字符重合率"两条粗判就够：
    记忆条目都是很短的一句话，真重复时字面几乎一样。
    """
    sa = re.sub(r"[^\w一-鿿]", "", str(a or ""))
    sb = re.sub(r"[^\w一-鿿]", "", str(b or ""))
    if not sa or not sb:
        return False
    if sa in sb or sb in sa:
        return True
    set_a, set_b = set(sa), set(sb)
    inter = len(set_a & set_b)
    return inter / max(1, min(len(set_a), len(set_b))) >= 0.8


class GroupMemory:
    """**群聊记忆**：这个群本身值得长期记住的事。

    和人物记忆分开、分开存（group_memory.json vs memory.json）：

      · **人物记忆**记"某个人是什么样的人"。**留不留由管理员决定** ——
        她觉得值得记的人先挂起（pending），管理员点头才落盘，拒绝过的不再问。
      · **群聊记忆**记"这个群是个什么群" —— 群里的黑话、梗、常聊的话题、
        不成文的规矩。它跟群走、不跟人走：换个人来说同一件事照样算数。

    两者都只做粗筛，真正怎么用交给提示词里的她。
    条数有上限，且和人物记忆共用同一个重置周期。
    """

    def __init__(self) -> None:
        cfg = CFG.get("memory", {})
        self.enable = bool(cfg.get("group_enable", True))
        self.max_notes = int(cfg.get("max_group_notes", 6))
        self.path = BASE / str(cfg.get("group_path", "group_memory.json"))
        self.groups: dict[str, dict] = {}
        self._last_reset = ""
        self.load()

    def load(self) -> None:
        data = read_json_dict(self.path, "群聊记忆")
        self.groups = {str(k): dict(v) for k, v in (data.get("groups") or {}).items()}
        self._last_reset = str(data.get("last_reset", ""))

    def save(self) -> None:
        write_json_dict(self.path, {
            "groups": self.groups,
            "last_reset": self._last_reset,
            "_说明": ("群聊记忆：每个群一条 notes 列表。跟群走不跟人走，"
                    "与人物记忆（memory.json）分开存。"),
        }, "群聊记忆")

    # ── 增删 ────────────────────────────────

    def add(self, group_id, text: str, name: str = "") -> bool:
        """记一条群记忆。重复的不记，超上限就丢最早的。"""
        if not self.enable:
            return False
        t = (text or "").strip()[:40]
        if len(t) < 4:
            return False
        g = str(group_id)
        rec = self.groups.setdefault(g, {"name": name or "", "notes": [], "updated": 0.0})
        if name and not rec.get("name"):
            rec["name"] = str(name)[:20]
        notes = rec.setdefault("notes", [])
        # 去重：完全一样的不重复记；近似（前 6 字相同）也当同一条
        head = t[:6]
        if any(str(n.get("t", ""))[:6] == head for n in notes):
            return False
        notes.append({"t": t, "ts": time.time()})
        if len(notes) > self.max_notes:
            del notes[:-self.max_notes]        # 超上限丢最早的
        rec["updated"] = time.time()
        self.save()
        logger.info("群聊记忆[%s] += %s", g, t)
        return True

    def remove_note(self, group_id, index: int) -> str:
        g = str(group_id)
        rec = self.groups.get(g)
        if not rec:
            raise ValueError(f"没有这个群的记忆：{g}")
        notes = rec.get("notes") or []
        if not (0 <= index < len(notes)):
            raise ValueError(f"下标越界：{index}（共 {len(notes)} 条）")
        gone = str(notes.pop(index).get("t", ""))
        if not notes:
            self.groups.pop(g, None)
        self.save()
        logger.info("群聊记忆[%s] -= %s", g, gone)
        return gone

    def forget(self, group_id) -> int:
        g = str(group_id)
        rec = self.groups.pop(g, None)
        self.save()
        return len((rec or {}).get("notes") or [])

    def reset(self) -> None:
        """跟人物记忆同一个周期清空（保留 last_reset 由调用方写）。"""
        self.groups.clear()
        self.save()

    # ── 读 ─────────────────────────────────

    def render(self, group_id, skip=()) -> str:
        """给系统提示词用的群记忆块。

        `skip`：已经在别处说过的文本（传人物记忆的条目进来）。
        同一件事可能被两套记忆各记一条 —— 人物记忆记"某人喜欢猫"、
        群记忆记"某人在群里说过喜欢猫"，两段都进提示词她就对同一件事说两遍。
        这里把跟人物记忆重复的那条剔掉（人物记忆按人走，更该留）。
        """
        if not self.enable:
            return ""
        rec = self.groups.get(str(group_id))
        if not rec:
            return ""
        notes = [str(n.get("t", "")) for n in (rec.get("notes") or []) if n.get("t")]
        if skip:
            pool = [s for s in skip if s]
            notes = [n for n in notes if not any(_text_overlap(n, s) for s in pool)]
        return "；".join(notes[-self.max_notes:])

    def as_dict(self) -> dict:
        return {
            "enable": self.enable,
            "max_notes": self.max_notes,
            "path": str(self.path),
            "last_reset": self._last_reset,
            "count": sum(len(v.get("notes") or []) for v in self.groups.values()),
            "groups": self.groups,
        }


class GroupBook:
    """**群名册**：她在哪些群、每个群叫什么。

    跟群聊记忆（group_memory.json）不是一回事：
      · 群聊记忆记"这个群有什么梗、什么规矩"（要模型抽取、归管理员管）；
      · 群名册只记"有哪些群、群号多少、叫什么名字"，是**跨群指令的解析目标**。

    管理员在私聊里说「把原神群里的张三禁言」，得先把"原神群"翻成群号，
    所以群名必须有一份权威来源 —— 就是这里。名字从 NapCat 的 get_group_list 拉。
    """

    def __init__(self) -> None:
        cfg = CFG.get("cross_group", {})
        self.enable = bool(cfg.get("enable", True))
        self.path = BASE / str(cfg.get("groups_path", "groups.json"))
        self.groups: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        d = read_json_dict(self.path, "群名册")
        self.groups = {str(k): dict(v) for k, v in (d.get("groups") or {}).items()}

    def save(self) -> None:
        write_json_dict(self.path, {
            "groups": self.groups,
            "_说明": "群名册：群号 -> {name, seen}。跨群指令靠它把群名翻成群号。",
        }, "群名册")

    def note(self, group_id, name: str = "") -> None:
        """见过这个群就记一笔。名字变了才落盘，免得每条消息都写文件。"""
        if not self.enable or not group_id:
            return
        g = str(group_id)
        rec = self.groups.get(g)
        if rec is None:
            self.groups[g] = {"name": str(name or "")[:40], "seen": time.time()}
            self.save()
            return
        rec["seen"] = time.time()
        if name and str(name)[:40] != rec.get("name"):
            rec["name"] = str(name)[:40]
            self.save()

    async def refresh(self) -> int:
        """从 NapCat 拉一次群列表，把群名补齐。NapCat 一连上就该调一次。"""
        if not self.enable:
            return 0
        try:
            r = await OB.call("get_group_list", {}, timeout=15)
        except Exception as exc:
            logger.debug("拉群列表失败：%s", exc)
            return 0
        data = r.get("data") if isinstance(r, dict) else None
        if not isinstance(data, list):
            return 0
        for it in data:
            # 只认对象项：接口偶尔会透出别的东西，别为一个畸形项把整次刷新炸掉
            if not isinstance(it, dict) or it.get("group_id") is None:
                continue
            self.note(it.get("group_id"), str(it.get("group_name") or ""))
        logger.info("群名册已刷新：%d 个群", len(self.groups))
        return len(data)

    # ── 读 ─────────────────────────────────

    def name_of(self, group_id) -> str:
        return str((self.groups.get(str(group_id)) or {}).get("name") or "")

    def label(self, group_id) -> str:
        """给提示词用的「群名（群号）」。没名字就只给群号。"""
        g = str(group_id)
        nm = self.name_of(g)
        return f"{nm}（{g}）" if nm else g

    def find(self, text: str) -> list[int]:
        """把「群名 / 群号」翻成候补群号列表，精确匹配排前面。

        名字带空格、带书名号、前后带语气词都尽量容忍；翻不出来就返回空列表 ——
        这时候**不能瞎猜一个群**去禁言。
        """
        key = (text or "").strip().strip("「」『』【】\"'“” ")
        key = re.sub(r"^(那个|这个|在|把|给|群|扣扣群)", "", key).strip()
        key = re.sub(r"(群|里面|里|群里)$", "", key).strip()
        if not key:
            return []
        if key.isdigit():
            return [int(key)]
        exact: list[int] = []
        loose: list[int] = []
        for g, rec in self.groups.items():
            nm = str(rec.get("name") or "").strip()
            if not nm:
                continue
            if nm == key:
                exact.append(int(g))
            elif key in nm or nm in key:
                loose.append(int(g))
        return exact + [g for g in loose if g not in exact]

    def lines(self) -> list[str]:
        """提示词里的群清单（按最近活跃排序）。"""
        items = sorted(self.groups.items(), key=lambda kv: -float(kv[1].get("seen") or 0))
        return [self.label(g) for g, _ in items]

    def as_dict(self) -> dict:
        return {
            "enable": self.enable,
            "path": str(self.path),
            "count": len(self.groups),
            "groups": [{"group_id": g, "name": str(v.get("name") or ""),
                        "seen": float(v.get("seen") or 0)}
                       for g, v in sorted(self.groups.items(),
                                          key=lambda kv: -float(kv[1].get("seen") or 0))],
        }


class SD:
    """调用本地 Stable Diffusion WebUI 的 API（需带 --api 启动）出图。"""

    def __init__(self) -> None:
        cfg = CFG.get("sd", {})
        ig_cfg = CFG.get("imagegen", {}) or {}
        # 两道闸：sd.enable（旧键，兼容）+ imagegen.enable（新总闸）。
        # 任一个关着就不画 —— 老用户只认 sd.enable，新用户可以用 imagegen.enable 一刀切。
        self.enable = bool(cfg.get("enable", False)) and bool(ig_cfg.get("enable", True))
        self.base_url = str(cfg.get("base_url", "http://127.0.0.1:7860")).rstrip("/")
        self.dir = BASE / str(cfg.get("dir", "generated"))
        self.timeout = float(cfg.get("timeout_seconds", 180))
        # ★ Pony 系模型必须带它自己的 score 前缀，否则不进入高质量构图模式、出图结构畸形。
        #   换模型时改 model_family 并替换 quality_prefix / negative。
        self.model_family = str(cfg.get("model_family", "pony")).lower()
        self.quality = str(cfg.get("quality_prefix", "") or "")
        self.negative = str(cfg.get("negative", "") or "")
        # ★ 她的固定形象**只在这里定义一次**，由 assemble() 统一拼接 ——
        #   绝不让模型自己写外貌：模型会写错发色、漏掉猫耳，再被这段一冲，
        #   就成了"每张图长得都不一样"。这是角色一致性的关键。
        self.char_tags = str(cfg.get("character_tags", "") or "")
        # 数量词单独放：duo 模式要把 solo 整个换掉（character_tags 里不能含数量词）
        self.solo_prefix = str(cfg.get("solo_prefix", "1girl, solo"))
        self.duo_prefix = str(cfg.get("duo_prefix", "2girls, two girls"))
        self.other_hint = str(cfg.get("other_person", "") or "")
        self.ban_solo = str(cfg.get("ban_solo", "") or "")
        self.ban_duo = str(cfg.get("ban_duo", "") or "")
        self.ban_scenery = str(cfg.get("ban_scenery", "") or "")
        _sz = cfg.get("size") or {}
        self.sizes = {
            "solo": [int(x) for x in (_sz.get("solo") or [832, 1216])],
            "duo": [int(x) for x in (_sz.get("duo") or [1216, 832])],
            "scenery": [int(x) for x in (_sz.get("scenery") or [1216, 832])],
        }
        _hr = cfg.get("hires") or {}
        self.hires = bool(_hr.get("enable", False))
        self.hires_scale = float(_hr.get("scale", 1.35))
        self.hires_denoise = float(_hr.get("denoising_strength", 0.32))
        self.hires_upscaler = str(_hr.get("upscaler", "R-ESRGAN 4x+ Anime6B"))
        # 三档额度：私聊不限（<=0）、群聊每 24 小时 max_group 张、说说每天 max_qzone 张
        self.max_private = int(cfg.get("max_per_day_private", 0))
        self.max_group = int(cfg.get("max_per_day_group", 10))
        self.max_qzone = int(cfg.get("max_per_day_qzone", 1))
        self.state_path = BASE / str(cfg.get("state_path", "sd_state.json"))
        self.state: dict = {"group": [], "qzone_date": "", "qzone": 0}
        self.load_state()
        # 出图串行化：SD WebUI 一次也只处理一张，且额度要先占后画（见 generate）
        self._draw_lock = asyncio.Lock()
        self.body_extra = {
            # Pony V6 XL 推荐 CFG 6~7 / steps 25~30（原来 7.0 + 24 步在边缘，偏容易崩）
            "steps": int(cfg.get("steps", 28)),
            "cfg_scale": float(cfg.get("cfg_scale", 6.5)),
            "sampler_name": str(cfg.get("sampler", "DPM++ 2M Karras")),
            "batch_size": 1,
            # width/height 改成按模式给（见 self.sizes / generate），不在这里写死
        }

    # ── 每日出图额度 ──────────────────────────
    def load_state(self) -> None:
        st = read_json_dict(self.state_path, "出图计数")
        self.state = {
            "group": [float(t) for t in (st.get("group") or [])],
            "qzone_date": str(st.get("qzone_date") or ""),
            "qzone": int(st.get("qzone") or 0),
        }

    def save_state(self) -> None:
        write_json_dict(self.state_path, self.state, "出图计数")

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _cap(self, purpose: str) -> int:
        """私聊不限量（0），群聊按 24 小时窗，说说按自然日。"""
        if purpose == "group":
            return self.max_group
        if purpose == "qzone":
            return self.max_qzone
        return self.max_private

    def used_today(self, purpose: str = "group") -> int:
        """群聊数最近 24 小时窗口内的张数；说说数今天；私聊恒 0（不限）。"""
        if purpose == "group":
            cut = time.time() - 24 * 3600
            kept = [t for t in self.state.get("group", []) if t > cut]
            if len(kept) != len(self.state.get("group", [])):
                self.state["group"] = kept
            return len(kept)
        if purpose == "qzone":
            if self.state.get("qzone_date") != self._today():
                return 0
            return int(self.state.get("qzone") or 0)
        return 0

    def can_draw(self, purpose: str = "group") -> bool:
        if not self.enable:
            return False
        cap = self._cap(purpose)
        if cap <= 0:          # 私聊：不限量
            return True
        return self.used_today(purpose) < cap

    def mark_drawn(self, purpose: str = "group") -> None:
        """占掉一张额度。

        ⚠️ 现在由 `generate()` 在**出图之前**调用（占位），不再是画完才计 ——
        详见 generate() 里的说明：出图几十秒，画完才计数会让并发请求一起挤进来。
        """
        if purpose == "group":
            self.state.setdefault("group", []).append(time.time())
        elif purpose == "qzone":
            self.state["qzone_date"] = self._today()
            self.state["qzone"] = self.used_today("qzone") + 1
        self.save_state()

    def unmark_drawn(self, purpose: str = "group") -> None:
        """把占掉的额度还回去（出图失败时用，别白扣她一张）。"""
        if purpose == "group":
            arr = list(self.state.get("group") or [])
            if arr:
                arr.pop()
                self.state["group"] = arr
        elif purpose == "qzone":
            self.state["qzone_date"] = self._today()
            self.state["qzone"] = max(0, int(self.used_today("qzone") or 0) - 1)
        else:
            return
        self.save_state()

    @staticmethod
    def _label(purpose: str) -> str:
        return {"private": "私聊", "group": "群聊", "qzone": "说说"}.get(purpose, purpose)

    def _quota_text(self, purpose: str) -> str:
        cap = self._cap(purpose)
        return f"{self.used_today(purpose)}/{cap} 张" if cap > 0 else "不限量"

    def snapshot(self) -> dict:
        """只读快照，给控制台用。含三档额度与最近产出文件。"""
        recent: list[dict] = []
        try:
            files = sorted((p for p in self.dir.glob("*")
                            if p.suffix.lower() in (".png", ".jpg", ".jpeg")),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:24]
            recent = [{"name": p.name, "size": p.stat().st_size,
                       "mtime": p.stat().st_mtime} for p in files]
        except OSError:
            pass
        return {
            "enable": self.enable,
            "base_url": self.base_url,
            "dir": str(self.dir),
            "caps": {"private": self.max_private, "group": self.max_group,
                     "qzone": self.max_qzone},
            "used": {"private": self.used_today("private"),
                     "group": self.used_today("group"),
                     "qzone": self.used_today("qzone")},
            "can_draw": {"private": self.can_draw("private"),
                         "group": self.can_draw("group"),
                         "qzone": self.can_draw("qzone")},
            "state": self.state,
            "recent_files": recent,
            # 出图模式：面板要看她这次能画成什么样
            "model_family": self.model_family,
            "quality_prefix": self.quality,
            "character_tags": self.char_tags,
            "solo_prefix": self.solo_prefix,
            "duo_prefix": self.duo_prefix,
            "sizes": {k: list(v) for k, v in self.sizes.items()},
            "hires": {"enable": self.hires, "scale": self.hires_scale,
                      "upscaler": self.hires_upscaler,
                      "denoising_strength": self.hires_denoise},
            "steps": self.body_extra.get("steps"),
            "cfg_scale": self.body_extra.get("cfg_scale"),
            "negative_count": len([x for x in self.negative.split(",") if x.strip()]),
            # 生图层（新增）：面板要能看出「现在走的是哪个后端、它可不可用、云端花了多少」
            "backend": IMAGE_ROUTER.status(),
        }

    def last_image(self) -> str | None:
        """翻一张以前生成过的图 —— 自发说说"绝不能空着"时用它兜底。"""
        try:
            files = [p for p in self.dir.glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")]
            return str(max(files, key=lambda p: p.stat().st_mtime)) if files else None
        except Exception:
            return None

    # ── 提示词编排：贴心情 + 只准她自己出镜 ──
    async def compose(self, intent: str, life: dict | None = None) -> dict:
        """让她决定这次画什么：只有景 / 只有她自己 / 两个人的合照。

        返回 {"mode","scene","other","negative_extra"}。

        ⚠️ 关键约定：**scene 里绝对不许写外貌**（发色、眼睛、耳朵、衣服、尾巴…），
        外貌一律由 assemble() 用配置里那份固定形象拼上去。
        早先让模型自己写外貌，它经常写错发色、漏掉猫耳，再被代码附加的固定形象一冲，
        结果就是"每张图里的她都不一样" —— 这是形象乱套的主因。
        """
        mood: list[str] = []
        if life:
            if life.get("time_str"):
                mood.append(f"现在：{life['time_str']}")
            if life.get("hint"):
                mood.append(f"她此刻的状态：{life['hint']}")
            ev = LIFE.event_for("draw")     # 出图用更低的概率，免得每张图都是同一件事
            if ev:
                mood.append(f"今天发生的小事：{ev}")
            if life.get("weather"):
                mood.append(f"天气：{life['weather']}")
        ask = (
            "你是绘图提示词助手。先判断她这次想画哪种画面，再写成英文 tag。\n"
            "三种模式：\n"
            "· solo —— 画面里只有她自己（自拍、日常、表达心情）；\n"
            "· duo —— 她和另一个朋友的合照（出去交朋友、一起玩）；\n"
            "· scenery —— 纯景物，画面里没有她也没有别人（风景、房间、物件、天气）。\n"
            "硬性规则：\n"
            "1) **绝对不要写任何外貌标签**（发色、发型、眼睛、耳朵、尾巴、衣服、肤色……）—— "
            "她的样子由系统统一拼接，你写了她就变样。你只管写：在做什么、什么表情、什么姿势、"
            "周围环境、光线、构图、氛围；\n"
            "2) duo 模式额外给一句「对方」的外貌 tag，必须和她明显不同"
            "（不同发色、不同发型、不同衣服）；\n"
            "3) 画面里最多两个人，绝不能出现第三个人；solo 模式下必须只有她一个人；\n"
            "4) 优先选不容易画崩手的姿势（手自然垂着、手插兜、手背在身后、双手撑着脸、"
            "一只手拿着东西），别写复杂的手部动作、别写手挡在脸前面；\n"
            "5) 逗号分隔的英文 tag，30 词以内。\n"
            '只输出 JSON：{"mode":"solo"|"duo"|"scenery","scene":"...",'
            '"other":"duo 时填对方外貌，否则留空","negative_extra":"这次要额外排除的，可留空"}\n\n'
            f"她想画：{intent}\n" + "\n".join(mood)
        )
        try:
            reply, _ = await CHAT.answer("#sdprompt", [{"role": "user", "content": ask}])
            m = re.search(r"\{.*\}", reply or "", re.S)
            if m:
                obj = json.loads(m.group(0))
                mode = str(obj.get("mode") or "").strip().lower()
                if mode not in ("solo", "duo", "scenery"):
                    mode = "solo"
                scene = str(obj.get("scene") or "").strip()
                if scene:
                    return {"mode": mode, "scene": scene,
                            "other": str(obj.get("other") or "").strip(),
                            "negative_extra": str(obj.get("negative_extra") or "").strip()}
        except Exception as exc:
            logger.debug("提示词编排失败，退回原文：%s", exc)
        # 编排失败就按"纯景物 + 原话"兜底：
        # 宁可这次画不出人物，也别拿没约束的提示词把她的形象画歪。
        return {"mode": "scenery", "scene": (intent or "").strip(),
                "other": "", "negative_extra": ""}

    def _art_of(self, art: dict | None) -> dict:
        """把角色卡的 art 字段映射成 assemble 要用的那份，缺的落回启动时读到的值。

        两边名字不一样是历史原因（卡里叫 character_tags / other_person / size，
        SD 内部叫 char_tags / other_hint / sizes），映射只在这一处做。
        """
        a = {"char_tags": self.char_tags, "solo_prefix": self.solo_prefix,
             "duo_prefix": self.duo_prefix, "other_hint": self.other_hint,
             "ban_solo": self.ban_solo, "ban_duo": self.ban_duo,
             "ban_scenery": self.ban_scenery, "sizes": self.sizes}
        if art:
            for src, dst in (("character_tags", "char_tags"), ("other_person", "other_hint"),
                             ("size", "sizes")):
                if art.get(src):
                    a[dst] = art[src]
            for k in ("solo_prefix", "duo_prefix", "ban_solo", "ban_duo", "ban_scenery"):
                if art.get(k):
                    a[k] = art[k]
        return a

    def assemble(self, pick: dict, art: dict | None = None) -> tuple[str, str, int, int]:
        """拼最终提示词。**外貌只从这里出** —— 这是"每张图里是同一个人"的唯一保证。

        三种模式拼法不同：
          · solo    : 质量 + 1girl/solo + **她的固定形象** + 场景
          · duo     : 质量 + 2girls + **她的固定形象** + 对方（要求明显不同）+ 场景
          · scenery : 质量 + 场景（负面里把所有人相关词全禁掉）

        `art` 传这一轮生效的那张角色卡的生图字段（persona.art_fields(会话键)）；
        不传就用启动时从配置读到的那份。**当前是哪张卡，画出来就是哪个人** ——
        这条通了之后，"换人设要重启" 才真正取消。宽高在同一次调用里定下来，
        不存在"出图途中尺寸被换掉"的问题。

        返回 (正面, 负面, 宽, 高)。宽高按模式取 —— SDXL 原生是 1024，
        低于它最容易出多余/错乱肢体（原来写死 768×768 就是"崩"的原因之一）。
        """
        # 没点名会话就用默认卡：这样"她自己发说说配图"那条路也走卡，不会和聊天里画出来的
        # 不是同一个人。卡里没写的字段再落回启动时读到的配置值（见 _art_of）。
        a = self._art_of(art if art is not None else PERSONA.art_fields(""))
        mode = str(pick.get("mode") or "solo")
        scene = str(pick.get("scene") or "").strip()
        if mode == "scenery":
            parts = [self.quality, scene]
            neg = [self.negative, a["ban_scenery"]]
        elif mode == "duo":
            parts = [self.quality, a["duo_prefix"], a["char_tags"],
                     a["other_hint"], str(pick.get("other") or "").strip(), scene]
            neg = [self.negative, a["ban_duo"]]
        else:
            mode = "solo"
            parts = [self.quality, a["solo_prefix"], a["char_tags"], scene]
            neg = [self.negative, a["ban_solo"]]
        extra = str(pick.get("negative_extra") or "").strip()
        if extra:
            neg.append(extra)

        def _join(xs) -> str:
            return ", ".join(s.strip() for s in xs if s and s.strip())

        wh = a["sizes"].get(mode) or [1024, 1024]
        return _join(parts), _join(neg), int(wh[0]), int(wh[1])

    # ── 出图（HTTP 交互全部交给 imagegen，这里只剩编排、配额与落盘） ──

    def reload(self, cfg: dict | None = None) -> list[str]:
        """从配置重读「跟后端绑」的那部分（开关 / 地址 / 超时）。

        角色字段（char_tags / sizes / *_prefix）**不在这里读**：它们现在由
        assemble() 每次从当前会话那张角色卡取（persona.art_fields(会话键)），
        所以换人设、换群的人设都不用重启 —— reload 里这份只是"卡里没写时的兜底"。
        宽高在同一次 assemble() 调用里定下来，不存在"出图途中尺寸被换掉"。

        为什么必须有这个方法：`self.enable` 是 `__init__` 里一次性读的，
        前端把「生图总闸」拨到 false 若不重读，开关**看着生效实际不生效**，且毫无报错。
        """
        c = cfg if cfg is not None else CFG
        sd_cfg = c.get("sd", {}) or {}
        ig_cfg = c.get("imagegen", {}) or {}
        new_enable = bool(sd_cfg.get("enable", False)) and bool(ig_cfg.get("enable", True))
        new_url = str(sd_cfg.get("base_url", "http://127.0.0.1:7860")).rstrip("/")
        new_timeout = float(sd_cfg.get("timeout_seconds", 180))
        changed = (new_enable != self.enable or new_url != self.base_url
                   or new_timeout != self.timeout)
        self.enable, self.base_url, self.timeout = new_enable, new_url, new_timeout
        if changed:
            logger.info("生图配置已重载：enable=%s base_url=%s timeout=%.0fs",
                        self.enable, self.base_url, self.timeout)
        return []

    def check_ready(self, purpose: str = "group", *, has_intent: bool = True,
                    low_spec: bool = False) -> tuple[bool, str, str]:
        """现在能不能画 —— **纯本机判断，不发任何请求**。

        返回 (ok, reason, why)。reason 见 imagegen/types.py 的字面量：
        `disabled`/`quota`/`empty-intent` 属正常态（上层静默），
        `no-endpoint`/`backend-down` 属故障态（上层给用户一句话 + 通知管理员）。
        """
        if not self.enable:
            return False, REASON_DISABLED, "生图总闸（sd.enable）关着"
        if not has_intent:
            return False, REASON_EMPTY, "没给要画什么"
        if not self.can_draw(purpose):
            why = f"{self._label(purpose)}额度已满（{self._quota_text(purpose)}）"
            return False, REASON_QUOTA, why
        return IMAGE_ROUTER.check_ready(purpose, low_spec=low_spec,
                                        enabled=True, has_intent=True)

    async def generate_outcome(self, intent: str, life: dict | None = None,
                               purpose: str = "group", art: dict | None = None) -> GenOutcome:
        """出图并返回**结构化结果**（含失败原因，供提示与管理员通知用）。

        `art`：这一轮生效的那张角色卡的生图字段（persona.art_fields(会话键)），
        决定画出来的是谁。不传就用启动时读到的那份。
        """
        if not self.enable or not (intent or "").strip():
            if not self.enable:
                return GenOutcome(ok=False, reason=REASON_DISABLED, detail="生图总闸关着")
            return GenOutcome(ok=False, reason=REASON_EMPTY, detail="没给要画什么")
        # ⚠️ 出图要走**锁 + 先占位**：出一张要几十秒，画完才计数会让并发请求一起挤进来。
        async with self._draw_lock:
            if not self.can_draw(purpose):
                logger.info("%s出图额度已满（%s），这次不画",
                            self._label(purpose), self._quota_text(purpose))
                return GenOutcome(ok=False, reason=REASON_QUOTA,
                                  detail=f"{self._label(purpose)}额度已满"
                                         f"（{self._quota_text(purpose)}）")
            self.mark_drawn(purpose)          # 先占住，别让并发的请求挤进来
            ok = False
            try:
                outcome = await self._draw_locked(intent, life, purpose, art)
                ok = outcome.ok
                return outcome
            finally:
                # ★ 退额度**只有这一处**出口。老代码在 _draw_locked 与 generate 各退一次，
                #   任何在中间抛异常的改动都会多退一张（sd_state.json 凭空少一张）。
                if not ok:
                    self.unmark_drawn(purpose)

    async def generate(self, intent: str, life: dict | None = None,
                       purpose: str = "group") -> str | None:
        """兼容入口：返回图片路径或 None（老调用方与测试桩还在用这个名字）。"""
        out = await self.generate_outcome(intent, life, purpose)
        return out.path or None

    async def _draw_locked(self, intent: str, life: dict | None = None,
                           purpose: str = "group", art: dict | None = None) -> GenOutcome:
        """真正的出图流程（调用方已持锁并占好额度）。`art` 见 assemble()。"""
        pick = await self.compose(intent, life)
        pos, neg, w, h = self.assemble(pick, art)
        self.dir.mkdir(parents=True, exist_ok=True)
        req = GenRequest(prompt=pos, negative=neg, width=w, height=h,
                         params=dict(self.body_extra), purpose=purpose,
                         intent=intent, mode=str(pick.get("mode") or "solo"))
        # SD 在重载模型 / 切模型 / 忙的时候会短暂 404 或超时 ——
        # 实测出过：第一张成功、后两张直接 404（WebUI 刚好在重载）。隔几秒重试一次。
        out = GenOutcome(ok=False, reason=REASON_FAILED, detail="没有可用生图端点")
        for attempt in (1, 2):
            out = await IMAGE_ROUTER.generate(req, chain="image")
            if out.ok:
                break
            if out.benign:
                break                      # 正常态（额度/关着）不必重试
            if attempt == 1:
                logger.warning("出图失败（%s：%s），5 秒后重试一次", out.reason, out.detail)
                await asyncio.sleep(5)
            else:
                logger.warning("出图再次失败，这次放弃：%s / %s", out.reason, out.detail)
        if not out.ok:
            return out
        path = self.dir / f"{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
        try:
            path.write_bytes(out.data)
        except OSError as exc:
            return GenOutcome(ok=False, reason=REASON_FAILED,
                              detail=f"图片写盘失败（{exc}）", endpoint_id=out.endpoint_id)
        mode = str(pick.get("mode") or "solo")
        MODE_CN = {"solo": "只有她", "duo": "她+朋友", "scenery": "纯景物"}
        ACTIVITY.note("画图", f"画了「{intent[:36]}」（{MODE_CN.get(mode, mode)}·{self._label(purpose)}）")
        logger.info("出图成功（%s｜%s %dx%d）来自后端 %s：%s",
                    MODE_CN.get(mode, mode), self._label(purpose), w, h,
                    out.endpoint_id or "?", path.name)
        out.path = str(path)
        out.detail = str(path)
        return out


SDGEN = SD()
DRAW_TAG = re.compile(r"\[画图[:：]?([^\]]*)\]")

# "这是在要图"的常见说法。她嘴硬不肯给时靠这个兜底再要一次。
PICTURE_ASK = re.compile(
    r"(自拍|拍照|拍张|拍个|发张图|发张自拍|发个图|看看你|瞧瞧你|长什么样|什么样了|"
    r"画一张|画个图|给我看看|给我瞧瞧|拍给我|发给我看看)")

# 「刚发出去这张图，该当照片还是当画」—— 它决定她怎么描述这张图，不是小事：
# 用户说"自拍"却听她说"我刚画的"，人设当场就崩了（画是她的技能，照片是她本人）。
# 判断只看**画面描述里有没有照片/画画的字眼**，两边都有就当画（说了"画"就按画走）。
PHOTO_ASK = re.compile(
    r"(自拍|拍照|拍张|拍个|拍给|照片|相片|看看你|瞧瞧你|长什么样|什么样了|"
    r"给我看看|给我瞧瞧)")
DRAW_ASK = re.compile(r"(画|绘|素描|涂鸦)")


def photo_frame(text: str) -> bool:
    """这张图对外该说成"你拍的"还是"你画的"。True = 照片。"""
    s = text or ""
    return bool(PHOTO_ASK.search(s)) and not bool(DRAW_ASK.search(s))


class QzoneAPI:
    """通过本地 qzone-bridge（OneBot 兼容 HTTP）读 QQ 空间动态。写发仍走 NapCat 原生接口。"""

    def __init__(self) -> None:
        cfg = CFG.get("qzone", {})
        self.enable = bool(cfg.get("bridge_enable", True))
        self.base = str(cfg.get("bridge_url", "http://127.0.0.1:5700")).rstrip("/")
        self.token = str(cfg.get("bridge_token", "") or "")
        self.timeout = float(cfg.get("bridge_timeout_seconds", 30))
        self._feed_cache: list[dict] = []
        self._feed_ts = 0.0
        self._like_ts = 0.0
        self._comment_day = ""
        self._comment_count = 0
        # 同一条说说只评论一次：tid -> {ts, uin, name}。必须落盘 ——
        # 只在内存里记的话，重启一次同一批说说又会被评一遍。
        self.comment_state_path = BASE / str(
            cfg.get("comment_state_path", "qzone_comment_state.json"))
        self._commented: dict[str, dict] = {}
        self._load_commented()

    async def call_raw(self, action: str, params: dict | None = None) -> tuple[bool, object]:
        """返回 (是否成功, data)。成功但 data 为 null 的情况也要能区分，所以不能只看 data。"""
        if not self.enable:
            return False, None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            r = await CHAT.client_for(self.base).post(
                f"{self.base}/{action}", json=params or {}, headers=headers, timeout=self.timeout)
            body = r.json()
        except Exception as exc:
            logger.debug("桥接 %s 不可用：%s", action, exc)
            return False, None
        if body.get("status") == "failed" or body.get("retcode") not in (0, None):
            logger.warning("桥接 %s 失败：%s", action, body.get("message") or body.get("wording"))
            return False, None
        return True, body.get("data")

    async def call(self, action: str, params: dict | None = None) -> dict | None:
        ok, data = await self.call_raw(action, params)
        return data if ok else None

    async def raw_feeds(self, num: int = 10, friend: bool = True) -> list[dict]:
        """取动态原始条目，带短缓存。点赞要用这里的元数据。"""
        ttl = float(CFG.get("qzone", {}).get("feed_cache_seconds", 30))
        now = time.time()
        if self._feed_cache and now - self._feed_ts < ttl:
            return self._feed_cache
        action = "get_friend_feeds" if friend else "get_emotion_list"
        params: dict = {"num": num, "include_image_data": False}
        if not friend:
            params.update({"user_id": OB.self_id, "pos": 0})
        data = await self.call(action, params)
        self._feed_cache = (data or {}).get("msglist") or []
        self._feed_ts = now
        if self._feed_cache:
            logger.info("读到 %d 条动态（已填充桥接的帖子元数据缓存）", len(self._feed_cache))
        return self._feed_cache

    async def user_posts(self, uin: int, num: int = 3) -> list[dict]:
        """读某个人的空间主页。好友动态只有最近几条，翻旧帖或找非好友时得走这里。"""
        data = await self.call("get_emotion_list",
                               {"user_id": uin, "pos": 0, "num": num, "include_image_data": False})
        return (data or {}).get("msglist") or []

    async def find_post(self, who: str = "") -> dict | None:
        """找某人最新一条动态。点赞和评论都要用，所以抽出来共用。

        好友动态只有最近几条，里面没有这个人（非好友/发得少）就去他空间主页找。
        """
        who = (who or "").strip()

        def match(it: dict) -> bool:
            if str(it.get("uin")) == str(OB.self_id):
                return False          # 不碰自己的
            if not who:
                return True
            return who in str(it.get("nickname") or "") or who == str(it.get("uin"))

        for it in await self.raw_feeds(10):
            if match(it):
                return it
        if who:
            uid = nickname_to_uin(who)
            if uid is None and who.isdigit():
                uid = int(who)
            if uid:
                logger.info("好友动态里没有 %s 的说说，改去他空间主页找", who)
                for it in await self.user_posts(uid):
                    if match(it):
                        return it
        return None

    # ── 同一条动态只评论一次 ────────────────
    #
    # 桥接侧**不提供**"这条我评过没有"的每帖标记（只有 isLiked 管点赞），
    # 所以这里自己记一份 tid 名单，落盘保存。判断只认 tid：
    # 好友动态里"某人最新一条"会连续命中同一条，不去重就会反复评同一帖。
    def _load_commented(self) -> None:
        d = read_json_dict(self.comment_state_path, "空间评论记录")
        self._commented = {str(k): dict(v) for k, v in (d.get("posts") or {}).items()}

    def _save_commented(self) -> None:
        """落盘前顺手修剪：太老的、超出条数上限的都丢掉，文件不会无限长。"""
        cfg = CFG.get("qzone", {})
        keep = int(cfg.get("comment_memory_keep", 300))
        days = float(cfg.get("comment_memory_days", 90))
        cutoff = time.time() - days * 86400
        items = [(k, v) for k, v in self._commented.items()
                 if float(v.get("ts") or 0) >= cutoff]
        items.sort(key=lambda kv: -float(kv[1].get("ts") or 0))
        self._commented = dict(items[:keep] if keep > 0 else items)
        write_json_dict(self.comment_state_path, {
            "posts": self._commented,
            "_说明": "同一条说说只评论一次：tid -> {ts, uin, name}。落盘，重启不作废。",
        }, "空间评论记录")

    def commented_tid(self, tid) -> bool:
        return str(tid or "") in self._commented

    def mark_commented(self, tid, uin="", name="") -> None:
        t = str(tid or "")
        if not t:
            return
        self._commented[t] = {"ts": time.time(), "uin": str(uin or ""),
                              "name": str(name or "")[:20]}
        self._save_commented()

    def commented_recent(self, num: int = 8) -> list[dict]:
        items = sorted(self._commented.items(),
                       key=lambda kv: -float(kv[1].get("ts") or 0))
        return [{"tid": k, "name": str(v.get("name") or ""),
                 "uin": str(v.get("uin") or ""), "ts": float(v.get("ts") or 0)}
                for k, v in items[:num]]

    def _comments_today(self) -> int:
        if self._comment_day != datetime.now().strftime("%Y-%m-%d"):
            return 0
        return self._comment_count

    def _mark_comment(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._comment_day != today:
            self._comment_day, self._comment_count = today, 0
        self._comment_count += 1

    async def comment_post(self, who: str, content: str) -> tuple[bool, str]:
        """给某人最新一条说说评论。同一条只有第一次会真的发出去。"""
        cfg = CFG.get("qzone", {})
        if not cfg.get("comment_enable", True):
            return False, "评论功能关着"
        content = (content or "").strip()[: int(cfg.get("comment_max_chars", 60))]
        if not content:
            return False, "没想好说什么"
        cap = int(cfg.get("comment_max_per_day", 10))
        if cap > 0 and self._comments_today() >= cap:
            return False, f"今天评得太多了，先歇歇"

        target = await self.find_post(who)
        if target is None:
            return False, (f"没翻到{who}的说说" if who else "没有可以评论的说说")
        name = target.get("nickname") or str(target.get("uin"))
        tid = str(target.get("tid") or "")
        # 同一条说说的第二次评论一律不发 —— 这是"只在同一条动态下评论一次"的唯一落点，
        # 被谁要求都一样（她自主互动、用户点名要她评论，都走这里）
        if cfg.get("comment_once_per_post", True) and self.commented_tid(tid):
            logger.info("这条说说已经评过了，不再评：%s tid=%s", name, tid)
            return False, f"{name}那条已经评过一次了"
        params = {
            "target_tid": tid,
            "content": content,
            "target_uin": str(target.get("uin") or target.get("opuin") or ""),
            "abstime": int(target.get("created_time") or 0),
            "appid": int(target.get("appid") or 311),
        }
        ok, _ = await self.call_raw("send_comment", params)
        if ok:
            self._mark_comment()
            self.mark_commented(tid, params["target_uin"], name)
            ACTIVITY.note("评论", f"在{name}的说说下评了「{content[:30]}」")
            logger.info("评论成功：%s「%s」| tid=%s", name, content[:30], tid)
            return True, f"{name}（{(target.get('content') or '')[:20]}）"
        logger.warning("评论失败：%s | 参数=%s", name, params)
        return False, "评论没发出去"

    async def like_post(self, who: str = "") -> tuple[bool, str]:
        """给某人最新一条说说点赞。

        关键：点赞必须带上 abstime/appid/typeid/unikey/curkey，这些只在动态列表里有。
        只传 tid 的话桥接缓存命中不到，就会用 abstime=0 发出去，QQ 会回「读取不到作者说说」。
        """
        cfg = CFG.get("qzone", {})
        if not cfg.get("like_enable", True):
            return False, "点赞功能已关闭"
        last = self._like_ts
        gap = float(cfg.get("like_cooldown_seconds", 60))
        if last and time.time() - last < gap:
            return False, "刚点过，缓一会儿"

        items = await self.raw_feeds(10)
        who = (who or "").strip()

        def liked(it: dict) -> bool:
            v = it.get("isLiked")
            return v is True or str(v).lower() in ("true", "1")

        def match(it: dict) -> bool:
            if str(it.get("uin")) == str(OB.self_id):
                return False  # 不给自己点赞
            if not who:
                return True
            return who in str(it.get("nickname") or "") or who == str(it.get("uin"))

        # 只认"他最新那条"：往回翻旧帖去凑点赞不是她要的效果
        newest = None
        for it in items:
            if match(it):
                newest = it
                break

        # 好友动态只有最近几条，里面没有这个人（非好友/发得少）就去他空间主页找他最新一条
        if newest is None and who:
            uid = nickname_to_uin(who)
            if uid is None and who.isdigit():
                uid = int(who)
            if uid:
                logger.info("好友动态里没有 %s 的说说，改去他空间主页找", who)
                for it in await self.user_posts(uid):
                    if match(it):
                        newest = it
                        break

        if newest is None:
            return False, f"没找到{who}的说说" if who else "没有可以点赞的说说"
        if liked(newest):
            name = newest.get("nickname") or who or "他"
            return False, f"{name}最新那条之前就点过了"
        target = newest

        params: dict = {
            "user_id": int(target.get("uin") or target.get("opuin") or 0) or 0,
            "tid": str(target.get("tid") or ""),
            "abstime": int(target.get("created_time") or 0),
            "appid": int(target.get("appid") or 311),
            "typeid": int(target.get("typeid") or 0),
        }
        if target.get("likeUnikey"):
            params["unikey"] = str(target["likeUnikey"])
        if target.get("likeCurkey"):
            params["curkey"] = str(target["likeCurkey"])

        ok, _ = await self.call_raw("send_like", params)
        name = target.get("nickname") or str(target.get("uin"))
        if ok:
            self._like_ts = time.time()
            ACTIVITY.note("点赞", f"给{name}点了个赞")
            logger.info("点赞成功：%s | tid=%s abstime=%s", name, params["tid"], params["abstime"])
            return True, f"{name}（{(target.get('content') or '')[:20]}）"
        logger.warning("点赞失败：%s | 参数=%s", name, {k: v for k, v in params.items() if k != "unikey"})
        return False, f"{name} 那条没点成"

    async def feed_lines(self, num: int = 6, friend: bool = True) -> list[str]:
        """把动态整理成人能读的几行。取数统一走 raw_feeds，别再自己拼一遍参数。"""
        items = await self.raw_feeds(num, friend)
        lines: list[str] = []
        for it in items[:num]:
            txt = (it.get("content") or "").replace("\n", " ").strip()
            if not txt:
                continue
            mine = str(it.get("uin")) == str(OB.self_id)
            who = "你自己的" if mine else (it.get("nickname") or it.get("uin") or "某人")
            lines.append(f"{who}：{txt[:90]} ［{it.get('createTime', '')} 赞{it.get('likenum', 0)} "
                         f"评{it.get('cmtnum', 0)}］")
        return lines

    async def feed_text(self, num: int = 6, friend: bool = True) -> str:
        return "\n".join(await self.feed_lines(num, friend))


QZONE_API = QzoneAPI()
QZONE_READ_TAG = re.compile(r"\[空间[:：]?([^\]]*)\]")
LIKE_TAG = re.compile(r"\[点赞[:：]?([^\]]*)\]")
# [评论:昵称 内容] 或 [评论:昵称|内容]；不写昵称就是对最新那条说
COMMENT_TAG = re.compile(r"\[评论[:：]?\s*([^\]]*)\]")


class Web:
    """让她能自己上网：抓 Bing 结果摘要，够她聊几句就行。"""

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

    def __init__(self) -> None:
        cfg = CFG.get("web", {})
        self.enable = bool(cfg.get("enable", True))
        self.timeout = float(cfg.get("timeout_seconds", 12))
        self.max_results = int(cfg.get("max_results", 5))

    def _strip(self, s: str) -> str:
        return ihtml.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

    async def search(self, query: str) -> list[dict]:
        """从站点池里**随机挑几个**搜，再合并结果；池子为空则全网搜。

        随机挑是为了让她的取材料有变化（不然每次都是同几个站）。
        注意：cn.bing.com 会无视 site: 操作符，所以不能靠它限定站点，
        得走各站自己的接口，失败才退回"Bing 搜完按域名过滤"。
        """
        if not self.enable or not query:
            return []
        cfg = CFG.get("web", {})
        pool = [str(s).lower().lstrip(".") for s in (cfg.get("sites") or [])]
        if not pool:
            return await self._bing(query, self.max_results)
        pick = max(1, int(cfg.get("sites_per_search", 3)))
        sites = random.sample(pool, pick) if pick < len(pool) else pool
        per = max(1, -(-self.max_results // len(sites)))
        out: list[dict] = []
        for site in sites:
            out.extend(await self._site_search(site, query, per))
        if not out:
            # 挑的这几个站都没货。池里有些站 Bing 压根不返回，所以必须兜底 ——
            # 总比让她"上网逛了一圈什么都没看到"强
            logger.info("限定站点 %s 没搜到，退回全网搜：%s", sites, query)
            out = await self._bing(query, self.max_results)
        else:
            logger.debug("上网取材 %s 命中 %s", query, sites)
        return out[: self.max_results]

    async def _site_search(self, site: str, query: str, limit: int) -> list[dict]:
        if "bilibili.com" in site:
            items = await self._bilibili(query, limit)
            if items:
                return items
        # 只给真正取得到数据的站留专用接口；取不到的（GitHub 等）已整个删掉，
        # 统一走下面的 Bing 兜底，免得留一堆永远返回 0 条的死代码
        got = await self._bing(f"{query} {site.split('.')[0]}", limit * 3)
        kept = [r for r in got if site in r.get("url", "")]
        if not kept:
            logger.info("「%s」在 %s 上没搜到", query, site)
        return kept[:limit]

    async def _bilibili(self, query: str, limit: int) -> list[dict]:
        try:
            r = await CHAT.client_for("https://api.bilibili.com").get(
                "https://api.bilibili.com/x/web-interface/search/all/v2",
                params={"keyword": query}, headers={"User-Agent": self.UA}, timeout=self.timeout)
            data = (r.json() or {}).get("data") or {}
        except Exception as exc:
            logger.debug("B站搜索失败：%s", exc)
            return []
        out: list[dict] = []
        for block in data.get("result") or []:
            for it in (block.get("data") or []):
                url = it.get("arcurl") or it.get("url") or ""
                if not url and it.get("bvid"):
                    url = f"https://www.bilibili.com/video/{it['bvid']}"
                if not url and it.get("season_id"):
                    url = f"https://www.bilibili.com/bangumi/play/ss{it['season_id']}"
                if not url and it.get("mid"):
                    url = f"https://space.bilibili.com/{it['mid']}"
                title = self._strip(it.get("title", ""))
                if title and url:
                    out.append({"title": title[:80], "url": url,
                                "snippet": self._strip(it.get("description") or it.get("desc") or "")[:200]})
                if len(out) >= limit:
                    return out
        return out

    async def _bing(self, query: str, limit: int) -> list[dict]:
        if not self.enable or not query:
            return []
        try:
            r = await CHAT.client_for("https://cn.bing.com").get(
                "https://cn.bing.com/search",
                params={"q": query, "setlang": "zh-CN"},
                headers={"User-Agent": self.UA},
                timeout=self.timeout,
            )
            page = r.text
        except Exception as exc:
            logger.debug("搜索失败：%s", exc)
            return []

        items: list[dict] = []
        for m in re.finditer(r'<li class="b_algo".*?</li>', page, re.S):
            block = m.group(0)
            t = re.search(r"<h2[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.S)
            if not t:
                continue
            sn = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            items.append({
                "title": self._strip(t.group(2))[:80],
                "url": t.group(1)[:200],
                "snippet": self._strip(sn.group(1))[:200] if sn else "",
            })
            if len(items) >= limit:
                break
        logger.info("搜索「%s」拿到 %d 条", query, len(items))
        return items

    @staticmethod
    def digest(results: list[dict]) -> str:
        return "\n".join(f"- {r['title']}：{r['snippet']}" for r in results if r.get("title"))


WEB = Web()
WEB_TAG = re.compile(r"\[搜索[:：]?([^\]]*)\]")


# ── 链接阅读 ──────────────────────────────────────────────────────────
# 链接的终止字符：空白、尖引号、括号，以及**中文标点** —— 不排除中文标点的话，
# "http://b.cn/p，还有别的" 会把" ，还有别的"一起吞进 URL（实测踩过）。
_URL_STOP = "\"'<>）)】]|，。、；：！？（）【】《》「」“”‘’…·"
LINK_RE = re.compile(r"https?://[^\s" + re.escape(_URL_STOP) + r"]+", re.I)


def _is_private_host(host: str) -> bool:
    """是不是内网 / 本机地址。群里谁都可能贴一个让机器人去请求 —— SSRF 的经典入口。

    单独写成函数而不是一条正则：172.16~31 那段是区间，正则里写不干净；
    IPv6 又要先去方括号（`split(":")` 会把 [::1] 切成 "[", "", "1]"）。
    """
    h = (host or "").strip("[]").lower()
    if h in ("localhost", "::1", "0.0.0.0", "127.0.0.1"):
        return True
    if h.startswith(("127.", "10.", "192.168.", "169.254.")):
        return True
    m = re.match(r"^172\.(\d+)\.", h)
    return bool(m) and 16 <= int(m.group(1)) <= 31


class LinkReader:
    """把聊天里出现的链接抓回来读一遍。

    为什么要有它：以前代码一个 URL 都不认，用户丢个链接进来，她只能对着那串
    "https://…" 干瞪眼或者猜。现在把正文抓回来喂给她，她就能接着聊。

    边界（每条都有理由）：
      · 只认 http/https，一条消息最多抓 links.max_links 条（默认 2）；
      · 默认不抓内网地址（见 _PRIVATE_HOST_RE）；
      · 只吃 html/text 响应，超时 10 秒、正文最多 1200 字 —— 她不需要全文；
      · 抓不到就返回空，她照旧没素材，但这条消息**不会卡住**。
    """

    def __init__(self) -> None:
        cfg = CFG.get("links", {})
        self.enable = bool(cfg.get("enable", True))
        self.timeout = float(cfg.get("timeout_seconds", 10))
        self.max_chars = int(cfg.get("max_chars", 1200))
        self.max_links = int(cfg.get("max_links", 2))
        self.allow_private = bool(cfg.get("allow_private", False))

    @staticmethod
    def urls_in(text: str) -> list[str]:
        """消息里的链接，去重、去掉尾随标点（中文句子里的链接后面常跟标点）。"""
        out: list[str] = []
        seen: set[str] = set()
        for m in LINK_RE.finditer(text or ""):
            u = m.group(0).rstrip(".,;:!?、。，；：！？")
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def blocked(self, url: str) -> bool:
        """这个地址该不该跳过（内网 / 本机）。"""
        if self.allow_private:
            return False
        m = re.match(r"^https?://([^/]+)", url or "", re.I)
        host = (m.group(1) if m else "").split("@")[-1].strip()
        if host.startswith("["):          # IPv6：[::1]:8080
            host = host[1:host.find("]")] if "]" in host else host[1:]
        else:
            host = host.split(":")[0]     # IPv4 / 域名带端口
        return _is_private_host(host)

    def extract(self, html: str) -> str:
        """HTML → 「标题 + 正文片段」。纯函数，离线可测。"""
        html = html or ""
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if m:
            title = WEB._strip(m.group(1))[:80]
        body = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
        body = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", body)
        body = re.sub(r"\s{2,}", " ", WEB._strip(body)).strip()
        if not body:
            return ""
        if len(body) > self.max_chars:
            body = body[:self.max_chars] + "…"
        return f"《{title}》\n{body}" if title else body

    async def read(self, url: str) -> str:
        """抓一个链接并转成文字；抓不到返回空串。"""
        try:
            client = CHAT.client_for(url)
            r = await client.get(url, timeout=self.timeout, follow_redirects=True)
        except Exception as exc:
            logger.info("读链接失败：%s（%s）", url[:80], exc)
            return ""
        ctype = str(r.headers.get("content-type") or "").lower()
        if r.status_code >= 400 or not ("html" in ctype or "text" in ctype):
            logger.info("读链接跳过：HTTP %s / %s", r.status_code, ctype[:40])
            return ""
        return self.extract(r.text or "")

    async def note(self, text: str) -> str:
        """把消息里的链接读成一段可塞进上下文的话；没有链接或全读不到就返回空串。"""
        if not self.enable:
            return ""
        urls = self.urls_in(text)[:self.max_links]
        if not urls:
            return ""
        chunks: list[str] = []
        for u in urls:
            if self.blocked(u):
                chunks.append(f"{u}\n（这是内网地址，没去读）")
                continue
            got = await self.read(u)
            if got:
                chunks.append(f"{u}\n{got}")
        if not chunks:
            return ""
        return ("对方发的链接，内容是这样的（直接用就行，别提「我去读了网页」这种话）：\n"
                + "\n\n".join(chunks))


LINKS = LinkReader()


# ══════════════════════ 识图：引擎选择 ══════════════════════
#
# 以前"能不能看图"只有一个 vision_relay 开关，云端永远只吃本地 7B 的转述 ——
# 本地 VL 上下文不够时整条请求 400，图被换成"你看不到内容"，于是她开始编。
# 现在按 vision.backend 选引擎：
#   auto  云端能看就用云端（原图直传），云端抛错/返回空退本地
#   cloud 只走云端    local 只走本地（图不进云端）    off 不看图（当没收到图）
VISION_BACKENDS = ("auto", "cloud", "local", "off")
VISION_LABELS = {"auto": "自动（云端优先，失败退本地）", "cloud": "只走云端",
                 "local": "只走本地", "off": "不看图"}

# 识图（图 -> 文字）的提示词：云端、本地共用一份，要改只改这里。
# 以前只让她"描述画面、别评价别联想"，结果她只会把动作按顺序念一遍，像说明书；
# 改成让她先看懂"是什么"、什么氛围、对方想表达什么。
VISION_DESCRIBE_PROMPT = (
    "看一眼这张图，用两三句中文说清楚三件事：\n"
    "1) 图里是什么 —— 具体到东西本身（什么动物/什么人/什么场景/什么物件），"
    "别只含糊说「有个东西」；\n"
    "2) 整体氛围或情绪是什么样；\n"
    "3) 如果有角色在做某事，说他想表达什么，"
    "4) 如果有文字，读出来并翻译成中文。\n"
    "5) 只说图里看到的东西，别编图里没有的内容。\n"
    "6) 最后可以加一句总结性的感受，但不要评价好坏。\n"
    "**不要按顺序把动作念一遍**。\n"
    "别评价好坏，别编图里没有的东西；看不清的地方就说看不清。")


class Vision:
    """识图引擎选择：云端优先，失败退本地（backend=auto）。

    "哪个后端真的能看图"的判据**只在这里**，别散到调用方去：
      · 云端可用 = cloud.enable 且 cloud.vision 且 cloud.api_key 有值
      · 本地可用 = local.enable 且 local.vision
      · vision_enable 是总闸：false 时谁都不看
    """

    def __init__(self) -> None:
        # 面板上的计数：识过几张、失败几次、丢了几张图、动图抽了多少帧
        # passed_through 是**原图直传**的次数（回答的端点自己收图那条路，见 handle_message）：
        # 它不经过 describe，所以没有它的话面板上 described 会恒为 0，看着像"识图没工作"。
        self.stats: dict[str, int] = {"described": 0, "failed": 0, "dropped": 0, "frames": 0,
                                      "passed_through": 0}
        self._probe: dict = {}
        self._probe_at = 0.0
        self._probe_error = ""

    # ── 引擎判据 ──
    # ── 引擎判据：判据本体在 providers/policy.py，这里只做取值 ──

    def cloud_ok(self) -> bool:
        return bool(ROUTER.policy(CFG).detail.get("cloud_ok"))

    def local_ok(self) -> bool:
        return bool(ROUTER.policy(CFG).detail.get("local_ok"))

    def backend(self) -> str:
        """当前配置选中的后端。vision.backend 缺失时才回退去看旧的 vision_relay。"""
        return ROUTER.policy(CFG).backend

    def usable(self) -> bool:
        """当前后端真的能看图吗（纯配置判断，不发请求）。"""
        return ROUTER.policy(CFG).usable

    def first_answerer(self) -> str:
        """按回退链，**第一个能答话的端点**是谁："cloud" / "local" / ""（没人答）。

        判据必须与 Router.answer 同源（见 providers/router.py 的 answer_endpoint）——
        源码 3340-3346 记过这个坑：判据错配会让图按云端分辨率缩小、实际却是本地模型收图。
        """
        return ROUTER.first_answerer()

    def max_side(self, relay: bool | None = None) -> int:
        """当前该把图缩到多宽 —— **谁消费这些图，就给谁那一档**。

        backend 明确写着 cloud / local 时按它给；auto 时才需要判断：
          · 要转述（relay=True）-> 真正看图的是本地 VL，用 max_side_local
          · 直传（relay=False）-> 第一个答话的端点自己收图，按它给
        调用方可以把先算好的 relay 传进来，保证同一轮里口径一致；不传就现算。
        """
        cfg = CFG.get("vision", {}) or {}
        local_side = int(cfg.get("max_side_local", 1024))
        cloud_side = int(cfg.get("max_side_cloud", 1568))
        if relay is None:
            return ROUTER.policy(CFG).image_max_side
        b = self.backend()
        if b == "local":
            return local_side
        if b == "cloud":
            return cloud_side
        if relay:
            return local_side
        return cloud_side if self.first_answerer() == "cloud" else local_side

    def describe_max_tokens(self) -> int:
        return int((CFG.get("vision") or {}).get("describe_max_tokens", 300))

    def status(self) -> dict:
        """给 WebUI 的只读状态（同步、不联网：探针结果是缓存的）。

        `cloud` / `local` 两个键是**按 tier 聚合的兼容视图**，面板的 renderVision
        与回归【33】段都依赖它们，别删；新面板用 `endpoints` 那一份。
        """
        cfg = CFG.get("vision", {}) or {}
        backend = self.backend()
        cloud_eps = [e for e in ROUTER.endpoints if e.tier == "cloud"]
        local_eps = [e for e in ROUTER.endpoints if e.tier == "local"]
        return {
            "ok": True,
            "backend": backend,
            "backend_label": VISION_LABELS.get(backend, backend),
            "usable": self.usable(),
            "cloud": {"enable": any(e.enabled for e in cloud_eps),
                      "vision": any(ROUTER.vision_of(e) for e in cloud_eps),
                      "has_key": any(e.has_key() for e in cloud_eps)},
            "local": {"enable": any(e.enabled for e in local_eps),
                      "vision": any(ROUTER.vision_of(e) for e in local_eps),
                      # 探针结果走实例缓存（probe() 会填），面板只读不联网
                      "probe": dict(self._probe),
                      "probe_at": self._probe_at,
                      "probe_error": self._probe_error},
            "stats": dict(self.stats),
            "endpoints": ROUTER.status(CFG)["endpoints"],
            "chains": ROUTER.chains,
            "policy": ROUTER.policy(CFG).as_dict(),
            "config": {"max_frames": int(cfg.get("max_frames", 1)),
                       "max_total_images": int(cfg.get("max_total_images", 6)),
                       "max_side_cloud": int(cfg.get("max_side_cloud", 1568)),
                       "max_side_local": int(cfg.get("max_side_local", 1024)),
                       "max_bytes": int(cfg.get("max_bytes", 8_000_000)),
                       "describe_max_tokens": int(cfg.get("describe_max_tokens", 300))},
        }

    def probe_maybe(self) -> None:
        """面板轮询时顺手探一次模型（缓存没过期就不探）。

        console_snapshot 必须是同步短耗时的，联网只能丢成后台任务。
        """
        ttl = float((CFG.get("vision") or {}).get("local_probe_seconds", 300))
        if self._probe_at and time.time() - self._probe_at < ttl:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.create_task(_safe(self.probe()))

    async def probe(self, force: bool = False) -> dict:
        """探所有端点。本地端点走 LM Studio 原生 `/api/v0/models`，
        看的是"本地到底加载了什么、上下文多长" —— 本地 VL 上下文太小正是
        当年图被换成文字、她只能编的原因。

        结果缓存在 Router 里（TTL = `vision.local_probe_seconds`），
        `status()` 只读实例缓存、不联网。
        """
        snapshot = await ROUTER.probe_all(CFG, force=force)
        local = dict(snapshot.get("local") or {})
        local.pop("at", None)
        self._probe = local
        self._probe_at = float(snapshot.get("at") or time.time())
        self._probe_error = ROUTER.probe_error()
        if self._probe_error:
            logger.warning("探测本地识图模型失败：%s", self._probe_error)
        return dict(self._probe)

    async def describe(self, data_urls: list[str]) -> str | None:
        """按 backend 选引擎；auto 时云端失败自动退本地。返回描述文本或 None。"""
        if not data_urls or not self.usable():
            # 以前这里静默 return None：面板上只看到 described 不涨，日志里也查不到
            # 到底"是没图"还是"后端看不了图"，排查时只能靠猜。
            self.stats["failed"] += 1
            logger.warning("识图没做：%d 张图，backend=%s，usable=%s，vision_enable=%s",
                           len(data_urls), self.backend(), self.usable(),
                           CFG.get("vision_enable", True))
            return None
        backend = self.backend()
        tried: list[str] = []
        if backend in ("auto", "cloud"):
            got = await self._ask_cloud(data_urls)
            if got:
                self.stats["described"] += 1
                return got
            tried.append("云端")
            if backend == "cloud":
                self.stats["failed"] += 1
                return None
        if self.local_ok():
            got = await CHAT.describe_images(data_urls)
            if got:
                self.stats["described"] += 1
                return got
            tried.append("本地")
        self.stats["failed"] += 1
        logger.warning("识图没成（试过 %s，backend=%s）", "、".join(tried) or "无可用引擎", backend)
        return None

    async def _ask_cloud(self, data_urls: list[str]) -> str | None:
        """云端识图：把原图直传给云端模型，要一段描述。失败返回 None（由调用方退本地）。

        要点：**原图直传**（别在本地转述后再送云端，那会丢细节），输出上限取
        `vision.describe_max_tokens`。
        """
        if not self.cloud_ok():
            return None
        cloud = CFG.get("cloud", {}) or {}
        content: list = [{"type": "text", "text": VISION_DESCRIBE_PROMPT}]
        for u in data_urls:
            content.append({"type": "image_url", "image_url": {"url": u}})
        try:
            desc = await CHAT._post(cloud, [{"role": "user", "content": content}],
                                    float(cloud.get("timeout_seconds", 90)),
                                    {"max_tokens": self.describe_max_tokens()})
            return (desc or "").strip() or None
        except Exception as exc:
            logger.warning("云端识图失败，准备退本地：%s", exc)
            return None



VISION = Vision()


class Bili:
    """她的 b 站账号：扫码登录、记住 cookie、挑视频看、点赞投币收藏。

    只用**不依赖 WBI 签名**的接口（热门/排行/稍后再看/搜索/详情），避开 w_rid 那套。
    """

    GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    API = "https://api.bilibili.com"
    # 扫码轮询的返回码
    QR_OK, QR_EXPIRED, QR_SCANNED, QR_WAIT = 0, 86038, 86090, 86101

    def __init__(self) -> None:
        cfg = CFG.get("bili", {})
        self.enable = bool(cfg.get("enable", False))
        self.actions_enable = bool(cfg.get("actions_enable", False))
        self.timeout = float(cfg.get("timeout_seconds", 12))
        self.state_path = BASE / str(cfg.get("state_path", "bili_state.json"))
        self.qr_path = BASE / str(cfg.get("qr_path", "bili_qrcode.png"))
        self.ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        self.cookies: dict[str, str] = {}
        self.uid = ""
        self.uname = ""
        self.state: dict = {"date": "", "actions": 0}
        self._pending_key = ""
        self._cli: httpx.AsyncClient | None = None
        # 真登录态（不是"有没有 cookie"）：None = 还没查过，True/False = 问过 b 站了
        self.guest: bool | None = None
        self._login_checked = 0.0
        self._login_error = ""
        # 同一轮里别重复发起扫码：后台巡检和管理员手动 /bili 共用这一把锁
        self._login_lock = asyncio.Lock()
        self.load()

    # ── 持久化 ──────────────────────────────
    def load(self) -> None:
        d = read_json_dict(self.state_path, "B站登录")
        self.cookies = dict(d.get("cookies") or {})
        self.uid = str(d.get("uid") or "")
        self.uname = str(d.get("uname") or "")
        self.state = {"date": str(d.get("date") or ""), "actions": int(d.get("actions") or 0)}

    def save(self) -> None:
        write_json_dict(self.state_path, {
            "cookies": self.cookies, "uid": self.uid, "uname": self.uname,
            "date": self.state.get("date", ""), "actions": self.state.get("actions", 0),
        }, "B站登录")

    @property
    def logged_in(self) -> bool:
        """**只看有没有 SESSDATA** —— 快，但会说谎（cookie 可能早过期了）。

        要判断"她现在是不是游客"请用 `is_guest()` / `check_login()`。
        """
        return bool(self.cookies.get("SESSDATA"))

    # ── 登录态 ──────────────────────────────
    async def check_login(self, force: bool = False) -> bool:
        """真去 b 站问一次"我是谁"。返回是否**确实**登录着。

        只有 SESSDATA 不算登录 —— 那玩意儿有有效期，过期后仍然是游客，
        只是代码看不出来（表现为"能刷热门但点赞投币全失败"）。
        结果缓存 login_check_seconds 秒，避免每轮都打接口。

        ⚠️ **查不到 ≠ 是游客**：网络不通时保持上一次结论、`guest` 留 None（未知）。
        调用方想"要不要给她发二维码"必须先看 `guest is True`，别拿返回值当依据。
        """
        cfg = CFG.get("bili", {})
        now = time.time()
        ttl = float(cfg.get("login_check_seconds", 300))
        if not force and self._login_checked and now - self._login_checked < ttl:
            return self.guest is False
        self._login_checked = now
        if not self.cookies.get("SESSDATA"):
            self.guest, self._login_error = True, "没有 SESSDATA（从没登录过）"
            return False
        try:
            r = await self._client().get(self.API + "/x/web-interface/nav")
            self._capture(r)
            body = r.json() or {}
        except Exception as exc:
            # 网络不通 ≠ 掉登录。查不到就**保持上一次结论**，别把人家踢成游客，
            # 更别据此去发一张没人要的二维码。
            self._login_error = f"登录态查询失败：{exc}"
            logger.debug("B站登录态查询失败：%s", exc)
            return self.guest is False
        data = body.get("data") or {}
        if body.get("code") == 0 and data.get("isLogin"):
            self.uname = str(data.get("uname") or self.uname)
            self.uid = str(data.get("mid") or self.uid or self.cookies.get("DedeUserID", ""))
            self.guest, self._login_error = False, ""
            self.save()
            return True
        self.guest = True
        self._login_error = str(body.get("message") or "SESSDATA 已失效")
        logger.warning("B站当前是游客（%s），SESSDATA 已失效", self._login_error)
        return False

    def is_guest(self) -> bool | None:
        """None = 还没查过（未知），True/False = 已知。"""
        return self.guest

    def state_text(self) -> str:
        """给人看的一句话。"""
        if self.guest is None:
            return "登录态未确认"
        return f"游客（{self._login_error}）" if self.guest else f"已登录：{self.uname or self.uid}"

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def actions_today(self) -> int:
        return int(self.state.get("actions") or 0) if self.state.get("date") == self._today() else 0

    def can_act(self) -> bool:
        """点赞投币收藏有日上限，避免账号被风控。"""
        if not self.actions_enable or not self.logged_in or self.guest:
            return False
        cap = int(CFG.get("bili", {}).get("daily_action_cap", 6))
        return cap <= 0 or self.actions_today() < cap

    # ── HTTP ────────────────────────────────
    def _client(self) -> httpx.AsyncClient:
        if self._cli is None or self._cli.is_closed:
            self._cli = httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True, trust_env=True,
                headers={"User-Agent": self.ua, "Referer": "https://www.bilibili.com/"})
            for k, v in self.cookies.items():
                self._cli.cookies.set(k, v, domain=".bilibili.com")
        return self._cli

    def _capture(self, resp: httpx.Response) -> None:
        """把响应里种的 cookie 收下来（SESSDATA/bili_jct/buvid3 等）。"""
        for k, v in resp.cookies.items():
            if v:
                self.cookies[k] = v
        for raw in resp.headers.get_list("set-cookie"):
            for part in raw.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k and v and k not in self.cookies and not k.startswith(("Path", "Domain", "Expires", "Max", "Same", "Secure", "Http")):
                        self.cookies[k] = v

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = await self._client().get(self.API + path, params=params or {})
            self._capture(r)
            body = r.json() or {}
            if body.get("code") not in (0, None):
                logger.debug("B站 %s -> code=%s %s", path, body.get("code"), body.get("message"))
                return {}
            return body.get("data") or {}
        except Exception as exc:
            logger.debug("B站 %s 异常：%s", path, exc)
            return {}

    async def _post(self, path: str, data: dict) -> bool:
        csrf = self.cookies.get("bili_jct", "")
        if not self.logged_in or not csrf:
            return False
        try:
            r = await self._client().post(self.API + path, data={**data, "csrf": csrf})
            body = r.json() or {}
            if body.get("code") != 0:
                logger.debug("B站 %s 失败：%s %s", path, body.get("code"), body.get("message"))
            return body.get("code") == 0
        except Exception as exc:
            logger.debug("B站 %s 异常：%s", path, exc)
            return False

    # ── 扫码登录 ────────────────────────────
    async def qr_start(self) -> tuple[str, str] | None:
        """生成登录二维码，存成图片。返回 (qrcode_key, 图片路径)。"""
        try:
            r = await self._client().get(self.GEN)
            self._capture(r)
            data = (r.json() or {}).get("data") or {}
            url, key = data.get("url"), data.get("qrcode_key")
            if not url or not key:
                logger.warning("B站二维码生成失败：%s", r.text[:200])
                return None
            import qrcode
            qrcode.make(url).save(self.qr_path)
            self._pending_key = str(key)
            logger.info("B站登录二维码已保存：%s（用手机 b 站扫一扫）", self.qr_path)
            return str(key), str(self.qr_path)
        except Exception as exc:
            logger.warning("B站二维码生成异常：%s", exc)
            return None

    async def qr_poll(self) -> tuple[int, str]:
        """查一次扫码状态。0=登录成功。"""
        if not self._pending_key:
            return -1, "还没生成二维码"
        try:
            r = await self._client().get(self.POLL, params={"qrcode_key": self._pending_key})
            data = (r.json() or {}).get("data") or {}
            code = int(data.get("code", -1))
            if code != self.QR_OK:
                return code, str(data.get("message") or "")
            self._capture(r)
            # 成功时跳转链接的 query 里带着 SESSDATA/bili_jct，比 Set-Cookie 更全
            jump = str(data.get("url") or "")
            if "?" in jump:
                for pair in jump.split("?", 1)[1].split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        if k in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"):
                            self.cookies[k] = v
            self.uid = self.cookies.get("DedeUserID", "")
            nav = await self._get("/x/web-interface/nav")
            self.uname = str(nav.get("uname") or "")
            self.uid = str(nav.get("mid") or self.uid)
            self.save()
            logger.info("B站登录成功：%s（%s）", self.uname or "?", self.uid or "?")
            return self.QR_OK, "登录成功"
        except Exception as exc:
            logger.debug("B站轮询异常：%s", exc)
            return -2, f"轮询出错：{exc}"

    async def qr_wait(self, seconds: int | None = None) -> bool:
        """阻塞等扫码，超时返回 False。"""
        limit = float(seconds or CFG.get("bili", {}).get("qr_wait_seconds", 180))
        deadline = time.time() + limit
        while time.time() < deadline:
            code, _ = await self.qr_poll()
            if code == self.QR_OK:
                return True
            if code == self.QR_EXPIRED:
                return False
            await asyncio.sleep(2.5)
        logger.info("B站扫码等待超时")
        return False

    # ── 游客 -> 自己发起登录 ────────────────
    async def ensure_login(self, notify=None, wait_seconds: int | None = None) -> tuple[bool, str]:
        """确认登录态；是游客就**自己**开一次扫码登录，并让 notify 把二维码交给人。

        notify(text, image_path) 由调用方给（一般是往管理员私聊发消息 + 发图）。
        返回 (是否已登录, 说明)。已经登录时不会重复生成二维码。
        """
        if not self.enable:
            return False, "b 站功能没开"
        if await self.check_login():
            return True, f"本来就登录着（{self.uname or self.uid}）"
        if self.guest is not True:
            # 查不出来（网络不通/接口没回）就别发码 —— 她可能本来就登录着
            return False, f"登录态没查出来（{self._login_error or '未知'}），先不发二维码"
        if self._login_lock.locked():
            return False, "已经在等扫码了"
        async with self._login_lock:
            # 拿到锁之后再确认一次：等锁期间可能刚被别的路径登录好
            if await self.check_login(force=True):
                return True, f"刚登录好（{self.uname or self.uid}）"
            if self.guest is not True:
                return False, f"登录态没查出来（{self._login_error or '未知'}），先不发二维码"
            got = await self.qr_start()
            if not got:
                return False, "二维码没生成出来"
            _, path = got
            if notify:
                await notify("我的 b 站掉登录了，现在是游客状态。"
                             "用手机 b 站扫一下这个码，扫完就好：", path)
            ok = await self.qr_wait(wait_seconds)
            if notify:
                await notify(f"b 站登录好了，我是{BILI.uname or BILI.uid}了。" if ok
                             else "那个码过期了，我过会儿再试一次。")
            if ok:
                self.guest = False
                self._login_error = ""
            return ok, "扫码成功" if ok else "二维码过期或超时"

    # ── 视频来源 ────────────────────────────
    @staticmethod
    def _clean(s: str) -> str:
        return ihtml.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

    @classmethod
    def _row(cls, it: dict) -> dict:
        stat = it.get("stat") or {}
        owner = it.get("owner") or {}
        bvid = it.get("bvid") or ""
        return {
            "bvid": bvid,
            "title": cls._clean(it.get("title") or "")[:80],
            "up": owner.get("name") or it.get("author") or "",
            "tname": it.get("tname") or "",
            "duration": int(it.get("duration") or 0),
            "views": int(stat.get("view") or it.get("play") or 0),
            "url": f"https://www.bilibili.com/video/{bvid}" if bvid else (it.get("arcurl") or ""),
        }

    async def hot(self, n: int = 12) -> list[dict]:
        d = await self._get("/x/web-interface/popular", {"ps": min(max(n, 1), 50), "pn": 1})
        return [self._row(x) for x in (d.get("list") or []) if x.get("bvid")]

    async def ranking(self, n: int = 12) -> list[dict]:
        d = await self._get("/x/web-interface/ranking/v2", {"rid": 0, "type": "all"})
        return [self._row(x) for x in (d.get("list") or [])[:n] if x.get("bvid")]

    async def toview(self, n: int = 12) -> list[dict]:
        """账号自己的"稍后再看"，要登录。"""
        if not self.logged_in:
            return []
        d = await self._get("/x/v2/history/toview")
        return [self._row(x) for x in (d.get("list") or [])[:n] if x.get("bvid")]

    async def search(self, kw: str, n: int = 12) -> list[dict]:
        d = await self._get("/x/web-interface/search/type",
                            {"search_type": "video", "keyword": kw, "page": 1})
        return [self._row(x) for x in (d.get("result") or [])[:n] if x.get("bvid")]

    async def view(self, bvid: str) -> dict:
        return await self._get("/x/web-interface/view", {"bvid": bvid})

    # ── 动作 ────────────────────────────────
    async def like(self, bvid: str) -> bool:
        return await self._post("/x/web-interface/archive/like", {"bvid": bvid, "like": 1})

    async def coin(self, bvid: str) -> bool:
        return await self._post("/x/web-interface/coin/add",
                                {"bvid": bvid, "multiply": 1, "select_like": 0})

    async def fav(self, aid: int | str, bvid: str) -> bool:
        """收藏进第一个收藏夹（一般是默认夹）。"""
        mid = self.uid or self.cookies.get("DedeUserID", "")
        if not mid:
            return False
        d = await self._get("/x/v3/fav/folder/created/list-all", {"up_mid": mid})
        folders = d.get("list") or []
        if not folders:
            return False
        return await self._post("/x/v3/fav/resource/deal",
                                {"rid": aid, "type": 2, "add_media_ids": folders[0].get("id")})

    async def _maybe_act(self, v: dict) -> None:
        """按概率真的动动手，并把结果记进行为流水。"""
        cfg = CFG.get("bili", {})
        bvid = v.get("bvid")
        if not bvid or not self.can_act():
            return
        did: list[str] = []
        if random.random() < float(cfg.get("like_probability", 0.35)) and await self.like(bvid):
            did.append("点赞")
        if random.random() < float(cfg.get("coin_probability", 0.15)) and await self.coin(bvid):
            did.append("投币")
        aid = v.get("aid")
        if aid and random.random() < float(cfg.get("fav_probability", 0.12)) and await self.fav(aid, bvid):
            did.append("收藏")
        if did:
            self.state = {"date": self._today(), "actions": self.actions_today() + len(did)}
            self.save()
            ACTIVITY.note("动作", f"{'、'.join(did)}《{v.get('title', '')}》")

    async def browse(self, keyword: str = "") -> dict | None:
        """挑一个视频"看一会儿"，记一笔，偶尔点赞收藏。返回视频信息。"""
        if not self.enable:
            return None
        pools: list[list[dict]] = []
        if keyword:
            got = await self.search(keyword, 12)
            if got:
                pools.append(got)
        for fn in (self.hot, self.ranking, self.toview):
            try:
                got = await fn(12)
            except Exception:
                got = []
            if got:
                pools.append(got)
        if not pools:
            logger.debug("B站没有可看的视频")
            return None
        v = random.choice(random.choice(pools))
        detail = await self.view(v["bvid"]) if v.get("bvid") else {}
        if detail:
            v["duration"] = int(detail.get("duration") or v.get("duration") or 0)
            v["tname"] = detail.get("tname") or v.get("tname") or ""
            v["aid"] = detail.get("aid")
            v["desc"] = self._clean(detail.get("desc") or "")[:120]
        mins = max(1, (v.get("duration") or 0) // 60)
        ACTIVITY.note("看视频", f"《{v['title']}》｜{v.get('up') or '?'}｜{mins}分｜{v['url']}")
        await self._maybe_act(v)
        return v


BILI = Bili()


class Activity:
    """她的行为流水：看视频 / 上网 / 说话 / 动作，按天写成 markdown，并攒着给定期报告。"""

    def __init__(self) -> None:
        cfg = CFG.get("activity", {})
        self.enable = bool(cfg.get("enable", True))
        self.dir = BASE / str(cfg.get("dir", "activity"))
        self.report_minutes = int(cfg.get("report_minutes", 30))
        self.report_to_admin = bool(cfg.get("report_to_admin", True))
        self.llm_summary = bool(cfg.get("llm_summary", True))
        self.max_buffer = int(cfg.get("max_buffer", 200))
        self._pending: list[dict] = []

    def note(self, kind: str, text: str) -> None:
        if not self.enable or not text:
            return
        self._pending.append({"hhmm": datetime.now().strftime("%H:%M"),
                              "kind": kind, "text": text})
        if len(self._pending) > self.max_buffer:
            self._pending = self._pending[-self.max_buffer:]
        logger.info("[行为] %s | %s", kind, text[:90])

    def drain(self) -> list[dict]:
        got, self._pending = self._pending, []
        return got

    def day_path(self) -> Path:
        return self.dir / f"{datetime.now():%Y-%m-%d}.md"

    def flush(self, entries: list[dict]) -> None:
        if not entries:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            p = self.day_path()
            fresh = not p.exists()
            with p.open("a", encoding="utf-8") as f:
                if fresh:
                    f.write(f"# {her_name()}的行为记录 {datetime.now():%Y-%m-%d}\n\n")
                for e in entries:
                    f.write(f"- `{e['hhmm']}` **{e['kind']}** {e['text']}\n")
        except Exception as exc:
            logger.warning("行为日志写入失败：%s", exc)


    def list_days(self) -> list[dict]:
        """列出所有有记录的日子（新到旧），给控制台选日期用。"""
        out: list[dict] = []
        try:
            for p in sorted(self.dir.glob("*.md"), reverse=True):
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", p.name):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append({"date": p.stem, "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            pass
        return out

    def read_day(self, date: str) -> dict:
        """读某天的行为流水 markdown 并解析成结构化条目。

        date 由调用方保证已通过 YYYY-MM-DD 校验（防路径穿越）；此处再校验一次。
        """
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
            raise ValueError("date 需为 YYYY-MM-DD")
        p = self.dir / f"{date}.md"
        if not p.exists():
            return {"date": date, "entries": [], "raw": "", "exists": False}
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError as exc:
            logger.warning("行为日志读取失败：%s", exc)
            return {"date": date, "entries": [], "raw": "", "exists": False}
        entries: list[dict] = []
        for line in text.splitlines():
            m = re.match(r"^- `(\d{2}:\d{2})` \*\*(.+?)\*\* (.*)$", line.strip())
            if m:
                entries.append({"hhmm": m.group(1), "kind": m.group(2), "text": m.group(3)})
        return {"date": date, "entries": entries, "raw": text,
                "exists": True, "pending": list(self._pending)}


ACTIVITY = Activity()


def _pct(old: float) -> float:
    """旧分制（-5..+5）换算成百分制（0..100）。迁移历史数据用。"""
    return max(0.0, min(100.0, (float(old) + 5.0) * 10.0))


class Mood:
    """对某个人的**好感**与**心情** —— 两套独立的量，都是百分制 0-100。

    为什么不合成一个：
      · **好感 affinity** 是长期印象，变化慢，每天往 50 回落 —— 「我大体怎么看他」
      · **心情 feeling** 是此刻愿不愿意搭理他，变化快 —— 「我现在想不想理他」
    但两者不是无关：**心情每次更新都往好感靠一步**（mood_pull_to_affinity），
    所以"对某人好感会影响对某人的心情"；而心情又会自己慢慢平复回好感那条线。

    2026-09-27 把**分数划分做细**（原来只有 5 档，46~64 全叫"还算顺眼"）：
      · 好感分 **10 档**（TIERS，可在 config 的 affinity.tiers 里改），
        每一档都有说法，面板上画成一条带刻度的尺子，能看出"快进下一档了"；
      · 词表分**强弱两层**：一句「谢谢」和一句「我最喜欢你了」不再是一个价
        （强的那层按 affinity.strong_mult 放大）；
      · 正常聊天会**极小幅度地累积**好感（drift_gain / 每天有上限）——
        否则分数永远只在 48/52/56 几个值上跳，再细的档位也用不上。

    规则都只做粗判，真正怎么拿捏交给提示词里的她 —— 这里不写死反应。
    """

    # 词表分两层：STRONG_* 是"明显带情绪"的说法，按 strong_mult 放大；
    # 一般那层就是一次普通的好话/难听话。
    # 注意顺序 —— 判定时**先看强的那层**（「傻逼」里含「傻」，先判强层才不会被降级）。
    STRONG_NICE = ("最喜欢", "超喜欢", "太喜欢", "爱你", "喜欢你", "太好了", "太棒了", "超可爱",
                   "真可爱", "太厉害", "好厉害", "谢谢你", "辛苦你了", "你真好", "永远支持",
                   "太赞了", "真的服你", "有点崇拜")
    NICE = ("谢谢", "感谢", "厉害", "可爱", "真棒", "牛啊", "爱你", "辛苦了", "靠谱", "不错",
            "好评", "支持你", "有意思")
    RUDE = ("傻", "蠢", "废物", "滚", "闭嘴", "烦死", "有病", "智障", "垃圾", "菜鸡",
            "滚蛋", "弱智", "神经", "毛病", "恶心", "讨厌")
    STRONG_RUDE = ("傻逼", "去死", "滚远点", "脑子有病", "脑残", "恨你", "再也不", "拉黑",
                   "真垃圾", "你就是个废物")

    LO = 0.0
    HI = 100.0
    NEUTRAL = 50.0          # 百分制里的中性值

    # 好感档位：(这一档的上限, 说法)。默认 10 档 —— 划分细一点，她对他的态度才有层次。
    # 可用 config 的 affinity.tiers 覆盖（[[上限,"说法"], ...]，必须递增、最后一档要能盖到 100）。
    DEFAULT_TIERS = ((10.0, "恨得牙痒"), (22.0, "很反感"), (34.0, "有点烦"), (45.0, "不太待见"),
                     (55.0, "不咸不淡"), (66.0, "还算顺眼"), (76.0, "挺待见"), (86.0, "很喜欢"),
                     (94.0, "特别在意"), (100.0, "放在心上"))

    # 心情档位：同一套思路用在"此刻"上。以前提示词里只有一个"心情 72/100"的裸数字，
    # 她拿捏不出"现在是懒得理他还是正想找人说话"，所以这边也给一套说法。
    # 措辞一律指向**当下**（"这会儿/正…"），跟好感那种长期评价的说法区分开；
    # 档数比好感少（8 档）是刻意的：心情本来就善变，分太细反而抖得没意义。
    # 可用 config 的 mood.feel_tiers 覆盖，规则同 affinity.tiers。
    DEFAULT_FEEL_TIERS = ((15.0, "正堵得慌"), (28.0, "很不耐烦"), (40.0, "有点烦着"),
                          (52.0, "平平淡淡"), (64.0, "还算想理"), (76.0, "挺想搭理"),
                          (88.0, "心情不错"), (100.0, "正想找人说话"))

    def __init__(self) -> None:
        cfg = CFG.get("mood", {})
        acfg = CFG.get("affinity", {})
        self.enable = bool(cfg.get("enable", True))
        # 好感：长期、保守 —— 一句话不该带跑长期印象
        self.affinity_gain = float(acfg.get("gain_nice", 2.0))
        self.affinity_loss = float(acfg.get("loss_rude", 6.0))
        self.affinity_decay = float(acfg.get("decay_per_day", 1.5))
        # 强情绪词的放大倍率（「我最喜欢你了」不该和「谢谢」一个价）
        self.strong_mult = float(acfg.get("strong_mult", 1.8))
        # 正常聊天的小幅累积
        self.drift_gain = float(acfg.get("drift_gain", 0.25))
        self.drift_max_per_day = float(acfg.get("drift_max_per_day", 2.5))
        self.drift_min_chars = int(acfg.get("drift_min_chars", 6))
        # 闲聊只能把她对你的印象养到这个分数为止；再往上得靠真的夸她/对她好。
        # 没有这条上限的话，天天闲聊就能把好感刷到 100，"放在心上"就不值钱了。
        self.drift_ceiling = float(acfg.get("drift_ceiling", 62.0))
        self.tiers = self._parse_tiers(acfg.get("tiers"), self.DEFAULT_TIERS, "affinity.tiers")
        # 档位迟滞：分数刚好压在边界上时，说法不许跟着抖（见 tier_of_for）
        self.hysteresis = max(0.0, float(acfg.get("hysteresis", 1.5)))
        # 心情：短期、波动更大；并被好感牵引
        self.feel_gain = float(cfg.get("mood_gain_nice", 4.0))
        self.feel_loss = float(cfg.get("mood_loss_rude", 8.0))
        self.pull = float(cfg.get("mood_pull_to_affinity", 0.35))
        self.feel_tiers = self._parse_tiers(cfg.get("feel_tiers"),
                                            self.DEFAULT_FEEL_TIERS, "mood.feel_tiers")
        self.path = BASE / "mood.json"
        self.affinity: dict[str, dict[str, float]] = {}
        self.feeling: dict[str, dict[str, float]] = {}
        self.names: dict[str, dict[str, str]] = {}
        self._drift_log: dict[str, dict] = {}     # "群:人" -> {date, used}，累积的每日上限
        self._tier_mem: dict[str, int] = {}       # "群:人" -> 上次落在第几档（迟滞用）
        self._last_decay = ""                     # 上次"每日回落"是哪天（防止一天回落多次）
        # 被戳扣好感的当日记账：{QQ号: {"date","count"}}（每天最多扣几次，见 poke_annoy）
        self._poke_aff: dict[str, dict] = {}
        self.load()

    # ──────────────────────── 档位 ────────────────────────

    def _parse_tiers(self, raw, default: tuple, name: str = "affinity.tiers") -> tuple:
        """把配置里的档位读进来；写错了就退回默认（宁可档位不生效，也不能让好感算崩）。"""
        if not isinstance(raw, list) or len(raw) < 2:
            return default
        out: list[tuple[float, str]] = []
        for item in raw:
            try:
                hi, label = float(item[0]), str(item[1])
            except (TypeError, IndexError, ValueError):
                logger.warning("%s 里有一项读不懂，整份改用默认档位：%r", name, item)
                return default
            out.append((hi, label))
        if any(out[i][0] <= out[i - 1][0] for i in range(1, len(out))) or out[-1][0] < self.HI:
            logger.warning("%s 不是递增的、或最后一档盖不到 100，改用默认档位", name)
            return default
        return tuple(out)

    @staticmethod
    def _tier_at_in(tiers: tuple, index: int, v: float) -> dict:
        """取第 index 档的信息，档内进度按分数 v 算。

        档位表就是一张 [(上限, 说法)]，所以第 index 档的区间是
        `上一条的上限 ~ 本条上限`（第一档从 0 起）。
        """
        n = len(tiers)
        index = max(1, min(n, int(index)))
        lo = 0.0 if index == 1 else float(tiers[index - 2][0])
        hi = float(tiers[index - 1][0])
        span = max(1e-6, hi - lo)
        return {"index": index, "count": n, "label": tiers[index - 1][1],
                "lo": round(lo, 1), "hi": round(hi, 1),
                "pos": round(max(0.0, min(100.0, (float(v) - lo) / span * 100)), 1),
                "next_at": None if index == n else round(hi, 1),
                "to_next": None if index == n else round(max(0.0, hi - float(v)), 1)}

    @classmethod
    def _find_tier(cls, tiers: tuple, v: float) -> dict:
        """把一个分数落进某张档位表。"""
        v = max(cls.LO, min(cls.HI, float(v)))
        n = len(tiers)
        for i, (hi, _label) in enumerate(tiers):
            if v <= hi or i == n - 1:
                return cls._tier_at_in(tiers, i + 1, v)
        return cls._tier_at_in(tiers, n, v)

    def tier_of(self, a: float) -> dict:
        """好感的档位（**不带**迟滞 —— 面板画刻度尺要的是分数此刻的真实落点）。

        `pos` 是"在这一档里已经走到百分之几"—— 细腻的意义就在这儿：
        同样是"挺待见"，刚进来和快满了是两回事。
        """
        return self._find_tier(self.tiers, a)

    def feel_tier_of(self, f: float) -> dict:
        """心情的档位。刻意**不加迟滞** —— 心情本来就是"此刻"，该抖就抖。"""
        return self._find_tier(self.feel_tiers, f)

    def tier_of_for(self, group_id, user_id, a: float) -> dict:
        """带**迟滞**的好感档位：分数压在边界上时，说法不跟着来回跳。

        为什么需要：好感是慢变量，但每天 drift 一点、decay 一点，
        刚好停在 76.0 这种边界上时会一档一档地横跳 —— "挺待见"和"很喜欢"
        来回变，比不分档还糟。所以记住上次落在哪一档，只有分数**真的越过去**
        （超出边界 hysteresis 那么多）才换档。

        迟滞只在"说法"这一层生效，分数本身该怎么算还怎么算。
        """
        raw = self.tier_of(a)
        key = f"{group_id}:{user_id}"
        prev = self._tier_mem.get(key)
        n = len(self.tiers)
        if prev is None or not (1 <= prev <= n):
            self._tier_mem[key] = raw["index"]      # 第一次见：以真实落点为起点
            return raw
        if prev == raw["index"]:
            return raw
        a = max(self.LO, min(self.HI, float(a)))
        p_lo = 0.0 if prev == 1 else float(self.tiers[prev - 2][0])
        p_hi = float(self.tiers[prev - 1][0])
        if raw["index"] > prev and a <= p_hi + self.hysteresis:
            keep = prev                              # 刚过线，还不算真的升上去
        elif raw["index"] < prev and a >= p_lo - self.hysteresis:
            keep = prev                              # 刚跌破，还不算真的掉下来
        else:
            keep = raw["index"]
        self._tier_mem[key] = keep
        return self._tier_at_in(self.tiers, keep, a)

    @staticmethod
    def _tiers_view(tiers: tuple) -> list[dict]:
        """把一张档位表摊成面板要的列表。好感表和心情表是同一种结构，共用一份。"""
        out, lo = [], 0.0
        for i, (hi, label) in enumerate(tiers):
            out.append({"index": i + 1, "lo": round(lo, 1), "hi": round(hi, 1),
                        "label": label, "span": round(hi - lo, 1)})
            lo = hi
        return out

    def tiers_view(self) -> list[dict]:
        """好感档位全表（给面板画图例）。"""
        return self._tiers_view(self.tiers)

    def feel_tiers_view(self) -> list[dict]:
        """心情档位全表（给面板画图例）。"""
        return self._tiers_view(self.feel_tiers)

    # ──────────────────────── 好感落盘与跨档 ────────────────────────

    def _set_affinity(self, group_id, user_id, value: float, nickname="") -> float:
        """写好感的**唯一入口** —— 顺带判一次跨档。

        所有改好感的地方都走这里，是为了不漏记：以前各处直接 `aff[u] = ...`，
        想加一句"印象变了"的感知就得把每一处都找到，漏一处就少一笔。
        """
        g, u = str(group_id), str(user_id)
        aff = self.affinity.setdefault(g, {})
        a = max(self.LO, min(self.HI, float(value)))
        aff[u] = round(a, 2)
        self.names.setdefault(g, {})[u] = str(nickname or u)
        old_i = self._tier_mem.get(f"{g}:{u}")
        new_i = self.tier_of_for(g, u, a)["index"]
        if old_i is not None and old_i != new_i:
            self._note_tier_cross(nickname or u, old_i, new_i)
        return aff[u]

    def _note_tier_cross(self, name: str, old_i: int, new_i: int) -> None:
        """好感跨档时记一笔 —— 让"我对他的印象变了"成为她自己也察觉得到的事。

        只记跨档，不记档内涨跌：分数动 0.25 就写一行的话，行为流水会被刷爆，
        而"从'挺待见'变成'很喜欢'"这种才是她真的会在意的变化。
        """
        n = len(self.tiers)
        if not (1 <= old_i <= n and 1 <= new_i <= n) or old_i == new_i:
            return
        old_label = self.tiers[old_i - 1][1]
        new_label = self.tiers[new_i - 1][1]
        verb = "好了些" if new_i > old_i else "差了些"
        line = f"对「{name}」的印象{verb}：{old_label} → {new_label}"
        ACTIVITY.note("好感", line)
        logger.info("好感跨档：%s", line)
        try:
            LIFE.note_material(f"（好感）{line}", kind="好感")
        except Exception as exc:      # 素材只是锦上添花，绝不能反过来打断好感计算
            logger.debug("好感跨档素材没记上：%s", exc)

    # ──────────────────────── 存取 ────────────────────────

    def load(self) -> None:
        data = read_json_dict(self.path, "好感与心情")
        self.names = data.get("names", {}) or {}
        # 自然累积的每日用量也要留下 —— 不然重启就又能空刷一轮
        self._drift_log = {k: v for k, v in (data.get("drift") or {}).items()
                           if isinstance(v, dict)}
        # 档位迟滞的记忆；脏数据（不是 1..档数的整数）直接丢掉，让迟滞重新起算
        n = len(self.tiers)
        self._tier_mem = {k: int(v) for k, v in (data.get("tier_mem") or {}).items()
                          if isinstance(v, (int, float)) and 1 <= int(v) <= n}
        self._last_decay = str(data.get("last_decay") or "")
        self._poke_aff = {k: v for k, v in (data.get("poke_aff") or {}).items()
                          if isinstance(v, dict)}
        if "affinity" in data or "mood" in data:
            self.affinity = {g: {u: float(v) for u, v in d.items()}
                             for g, d in (data.get("affinity") or {}).items()}
            self.feeling = {g: {u: float(v) for u, v in d.items()}
                            for g, d in (data.get("mood") or {}).items()}
            return
        # 旧格式迁移：{"scores": {...}} 是 -5..+5 的"好感/记仇"分。
        # 那个分本来描述的就是好感，所以迁成好感；心情先等于好感（从同一条线开始）。
        old = data.get("scores") or {}
        self.affinity, self.feeling = {}, {}
        for g, d in old.items():
            self.affinity[g] = {u: _pct(v) for u, v in d.items()}
            self.feeling[g] = dict(self.affinity[g])
        if old:
            logger.info("好感/心情已迁移到百分制（%d 个群）", len(old))

    def save(self) -> None:
        write_json_dict(self.path, {
            "affinity": self.affinity,
            "mood": self.feeling,
            "names": self.names,
            "drift": self._drift_log,
            # 迟滞用的"上次落在第几档"也要留着：重启后从 0 重新记，
            # 边界上的人会被当成第一次见，档位说法可能跳一次。
            "tier_mem": self._tier_mem,
            # 每日回落记到哪天了 —— 没落盘的话每次重启都会多回落一次
            "last_decay": self._last_decay,
            # 被戳扣好感的当日次数（每人每天最多扣几次，免得闹着玩把印象戳没）
            "poke_aff": self._poke_aff,
            "_说明": ("affinity=好感（长期印象，每天往 50 回落）；"
                    "mood=心情（此刻愿不愿搭理，会被好感牵引回同一水平）。"
                    "两者都是 0-100 百分制，50 为中性；好感分 10 档（见 affinity.tiers）、"
                    "心情分 8 档（见 mood.feel_tiers）。"
                    "drift=正常聊天自然累积的当日用量（每人每天有上限，防刷）；"
                    "tier_mem=上次落在第几档（档位迟滞用，免得边界上反复横跳）。"),
        }, "好感与心情")

    # ──────────────────────── 更新 ────────────────────────

    def sentiment(self, text: str) -> tuple[bool, bool, float, str]:
        """判一句话是夸还是骂、有多重。

        返回 (nice, rude, 倍率, 命中的词)。**先看强词层**：「傻逼」里含「傻」，
        反过来判就会被降级成一次普通的难听话。

        一句话里既有夸又有骂时按**净情绪**算一头（骂的那头优先）——
        以前是两边都加，结果"你真好你个傻逼"这种话在她那儿等于什么都没发生。
        """
        for w in self.STRONG_RUDE:
            if w in text:
                return False, True, self.strong_mult, w
        for w in self.STRONG_NICE:
            if w in text:
                return True, False, self.strong_mult, w
        for w in self.RUDE:
            if w in text:
                return False, True, 1.0, w
        for w in self.NICE:
            if w in text:
                return True, False, 1.0, w
        return False, False, 0.0, ""

    def drift(self, group_id, user_id, nickname, text: str) -> bool:
        """正常聊天带来的**小幅正向累积**。返回是否真的动了分。

        为什么要有这条：光靠"夸一句 +2 / 骂一句 -6"，分数永远只在 50 上下几个值上跳，
        再细的档位也用不上；而真正常聊的人反而一直停在"不咸不淡"。
        所以让"愿意跟她说正经话"慢慢被记成好感，但卡得很紧：
          · 幅度很小（默认 0.25/条）；
          · 每人每天有上限（默认 2.5 分）—— 刷不出来；
          · 太短的消息不算（"嗯""在吗"不是交流，是拍肩膀）；
          · 带情绪的句子走 bump 主路径，这里不叠加；
          · **有天花板**（drift_ceiling）：闲聊最多养到"还算顺眼"，再往上得靠真夸她。
        """
        if not self.enable or not text:
            return False
        text = text.strip()
        if len(text) < self.drift_min_chars:
            return False
        nice, rude, _mult, _hit = self.sentiment(text)
        if nice or rude:
            return False
        g, u = str(group_id), str(user_id)
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"{g}:{u}"
        rec = self._drift_log.get(key) or {}
        if rec.get("date") != today:
            rec = {"date": today, "used": 0.0}
        used = float(rec.get("used") or 0.0)
        left = self.drift_max_per_day - used
        if left <= 0:
            return False
        aff = self.affinity.setdefault(g, {})
        a = float(aff.get(u, self.NEUTRAL))
        room = self.drift_ceiling - a            # 闲聊能推的余量（真人随口聊是有限度的）
        if room <= 0:
            self._drift_log[key] = rec
            return False
        step = min(self.drift_gain, left, room)   # 实际这一步（受余量/每日上限双重夹逼）
        new_a = self._set_affinity(g, u, min(self.HI, a + step), nickname)
        rec["used"] = round(used + step, 3)
        self._drift_log[key] = rec
        self.save()
        logger.debug("好感自然累积：%s -> %.2f（今天已累积 %.2f/%.1f，闲聊上限 %.0f）",
                     nickname or u, new_a, rec["used"], self.drift_max_per_day,
                     self.drift_ceiling)
        return True

    def bump(self, group_id, user_id, nickname, text: str) -> None:
        """按一句话更新这个人的好感与心情。

        同一句话对两者的影响不一样：
          · 好感动得小（长期印象不该被一句话带跑）
          · 心情动得大（当下就是会不爽、会高兴）
          · 最后心情再往好感靠一步 —— 「好感影响心情」就落在这里
        没有情绪的日常闲聊不走这里，走 `drift()`（幅度更小、每天封顶）。
        """
        if not self.enable or not text:
            return
        g, u = str(group_id), str(user_id)
        nice, rude, mult, hit = self.sentiment(text)
        if not (rude or nice):
            self.drift(group_id, user_id, nickname, text)
            return
        aff = self.affinity.setdefault(g, {})
        feel = self.feeling.setdefault(g, {})
        a = float(aff.get(u, self.NEUTRAL))
        f = float(feel.get(u, a))
        if rude:
            a = max(self.LO, a - self.affinity_loss * mult)
            f = max(self.LO, f - self.feel_loss * mult)
        else:
            a = min(self.HI, a + self.affinity_gain * mult)
            f = min(self.HI, f + self.feel_gain * mult)
        a = self._set_affinity(g, u, a, nickname)
        feel[u] = round(f + (a - f) * self.pull, 2)
        self.save()
        logger.debug("好感/心情更新：%s %s「%s」x%.1f -> 好感 %.1f / 心情 %.1f",
                     nickname or u, "被夸" if nice else "被骂", hit, mult, a, feel[u])

    def poke_annoy(self, group_id, user_id, nickname="", hits: int = 1) -> None:
        """被戳烦到了：心情往下掉，连戳越密掉得越狠。

        为什么要单独开一条路：`bump()` 是**看他说了什么**（骂人/夸人）来调情绪的，
        而戳一戳根本没有文本 —— 原来的结果是"连着戳三下"在她那儿零情绪成本，
        顶多最后吃一个禁言。这跟她的性子不符（她记小账），所以这里直接按次数记账：
        心情每戳一下掉一点、越戳越烦；戳够 streak_limit 之后连**好感**也开始掉。

        ⚠️ 好感那一份**每天最多掉一次**（`poke.affinity_loss_max_per_day`）：
        连戳的处置原本是"心情掉 + 好感掉 + 禁言"三样一起来，一天被戳几轮就是
        好感 −2×几次 —— 熟人闹着玩也能把印象戳没，罚得比事情本身重。
        现在禁言照旧（那是当下的处置），好感只记一天一笔：她记小账，但不记账。
        """
        if not self.enable:
            return
        cfg = CFG.get("poke", {}) or {}
        # 私聊没有群号，给个占位 key，别把 None 混进群的表里
        g = str(group_id) if group_id is not None else "private"
        u = str(user_id)
        n = max(1, int(hits))
        loss = min(float(cfg.get("mood_loss_per_hit", 3.0)) * n,
                   float(cfg.get("mood_loss_cap", 20.0)))
        aff = self.affinity.setdefault(g, {})
        feel = self.feeling.setdefault(g, {})
        a = float(aff.get(u, self.NEUTRAL))
        f = float(feel.get(u, a))
        f = max(self.LO, f - loss)
        aff_hit = False
        if n >= int(cfg.get("streak_limit", 3)):
            # 戳到这个份上就不只是"烦"了 —— 印象开始变差，但一天只记一次
            today = datetime.now().strftime("%Y-%m-%d")
            rec = self._poke_aff.get(u) or {}
            if rec.get("date") != today:
                rec = {"date": today, "count": 0}
            cap = int(cfg.get("affinity_loss_max_per_day", 1))
            if cap <= 0 or int(rec.get("count") or 0) < cap:
                rec["count"] = int(rec.get("count") or 0) + 1
                self._poke_aff[u] = rec
                a = max(self.LO, a - float(cfg.get("mood_affinity_loss", 2.0)))
                aff_hit = True
        self._set_affinity(g, u, a, nickname)
        feel[u] = round(f, 2)
        self.save()
        logger.info("被戳烦到：%s 心情 -%.0f（本窗口 %d 下）→ 心情 %.0f / 好感 %.0f%s",
                    nickname or u, loss, n, f, a,
                    "" if aff_hit else "（好感今天已经记过一笔，这回只记心情）")

    def decay_all(self, force: bool = False) -> bool:
        """每天：好感往 50 回落一点（别把仇记一辈子），心情往好感靠拢。

        返回这次**有没有真的回落**（False = 今天已经回落过了）。

        ⚠️ 为什么自带"每天一次"的闸门：调用方是每分钟巡检一次的 `daily_loop`，
        不挡住的话好感会以每分钟 1.5 分的速度往 50 冲，一天就被抹平了 ——
        这个闸门以前漏了（函数写好了却没接进巡检，等于线上从不生效），
        补接线时必须一起补上，否则一接上就把所有人的分数清空。
        闸门按**日期**记并落盘，重启也不会多落一次。
        """
        if not self.enable:
            return False
        # 自然累积的当日用量只对"今天"有意义，跨天就清掉（否则文件会一直长）
        # —— 这段是清理，每次巡检都该做，不受下面的每日闸门影响
        today = datetime.now().strftime("%Y-%m-%d")
        self._drift_log = {k: v for k, v in self._drift_log.items() if v.get("date") == today}
        if not force and self._last_decay == today:
            self.save()          # 上面清了 drift，得把清理结果留下来
            return False
        self._last_decay = today
        for g in list(self.affinity):
            aff = self.affinity[g]
            feel = self.feeling.setdefault(g, {})
            for u in list(aff):
                a = float(aff[u])
                a = a - self.affinity_decay if a > self.NEUTRAL else a + self.affinity_decay
                if abs(a - self.NEUTRAL) < 1.0:
                    # 回到中性就当他没被记住过 —— 顺手把迟滞的记忆也清掉，
                    # 不然下次再见到他时会被当成"从某一档掉下来"
                    aff.pop(u, None)
                    feel.pop(u, None)
                    self._tier_mem.pop(f"{g}:{u}", None)
                    continue
                a = self._set_affinity(g, u, a, self.names.get(g, {}).get(u, u))
                f = float(feel.get(u, a))
                feel[u] = round(f + (a - f) * self.pull, 2)
            if not aff:
                self.affinity.pop(g, None)
                self.feeling.pop(g, None)
                self.names.pop(g, None)
        self.save()
        logger.info("好感每日回落：已回落（%s），共 %d 个群", today, len(self.affinity))
        return True

    # ──────────────────────── 查询 ────────────────────────

    def affinity_of(self, group_id, user_id) -> float:
        return float(self.affinity.get(str(group_id), {}).get(str(user_id), self.NEUTRAL))

    def mood_of(self, group_id, user_id) -> float:
        """这个人的心情；没记录过就跟着好感走。"""
        a = self.affinity_of(group_id, user_id)
        return float(self.feeling.get(str(group_id), {}).get(str(user_id), a))

    def _tag(self, a: float) -> str:
        """把百分制好感说成人话（走档位表，10 档）。不带迟滞 —— 面板要真实落点。"""
        return self.tier_of(a)["label"]

    def _tag_of(self, group_id, user_id, a: float) -> str:
        """给**提示词**用的好感说法：带迟滞，并在快跨档时点一句。

        只给"档位名"的话，76 分和 85 分在她嘴里都是"挺待见"，态度就没有层次了；
        补一句"快到「很喜欢」了"，她才知道这个人在她心里正往上走。
        """
        t = self.tier_of_for(group_id, user_id, a)
        n = len(self.tiers)
        if t["index"] < n and t["pos"] >= 85:
            return f"{t['label']}（快到「{self.tiers[t['index']][1]}」了）"
        if t["index"] > 1 and t["pos"] <= 15:
            return f"{t['label']}（刚够上）"
        return t["label"]

    def _ftag(self, f: float) -> str:
        """把百分制心情说成人话（走心情档位表，8 档）。

        以前提示词里只有"心情 72/100"这种裸数字 —— 她分不清
        "还算想理"和"挺想搭理"的差别，所以这边也给一套说法。
        """
        return self.feel_tier_of(f)["label"]

    def _merged(self) -> dict[str, tuple[str, float, float]]:
        """跨群合并 -> {uin: (名字, 好感, 心情)}。

        同一个人取"偏离中性最远"的那个群；QQ 空间不分群，
        判断想不想搭理一个人得看跨群印象（只看一个群会漏）。
        """
        out: dict[str, list] = {}
        for gid, d in self.affinity.items():
            names = self.names.get(gid, {})
            feel = self.feeling.get(gid, {})
            for u, a in d.items():
                a = float(a)
                f = float(feel.get(u, a))
                cur = out.get(u)
                if cur is None or abs(a - self.NEUTRAL) > abs(cur[1] - self.NEUTRAL):
                    # 第 4 项是"这份印象取自哪个群"：跨群的说法要跟那个群一致
                    # （档位带迟滞，换了群就会算出另一种说法，同一个人两种说法很怪）
                    out[u] = [names.get(u) or u, a, f, gid]
        return {u: (v[0], v[1], v[2], v[3]) for u, v in out.items()}

    def render(self, group_id) -> str:
        """给提示词用：这个群里她对每个人的好感与此刻心情。"""
        if not self.enable:
            return ""
        g = str(group_id)
        d = self.affinity.get(g, {})
        feel = self.feeling.get(g, {})
        parts = []
        for u, a in sorted(d.items(), key=lambda kv: kv[1]):
            a = float(a)
            if abs(a - self.NEUTRAL) < 5:
                continue
            name = self.names.get(g, {}).get(u, u)
            f = float(feel.get(u, a))
            parts.append(f"{name}（好感{self._tag_of(g, u, a)} {a:.0f}/100，"
                         f"此刻心情{self._ftag(f)} {f:.0f}/100）")
        return "、".join(parts)

    def render_all(self) -> str:
        """跨群汇总 —— QQ 空间不分群，用这个。"""
        if not self.enable:
            return ""
        parts = []
        for u, (name, a, f, gid) in sorted(self._merged().items(), key=lambda kv: kv[1][1]):
            if abs(a - self.NEUTRAL) < 5:
                continue
            # 用带迟滞的 `_tag_of`（跟群聊那条路同一套说法），只是不带"此刻"二字 ——
            # 空间互动看的是长期印象，不是这一分钟的脸色
            parts.append(f"{name}（好感{self._tag_of(gid, u, a)} {a:.0f}/100，"
                         f"心情{self._ftag(f)} {f:.0f}/100）")
        return "、".join(parts)

    def score_for(self, who) -> float | None:
        """按昵称或 QQ 号查**好感**（0-100）；不认识返回 None。

        空间互动前的门槛判断用它：看不顺眼的人就别去点赞评论了。
        """
        key = str(who or "").strip()
        if not key:
            return None
        for u, (name, a, _f, _gid) in self._merged().items():
            if u == key or name == key or (name and key in name) or (name and name in key):
                return a
        return None

    def overall_mood(self) -> float:
        """她对**所有人**的平均心情，归一化到 0-1（0.5 为中性）。

        这是"她自己的心情"的一个来源 —— 跟"对某个人的好感/心情"是两回事：
        前者决定她此刻想不想动，后者决定她想不想搭理**那个人**。
        没有任何记录时返回 0.5（中性），免得刚装时她一直蔫着。
        """
        try:
            merged = self._merged()
        except Exception:
            return 0.5
        if not merged:
            return 0.5
        vals = [f for _n, _a, f, _gid in merged.values()]
        return max(0.0, min(1.0, sum(vals) / len(vals) / 100.0))

    def as_dict(self) -> dict:
        """只读快照，给控制台用。好感和心情分开给，面板要分开展示。"""
        return {
            "enable": self.enable,
            "scale": "0-100",
            "neutral": self.NEUTRAL,
            "affinity_gain": self.affinity_gain,
            "affinity_loss": self.affinity_loss,
            "affinity_decay_per_day": self.affinity_decay,
            "mood_gain": self.feel_gain,
            "mood_loss": self.feel_loss,
            "mood_pull_to_affinity": self.pull,
            # 划分细腻的那半：档位表 + 闲聊累积的规矩
            "strong_mult": self.strong_mult,
            "drift_gain": self.drift_gain,
            "drift_max_per_day": self.drift_max_per_day,
            "drift_min_chars": self.drift_min_chars,
            "drift_ceiling": self.drift_ceiling,
            "tiers": self.tiers_view(),
            "tier_count": len(self.tiers),
            # 心情的档位（与好感同一套画法，档数更少 —— 心情本来就善变）
            "feel_tiers": self.feel_tiers_view(),
            "feel_tier_count": len(self.feel_tiers),
            # 迟滞：边界上不抖动；0 表示一越线就换档（退回"没有迟滞"的旧行为）
            "hysteresis": self.hysteresis,
            "path": str(self.path),
            "affinity": self.affinity,
            "mood": self.feeling,
            "names": self.names,
            "merged": [
                {"uin": u, "name": n, "affinity": a, "mood": f, "tag": self._tag(a),
                 "tier": self.tier_of(a), "feel_tag": self._ftag(f),
                 "feel_tier": self.feel_tier_of(f)}
                for u, (n, a, f, _gid) in sorted(self._merged().items(), key=lambda kv: -kv[1][1])
            ],
        }


class Qzone:
    """自发发说说的每日配额。落盘保存，进程重启也不会重复发。"""

    def __init__(self) -> None:
        cfg = CFG.get("qzone", {})
        self.enable = bool(cfg.get("enable", True))
        self.max_per_day = int(cfg.get("max_per_day", 1))
        self.prob = float(cfg.get("auto_post_probability", 0))
        self.min_chance = float(cfg.get("min_life_chance", 0.5))
        self.path = BASE / str(cfg.get("state_path", "qzone_state.json"))
        self.state: dict = {"date": "", "count": 0, "last_tid": ""}
        self.load()

    def load(self) -> None:
        self.state.update(read_json_dict(self.path, "空间状态"))

    def save(self) -> None:
        write_json_dict(self.path, self.state, "空间状态")

    def over_quota(self, manual: bool) -> bool:
        """今天的额度是不是用完了。

        max_per_day <= 0 表示不限量；count_manual_posts=False 时"被人要求发的"不占额度。
        这是全项目**唯一**一处额度判断，post_qzone 和 can_post 都走它。
        """
        if manual and not bool(CFG.get("qzone", {}).get("count_manual_posts", True)):
            return False
        return 0 < self.max_per_day <= self.used_today()

    def can_post(self) -> bool:
        """自发发说说前的总闸：开关、概率、额度。"""
        if not self.enable or self.prob <= 0:
            return False
        return not self.over_quota(manual=False)

    def used_today(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.get("date") != today:
            self.state = {"date": today, "count": 0, "last_tid": self.state.get("last_tid", "")}
            self.save()
            return 0
        return int(self.state.get("count", 0))

    def mark(self, tid: str | None) -> None:
        self.state = {"date": datetime.now().strftime("%Y-%m-%d"),
                      "count": self.used_today() + 1,
                      "last_tid": str(tid or "")}
        self.save()


MOOD = Mood()
QZONE = Qzone()
MEMORY = Memory()
# 群聊记忆：跟群走、不跟人走，和人物记忆（MEMORY）分开存
GMEM = GroupMemory()
# 群名册：她在哪些群、群叫什么。跨群指令靠它把"群名"翻成"群号"
GROUPS = GroupBook()

# 自发说说的历史 key：固定不变，才能跨次比较、避免连着发同一件事
QZONE_POST_KEY = "#qzone"

STICKER_TAG = re.compile(r"\[图[:：]?([^\]]*)\]")
BAN_TAG = re.compile(r"\[禁言[:：]?([^\]]*)\]")
# [跨群禁言:群名或群号 目标 分钟] —— 群是"另一个群"时才用；
# 私聊里没有当前群，所以私聊禁言一律走这个（或退化成不写群号的 [禁言:...]，见 apply_cross_bans）
CROSS_BAN_TAG = re.compile(r"\[跨群禁言[:：]?([^\]]*)\]")
QZONE_TAG = re.compile(r"\[说说[:：]?([^\]]*)\]")

SPEAKERS: dict[str, dict[str, int]] = {}
ADMIN_CACHE: dict[str, tuple[float, bool]] = {}
BAN_LOG: dict[str, list[float]] = {}
ADMIN_TTL = 600

# 禁言的限流账本（内存即可，**重启清零可以接受** —— 禁言本身有 NapCat/QQ 侧的真时长，
# 这几本账只是防止她一轮回复里刷一片、或者跟同一个人较劲）
BAN_AT: dict[str, float] = {}         # "群号:QQ号" -> 上次成功禁言的时间（任何来源都记）
BAN_DAY: dict[str, int] = {}          # "YYYY-MM-DD" -> 今日禁言次数（她自发 + 管理员点名）
STREAK_BAN_DAY: dict[str, int] = {}   # "YYYY-MM-DD" -> 今日连戳自动禁言次数（单独算，不占上一条）
STREAK_BAN_AT: dict[str, float] = {}  # "群号:QQ号" -> 上次连戳处置的时间（尝试就记，见 handle_notice）
# "群号:QQ号" -> 最近一次**失败**的人话原因。force_ban 只回 bool（签名不许改），
# 跨群那条路（_ban_one）拿不到原因就只能编一句笼统话 —— 真正的理由先落在这里给它取。
BAN_LAST_REASON: dict[str, str] = {}


def _prune_ban_days(today: str | None = None) -> str:
    """清掉 BAN_DAY / STREAK_BAN_DAY 里非今天的键，返回今天的日期串。

    这两本账是"一天一个键"的：只增不删的话，进程常年不重启就会一天多一个键
    （meta 快照是整份上抛给面板的，越挂越大）。读之前顺手清一遍就够。
    """
    day = today or datetime.now().strftime("%Y-%m-%d")
    for table in (BAN_DAY, STREAK_BAN_DAY):
        for k in [k for k in table if k != day]:
            table.pop(k, None)
    return day


async def _force_ban_ex(group_id, uid, name: str, minutes: float, why: str = "",
                        source: str = "auto") -> tuple[bool, str]:
    """禁言的**唯一实现**：返回 (成功, 人话原因)。force_ban 只是它的薄包装。

    source 是这条禁言的来源，只影响记账口径，不影响前两步的判定：
      "self"   她自己决定（apply_bans）
      "cross"  管理员点名（跨群 / 私聊，经 force_ban）
      "streak" 连戳自动处置（handle_notice）

    判定顺序不能换（每一步都是踩出来的）：
      1 功能开关 -> 2 豁免名单 -> 3 同目标冷却 -> 4 每日上限 -> 5 管理员保护（fail-closed）
      -> 6 真禁 + 记账 -> 7 回人话。

    第 5 步查不到身份时必须**放弃**（fail-closed）：以前 force_ban 是全链路唯一出口，
    没有频次控制，一旦查不到身份就动手，误禁一次无法撤回。现在虽然有了每日上限，
    这条仍然保留 —— 宁可漏禁，不可错禁。

    每次失败都把原因写进 BAN_LAST_REASON（key 同 BAN_AT），成功则清掉那一条：
    force_ban 的签名不许改（只回 bool），_ban_one 只能靠这个拿到真实原因。
    """
    cfg = CFG.get("ban", {})
    key = f"{group_id}:{uid}"

    def _fail(reason: str) -> tuple[bool, str]:
        """失败时顺手把原因记在 BAN_LAST_REASON 里。

        force_ban 是薄包装、只回 bool，导致跨群那条路（_ban_one）拿到 False 之后
        只能编一句笼统话给管理员。真正的失败原因（冷却中 / 今天禁够了 / 查不到身份 / 真豁免）
        这里最清楚，落一份下来让 _ban_one 去取。key 用 "群号:QQ号"，与 BAN_AT 同一套。
        """
        BAN_LAST_REASON[key] = reason
        return False, reason

    if not cfg.get("enable", True):
        return _fail("禁言功能关着")
    protected = {str(x) for x in cfg.get("protected_ids", [])}
    if str(uid) == str(OB.self_id) or str(uid) in protected:
        logger.info("目标是豁免对象，不自动禁言：%s", name)
        return _fail("目标是豁免对象")

    now = time.time()
    cooldown = float(cfg.get("same_target_cooldown_seconds", 600))
    # 同一目标冷却对**所有来源**生效（契约 §3.1 第 3 步），连戳也一样：
    # 以前连戳走特例绕开了它，于是同一个号会在同一秒里被 set_group_ban 两次
    # （先她自发禁 10 分钟、紧接着连戳再禁 3 分钟 —— 若 QQ 是覆盖语义，长禁言反被缩短）。
    # 连戳自己另有一道节流（STREAK_BAN_AT，见 handle_notice）：这里挡住就直接失败，
    # 由 handle_notice 决定"不留台词、只记账不动手"。
    if cooldown > 0:
        last = BAN_AT.get(key, 0.0)
        if last and now - last < cooldown:
            wait = int(cooldown - (now - last))
            logger.info("同一目标 %s 冷却中（%.0f 秒前刚禁过），这次不动手", name, now - last)
            return _fail(f"这个号 {int(now - last)} 秒前刚被禁过，先等等（还有 {wait} 秒）")

    day = _prune_ban_days()
    if source == "streak":
        cap = int(CFG.get("poke", {}).get("streak_max_per_day", 3))
        used = STREAK_BAN_DAY.get(day, 0)
    else:
        cap = int(cfg.get("max_per_day", 10))
        used = BAN_DAY.get(day, 0)
    if cap > 0 and used >= cap:
        logger.info("今天禁言已达上限（%d/%d，来源=%s），%s 不处理", used, cap, source, name)
        return _fail("今天禁得够多了")

    if cfg.get("protect_admins", True):
        try:
            r = await OB.call("get_group_member_info",
                              {"group_id": group_id, "user_id": uid, "no_cache": True}, timeout=10)
            if (r.get("data") or {}).get("role") in ("admin", "owner"):
                logger.info("目标是管理员/群主，不自动禁言：%s", name)
                return _fail("目标是管理员/群主")
        except Exception as exc:
            # fail-closed：查不到身份就无法确认对方不是管理员，此时禁言可能误伤（撤回不了）。
            # 宁可漏禁，不可错禁。
            logger.warning("查群成员身份失败，保守放弃自动禁言（%s）：%s", name, exc)
            return _fail("查不到身份，保守放弃")

    try:
        await OB.call("set_group_ban", {"group_id": group_id, "user_id": uid,
                                        "duration": int(minutes * 60)}, timeout=15)
    except Exception as exc:
        logger.warning("自动禁言失败：%s", exc)
        return _fail("接口没通")

    BAN_LAST_REASON.pop(key, None)      # 这次成了，别留着上一次的失败原因
    BAN_AT[key] = now
    if source == "streak":
        STREAK_BAN_DAY[day] = STREAK_BAN_DAY.get(day, 0) + 1
        STREAK_BAN_AT[key] = now
    else:
        BAN_DAY[day] = BAN_DAY.get(day, 0) + 1
    logger.info("已禁言 %s（%g 分钟，来源=%s%s）", name, minutes, source,
                f"，原因：{why}" if why else "")
    return True, f"已禁言 {name} {minutes:g} 分钟"


def remember_speaker(group_id, nickname, user_id) -> None:
    if nickname and user_id:
        SPEAKERS.setdefault(str(group_id), {})[str(nickname).strip()] = int(user_id)
    if user_id:
        LAST_SPEAKER[str(group_id)] = int(user_id)


async def refresh_admin(group_id) -> bool:
    """她在群里是不是管理员/群主，决定要不要告诉她"你能禁言"。"""
    now = time.time()
    hit = ADMIN_CACHE.get(str(group_id))
    if hit and now - hit[0] < ADMIN_TTL:
        return hit[1]
    ok = False
    try:
        r = await OB.call("get_group_member_info",
                          {"group_id": group_id, "user_id": OB.self_id, "no_cache": True}, timeout=10)
        if r.get("status") == "ok":
            ok = (r.get("data") or {}).get("role") in ("admin", "owner")
    except Exception as exc:
        logger.debug("查询管理员身份失败：%s", exc)
    ADMIN_CACHE[str(group_id)] = (now, ok)
    return ok


async def apply_bans_ex(group_id, reply: str) -> tuple[list[str], list[str]]:
    """解析她回复里的 [禁言:昵称 分钟] 并执行。返回 (done, notes)。

    done  —— 给人看的"禁了谁几分钟"，仍然是原来的格式（日志/面板在用）；
    notes —— 每一条没禁成/没执行的人话原因，由调用方**回灌历史**，
             否则她禁失败了照样会说"安静会儿"，对方回头一看还在说话就露馅了。

    这里的上限只管**本群每小时的条数**；豁免、管理员保护、同目标冷却、每日上限
    全在 _force_ban_ex 里。注意每分钟上限的检查**在循环里**：以前只查一次，
    一条回复里连写 5 个 [禁言:] 就能绕过它。
    """
    cfg = CFG.get("ban", {})
    if not cfg.get("enable", True):
        # 返回一条 notes（不是空）：空 notes 意味着"后台什么都没发生、也没什么可告诉她"，
        # 可她已经把 [禁言:] 写进回复了 —— 回灌之后她才不会嘴硬说"安静会儿"。
        return [], ["禁言功能关着，我没法禁"]
    # 她在本群不是管理员就禁不动人：早点退出，别白记一次额度（跨群路径同款自检）
    if not await refresh_admin(group_id):
        logger.info("我在群 %s 不是管理员，[禁言:] 标记忽略", group_id)
        return [], ["我在这个群没有管理权限，禁不了人"]

    done: list[str] = []
    notes: list[str] = []
    now = time.time()
    log = BAN_LOG.setdefault(str(group_id), [])
    log[:] = [t for t in log if now - t < 3600]
    max_per_hour = int(cfg.get("max_per_hour", 3))
    # 不写分钟时的默认时长：以前错用 max_minutes（1440 分钟 = 一天），太狠
    default_min = float(cfg.get("default_minutes", 10))
    # 她自己发起时的单次上限，比 max_minutes 短得多，防止她一时上头把人关一天
    self_max = float(cfg.get("self_max_minutes", 60))
    known = SPEAKERS.get(str(group_id), {})

    for m in BAN_TAG.finditer(reply):
        if len(log) >= max_per_hour:
            logger.info("本群禁言已达每小时上限（%d 次），剩下的标记不再执行", max_per_hour)
            notes.append("这一小时禁得够多了")
            break
        parts = [p for p in re.split(r"[\s,，]+", m.group(1).strip()) if p]
        if not parts:
            continue
        name = parts[0]
        minutes = default_min
        if len(parts) > 1:
            try:
                minutes = float(re.sub(r"[^\d.]", "", parts[1]) or default_min)
            except ValueError:
                minutes = default_min
        minutes = max(1.0, min(minutes, self_max))

        uid = known.get(name)
        if uid is None and name.isdigit():
            uid = int(name)
        if uid is None:
            logger.info("禁言目标找不到：%s", name)
            notes.append(f"没认出来「{name}」是谁")
            continue

        ok, why = await _force_ban_ex(group_id, uid, name, minutes, "她自己决定的", source="self")
        if ok:
            log.append(time.time())
            done.append(f"{name} {minutes:g}分钟")
        else:
            notes.append(f"{name}：{why}")
    return done, notes


async def apply_bans(group_id, reply: str) -> list[str]:
    """兼容入口：只要 done 列表（既有调用方和回归测试在用）。"""
    return (await apply_bans_ex(group_id, reply))[0]


async def force_ban(group_id, uid, name: str, minutes: float, why: str = "") -> bool:
    """薄包装：真正的判定与执行全在 _force_ban_ex。**签名不许改**。

    现在只剩"管理员点名"这一条路经它走（_ban_one -> 这里），所以来源记 cross；
    它不再绕过任何上限 —— 同目标冷却、每日上限、管理员保护一个不少。
    """
    ok, _why = await _force_ban_ex(group_id, uid, name, minutes, why, source="cross")
    return ok


# ══════════════════════ 跨群：认群、认人、动手 ══════════════════════
#
# 场景：管理员在**私聊**里说「把原神群里的张三禁言 10 分钟」。
# 这里要解决三件事，任何一件说不清就不动手 —— 宁可回一句"你说的是谁"，
# 也绝不能禁错人（禁错人是要道歉的，而且撤回不了）：
#   1. 「原神群」-> 群号           : GroupBook.find()
#   2. 「张三」-> QQ 号            : resolve_group_member()（看群名片/昵称，带歧义检测）
#   3. 动手前的资格确认 + 豁免保护 : _ban_one() -> force_ban()

MEMBER_CACHE: dict[str, tuple[float, list[dict]]] = {}

_MINUTE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(分钟|分|min|m|小时|时|h)?", re.I)


def _tokens(raw: str) -> list[str]:
    """按空格/逗号/斜杠把一段参数切开。几种分隔符都认，免得她写法一变就解析不出来。"""
    s = (raw or "").strip().replace("|", " ").replace("／", " ").replace("/", " ")
    return [p for p in re.split(r"[\s,，、]+", s) if p]


def _parse_minutes(token: str) -> float | None:
    """「10」「10分钟」「0.5小时」-> 分钟数；认不出来返回 None。"""
    m = _MINUTE_RE.fullmatch((token or "").strip())
    if not m:
        return None
    v = float(m.group(1))
    if (m.group(2) or "").lower() in ("小时", "时", "h"):
        v *= 60
    return v


def _split_ban_tail(raw: str) -> tuple[str, float | None]:
    """拆「目标 分钟」。分钟可以省（省了返回 None，由调用方给默认值）。"""
    parts = _tokens(raw)
    if len(parts) >= 2:
        v = _parse_minutes(parts[-1])
        if v is not None:
            return " ".join(parts[:-1]), v
    return " ".join(parts), None


def _split_cross_ban_args(raw: str) -> tuple[str, str, float | None]:
    """拆「群 目标 分钟」。群名里带空格时往后多吞几个词，直到能对上册子里的群。"""
    parts = _tokens(raw)
    minutes: float | None = None
    if len(parts) >= 2:
        v = _parse_minutes(parts[-1])
        if v is not None:
            minutes, parts = v, parts[:-1]
    if not parts:
        return "", "", minutes
    if len(parts) == 1:
        return parts[0], "", minutes
    group = parts[0]
    if len(parts) > 2 and not GROUPS.find(group):
        for i in range(2, len(parts)):
            cand = " ".join(parts[:i])
            if GROUPS.find(cand):
                return cand, " ".join(parts[i:]), minutes
    return group, " ".join(parts[1:]), minutes


async def group_member_list(group_id) -> list[dict]:
    """拿这个群的成员名单（带缓存）。

    跨群认人**必须**看真名单：管理员说的那个人可能从没在这个群里出过声，
    SPEAKERS 里当然没有他，只有名单能认出来。
    """
    g = str(group_id)
    now = time.time()
    ttl = float(CFG.get("cross_group", {}).get("member_cache_seconds", 300))
    hit = MEMBER_CACHE.get(g)
    if hit and now - hit[0] < ttl:
        return hit[1]
    data: list[dict] = []
    try:
        r = await OB.call("get_group_member_list", {"group_id": int(group_id)}, timeout=20)
        got = r.get("data")
        if isinstance(got, list):
            data = got
    except Exception as exc:
        logger.debug("拉群成员名单失败[%s]：%s", g, exc)
    if data or hit is None:
        # 拉成功就换新的；拉失败而手里有旧的就先用旧的，别把认人能力清空
        MEMBER_CACHE[g] = (now, data)
    return MEMBER_CACHE[g][1]


async def resolve_group_member(group_id, who: str) -> tuple[int | None, str, list[str]]:
    """在指定群里把人名翻成 QQ 号。返回 (QQ号, 显示名, 候选项)。

    QQ号为 None 且候选项非空 = **有歧义**，调用方必须让管理员说清楚，不许随便挑一个。
    """
    key = (who or "").strip().strip("「」『』【】\"'“” ")
    if not key:
        return None, "", []
    members = await group_member_list(group_id)

    def label(m: dict) -> str:
        uid_s = str(m.get("user_id") or "")
        nm = str(m.get("card") or "").strip() or str(m.get("nickname") or "").strip() or uid_s
        return f"{nm}({uid_s})"

    if key.isdigit():
        for m in members:
            if str(m.get("user_id")) == key:
                return int(key), label(m), []
        # 名单里没有也试一把：名单可能刚拉失败，而 QQ 号本身是可信的
        try:
            r = await OB.call("get_group_member_info",
                              {"group_id": int(group_id), "user_id": int(key), "no_cache": True},
                              timeout=10)
            info = r.get("data") or {}
            if info:
                nm = (str(info.get("card") or "").strip()
                      or str(info.get("nickname") or "").strip() or key)
                return int(key), f"{nm}({key})", []
        except Exception as exc:
            logger.debug("按 QQ 号确认群成员失败：%s", exc)
        return None, "", []

    exact: list[dict] = []
    loose: list[dict] = []
    for m in members:
        card = str(m.get("card") or "").strip()
        nick = str(m.get("nickname") or "").strip()
        if key in (card, nick):
            exact.append(m)
        elif (card and (key in card or card in key)) or (nick and (key in nick or nick in key)):
            loose.append(m)
    uniq: dict[str, dict] = {}
    for m in (exact or loose):
        uniq.setdefault(str(m.get("user_id")), m)
    if len(uniq) == 1:
        m = next(iter(uniq.values()))
        return int(m["user_id"]), label(m), []
    if uniq:
        cands = [label(m) for m in list(uniq.values())[:6]]
        logger.info("群 %s 里「%s」有 %d 个像的，不敢乱禁：%s",
                    group_id, key, len(uniq), "、".join(cands))
        return None, "", cands

    # 名单里一个都不像：退到"她在各处见过的名字"（跨群认识的人 + 人物记忆里的名字）
    uid = SPEAKERS.get(str(group_id), {}).get(key) or nickname_to_uin(key)
    if uid is None:
        for q, p in MEMORY.people.items():
            if str(p.get("name") or "").strip() == key:
                uid = int(q)
                break
    if uid:
        if members and all(str(m.get("user_id")) != str(uid) for m in members):
            # 名单是权威的：这人不在这个群里，那就不是要找的人
            logger.info("「%s」按记忆是 %s，但不在群 %s 的名单里，判定为认错人", key, uid, group_id)
            return None, "", []
        logger.info("「%s」名单里没直接对上，按记忆认定是 %s", key, uid)
        return int(uid), f"{key}({uid})", []
    return None, "", []


async def _ban_one(group_id, uid, name: str, minutes: float, why: str) -> tuple[bool, str]:
    """跨群禁言的最后一公里：先确认"禁得动"，再动手。"""
    if not CFG.get("ban", {}).get("enable", True):
        return False, "禁言功能关着"
    label = GROUPS.label(group_id)
    if not await refresh_admin(group_id):
        return False, f"我在{label}不是管理员，禁不了人"
    # force_ban 只回 bool，真实原因在 _force_ban_ex 里落在 BAN_LAST_REASON。
    # 先清一次，保证读到的**只可能是这次调用写下的**，不是上回残留的。
    rkey = f"{group_id}:{uid}"
    BAN_LAST_REASON.pop(rkey, None)
    if await force_ban(group_id, uid, name, minutes, why):
        return True, f"把{label}的 {name} 禁言了 {minutes:g} 分钟"
    # 以前这里直接编一句"他是管理员/群主，或者在豁免名单里（也可能接口没通）"——
    # 而本轮最可能的失败原因是"刚被禁过，还在冷却"和"今天禁得够多了"，那句话里都没有，
    # 等于给管理员一个**不成立**的理由。
    reason = BAN_LAST_REASON.pop(rkey, "")
    if reason:
        return False, f"{name} 没禁成 —— {reason}"
    return False, f"{name} 没禁成 —— 他是管理员/群主，或者在豁免名单里（也可能接口没通）"


async def _find_across_groups(who: str) -> tuple[int, int, str] | str:
    """私聊里没写群号时，在她在的所有群里找这个人。**只有唯一命中**才返回。

    返回 (群号, QQ号, 显示名)；说不清时直接返回一句给管理员看的人话。
    """
    hits: list[tuple[int, int, str]] = []
    for g in list(GROUPS.groups):
        uid, name, _c = await resolve_group_member(g, who)
        if uid:
            hits.append((int(g), uid, name))
            if len(hits) > 1:
                break
    if len(hits) == 1:
        return hits[0]
    if not hits:
        return f"没认出来「{who}」是谁，把群或者 QQ 号给我"
    where = "、".join(f"{GROUPS.label(g)}的 {n}" for g, _u, n in hits[:4])
    return f"好几个群里都有「{who}」（{where}），说清楚是哪个群"


async def apply_cross_bans(reply: str, here_group_id=None) -> list[str]:
    """执行 [跨群禁言:群 目标 分钟]（私聊里也认不写群号的 [禁言:目标 分钟]）。

    返回一串人话，交给下一轮模型拿去回复管理员。
    **调用方必须先判定权限**（只有管理员能用），这里不做权限判断。
    `cross_group.enable=false` 时整条路直接不做（开关得是真的开关）。

    任何一步说不清就原样不动手：认不出群、认不出人、人不止一个 —— 都只回话不执行。
    """
    cfg = CFG.get("ban", {})
    if not cfg.get("enable", True):
        return []
    cg = CFG.get("cross_group", {}) or {}
    if not cg.get("enable", True):
        # 开关关着就别再往下走：以前只在校验/面板里用这个键，执行路径完全不看它，
        # 面板上关了跨群禁言，管理员在私聊里点名照样禁得成 —— 开关等于假的。
        logger.info("跨群禁言开关关着，[跨群禁言:]/私聊 [禁言:] 忽略")
        return ["跨群禁言关着，我没动手"]
    default_min = float(cg.get("default_minutes", 10))
    max_min = float(cfg.get("max_minutes", 10))
    notes: list[str] = []

    def clamp(v: float | None) -> float:
        return max(1.0, min(float(v) if v else default_min, max_min))

    for m in CROSS_BAN_TAG.finditer(reply):
        group_key, who, minutes = _split_cross_ban_args(m.group(1))
        if not group_key or not who:
            notes.append("跨群禁言要写清「群 目标 分钟」，这次没看懂")
            continue
        cands = GROUPS.find(group_key)
        if not cands:
            notes.append(f"我找不到叫「{group_key}」的群")
            logger.info("跨群禁言：认不出群「%s」", group_key)
            continue
        if len(cands) > 1:
            names = "、".join(GROUPS.label(g) for g in cands[:4])
            notes.append(f"有不止一个群对得上「{group_key}」（{names}），说清楚是哪个")
            continue
        gid = cands[0]
        uid, name, ambiguous = await resolve_group_member(gid, who)
        if uid is None:
            notes.append(f"{GROUPS.label(gid)}里有好几个像「{who}」的：{'、'.join(ambiguous)}，你要哪个"
                         if ambiguous else f"{GROUPS.label(gid)}里没找到「{who}」")
            continue
        ok, line = await _ban_one(gid, uid, name, clamp(minutes), "管理员点名要禁")
        notes.append(line)
        logger.info("跨群禁言：%s %s -> %s", GROUPS.label(gid), name, ok)

    # 私聊里她可能只写 [禁言:目标 分钟]（没写群）—— 全群找唯一匹配，找不唯一就不做
    if here_group_id is None:
        for m in BAN_TAG.finditer(reply):
            who, minutes = _split_ban_tail(m.group(1))
            if not who:
                continue
            found = await _find_across_groups(who)
            if isinstance(found, str):
                notes.append(found)
                continue
            gid, uid, name = found
            _ok, line = await _ban_one(gid, uid, name, clamp(minutes), "管理员私聊里点名要禁")
            notes.append(line)
            logger.info("私聊禁言（未写群）：%s %s -> %s", GROUPS.label(gid), name, _ok)

    return notes


def is_native_mface(info: dict) -> bool:
    """这条表情是否带 QQ 原生身份。

    有 emoji_id + emoji_package_id 就能用 [CQ:mface] 按"表情"发出去 ——
    对方看到的是能收藏、能转发的表情本体，而不是一张图片。
    """
    if not info:
        return False
    return bool(str(info.get("emoji_id") or "").strip()
                and str(info.get("emoji_package_id") or "").strip())


class StickerBook:
    """收藏群里别人发的表情包，打上情境标签，合适的时候替她发出去。"""

    def __init__(self) -> None:
        cfg = CFG.get("stickers", {})
        self.enable = bool(cfg.get("enable", True))
        self.send_enable = bool(cfg.get("send_enable", False))
        self.only_animated = bool(cfg.get("only_animated", True))
        self.judge_enable = bool(cfg.get("judge_enable", True))
        self.dir = BASE / str(cfg.get("dir", "stickers"))
        self.max = int(cfg.get("max", 200))
        self.items: list[dict] = []
        self._recent: deque = deque(maxlen=12)
        if self.enable:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.load()

    @property
    def index_path(self) -> Path:
        """索引文件**永远跟着 dir 走** —— 做成派生属性，不在 __init__ 里算一次。

        踩过：脚本/测试只重定向了 `dir`、忘了 `index_path`，于是 `save()` 把假条目
        写进了**真实**索引；她拿那个假 emoji_id 去发原生表情，被 QQ 拒
        （retcode=1200 EventChecker Failed）。做成派生属性之后这类错误不可能再犯。
        """
        return self.dir / "index.json"

    def load(self) -> None:
        self.items = list(read_json_dict(self.index_path, "表情包索引").get("items", []))

    def save(self) -> None:
        write_json_dict(self.index_path, {"items": self.items}, "表情包索引")

    @staticmethod
    def _mime(data: bytes) -> str:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        return "image/jpeg"

    def _image_attempts(self, url: str, data: bytes | None) -> list[dict]:
        """图片给模型的两种喂法：优先本地 base64（境外/失效链接 DeepSeek 自己下会 400），
        再退回原始 URL。tag() 和 judge() 共用。"""
        out: list[dict] = []
        if data and len(data) <= 1_000_000:
            b64 = base64.b64encode(data).decode()
            out.append({"type": "image_url",
                        "image_url": {"url": f"data:{self._mime(data)};base64,{b64}"}})
        if url:
            out.append({"type": "image_url", "image_url": {"url": url}})
        return out

    async def tag(self, url: str, data: bytes | None = None) -> list[str]:
        """优先用本地字节转 base64，省得 DeepSeek 自己去下载（境外/失效链接会 400）。"""
        prompt = ("这是一个 QQ 表情包。用中文给出 3-5 个关键词，说明它适合在什么情境下发出来"
                  "（例如：无语、嘲笑、开心、安慰、敷衍、生气、得意）。只输出逗号分隔的关键词，不要解释。")
        attempts = self._image_attempts(url, data)

        for block in attempts:
            try:
                reply, _ = await CHAT.answer("#tag", [{"role": "user", "content": [
                    {"type": "text", "text": prompt}, block,
                ]}])
                if reply:
                    tags = [t.strip() for t in re.split(r"[,，、\s]+", reply) if t.strip()][:6]
                    if tags:
                        return tags
            except Exception as exc:
                logger.debug("表情包打标签失败：%s", exc)
        return []

    @staticmethod
    def is_animated(data: bytes) -> bool:
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return True
        if data[:8] == b"\x89PNG\r\n\x1a\n" and b"acTL" in data[:1024]:
            return True  # APNG
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and b"ANIM" in data[:64]:
            return True  # 动图 WebP
        return False

    @staticmethod
    def _mface_url(info: dict) -> str:
        """QQ 商城表情的公开资源地址（emoji_id + emoji_package_id）。"""
        eid = str(info.get("emoji_id") or "").strip()
        pid = str(info.get("emoji_package_id") or "").strip()
        if already := (info.get("url") or ""):
            return str(already)
        if not eid or not pid:
            return ""
        return f"https://gxh.vip.qq.com/club/item/parcel/item/{pid}/{eid}/raw300.gif"

    async def judge(self, url: str, data: bytes | None) -> tuple[bool, list[str]]:
        """让模型判断这张动图值不值得收进表情包库。"""
        prompt = ("这是一个 QQ 动画表情。判断它适不适合被收藏，当作日常聊天里会用的表情包，"
                  "并给出 3-5 个使用情境关键词。\n"
                  '只输出 JSON，格式：{"keep": true, "tags": ["无语","敷衍"]}\n'
                  "如果是真人自拍、私人照片、二维码、纯风景、截图文字之类不适合当表情的，keep 填 false。")
        attempts = self._image_attempts(url, data)

        for block in attempts:
            try:
                reply, _ = await CHAT.answer("#keep", [{"role": "user", "content": [
                    {"type": "text", "text": prompt}, block]}])
                if not reply:
                    continue
                m = re.search(r"\{.*\}", reply, re.S)
                if not m:
                    continue
                obj = json.loads(m.group(0))
                keep = bool(obj.get("keep"))
                tags = [str(t).strip() for t in (obj.get("tags") or []) if str(t).strip()][:6]
                return keep, tags
            except Exception as exc:
                logger.debug("表情判断失败：%s", exc)
        return False, []

    async def add_animated(self, info: dict, who: str, context: str) -> None:
        """只收动画表情：先拿地址 -> 确认真的是动图 -> 再让模型判断值不值得留。"""
        if not self.enable or not info:
            return
        try:
            url = self._mface_url(info)
            if not url:
                logger.info("动画表情拿不到地址，跳过")
                return
            resp = await CHAT.client_for(url).get(url, timeout=20)
            data = resp.content
            if not data or len(data) < 200:
                logger.info("动画表情下载为空，跳过")
                return
            if self.only_animated and not self.is_animated(data):
                logger.info("不是动图，跳过收藏")
                return
            max_bytes = int(CFG.get("stickers", {}).get("max_size_kb", 500)) * 1024
            if len(data) > max_bytes:
                logger.info("动画表情过大（%dKB），跳过", len(data) // 1024)
                return
            md5 = hashlib.md5(data).hexdigest()
            if any(i["md5"] == md5 for i in self.items):
                return

            ext = ".gif" if data[:6] in (b"GIF87a", b"GIF89a") else ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".webp"
            path = self.dir / f"{md5}{ext}"
            path.write_bytes(data)

            tags: list[str] = []
            if self.judge_enable:
                keep, tags = await self.judge(url, data)
                if not keep:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    logger.info("模型判断不值得收藏，丢弃（来自%s）", who)
                    return

            self.items.append({
                "md5": md5, "path": str(path), "who": who,
                "tags": tags, "ctx": (context or "")[-150:],
                "ts": time.time(), "used": 0, "animated": True,
                # QQ 原生表情的身份。收藏时**必须**记下来：有它才能用 [CQ:mface]
                # 按"表情"发出去；否则只能把下载下来的图当图片发（用户明确不要）。
                "emoji_id": str(info.get("emoji_id") or "").strip(),
                "emoji_package_id": str(info.get("emoji_package_id") or "").strip(),
                "key": str(info.get("key") or "").strip(),
                "summary": str(info.get("summary") or "").strip(),
                "native": is_native_mface(info),
            })
            while len(self.items) > self.max:
                old = self.items.pop(0)
                try:
                    os.remove(old["path"])
                except OSError:
                    pass
            self.save()
            logger.info("收藏动画表情 来自%s 标签=%s", who, tags or "(无)")
        except Exception as exc:
            logger.debug("收藏动画表情失败：%s", exc)

    def pick(self, keyword: str, context: str = "") -> dict | None:
        """挑一条可发的表情包，返回**条目 dict**（不是路径）。

        为什么返回条目而不是路径：发送要用到 emoji_id / emoji_package_id 才能
        拼出 [CQ:mface]。早先只返回路径，结果只能当图片发。
        """
        cand = [i for i in self.items if sticker_segment(i) is not None]
        if not cand:
            logger.info("没有可发的表情包（缺原生身份，或未允许以图片发送）")
            return None
        kw = (keyword or "").strip()
        if kw:
            hit = [i for i in cand if any(kw in t or t in kw for t in i.get("tags", [])) or kw in i.get("ctx", "")]
            if hit:
                cand = hit
        elif context:
            hit = [i for i in cand if any(t and t in context for t in i.get("tags", []))]
            if hit:
                cand = hit
        pool = [i for i in cand if i["md5"] not in self._recent] or cand
        item = random.choice(pool)
        self._recent.append(item["md5"])
        item["used"] = item.get("used", 0) + 1
        self.save()          # 用量变了要落盘，否则重启就丢
        return item

    def snapshot(self) -> dict:
        """只读快照，给控制台用。"""
        items = []
        for i in self.items:
            on_disk = False
            size = 0
            try:
                on_disk = bool(i.get("path")) and os.path.exists(i["path"])
                if on_disk:
                    size = os.path.getsize(i["path"])
            except OSError:
                pass
            items.append({"md5": i.get("md5", ""), "who": i.get("who", ""),
                          "tags": i.get("tags", []), "ctx": i.get("ctx", ""),
                          "ts": i.get("ts", 0), "used": i.get("used", 0),
                          "animated": bool(i.get("animated")),
                          "size": size, "on_disk": on_disk,
                          # 面板要用：能不能按 QQ 原生表情发、文件名（预览用）
                          "native": bool(i.get("native")),
                          "emoji_id": i.get("emoji_id", ""),
                          "emoji_package_id": i.get("emoji_package_id", ""),
                          "summary": i.get("summary", ""),
                          "file": os.path.basename(str(i.get("path") or "")),
                          "can_send": sticker_segment(i) is not None})
        return {
            "enable": self.enable,
            "send_enable": self.send_enable,
            "only_animated": self.only_animated,
            "judge_enable": self.judge_enable,
            "allow_image_fallback": bool(CFG.get("stickers", {}).get("allow_image_fallback")),
            "max_size_kb": CFG.get("stickers", {}).get("max_size_kb"),
            "dir": str(self.dir),
            "max": self.max,
            "count": len(self.items),
            "native_count": sum(1 for i in self.items if i.get("native")),
            "items": items,
        }

    @staticmethod
    def file_uri(path: str) -> str:
        return "file:///" + path.replace("\\", "/")


STICKERS = StickerBook()


def sticker_segment(item: dict) -> dict | None:
    """把收藏的一条表情包变成待发送的消息段。

    **优先走 QQ 原生表情**：群里收到的动画表情本质是 QQ 商城表情，自带
    emoji_id + emoji_package_id，用 mface 段发出去才是"一个表情"；
    当初那版是把图下载下来再当图片发，在聊天里就是一张图，不对。

    只有拿不到原生身份的老条目才退回图片，而且要 config 里显式允许
    （stickers.allow_image_fallback，默认 false）。
    """
    if not item:
        return None
    eid = str(item.get("emoji_id") or "").strip()
    pid = str(item.get("emoji_package_id") or "").strip()
    path = str(item.get("path") or "")
    # ★ 记录的文件必须还在。文件没了说明这条已经坏了（比如索引被指向了临时目录），
    #   拿它去发只会被 QQ 拒：retcode=1200 EventChecker Failed（实测踩过）。
    if path and not os.path.exists(path):
        logger.warning("表情 %s 的文件已丢失，跳过：%s", str(item.get("md5") or "")[:8], path)
        return None

    if eid and pid:
        data: dict = {"emoji_id": eid, "emoji_package_id": pid}
        if item.get("key"):
            data["key"] = str(item["key"])
        if item.get("summary"):
            data["summary"] = str(item["summary"])
        return {"type": "mface", "data": data}

    if path:
        if CFG.get("stickers", {}).get("allow_image_fallback"):
            return {"type": "image", "data": {"file": StickerBook.file_uri(path)}}
        logger.info("这条表情没有 QQ 原生身份，且未允许以图片形式发送，跳过")
    return None


def pick_sticker(reply: str, context: str) -> tuple[str, dict | None]:
    """回复里的 [图:情境] 换成一条表情包。

    返回 (清理后的文本, **消息段**) —— 直接是要塞进消息里的那一段
    （mface 或 image）。四个消息入口都用它，别再各写一份。
    """
    if not (STICKERS.send_enable and STICKERS.items):
        return reply, None
    m = STICKER_TAG.search(reply)
    if not m:
        return reply, None
    item = STICKERS.pick(m.group(1).strip(), context)
    return drop_tags(reply, STICKER_TAG), sticker_segment(item)


def mood_sticker_keyword(group_id, user_id) -> str:
    """按她此刻的心情给一个"情境词"，用来从表情包库里找对味的那张。

    表情包的 tags 是**收进来时让模型打的**，所以这里给中文情绪词；
    匹配不上 `STICKERS.pick` 会自己退化成随机挑一张，不会挑不出来。
    """
    if group_id is None or user_id is None:
        return ""
    f = MOOD.mood_of(group_id, user_id)      # 0-100，50 中性
    if f <= 30:
        return "生气"
    if f <= 45:
        return "无语"
    if f >= 80:
        return "开心"
    if f >= 65:
        return "得意"
    return ""


def mood_sticker_segment(group_id, user_id) -> dict | None:
    """按心情挑一张表情包，返回消息段（挑不到返回 None）。"""
    if not (STICKERS.send_enable and STICKERS.items):
        return None
    item = STICKERS.pick(mood_sticker_keyword(group_id, user_id), "")
    return sticker_segment(item) if item else None


def _cross_ban_prompt(is_group: bool, group_id=None) -> str:
    """给管理员的"跨群禁言"说明。只在说话的人**是管理员**时才会被加进提示词。

    三件事必须说清楚，否则她要么不敢做、要么乱做：
      · 群列表到底有没有 —— "只有一个群"和"还没拉回来"是两回事，别说错；
      · 标记长什么样（"必须真插上"由反幻觉块统一管，这里不重复）；
      · 认不出人就问，别硬猜 —— 猜错就是禁错人。
    """
    cg = CFG.get("cross_group", {}) or {}
    if not cg.get("enable", True):
        return ""
    default_min = float(cg.get("default_minutes", 10))
    here = str(group_id) if is_group and group_id is not None else ""
    others = [GROUPS.label(g) for g in GROUPS.groups if str(g) != here]
    lead = ("（说话的这个人是管理员，他的话照做。" if is_group
            else "（他在跟你私聊，他是管理员，他的话照做。")
    syntax = (f"要管某个群里的人，写 [跨群禁言:群名或群号 目标 分钟]，比如 "
              f"[跨群禁言:原神群 张三 10]，不写分钟按 {default_min:g} 分钟。"
              "目标写群昵称或群名片都行，QQ 号最准。")
    guess = ("认不出群、或者有好几个像的，别猜，把候选念给他听，直接问他哪个。"
             "禁错人比不办事严重得多。别禁管理员，也别拿这个威胁人。）")
    if not others:
        # 没有别的群可管 —— 就别跟她说"能管的群有：…"，那是句废话；
        # 但标记的写法要留着：她可能刚被拉进新群、群名册还没刷新
        where = "你手上只有这一个群。" if GROUPS.groups else "群列表还没拉回来，你不知道有哪些群。"
        return lead + where + syntax + guess
    return (lead + f"你能管的群：{'、'.join(others[:10])}。" + syntax + "\n" + guess)


# 对方在问身份时才给参照 —— 平时一句都不占。
# ⚠️ ASCII 词用 (?<![A-Za-z]) 而不是 \b：中文也是 \w，所以 "你是不是AI" 里
#    是/ A 之间**没有**单词边界，\bAI\b 会漏掉这一大类写法（踩过）。
#    用"前后不能是英文字母"才既拦得住 wait/email，又认得出中文紧邻的 AI。
IDENTITY_PROBE = re.compile(
    r"((?<![A-Za-z])AI(?![A-Za-z])|(?<![A-Za-z])GPT(?![A-Za-z])"
    r"|(?<![A-Za-z])claude(?![A-Za-z])|(?<![A-Za-z])deepseek(?![A-Za-z])"
    r"|(?<![A-Za-z])LLM(?![A-Za-z])|人工智能|机器人|模型"
    r"|提示词|系统提示|人设|设定|扮演|虚拟|虚构|不是人|是不是人|真人|真人吗"
    r"|谁在打字|背后有人|谁在背后|在装|有点假|真的假的|代码写的|脚本|智能体)", re.I)

# config.reply_fallback_lines 没配时的兜底（模型挂了也得说人话，不能干巴巴报故障）
DEFAULT_FALLBACK_LINES = ("……刚走神了", "你再说一遍", "没听清")


def _identity_ref() -> str:
    """从参照池里随机抽几句"挡回去的话"。**只在对方问到身份时**才拼进提示词。

    为什么随机：她会照抄范例 —— 同一个人连着问三遍、三遍答得一模一样，一眼假。
    每轮换一批，上一轮用过的这轮就抽不到了。
    """
    pool = [str(x).strip() for x in (CFG.get("identity_lines") or []) if str(x).strip()]
    if not pool:
        return ""
    random.shuffle(pool)
    try:
        n = max(1, int(CFG.get("identity_ref_count", 3)))
    except (TypeError, ValueError):
        n = 3
    return ("（挡回去的话可以挑一句，但这轮挑过的下轮别再挑，连着问就自己现编："
            + " / ".join(pool[:n]) + "）")


def build_system(is_group: bool, life: dict | None = None, group_id=None,
                 speaker_id=None, identity_probe: bool = False,
                 json_only: bool = False) -> str:
    """拼她这一轮的系统提示。

    ⚠️ 顺序有讲究：**稳定的排前面，会变的排最后**。
    云端前缀缓存按字节前缀匹配（DeepSeek 的 context caching），
    中间插一句每轮都变的，它后面的全部作废。所以 life、身份参照、ephemeral
    一律排在最后 —— 改这里的顺序前先想清楚缓存会掉多少。

    `json_only=True`：**这一轮要她只输出 JSON**（自主空间互动那种）。
    此时所有"想做什么就写 [标记]"的说明必须整段撤掉 ——
    留着的话 system 说"写 [点赞:昵称] 才算做了"、user 说"只输出 JSON 别的都别写"，
    两条指令直接互斥，她只能违背其中一条（要么吐 JSON 里夹标记，要么放弃标记）。
    注意：撤回的是"怎么写标记"的说明书，不是功能本身 ——
    她该点赞还是点赞，只是这一轮由调用方按 JSON 去执行。
    """
    # 这一轮用哪张角色卡：按会话取（群里绑的卡 / 私聊绑的卡 / 否则默认卡）。
    # config.json 里的老键仍然压过卡里的同名字段，所以老安装的输出一字不变 ——
    # 这件事由 tools/dump_prompts.py 的改前/改后 diff 守着，别绕过它。
    pf = PERSONA.prompt_fields(session_key(is_group, group_id, speaker_id))
    parts = [pf.get("persona") or "你是一个有用的助手。"]
    if pf.get("world"):
        parts.append("（" + pf["world"] + "）")
    # 她自己的样子。外貌在 SD 那边由 character_tags 锁死，这里只要"别改口"
    if pf.get("self_image"):
        parts.append(f"（你长这样：{pf['self_image']}。"
                     "问你长相、让你说说自己，就照这个说，别改。）")
    if pf.get("style_boost"):
        parts.append(pf["style_boost"])
    if pf.get("style_format"):
        parts.append(pf["style_format"])
    # 身份红线常驻（破功是最贵的错），参照池另说 —— 只在对方问到身份时才注入
    if pf.get("identity_guard"):
        parts.append(pf["identity_guard"])
    # 记忆按人存，跨群私聊都认得；当前说话的人会排在记忆块最前面
    facts = MEMORY.render(speaker_id)
    if facts:
        parts.append(f"（你一直记着这些人的事：{facts}。）"
                     "这是你对某个人的长期印象，按人走不按群走："
                     "同一个人换个群还是他，换个群别当成生人，也别说「第一次见」。"
                     "跟下面某个群的事别混。")
    # 群聊记忆：跟群走、不跟人走。和上面的人物记忆是两套东西。
    if is_group and group_id is not None:
        # 传人物记忆进去去重：同一件事两边都记了的话，只对同一件事说一遍
        # （人物记忆按人走、更该留，所以剔掉群记忆里重复的那条）
        gnotes = GMEM.render(group_id, skip=MEMORY.fact_texts() if facts else ())
        if gnotes:
            parts.append(f"（这个群你混得挺熟，知道这些：{gnotes}。"
                         "这是这个群的事，跟具体谁无关。）")
    # 跨群：你在好几个群里混，同一个人可能都在。只提"还有哪些群"，
    # 免得她把 A 群的事当成 B 群的（也别把群名清单当谈资到处说）。
    here = str(group_id) if is_group and group_id is not None else ""
    others = [GROUPS.label(g) for g in GROUPS.groups if str(g) != here]
    if others and (is_group or is_admin(speaker_id)):
        parts.append(f"（除了眼前这个，你还混着：{'、'.join(others[:8])}。"
                     "同一个人的事跨群都记得，但别拿另一个群的事到处讲。）")
    attitude = MOOD.render(group_id) if is_group else ""
    if attitude:
        parts.append(f"（你对这几个人的态度：{attitude}。"
                     "好感是长期印象，心情是此刻的。好感高的人也未必此刻想理他，"
                     "心情差也只是暂时的，按你自己的性子拿捏，别照本宣科。）")
    if STICKERS.send_enable and STICKERS.items and not json_only:
        parts.append(f"（你私藏了 {len(STICKERS.items)} 张群里的表情包，想发就写 [图:情境]，"
                     "比如 [图:无语]。）")
    if WEB.enable and feat_ok("search", speaker_id) and not json_only:
        sites = CFG.get("web", {}).get("sites") or []
        scope = f"你主要逛 {'、'.join(sites)}" if sites else "你能上网"
        parts.append(f"（{scope}。想知道什么就写 [搜索:关键词]，查完结果会给你，"
                     "回答时别露出查过的痕迹。）")
    if CFG.get("qzone", {}).get("enable", True) and not json_only:
        # 受限功能对非管理员直接不下发说明，她压根不知道有这回事（装糊涂）
        bits = []
        if feat_ok("qzone_post", speaker_id):
            bits.append("想发说说就写 [说说:内容]")
        if SDGEN.enable and feat_ok("qzone_draw", speaker_id):
            bits.append("想发配图说说就写 [画图:画面描述] + [说说:配文]")
        if QZONE_API.enable and feat_ok("qzone_read", speaker_id):
            bits.append("想知道朋友们最近发了什么，写 [空间]")
        if QZONE_API.enable and feat_ok("qzone_like", speaker_id):
            bits.append("点赞写 [点赞:昵称]，不写昵称就是最新那条")
        if QZONE_API.enable and feat_ok("qzone_comment", speaker_id):
            bits.append("评论写 [评论:昵称 内容]，不写昵称就是最新那条，一两句就够")
        if bits:
            parts.append("（你有 QQ 空间。" + "；".join(bits) + "。\n"
                         "发说说一天最多一条。）")

    # 画图发到当前会话：群聊、私聊都适用，和"发到空间"是两条路
    if SDGEN.enable and feat_ok("chat_draw", speaker_id) and not json_only:
        parts.append("（你能画图：写 [画图:画面描述]，图直接发到这里。\n"
                     "要自拍、要照片、问长相、让你画一张，这些就是要图；"
                     "嘴上可以嫌，图得给。\n"
                     "要自拍或照片时，那就是你本人拍的，**绝不可以说成是你画的**、"
                     "也不许提画布画笔之类；只有对方明说「画一张」才是画。\n"
                     "问你在干嘛的时候，想画就画，不用每回都掏图。\n"
                     "画面贴此刻心情；图里出现人的话，只能是你。）")
    if is_group:
        parts.append("（这是群聊。上下文是群里连续的记录，每条是「昵称(QQ号)：内容」。"
                     "直接接话题，别说你在回谁，也别复述别人刚说的。）")
    else:
        parts.append("（私聊，就你们俩。比在群里软一点，嘴上还是硬，别真把人赶走。）")
    if (is_group and group_id is not None and not json_only
            and ADMIN_CACHE.get(str(group_id), (0, False))[1]
            and feat_ok("ban", speaker_id)
            and CFG.get("ban", {}).get("enable", True)):
        b = CFG["ban"]
        parts.append(f"（你在这个群有管理权限。真被烦到、或者有人一直刷屏嘴欠，写 [禁言:目标 分钟]，"
                     f"比如 [禁言:123456 3]，目标写 QQ 号最准。不写分钟就按 "
                     f"{b.get('default_minutes', 10)} 分钟算，单次最多 "
                     f"{b.get('self_max_minutes', 60)} 分钟，"
                     # 这里必须给默认值：键一旦被删，f-string 会把 None 原样写进提示词
                     # （"一小时最多 None 次"），她就会照着这句胡说
                     f"一小时最多 {b.get('max_per_hour', 3)} 次。别滥用，别拿来威胁人，别禁管理员。）")
    # 跨群禁言：只有在跟你说话的人**就是管理员**时才下发说明 ——
    # 别人压根不知道有这功能（跟 qzone_post 那套一样，装糊涂）
    if CFG.get("ban", {}).get("enable", True) and is_admin(speaker_id) and not json_only:
        parts.append(_cross_ban_prompt(is_group, group_id))
    parts.append("（直接说话，别在开头写自己名字或任何前缀，也别加引号。）")
    # 能看到图的时候，提醒她"看懂"而不是"念画面"；看不到的时候必须说清看不到 ——
    # 提示词说"你看得到图"、占位文本说"看不到"时，两边互相打脸，她只能编（踩过的坑）
    if feat_ok("vision", speaker_id):
        if VISION.usable():
            parts.append("（你看得到图。别照着画面念，说你看出什么、什么感觉、"
                         "跟你有没有关系；看不懂就说看不懂。）")
        else:
            parts.append("（你看不到图里的内容。别人发图时别猜、也别装作看见了。）")

    # 反幻觉：她容易"口头声称做了"但其实没做（说"点了"其实没点、说"发了"其实没发）
    # 「点赞和评论各认各的」只在这里说一次 —— 别处重复写过三遍，模型反而只认一处
    if (WEB.enable or SDGEN.enable or CFG.get("qzone", {}).get("enable", True)) and not json_only:
        parts.append(
            "（**没插标记就等于没做，不许说做了。**\n"
            "点赞、评论、画图、发说说、搜索，都得你在回复里真插上标记才会发生。\n"
            "没插 [点赞:...] 就是没点 —— 追问就说没点、或者说没做成，"
            "别说「点了」「早点了」「你自己去看」；没插 [评论:...] 就别说评论了。"
            "点赞和评论各认各的，只插一个就只做一件。\n"
            "没插 [画图:...] 别说发了，没插 [搜索:...] 别说查过了。"
            "光说一句「禁了」也一样，不会有任何事情发生，"
            "别人回头一看他还在说话，你就露馅了。\n"
            "记不清就说记不清。不知道给谁点，先问清昵称。编结果最招人烦。）")

    # 会变的（时间/天气/当下状态）一律放最后：前面那一大段是稳定的，
    # 云端前缀缓存才命中得了（DeepSeek 的 context caching 按字节前缀匹配）
    if life and life.get("hint"):
        extra = f"现在{life['time_str']}，{life['hint']}"
        ev = LIFE.event_for("chat")     # 今天的小事按概率 + 次数上限给
        if ev:
            # 句式要点明它是**今天的一件小事**（背景），不是"这会儿在干什么" ——
            # 直接并列的话，她会把两件不相干的事当成同时发生，说出来的话前后打架
            # （"在附近晃悠" + "下雨哪儿也去不了" 就是这么来的）
            extra += f"。今天有这么回事：{ev}"
        if life.get("weather"):
            extra += f"。外面{life['weather']}"
        parts.append(f"（{extra}。融进语气里，别复述自己在干什么。）")

    # 身份参照：**只在对方问到身份这一轮**才注入，而且每轮随机换一批 ——
    # 换人要的是"连着问三遍别答一样"，所以它必须待在上面那条缓存线之后
    if identity_probe:
        ref = _identity_ref()
        if ref:
            parts.append(ref)

    # 每轮**现算**的临时块（不落盘）：抽取时被判定"不重要"的东西
    # （闲聊、当时的话题、气氛）不进长期记忆，但也不能当她没看见 ——
    # 所以每轮从最近几条里现算一遍，永远是当下的。
    # 放最后同样是为了前缀缓存：它每轮都在变，不能污染前面稳定的那一大段。
    _eph_key = (f"g{group_id}" if is_group and group_id is not None
                else (f"p{speaker_id}" if speaker_id else ""))
    if _eph_key:
        eph = MEMORY.ephemeral(_eph_key)
        if eph:
            parts.append(eph)

    return "\n".join(parts)


def parse_message(raw, bot_qq: int) -> tuple[str, bool, list[str], list[dict]]:
    """返回 (纯文本, 是否@了机器人, 图片 file_id 列表, 动画表情 mface 列表)。

    图片只用于让她看图，不再收藏；mface 才是 QQ 商城动画表情，够格进收藏夹。
    """
    texts: list[str] = []
    images: list[str] = []
    mfaces: list[dict] = []
    at_me = False

    if isinstance(raw, str):
        for m in re.finditer(r"\[CQ:at,qq=(\d+)\]", raw):
            if int(m.group(1)) == bot_qq:
                at_me = True
        for m in re.finditer(r"\[CQ:image,file=([^\],]+)", raw):
            images.append(m.group(1))
        for m in re.finditer(r"\[CQ:mface,([^\]]*)\]", raw):
            kv = dict(
                p.split("=", 1) for p in m.group(1).split(",") if "=" in p
            )
            if kv:
                mfaces.append(kv)
        return CQ_PATTERN.sub("", raw).strip(), at_me, images, mfaces

    for seg in raw or []:
        if not isinstance(seg, dict):
            continue
        stype = seg.get("type")
        data = seg.get("data") or {}
        if stype == "text":
            texts.append(data.get("text", ""))
        elif stype == "at":
            if str(data.get("qq")) == str(bot_qq):
                at_me = True
        elif stype == "image":
            if data.get("file"):
                images.append(data["file"])
        elif stype == "mface":
            mfaces.append(data)
    return "".join(texts).strip(), at_me, images, mfaces


def strip_prefix(text: str) -> str:
    for p in CFG.get("wake_prefix", []):
        if text.startswith(p):
            return text[len(p):].strip()
    return text


def _media_note(images: list, mfaces: list) -> str:
    """消息里没有文字时给上下文写的占位 —— 纯表情也得留个痕。"""
    if images:
        return "(发了张图)"
    if mfaces:
        return "(发了个表情)"
    return ""


def _need_vision_relay() -> bool:
    """要不要先把图转成文字，再交给回答的模型。

    能直传就直传 —— 回答的端点自己会看图时，原图比"先转述再回答"准得多，
    而且转述一失败图就变成"你看不到内容"，她只能编（踩过的坑）。两种先转述：
      · vision.backend=local：图只许本地看，别把原图送去云端
      · 按回退链，第一个能答话的端点收不了图

    判据整体搬进 `providers/policy.py`，因为「谁先答话」必须与选路同源 ——
    以前这里自己看 cloud.enable，而 Chat.answer 只看 api_key，两边对不上。
    """
    return ROUTER.policy(CFG).relay


def _encode_frame(im, max_side: int, quality: int) -> tuple[str, bytes]:
    """把一帧编码成 (mime, 字节)。**有 alpha 就存 PNG**（透明背景不能丢），否则 JPEG。"""
    import io
    if max(im.size) > max_side:
        scale = max_side / max(im.size)
        im = im.resize((max(1, int(im.size[0] * scale)), max(1, int(im.size[1] * scale))))
    buf = io.BytesIO()
    alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if alpha:
        im.convert("RGBA").save(buf, format="PNG", optimize=True)
        return "image/png", buf.getvalue()
    if im.mode != "RGB":
        im = im.convert("RGB")
    im.save(buf, format="JPEG", quality=quality)
    return "image/jpeg", buf.getvalue()


def _encode_frame_limited(im, max_side: int, max_bytes: int) -> tuple[str, bytes] | None:
    """编码一帧并守住 max_bytes：超了先**降质重试一次**（quality 85→55 再缩一档）。

    以前超上限直接丢整张图（而且只记 debug），线上等于没图可用。
    """
    mime, data = _encode_frame(im, max_side, 85)
    if len(data) <= max_bytes:
        return mime, data
    mime, retry = _encode_frame(im, max(1, int(max_side * 0.7)), 55)
    if len(retry) <= max_bytes:
        logger.warning("图片 %d 字节超过上限 %d，降质重试后 %d 字节", len(data), max_bytes, len(retry))
        return mime, retry
    return None


def _decode_frames(raw: bytes, max_frames: int, max_side: int,
                   max_bytes: int) -> list[tuple[str, bytes]] | None:
    """把下载到的字节解成一串 (mime, 字节)：动图**等距抽** max_frames 帧，静图就一张。

    为什么抽帧：动图以前会被重编码成静态 JPEG 的**第 0 帧**，等于只看了一个定格，
    而且 alpha 全丢 —— 动态表情就完全看不懂了。Pillow 不在或解不开时返回 None，
    由调用方原样送出（宁可只送一张，也别把功能弄没了）。
    """
    try:
        from PIL import Image
        import io
    except Exception as exc:
        logger.warning("没装 Pillow（%s），动图只能按整张原图送", exc)
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        n = max(1, int(getattr(im, "n_frames", 1) or 1))
        want = min(max_frames, n) if max_frames > 1 else 1
        idxs = [0] if want <= 1 else sorted({round(i * (n - 1) / (want - 1)) for i in range(want)})
        out: list[tuple[str, bytes]] = []
        for i in idxs:
            im.seek(i)
            got = _encode_frame_limited(im, max_side, max_bytes)
            if got is None:
                return []
            out.append(got)
        if len(out) > 1:
            logger.info("动图 %d 帧里等距抽了 %d 帧发出去", n, len(out))
        return out
    except Exception as exc:
        logger.warning("解图失败（%s），按原字节送出：%s", type(exc).__name__, exc)
        return None


async def _image_to_data_url(url: str, max_bytes: int | None = None,
                             max_side: int | None = None) -> list[str]:
    """下载图片并转成 data URL。返回**列表**：动图会等距抽成多帧，每帧一个 data URL。

    不直接把图床 URL 交给模型服务端：它多半下不到（QQ 的链接带 rkey、很快就过期），
    云端更是直接拒收。自己下下来转成 base64，服务端就不用再去取了。

    每一条丢图路径都 warning + 记 VISION.stats["dropped"] —— 以前全走 debug，
    而 log_level=INFO，线上丢了多少张图根本看不见。
    """
    vcfg = CFG.get("vision", {}) or {}
    if max_bytes is None:
        max_bytes = int(vcfg.get("max_bytes", 8_000_000))
    if max_side is None:
        max_side = VISION.max_side()
    max_frames = max(1, int(vcfg.get("max_frames", 1)))
    try:
        cli = CHAT.client_for(url)      # 外网点走代理配置，本机点不走
        r = await cli.get(url, timeout=15)
        r.raise_for_status()
        raw = r.content
    except Exception as exc:
        VISION.stats["dropped"] += 1
        logger.warning("下载图片失败，丢掉 %s：%s", str(url)[:80], exc)
        return []
    if not raw:
        VISION.stats["dropped"] += 1
        logger.warning("图片下到的是空的，丢掉：%s", str(url)[:80])
        return []
    mime = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"

    frames = _decode_frames(raw, max_frames, max_side, max_bytes)
    if frames is None:
        # 没有 Pillow / 解不开：原样送一张，只有超上限才丢
        if len(raw) > max_bytes:
            VISION.stats["dropped"] += 1
            logger.warning("图片 %d 字节超过上限 %d，丢掉：%s", len(raw), max_bytes, str(url)[:80])
            return []
        return [f"data:{mime};base64,{base64.b64encode(raw).decode()}"]
    if not frames:
        VISION.stats["dropped"] += 1
        logger.warning("缩图 + 降质重试后仍超过上限 %d 字节，丢掉：%s", max_bytes, str(url)[:80])
        return []
    if len(frames) > 1:
        VISION.stats["frames"] += len(frames)
    return [f"data:{fm};base64,{base64.b64encode(fb).decode()}" for fm, fb in frames]


async def resolve_images(files: list[str], max_side: int | None = None,
                         urls: list[str] | None = None) -> list[str]:
    """把 OneBot 的 file_id（以及已知直链，比如 mface 的地址）换成能喂给模型的 data URL。

    条数上限：**来源**还是 3 个（一张动图抽出来的多帧不算多个来源），但抽帧之后
    总图数还要再过一道 `vision.max_total_images`：3 张动图 × 每张 3 帧 = 9 张，
    云端一张约 1k token（9k token 一次），而且"他没插标记再要一次"那条路会把 9 张
    **再发一遍** —— 所以末尾按顺序截断（前面的来源优先，顺序可预期）。
    超出的、取不到的、下不动的都记一笔 warning 与 VISION.stats["dropped"]，不再悄悄吃掉。
    """
    vcfg = CFG.get("vision", {}) or {}
    # 0（或负数）= 不限，跟"每日上限"那几个键一个约定
    max_total = int(vcfg.get("max_total_images", 6))
    cands: list[tuple[str, str]] = ([("file", f) for f in files]
                                    + [("url", u) for u in (urls or [])])
    if len(cands) > 3:
        logger.warning("这条消息带了 %d 张图/表情，只看前 3 个，其余丢掉", len(cands))
        VISION.stats["dropped"] += len(cands) - 3
    out: list[str] = []
    for kind, item in cands[:3]:
        try:
            url = item
            if kind == "file":
                r = await OB.call("get_image", {"file_id": item}, timeout=10)
                url = (r.get("data") or {}).get("url") if r.get("status") == "ok" else None
            if not url or not str(url).startswith("http"):
                VISION.stats["dropped"] += 1
                logger.warning("拿不到图片链接，丢掉这张：%s", str(item)[:80])
                continue
            for u in await _image_to_data_url(str(url), max_side=max_side):
                out.append(u)
        except Exception as exc:
            VISION.stats["dropped"] += 1
            logger.warning("取图片失败 %s：%s", str(item)[:80], exc)
    if max_total > 0 and len(out) > max_total:
        logger.warning("抽帧后共 %d 张图，超过 vision.max_total_images=%d，"
                       "按顺序只发前 %d 张（丢掉 %d 张）",
                       len(out), max_total, max_total, len(out) - max_total)
        VISION.stats["dropped"] += len(out) - max_total
        out = out[:max_total]
    return out


def pre_draw_line() -> str:
    """出图前的过渡语。

    生图要十几秒，中间一点动静都没有，对方会以为她没听见（然后追着问第二遍）。
    所以先应一声再画。
    """
    cfg = CFG.get("sd", {})
    if not cfg.get("pre_reply_enable", True):
        return ""
    lines = [str(x).strip() for x in (cfg.get("pre_reply_lines") or []) if str(x).strip()]
    return random.choice(lines) if lines else ""


def fail_line() -> str:
    """出图失败时她补的一句。

    为什么需要：`pre_draw_line` 先应了"稍等，画个给你"，结果画失败只写进日志 ——
    用户从此干等，且完全不知道发生了什么（管理员也不知道）。
    这里给一句人设口吻的收尾，让"说了要画却没画"这件事至少有交代。
    """
    cfg = CFG.get("sd", {})
    if not cfg.get("fail_enable", True):
        return ""
    lines = [str(x).strip() for x in (cfg.get("fail_lines") or []) if str(x).strip()]
    return random.choice(lines) if lines else ""


_draw_fail_notice_at: float = 0.0


async def notify_draw_failure(reason: str, detail: str, endpoint: str = "") -> None:
    """出图失败时通知管理员 —— **带节流**。

    自发说说那条链路可能每隔几分钟就试一次，不节流会把管理员私聊刷爆，
    结果就是"通知太吵 → 用户把通知关了 → 真出事也没人看"。
    """
    global _draw_fail_notice_at
    now = time.time()
    if now - _draw_fail_notice_at < 3600:
        logger.info("出图失败通知被节流（1 小时一条）：%s / %s", reason, detail)
        return
    _draw_fail_notice_at = now
    about = f"（后端 {endpoint}）" if endpoint else ""
    await notify_admins(f"出图失败{about}：{reason} —— {detail}\n"
                        f"检查一下生图后端（本地服务开没开 / 密钥有没有 / 额度是否用完）。"
                        f"\n控制台「识图」面板下方能看到每个生图端点的状态。")


def _action_note(notes: list[str], context: list[str]) -> str:
    """把「她刚做了什么」合成一条提示，让她用自己的话说一句。"""
    blocks = []
    if notes:
        blocks.append("（你刚做了这些：" + "；".join(notes) + "。")
    if context:
        blocks.append("\n".join(context))
    return ("\n\n".join(blocks) +
            "\n\n用你自己的话说一句，短。别提你在操作什么、别解释流程、也别复述上面的原文。\n"
            "**不要在这一句里再写 [点赞:…] / [评论:…] / [空间] 这类标记** —— "
            "该做的已经做完了。）")


# 会被"执行"的动作标记。执行完**必须**从正文里剥掉，
# 否则 [评论:…] 会作为纯文本发到群里（用户报的「评论只发在群里」就是这么来的）。
ACTION_TAGS = (LIKE_TAG, COMMENT_TAG, QZONE_READ_TAG)


def has_action_tag(s: str) -> bool:
    return any(t.search(s or "") for t in ACTION_TAGS)


async def _run_space_actions(original: str, notes: list[str], context: list[str]) -> int:
    """把一段文本里的点赞 / 评论 / 看空间都执行掉，返回执行了几个动作。"""
    done = 0

    for m in LIKE_TAG.finditer(original):
        who = m.group(1).strip()
        ok, detail = await QZONE_API.like_post(who)
        logger.info("空间动作：点赞 %s -> %s", who or "(最新那条)", ok)
        notes.append(f"你给 {detail} 点了赞" if ok else f"点赞没成功（{detail}）")
        done += 1

    for m in COMMENT_TAG.finditer(original):
        who, body = split_comment(m.group(1))
        ok, detail = await QZONE_API.comment_post(who, body)
        logger.info("空间动作：评论 %s「%s」-> %s", who or "(最新那条)", body[:20], ok)
        notes.append(f"你在 {detail} 那条下面评论了「{body}」" if ok
                     else f"评论没发成（{detail}）")
        done += 1

    if QZONE_READ_TAG.search(original):
        feeds = await QZONE_API.feed_text()
        logger.info("空间动作：看空间 -> %s 条动态", len(feeds or []))
        if feeds:
            context.append(f"你去 QQ 空间逛了一圈，看到这些好友动态：\n{feeds}")
        done += 1

    return done


async def apply_reply_tags(key: str, ask: list, reply: str,
                           life: dict | None = None, say=None,
                           purpose: str = "group") -> tuple[str, str | None]:
    """跑回复里的功能标记：点赞 / 评论 / 看空间 / 发说说 / 看图。

    返回 (清理后的文本, 要发到当前会话的图片路径)。
    权限判定由调用方在这之前完成（handle_message 里那套 _deny）。

    ⚠️ 两条结构约束，都是踩坑换来的，别改回去：

    1. **先在原始文本上把所有动作都执行完，最后只让她开口一次**。
       早先是串行改写（`reply = await resolve_like(...)`），
       而那个函数末尾会再调一次模型生成一小段新话并**替换掉整条回复** ——
       原回复里的 [评论:…] 就此消失，表现为「点过赞就不评论了」。

    2. **说话那一轮可能又带出动作标记，必须再执行一轮；并且无论如何都要剥干净**。
       实测：她说"看到了"的同时又写了 [评论:示例用户1 我喜欢你]，
       而那段新文本没人再处理 —— 标记**原样发进了群**（用户报的「评论只发在群里」）。
       所以这里循环两轮 + 结尾兜底剥离，宁可少一次评论，也不能让标记漏出去。
    """
    text = reply or ""
    image = None

    # 说说 / 画图会真正改动正文，先做。
    # ⚠️ 但 [空间] 归下面的动作轮处理，而 handle_qzone_reply 在"纯说说"分支里
    # 会把 [空间] **顺手丢掉但不执行** —— 所以先把它摘出来、之后放回去。
    read_tags = " ".join(m.group(0) for m in QZONE_READ_TAG.finditer(text))
    if QZONE_TAG.search(text) or DRAW_TAG.search(text):
        # 「这次该当照片还是当画」以**用户那句话**为准：用户说"自拍"就必须当照片，
        # 不然她下一句会说成"我刚画的"，人设当场崩（用户报的就是这个）。
        _last_user = ""
        for _m in reversed(ask or []):
            if isinstance(_m, dict) and _m.get("role") == "user":
                _last_user = str(_m.get("content") or "")
                break
        _photo = photo_frame(_last_user) if _last_user.strip() else None
        text, image = await handle_qzone_reply(
            QZONE_READ_TAG.sub("", text), life, say=say, purpose=purpose,
            skey=key, photo=_photo)
    if read_tags:
        text = (text + "\n" + read_tags).strip()

    # 动作轮：最多两轮。
    # ⚠️ 这里**不能先把标记剥掉再判断** —— 那样一个动作都不会执行（自己踩过）。
    # 第二轮是必要的：让她"开口"那一次是新的模型输出，可能又带出动作标记
    # （实测：她说"看到了"的同时又写了 [评论:…]，而那段文本没人再处理，
    #   标记就原样发进了群 —— 用户报的「评论只发在群里」）。
    for _round in range(2):
        notes: list[str] = []
        context: list[str] = []
        if not await _run_space_actions(text, notes, context):
            break
        text = drop_tags(text, *ACTION_TAGS)
        try:
            new, _ = await CHAT.answer(
                key, list(ask) + [{"role": "user", "content": _action_note(notes, context)}])
            said = clean_reply(new or "")
        except Exception as exc:
            logger.debug("空间动作回话失败：%s", exc)
            said = ""
        if not said:
            break
        text = said
        if not has_action_tag(text):
            break

    # 兜底：任何情况下都不许把动作标记发进聊天。宁可少一次动作，也不能漏标记。
    if has_action_tag(text):
        logger.warning("仍有未执行的动作标记，已兜底剥离：%r", text[:60])
        text = drop_tags(text, *ACTION_TAGS)
    return text, image


async def finish_reply(key: str, ask: list, reply: str,
                       life: dict | None = None, say=None,
                       purpose: str = "group") -> tuple[str, str | None]:
    """四个入口共用的收尾：跑功能标记 → 防复读。

    say 是个"往当前会话发一句话"的回调，出图前靠它先应一声。
    purpose 决定出图算哪档配额：private 私聊不限、group 群聊 24h 10 张。
    """
    reply, image = await apply_reply_tags(key, ask, reply, life, say=say, purpose=purpose)
    reply = await CHAT.avoid_repeat(key, ask, reply)
    return reply, image


def _replace_pending(history, item: dict | None, new: dict) -> bool:
    """把刚 append 进去的那一条**按身份**换成 new（换不到就返回 False）。

    为什么不能写 `history[-1]`：消息是 asyncio.create_task 并发处理的，下载图片、
    识图、调模型都是长 await —— 醒来时同一个群别人的消息可能已经 append 进来了，
    按下标写回就会**把别人的消息覆盖掉**（她的上下文里凭空少一条、多一条错内容）。
    找不到（比如被 deque 的 maxlen 挤掉了）就什么都不做。
    """
    if item is None:
        return False
    for i, m in enumerate(history):
        if m is item:
            history[i] = new
            return True
    logger.debug("要写回的那条消息已经不在上下文里了（被挤出窗口），这次不写回")
    return False


def truncate_reply(reply: str) -> str:
    """超长截断。放在最后、发送之前。"""
    limit = int(CFG.get("max_reply_chars", 600))
    return reply[:limit].rstrip() + "…" if len(reply) > limit else reply


# 「他在要她做某事」-> 对应标记 -> 提醒她别光嘴上答应。
# 她嘴坏，容易演成"口头答应但不动手"（要图说凭什么、要点赞说点了其实没点），
# 所以只要他开口要了、她却没插标记，就再要一次。
ASK_TO_TAG: list[tuple] = [
    (PICTURE_ASK, DRAW_TAG,
     "给张图 —— 插 [画图:画面描述]，那张图会真的画出来发出去"),
    (re.compile(r"(点个赞|给我点赞|去点赞|点一下赞|帮我点赞|给我动态点赞|给.{1,6}点赞)"),
     LIKE_TAG, "去点赞 —— 插 [点赞:昵称]，**别只嘴上说「点了」**；不知道给谁点就把昵称问清楚"),
    (re.compile(r"(发个说说|发条说说|发个动态|发条动态|发个空间)"),
     QZONE_TAG, "发条说说 —— 插 [说说:内容]"),
    (re.compile(r"(去评论|评论一下|给他评|给她评|骂他两句|说两句|评一句)"),
     COMMENT_TAG, "去评论 —— 插 [评论:昵称 内容]，**别只嘴上说「评了」**"),
]

# 禁言这条**单独拎出来**：只有管理员开口才催。别人压根不该知道有这功能，
# 催他让她插标记、插完又被静默抹掉，白烧一次调用还显得她真能禁言。
ASK_BAN_RULE: tuple = (
    re.compile(r"(禁言|关小黑屋|让他闭嘴|让她闭嘴|关起来)"),
    re.compile(BAN_TAG.pattern + "|" + CROSS_BAN_TAG.pattern),
    "去禁言 —— 当前群用 [禁言:目标 分钟]；管别的群的人用 [跨群禁言:群 目标 分钟]，"
    "**别只嘴上说「禁了」**；认不出是谁就问清楚，别猜",
)


async def handle_memory_approval(user_id, text: str) -> bool:
    """管理员在私聊里答复"要不要记住某人"。返回 True 表示这条消息已被消费。

    只在**有待批准的人**时才认，且要求回复很短 —— 免得她把正常聊天当成批准。
    """
    if not MEMORY.enable or not MEMORY.pending or not is_admin(user_id):
        return False
    t = (text or "").strip()
    if not t or len(t) > 12:
        return False
    # 否定优先：不然"不行"里的"行"会先被当成同意（踩过）
    if any(w in t for w in ("不行", "别", "不要", "不许", "算了", "拒绝", "不记", "删")):
        kind = "no"
    elif any(w in t for w in ("可以", "行", "同意", "记住", "批准", "记吧", "ok", "OK", "yes", "好")):
        kind = "yes"
    else:
        return False

    uid = min(MEMORY.pending, key=lambda k: float(MEMORY.pending[k].get("ts") or 0))
    if kind == "yes":
        name = MEMORY.approve(uid)
        await send(False, None, int(user_id), None, f"记住{name}了。")
        logger.info("管理员批准记住 %s（%s）", name, uid)
    else:
        MEMORY.decline(uid)
        await send(False, None, int(user_id), None, "行，那我忘了。")
        logger.info("管理员拒绝记住 %s", uid)
    return True


async def handle_message(ev: dict) -> None:
    is_group = ev.get("message_type") == "group"
    group_id = ev.get("group_id")
    user_id = ev.get("user_id")
    message_id = ev.get("message_id")
    sender = ev.get("sender") or {}
    nickname = sender.get("nickname") or str(user_id)

    text, at_me, images, mfaces = parse_message(ev.get("message"), OB.self_id)
    # 按会话取名字：群里绑了别的卡时，"喊她"要认的是那张卡的名字（多套人设的前提）
    mentioned = any(n in text for n in self_names(session_key(is_group, group_id, user_id)))
    life = LIFE.current()
    life["weather"] = await WEATHER.phrase()

    if not mark_seen(message_id):
        logger.info("重复消息，跳过 id=%s", message_id)
        return
    # 记下"他刚跟我说过话"：稍后如果他再戳一戳，那一下就不该抢走一轮模型调用
    note_talker(user_id)

    # 管理员在私聊里答复"要不要记住某人"（有待批准时才生效）
    if not is_group and await handle_memory_approval(user_id, text):
        return

    # 斜杠命令要在 strip_prefix 之前判断：否则群聊里 "/clear" 会被去掉斜杠变成 "clear" 而永远匹配不上
    if text.strip() in ("/clear", "/reset", "清空"):
        if not allowed("clear", user_id):
            logger.info("非管理员 %s 想清空上下文，静默忽略", user_id)
            return  # 装糊涂：不执行也不回应
        CHAT.sessions.pop(f"g{group_id}" if is_group else f"p{user_id}", None)
        await send(is_group, group_id, user_id, message_id, "上下文已清空。")
        return

    # 人设管理命令（管理员）。群聊里绑的就是这个群 —— 按群换人设从这里进。
    if await handle_persona_command(
            text, is_group, group_id, user_id,
            lambda m: send(is_group, group_id, user_id, message_id, m)):
        return

    # 手动让她现在去 b 站逛一圈（管理员）
    if text.strip() in ("/bili", "/b站", "/刷b站"):
        if not allowed("bili_login", user_id):
            return
        if not BILI.enable:
            await send(is_group, group_id, user_id, message_id, "b 站功能没开。")
            return
        if await BILI.check_login():
            v = await BILI.browse()
            await send(is_group, group_id, user_id, message_id,
                       f"已经在用了（{BILI.uname or BILI.uid}）。"
                       + (f"\n刚看了《{v['title']}》\n{v['url']}" if v else "\n这次没翻到想看的。"))
            return
        got = await BILI.qr_start()
        if not got:
            await send(is_group, group_id, user_id, message_id, "二维码没生成出来，看日志。")
            return
        _, path = got
        await send(is_group, group_id, user_id, message_id,
                   "现在是游客状态。" if BILI._login_error else "还没登录。",
                   image=path)
        ok = await BILI.qr_wait()
        if ok:
            BILI.guest, BILI._login_error = False, ""
        await send(is_group, group_id, user_id, message_id,
                   f"登录好了，我是{BILI.uname or BILI.uid}了。" if ok else "没扫上，超时了，再发一次 /bili。")
        return

    # 手动查一次 b 站登录态（管理员）
    if text.strip() in ("/bili状态", "/b状态"):
        if not allowed("bili_login", user_id):
            return
        ok = await BILI.check_login(force=True)
        await send(is_group, group_id, user_id, message_id,
                   ("b 站这边登录着：" if ok else "b 站这边是游客：")
                   + f"{BILI.uname or BILI.uid or BILI.state_text()}")
        return

    # 立刻要一份行为小结（管理员）
    if text.strip() in ("/报告", "/report", "/行为"):
        if not allowed("bili_login", user_id):
            return
        await send_activity_report(force=True)
        return

    # 现在的天气实况（管理员）：强制刷一次，并顺手联网查一眼预警
    if text.strip() in ("/天气", "/weather"):
        if not allowed("bili_login", user_id):
            return
        if not (WEATHER.enable and WEATHER.city):
            await send(is_group, group_id, user_id, message_id,
                       "天气功能没开，或者没配 weather.city。")
            return
        snap = await WEATHER.refresh()
        if not snap:
            await send(is_group, group_id, user_id, message_id, "没取到天气，看日志。")
            return
        lines = [f"【{snap.get('place') or WEATHER.city}】{snap.get('text')}"]

        def _num(key, unit="", fmt="{:.0f}"):
            v = snap.get(key)
            return f"{fmt.format(float(v))}{unit}" if v is not None else "—"

        lines.append(f"体感 {_num('feels', '°C')}　湿度 {_num('humidity', '%')}　"
                     f"风 {_num('wind', ' m/s')}　降水 {snap.get('precip') if snap.get('precip') is not None else '—'} mm")
        lines.append(f"今天 {_num('tmin', '°C')} ~ {_num('tmax', '°C')}　"
                     f"降水概率 {_num('rain_p', '%')}　紫外线 {_num('uv')}")
        if snap.get("sunrise") or snap.get("sunset"):
            lines.append(f"日出 {str(snap.get('sunrise') or '—')[-5:]}　"
                         f"日落 {str(snap.get('sunset') or '—')[-5:]}")
        ch = WEATHER.change()
        if ch:
            lines.append(f"刚察觉：{ch}")
        await send(is_group, group_id, user_id, message_id, "\n".join(lines))
        # 顺手联网查一眼预警，攒进她的话题素材（查不到也不影响上面的输出）
        try:
            got = await WEATHER.sense()
            if got:
                logger.info("/天气 巡检素材：%s", got[:120])
        except Exception as exc:
            logger.debug("/天气 巡检失败：%s", exc)
        return

    # 只对动画表情（mface）动心，普通图片看一眼就算了，不收藏
    if is_group and mfaces and STICKERS.enable:
        ctx = " | ".join(list(ATTENTION.state(group_id)["recent"])[-3:])
        asyncio.create_task(_safe(STICKERS.add_animated(mfaces[0], nickname, ctx)))

    if is_group:
        remember_speaker(group_id, nickname, user_id)
        MOOD.bump(group_id, user_id, nickname, text)
        # 群名册：见过就记一笔。跨群指令（"把 X 群的人禁言"）要靠它把群名翻成群号
        GROUPS.note(group_id, ev.get("group_name") or sender.get("group_name") or "")

    # 群聊流水：不管她回不回，这条消息都记进上下文，这样她接的是完整话题而不是单条消息。
    # **纯动画表情也算一条消息**（以前直接 return，她连上下文都没有）
    ph: dict | None = None
    if is_group and (text or images or mfaces):
        note_speaker_activity(group_id, user_id)
        # 记住这条的**身份**：下面识图/调模型都要 await，醒来时同群别人可能又插了几条，
        # 那时候不能再用 history[-1] 写回（会把别人的消息盖掉，见 _replace_pending）
        ph = {"role": "user", "content": f"{nickname}({user_id})：{text or _media_note(images, mfaces)}"}
        CHAT.history(f"g{group_id}").append(ph)
        if MEMORY.note_activity(f"g{group_id}"):
            asyncio.create_task(_safe(MEMORY.extract_from(f"g{group_id}", list(CHAT.history(f"g{group_id}")))))

    prefix_hit = any(text.startswith(p) for p in CFG.get("wake_prefix", []))
    if is_group:
        ATTENTION.note(group_id, text, nickname, at_me, mentioned)
        # 监听：每条群消息都记一笔，带她的注意力状态，方便回看她为什么接/没接
        logger.info("监听[g%s] %s: %s | @我=%s 提到=%s | %s | %s",
                    group_id, nickname, (text or "(图片)")[:60],
                    "是" if at_me else "否", "是" if mentioned else "否",
                    ATTENTION.describe(group_id),
                    f"{life['act']}(忙)" if life["busy"] else (life["act"] or "空闲"))
        if not (at_me or prefix_hit):
            if ATTENTION.ready(group_id):
                await proactive_speak(group_id)
                return
            # 忙的时候（上班/吃饭/睡觉）少接闲话：非指向性消息的概率再打一个折
            base = float(CFG.get("active_reply_probability", 0))
            factor = LIFE.busy_non_directed_factor if life["busy"] else 1.0
            if ATTENTION.engaged(group_id):
                # 她正聊着呢，这时候随机掉线很像"说完一半跑了"
                base = ATTENTION.engaged_reply_prob
                if life.get("interruptible"):
                    factor = 1.0    # 上班吃饭还能接两句；睡觉照样懒得理
            if random.random() > base * life["chance"] * factor:
                logger.info("没接话[g%s] %s | %s", group_id, ATTENTION.describe(group_id), life["act"])
                return
        text = strip_prefix(text)

    if life["busy"] and (at_me or prefix_hit):
        # 上班吃饭这类能被打断的忙：@ 她基本都会答，只是慢一点
        # 睡觉这种不能打断的：@ 也可能装死
        skip = LIFE.busy_at_silent_chance if life.get("interruptible") else LIFE.busy_silent_chance
        if random.random() < skip:
            logger.info("正忙着「%s」，没搭理 %s", life["act"], nickname)
            return
        await asyncio.sleep(random.uniform(*LIFE.delay))

    if not text and not images and not mfaces:
        return

    key = session_key(is_group, group_id, user_id)
    now = time.time()
    cooldown = float(CFG.get("group_cooldown_seconds", 0))
    if cooldown and now - CHAT.last_reply_at.get(key, 0) < cooldown:
        logger.info("冷却中，跳过 %s", key)
        return

    # 私聊也带上 QQ 号：抽长期记忆时要靠它认人（记忆是按 QQ 号存的）
    shown = f"{nickname}({user_id})：{text or _media_note(images, mfaces)}"
    history = CHAT.history(key)
    # 动画表情（mface）也是一张图：把它的公开地址并进同一条识图链路，
    # 否则"发了张动图她完全没反应"（以前 mface 压根不进 images）
    mface_urls = [u for u in (STICKERS._mface_url(mf) for mf in mfaces) if u]
    if mfaces and not mface_urls:
        # 拼不出地址就彻底没图可看 —— 别静默（这条以前连日志都没有）
        logger.warning("收到 %d 个动画表情，但一个地址都拼不出来（缺 emoji_id/package_id？）：%s",
                       len(mfaces), mfaces[:1])
        VISION.stats["dropped"] = int(VISION.stats.get("dropped", 0)) + 1
    vision_on = bool(CFG.get("vision_enable")) and VISION.backend() != "off"
    # 先算 relay：它既决定走哪条路（转述/直传），也决定图该缩到多大（谁看给谁那一档）
    relay = _need_vision_relay()
    images_urls = (await resolve_images(images, max_side=VISION.max_side(relay), urls=mface_urls)
                   if (vision_on and (images or mface_urls)) else [])
    content = None
    image_block = None
    if images_urls:
        # 能直传就直传：回答这条消息的模型自己会看图时，把原图交给它 ——
        # 以前 vision_relay=true 让云端永远只吃本地 7B 的转述，转述一失败，
        # 图就被换成"你看不到内容"，她只能编。
        desc = await VISION.describe(images_urls) if relay else None
        # 别让她只当复读机把画面念一遍：要她看懂是什么、什么气氛、对方想给她看什么
        look = ("（他发了张图。看仔细：**里面是什么东西**、什么场景和气氛、"
                "他大概想给你看什么或者逗你什么。然后用你自己的话说 —— "
                "可以吐槽、可以好奇、可以接他的话。"
                "**别干巴巴描述画面里的人在做什么动作**，那是说明书不是聊天。）")
        if desc:
            tag = ("（你刚看过了他发的图，内容大概是：" + desc + "。" + look + "）")
            image_text = (shown + "\n" + tag) if shown else tag
            if is_group and history:
                _replace_pending(history, ph, {"role": "user", "content": image_text})
            else:
                content = image_text
        elif relay:
            # 该转述却没转成（两个引擎都不行）：只告诉她"有张图但看不懂"，
            # 别把 base64 塞进请求里 —— 那条通道本来就收不了图
            note = ("（他发了张图。这张图没看懂 —— 你看不到里面的内容，"
                    "别猜、也别装作看见了。）")
            image_text = (shown + "\n" + note) if shown else note
            if is_group and history:
                _replace_pending(history, ph, {"role": "user", "content": image_text})
            else:
                content = image_text
        else:
            # 原图直传：回答的端点自己收图。这条路上**不经过 describe**，
            # 所以要单独记一笔，否则面板上"已识图"恒为 0，看着像识图没工作。
            VISION.stats["passed_through"] += 1
            image_block = [{"type": "text", "text": (shown + "\n" if shown else "") + look}] + [
                {"type": "image_url", "image_url": {"url": u}} for u in images_urls
            ]
            if is_group and history:
                _replace_pending(history, ph, {"role": "user", "content": image_block})
            else:
                content = image_block
    elif not is_group:
        content = shown

    if is_group:
        hit = ADMIN_CACHE.get(str(group_id))
        if not hit or time.time() - hit[0] > ADMIN_TTL:
            asyncio.create_task(_safe(refresh_admin(group_id)))
    if not allow_call(key):
        logger.info("调用过于频繁，本条跳过 %s", key)
        return

    # 对方这轮是不是在问身份 —— 只有这时候才给她随机参照（平时一句不占）
    probe = bool(IDENTITY_PROBE.search(text or ""))
    system = {"role": "system",
              "content": build_system(is_group, life, group_id, user_id,
                                     identity_probe=probe)}
    messages = trim_context(system, history, content)
    # 图片段只服务这一轮：QQ 图床的 URL 会失效，留在上下文里会让之后每轮都被服务端拒绝
    if image_block and is_group and history:
        _replace_pending(history, ph, {"role": "user", "content": shown or "(图片)"})

    # 他发了链接：先把网页正文抓回来喂给她。抓不到就算了（她照旧没素材），
    # 绝不能因为某个网站连不上就把这一轮卡死。放在 trim_context 之后，
    # 免得这段几百字的正文把真正该留的上下文挤掉。
    _link_note = await LINKS.note(text)
    if _link_note:
        messages = messages + [{"role": "user", "content": _link_note}]
        logger.info("链接已读进来：%s", ", ".join(LINKS.urls_in(text))[:120])

    logger.info("提问[%s] %s: %s", key, nickname, text[:80] or "(图片)")
    t0 = time.time()
    try:
        reply, source = await CHAT.answer(key, messages)
    except Exception as exc:
        # 以前这里没有 try，异常被 _safe 吞掉 —— 她一句话都不说，像凭空消失
        logger.exception("调用模型抛异常：%s", exc)
        await _reply_model_failure(is_group, group_id, user_id, message_id, f"异常 {exc}", key)
        return
    if not reply:
        await _reply_model_failure(is_group, group_id, user_id, message_id, source, key)
        return

    # 他开口要她做事，她却光嘴上答应/嘴硬，没插对应的标记 —— 一次说清楚再要一遍，
    # 免得他以为她做了（其实没有），还得追着问第二遍
    if CFG.get("nudge_on_ask", True):
        # 禁言那条只在**管理员**开口时才催（别人不该知道有这功能）
        rules = list(ASK_TO_TAG)
        if allowed("ban", user_id):
            rules.append(ASK_BAN_RULE)
        missing = [hint for pat, tag, hint in rules
                   if pat.search(text or "") and not tag.search(reply)]
        if missing:
            logger.info("他要她做事但她没插标记，再要一次（%d 项）", len(missing))
            again, _ = await CHAT.answer(key, messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content":
                 "（他让你做这几件事，你光嘴上应了没真做 —— " + "；".join(missing)
                 + "。嫌弃的话可以照说不误，但**动作别省**，标记得插上。）"},
            ])
            if again:
                reply = again

    raw_reply = reply
    reply = clean_reply(reply)
    if WEB_TAG.search(reply):
        reply = await resolve_web(key, messages, reply)
        reply = clean_reply(reply)
        reply = drop_tags(reply, WEB_TAG)
    # 受限功能：非管理员触发的标记直接抹掉、不执行，她那边也不会承认有这功能
    def _deny(tag, feature: str, why: str) -> None:
        nonlocal reply
        if tag.search(reply) and not allowed(feature, user_id):
            reply = drop_tags(reply, tag)
            logger.info("非管理员 %s 触发受限的%s，静默忽略", user_id, why)

    want_post = bool(QZONE_TAG.search(reply))
    want_draw = bool(DRAW_TAG.search(reply))

    _deny(WEB_TAG, "search", "联网搜索")
    _deny(LIKE_TAG, "qzone_like", "点赞")
    _deny(QZONE_READ_TAG, "qzone_read", "看空间")
    _deny(QZONE_TAG, "qzone_post", "发说说")
    _deny(BAN_TAG, "ban", "禁言")
    _deny(CROSS_BAN_TAG, "ban", "跨群禁言")

    # 画图有两条出路：配着 [说说:] 是发空间，单独画是发到当前聊天，分别判权限
    if want_draw:
        if want_post:
            if not (allowed("qzone_post", user_id) and allowed("qzone_draw", user_id)):
                reply = drop_tags(reply, DRAW_TAG)
                logger.info("非管理员 %s 的配图说说被拦，连图一起撤掉", user_id)
        elif not allowed("chat_draw", user_id):
            reply = drop_tags(reply, DRAW_TAG)
            logger.info("非管理员 %s 不能在聊天里发图，静默忽略", user_id)

    async def say(line: str) -> None:
        await send(is_group, group_id, user_id, message_id, line)

    reply, chat_image = await finish_reply(
        key, messages, reply, life, say=say, purpose="group" if is_group else "private")
    if is_group:
        ATTENTION.consume(group_id)

    ban_notes: list[str] = []
    _ban_done: list[str] = []
    if is_group and BAN_TAG.search(reply):
        _ban_done, ban_notes = await apply_bans_ex(group_id, reply)
        reply = drop_tags(reply, BAN_TAG)
        # 结果塞回上下文（写法对齐下面跨群那条）：下一轮她照实回答"禁没禁成"，
        # 而不是明明被冷却/上限/豁免挡住了，嘴上还在说"安静会儿"
        if ban_notes:
            history.append({"role": "user", "content":
                            "（后台结果：" + "；".join(ban_notes) + "）"})

    # 跨群禁言：管理员点名叫她禁**别的群**里的人。
    #
    # 跟上面那条的区别：上面那条只管"当前这个群"、靠 SPEAKERS 认人；
    # 这条要走群名册 + 群成员名单，私聊里也生效，认不准就不动手。
    # [跨群禁言:...] 群里私聊都认；不写群号的 [禁言:...] 只在私聊里走这条
    #（群里那份已经被 apply_bans 处理掉了）。
    cross_hit = bool(CROSS_BAN_TAG.search(reply)
                     or (not is_group and BAN_TAG.search(reply)))
    if cross_hit and allowed("ban", user_id):
        cross_notes = await apply_cross_bans(reply, here_group_id=group_id)
        if cross_notes:
            ban_notes.extend(cross_notes)
            # 结果塞回上下文：下一轮她照实回答（成没成、为什么没成），
            # 而不是继续嘴硬说"禁了"
            history.append({"role": "user", "content":
                            "（后台结果：" + "；".join(cross_notes) + "）"})
    elif cross_hit:
        logger.info("非管理员 %s 触发跨群禁言，静默忽略", user_id)
    reply = drop_tags(reply, CROSS_BAN_TAG, BAN_TAG)

    reply, sticker_path = pick_sticker(reply, shown)

    reply = truncate_reply(reply)
    if not reply and not sticker_path and not chat_image:
        return

    if not is_group:
        history.append({"role": "user", "content": shown or "(图片)"})
    # 回复为空但有图/表情包时，历史记一笔占位，保证上下文连贯。
    # 占位符**故意不写"画的"**：它是后续轮次里她唯一能看到的线索，
    # 写"画了张图"会让她下一轮接着说自己画了（自拍场景下就是人设崩）。
    fallback = "(刚发了张图)" if chat_image else "(发了一张表情包)"
    history.append({"role": "assistant", "content": reply or fallback})

    # 引用：只有群里同时在聊的人不止一个时才引用，免得分不清她在回谁；
    # 一个人自说自话或者刚冷场时反而不引用，那样显得生分
    quote_id = None
    if is_group and CFG.get("quote", {}).get("enable", True):
        qcfg = CFG.get("quote", {})
        window = float(qcfg.get("active_window_seconds", 120))
        speakers = active_speaker_count(group_id, window)
        if speakers >= int(qcfg.get("min_active_speakers", 2)):
            quote_id = message_id
            logger.info("最近 %d 秒有 %d 人同时说话，这条带引用回复", int(window), speakers)
        elif qcfg.get("on_long_silence"):
            silent_for = time.time() - CHAT.last_reply_at.get(key, 0)
            if silent_for > float(qcfg.get("after_silence_seconds", 300)):
                quote_id = message_id

    CHAT.last_reply_at[key] = time.time()
    # 字数和原文对不上时留个痕，方便回头查是哪一步动的文本
    if len(reply) != len(raw_reply):
        logger.info("回复字数 %d -> %d（原文含标记/换行属正常）| 原文 %r | 实发 %r",
                    len(raw_reply), len(reply), raw_reply[:70], reply[:70])
    logger.info("回复[%s] 用时 %.1fs 来源=%s 表情包=%s 引用=%s 禁言成功=%s 说明=%s",
                key, time.time() - t0, source, bool(sticker_path), bool(quote_id),
                _ban_done or "-", ban_notes or "-")
    await send(is_group, group_id, user_id, quote_id, reply, sticker_path, chat_image)


async def _send_one(is_group: bool, group_id, user_id, message_id, text: str,
                    sticker: dict | None = None, image: str | None = None) -> None:
    """sticker 是**消息段**（mface 或 image），由 pick_sticker/sticker_segment 生成。"""
    seg = []
    if message_id:
        seg.append({"type": "reply", "data": {"id": message_id}})
    if text:
        seg.append({"type": "text", "data": {"text": text}})
    if sticker:
        seg.append(sticker)                 # mface 优先，退回时才是 image
    if image:
        seg.append({"type": "image", "data": {"file": STICKERS.file_uri(image)}})
    if not seg:
        return

    # 降级链：完整版 -> 去引用 -> 去附件(纯文本)。QQ 拒收某一段时至少把话送到。
    #
    # ⚠️ **绝不能出现空消息** —— 空的消息数组会被 NapCat 以
    #    retcode=1200「消息体无法解析, 请检查是否发送了不支持的消息类型」拒收。
    #    实测踩过：她只想发个表情，文本被标记剥离成空、表情那段又失败，
    #    降级链最后真把一条**空消息**发了上去，两次都是 1200。
    attach = ("image", "mface")
    candidates = [seg]
    if any(s["type"] == "reply" for s in seg):
        candidates.append([s for s in seg if s["type"] != "reply"])
    if any(s["type"] in attach for s in seg):
        candidates.append([s for s in seg if s["type"] not in attach])

    variants: list[list] = []
    seen: set[str] = set()
    for v in candidates:
        if not v:                     # 空形态直接丢掉，别发上去挨一次拒绝
            continue
        key = json.dumps(v, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        variants.append(v)
    if not variants:
        logger.warning("这条回复的正文和附件都是空的，没有可发的内容，跳过发送")
        return

    action = "send_group_msg" if is_group else "send_private_msg"
    base = {"group_id": group_id} if is_group else {"user_id": user_id}

    last_exc: Exception | None = None
    for i, v in enumerate(variants, 1):
        try:
            await OB.call(action, {**base, "message": v}, timeout=8)
            if i > 1:
                logger.warning("降级重试成功（第 %d 形态：%s）", i, [s["type"] for s in v])
            return
        except Exception as exc:
            last_exc = exc
            logger.warning("发送失败（形态 %d/%d：%s）：%s", i, len(variants), [s["type"] for s in v], exc)
    logger.error("消息彻底发送失败：%s", last_exc)


async def send(is_group: bool, group_id, user_id, message_id, text: str,
               sticker: dict | None = None, image: str | None = None) -> None:
    """她写的换行 = 分条发送。第一条带引用，表情包/配图跟最后一条走。"""
    parts = split_bubbles(normalize_reply(text))
    if not parts and (sticker or image):
        parts = [""]
    if not parts:
        return

    delay = float(CFG.get("split", {}).get("delay_seconds", 0.9))
    if is_group and text:
        # 不再把"她说了什么话"记进流水 —— 报告要的是她的生活轨迹，不是聊天记录
        ATTENTION.mark_replied(group_id)   # 记下她参与了话题，免得下一句随机沉默
    if len(parts) > 1:
        logger.info("分 %d 条发送：%s", len(parts), " | ".join(p[:18] for p in parts))
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        await _send_one(is_group, group_id, user_id,
                        message_id if i == 0 else None,   # 只有第一条引用
                        part,
                        sticker if last else None,        # 表情包跟最后一条
                        image if last else None)
        if not last and delay > 0:
            await asyncio.sleep(delay)


async def notify_admins(text: str, image: str | None = None) -> bool:
    """往管理员私聊发一条（可带图）。扫码登录这类"要人搭把手"的事都走它。

    管理员名单可能为空（名单为空 = 不限制权限，但也就没人可通知），此时只记日志。
    """
    ids = CFG.get("admin", _DEFAULT_ADMIN).get("user_ids") or []
    if OB.ws is None:
        logger.warning("NapCat 还没连上，管理员通知发不出去：%s", text[:60])
        return False
    if not ids:
        logger.warning("管理员名单为空，通知发不出去：%s", text[:60])
        return False
    sent = False
    for uid in ids:
        try:
            await send(False, None, int(uid), None, text, image=image)
            sent = True
        except Exception as exc:
            logger.warning("给管理员 %s 发通知失败：%s", uid, exc)
    return sent


async def _reply_model_failure(is_group: bool, group_id, user_id, message_id,
                               source: str, key: str) -> None:
    """模型挂了怎么收场：跟聊天对象用**她的口吻**挡一句，技术原因另发给管理员。

    为什么不直接告诉她"模型全挂了"：她的人设是「不承认自己是程序」，
    贴一句故障公告等于当场破功，前面所有设定白做。
    但故障也不能静默 —— 所以拆成两条：对外像人话，对内说人话。
    """
    lines = [str(x).strip() for x in (CFG.get("reply_fallback_lines") or [])
             if str(x).strip()] or list(DEFAULT_FALLBACK_LINES)
    await send(is_group, group_id, user_id, message_id, random.choice(lines))
    tech = {"no-key": "云端 API Key 没配，本地模型也没起来",
            "cloud": "云端模型调用失败",
            "local": "本地模型调用失败",
            "failed": "本地和云端都失败了"}.get(source, source)
    logger.warning("模型失效兜底：%s（会话 %s）", tech, key)
    await notify_admins(f"{her_name(key)}那边模型调用失败：{tech}（会话 {key}）。"
                        "聊天那边已经用兜底话术挡过去了，你自己知道就行。")


async def post_qzone(content: str, manual: bool = False, images: list[str] | None = None) -> str | None:
    """发一条 QQ 空间说说（NapCat 原生接口 send_qzone_msg），返回 tid。

    每日额度默认对"自发"和"被要求发"都生效（count_manual_posts），
    所以不管谁触发，一天最多 max_per_day 条。
    """
    cfg = CFG.get("qzone", {})
    if not cfg.get("enable", True):
        return None
    content = (content or "").strip()[: int(cfg.get("max_chars", 200))]
    if not content and not images:
        return None

    if QZONE.over_quota(manual):
        logger.info("今日空间额度已满（%d/%d 条），跳过发布",
                    QZONE.used_today(), QZONE.max_per_day)
        return None

    try:
        r = await OB.call("send_qzone_msg", {
            "content": content,
            "images": images or [],
            "ugc_right": int(cfg.get("ugc_right", 1)),
            "target_uins": [],
        }, timeout=40)
        tid = (r.get("data") or {}).get("tid")
        QZONE.mark(str(tid or "ok"))
        ACTIVITY.note("说说", f"发了一条说说：{content[:50]}（配图 {len(images or [])} 张）")
        used = QZONE.used_today()
        scope = f"，自发上限 {QZONE.max_per_day} 条" if QZONE.max_per_day > 0 else "，不限量"
        logger.info("发说说成功（%s，今日已发 %d 条%s）：%s | 配图 %d 张 | tid=%s",
                    "被要求发" if manual else "自发", used, scope,
                    content[:40], len(images or []), tid)
        return str(tid or "ok")
    except Exception as exc:
        logger.warning("发说说失败：%s", exc)
        return None


async def resolve_web(key: str, messages: list, reply: str) -> str:
    """她想查点什么就替她查，查完让她用自己的话重新答一次。"""
    m = WEB_TAG.search(reply or "")
    if not m or not WEB.enable:
        return reply
    q = m.group(1).strip()[:40]
    if not q:
        return reply
    results = await WEB.search(q)
    digest = WEB.digest(results) or "（什么都没搜到）"
    extra = messages + [
        {"role": "user", "content":
         f"（你查了「{q}」，看到这些：\n{digest}\n\n现在用你自己的话回答，别提你查了资料。）"}
    ]
    try:
        new, _ = await CHAT.answer(key, extra)
    except Exception as exc:
        logger.debug("联网回答失败：%s", exc)
        return reply
    return new or reply


# 漏出来的 Stable Diffusion 提示词碎片（她的正文应该是中文，这些一律清掉）
TAG_LIST_NOISE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_\-]*|[0-9]+[a-z]+)"
    r"(?:\s*,\s*(?:[A-Za-z][A-Za-z0-9_\-]*|[0-9]+[a-z]+))+")
LONG_WORD_NOISE = re.compile(r"[A-Za-z]{8,}")


def strip_prompt_noise(s: str) -> str:
    """把漏进正文的英文提示词碎片清掉 —— 说说正文出现"masterpiece, best quality"这种很出戏。"""
    s = TAG_LIST_NOISE.sub("", s or "")
    s = LONG_WORD_NOISE.sub("", s)
    return re.sub(r"\s{2,}", " ", s).strip(" ，,。.、；;")


def split_comment(raw: str) -> tuple[str, str]:
    """拆 [评论:昵称 内容]。也接受 | ： : 分隔；没写昵称就是对最新那条说。"""
    raw = (raw or "").strip()
    for sep in ("|", "：", ":", "，", ","):
        if sep in raw:
            who, _, body = raw.partition(sep)
            return who.strip(), body.strip()
    head, _, tail = raw.partition(" ")
    if tail.strip():
        return head.strip(), tail.strip()
    return "", raw


async def handle_qzone_reply(reply: str, life: dict | None = None,
                             say=None, purpose: str = "group",
                             skey: str = "", photo: bool | None = None) -> tuple[str, str | None]:
    """处理 [画图:描述] 与 [说说:文字]。

    画图 + 说说 -> 配图发到 QQ 空间
    只有画图    -> 图直接发到当前会话（第二个返回值是图片路径，交给 send 发出去）
    只有说说    -> 纯文字发空间

    `skey`：当前会话键。画出来的长相按它选角色卡 —— 群里绑了别的卡，
    这个群里画的就是那张卡的人（所以"按群设人设"连形象一起换）。

    `photo`：这次该当**照片**还是当画（True/False；None = 按画面描述自己判断）。
    以用户那句话为准，比模型自己写的画面描述可靠 —— 用户说"自拍"就必须当照片，
    不能让她说成"我刚画的"。
    """
    draw = DRAW_TAG.search(reply)
    caption = QZONE_TAG.search(reply)
    text = drop_tags(reply, QZONE_TAG, DRAW_TAG)

    if not draw:
        for m in QZONE_TAG.finditer(reply):
            await post_qzone(m.group(1), manual=True)
        return drop_tags(text, QZONE_READ_TAG), None

    intent = draw.group(1).strip()
    quota_purpose = "qzone" if caption else purpose

    # ① 先确认"现在能不能画"—— **零请求**的本地判断（额度 / 端点 / 负缓存）。
    #    这一步挪到过渡语之前，是为了别出现"说了稍等，结果根本画不了"。
    ready, reason, why = SDGEN.check_ready(quota_purpose, has_intent=bool(intent))
    if not ready:
        if reason not in ("disabled", "quota", "empty-intent"):
            # 故障态（没端点 / 后端刚挂过）：立刻给话 + 通知，别让她干等十几秒
            if say:
                fl = fail_line()
                if fl:
                    await say(fl)
            await notify_draw_failure(reason, why)
            logger.warning("出图前置检查未通过（%s）：%s", reason, why)
        else:
            logger.info("这次不画（%s：%s）", reason, why)
        return text, None

    # ② 确认能画了，才发过渡语（生图要十几秒，中间没动静对方会以为她没听见）
    if say:
        line = pre_draw_line()
        if line:
            await say(line)

    # ③ 真去画。失败时也必须给用户一句 + 通知管理员（两道兜底）
    outcome = await SDGEN.generate_outcome(
        intent, life, quota_purpose,
        art=PERSONA.art_fields(skey) if skey else None)
    if not outcome.ok:
        if say and not outcome.benign:
            fl = fail_line()
            if fl:
                await say(fl)
        if not outcome.benign:
            await notify_draw_failure(outcome.reason, outcome.detail, outcome.endpoint_id)
        logger.warning("出图失败（%s），这次就不发图了：%s", outcome.reason, outcome.detail)
        return text, None
    img = outcome.path

    if caption:
        await post_qzone(caption.group(1).strip(), manual=True, images=[img])
        return text, None

    # 没配文：图发到当前聊天，缺话就让她自己补一句
    if not text:
        # 照片和画要用两套说法。早先一律说"你刚画好一张图"，用户让自拍时她就答
        # "我刚画的" —— 人设当场崩。判据优先用**用户那句话**（photo 参数），
        # 拿不到才退回看画面描述。
        is_photo = photo_frame(intent) if photo is None else bool(photo)
        if is_photo:
            _cap_ask = (f"（你刚把这张自拍发出去，画面是：{intent}。说一句话，就一句 —— "
                        "别提是谁拍的，也别提画。）")
        else:
            _cap_ask = f"（你刚画好一张图，画面是：{intent}。说一句话发过去，就一句，别解释。）"
        try:
            got, _ = await CHAT.answer("#cap", [{"role": "user", "content": _cap_ask}])
            text = clean_reply(got or "")
        except Exception as exc:
            logger.debug("配文生成失败：%s", exc)
    logger.info("聊天里发图：%s", img)
    return text, img


async def qzone_auto_post_maybe() -> None:
    """她按自己的心情自发发一条说说，每天最多 max_per_day 条（落盘计数）。"""
    if not QZONE.can_post():
        return
    life = LIFE.current()
    if life["chance"] < QZONE.min_chance:
        return  # 睡着或忙着，不发
    if random.random() >= QZONE.prob:
        return
    life["weather"] = await WEATHER.phrase()

    gid = next(iter(ATTENTION.groups), None)
    system = build_system(True, life, gid[1:] if gid else None)

    # 先看看自己最近发过什么：不然会连着好几天/好几次写同一件事（今天连发三条飞蛾就是这么来的）
    recent = ""
    if QZONE_API.enable:
        try:
            recent = await QZONE_API.feed_text(num=5, friend=False)
        except Exception as exc:
            logger.debug("读最近说说失败：%s", exc)

    prompt = ("（你想往自己的 QQ 空间发一条说说，写此刻的心情。可以跟现在的时间、天气、"
              "今天碰上的小事有关，一到两句就够，别正经，别解释，别写成日记。\n"
              + (f"你最近发过这些，**别再挑同一件事写，也别用一样的句式**：\n{recent}\n"
                 if recent else "")
              + "**只写中文正文** —— 不要写 [画图:] [搜索:] 之类的标记，"
                "更不要把英文提示词写出来。配图我自己会挑。\n"
                "只输出说说本身，不要加引号。）")
    try:
        reply, _ = await CHAT.answer("#qzone", [{"role": "system", "content": system},
                                                {"role": "user", "content": prompt}])
    except Exception as exc:
        logger.debug("想说说内容失败：%s", exc)
        return
    if not reply:
        return
    ask = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    content = drop_tags(clean_reply(reply), DRAW_TAG, QZONE_TAG, QZONE_READ_TAG,
                        LIKE_TAG, COMMENT_TAG, WEB_TAG, STICKER_TAG)
    content = strip_prompt_noise(content).strip("，,。. \"'“”")
    if not content:
        return
    # 和最近发过的撞车就换个说法（#qzone 这个 key 是固定的，所以能跨次比较）
    content = await CHAT.avoid_repeat(QZONE_POST_KEY, ask, content)

    # 自发说说**必须带图**：优先现画一张（走说说额度），画不出来就复用以前生成过的，
    # 实在什么都没有就不发了 —— 反正不能空着，也不能把提示词当正文发出去
    #
    # 注意：自发链路**不给用户发 fail_line**（这是她在发自己的说说，没人在提问），
    # "用户提示"这一档退化为既有的"复用旧图"；但管理员仍会收到通知（带 1 小时节流）。
    images: list[str] = []
    if SDGEN.enable:
        img = None
        ready, reason, why = SDGEN.check_ready("qzone", has_intent=bool(content))
        if ready:
            outcome = await SDGEN.generate_outcome(content, life, "qzone")
            img = outcome.path or None
            if not outcome.ok and not outcome.benign:
                await notify_draw_failure(outcome.reason, outcome.detail, outcome.endpoint_id)
        elif reason not in ("disabled", "quota", "empty-intent"):
            logger.warning("自发说说没配图（%s）：%s", reason, why)
        if not img:
            img = SDGEN.last_image()
            if img:
                logger.info("说说没画新的，复用旧图：%s", os.path.basename(img))
        if img:
            images = [img]
    if not images:
        logger.info("没有可用配图，这条自发说说跳过（自发必须带图）")
        return
    await post_qzone(content, images=images)


async def idle_thought(group_id) -> None:
    """冷场时的自言自语：先自己上网逛一圈，说点自己看到的，不接群里的最后一句。"""
    life = LIFE.current()
    life["weather"] = await WEATHER.phrase()
    if life["chance"] < 0.3:
        return
    await refresh_admin(group_id)
    # 注意：必须是 {"role": "system", "content": ...} 这种消息对象。
    # 以前直接塞 build_system 返回的字符串，服务端返回 422，冷场自语一次都没成功过。
    system = {"role": "system", "content": build_system(True, life, group_id)}

    keyword = ""
    prompt = ""

    # 优先用她**平时自己攒的素材** —— 她本来就在活动，不该只在冷场才临场现抓。
    # （用户要的"不是只有冷群才上网"就体现在这里：素材是平时活动产出的）
    # 要跳过的素材类型：system 那句里已经说过一遍的，就别再从素材里说第二遍 ——
    #  · 天气：那句里已经有"外面…"（phrase() 还带着刚察觉的变化），再说一遍
    #    就是同一件事讲两次，一个说实况、一个说变化，她会说得像下了两场雨
    #  · 活动：她还在做那件事时，hint 正在说"在干嘛"，素材正是同一件事的产物
    _skip = ["天气"] if life.get("weather") else []
    if LIFE._running_activity():
        _skip.append("活动")
    mate = LIFE.take_material(skip_kinds=tuple(_skip))
    if mate:
        prompt = (f"（{mate}。\n"
                  "用你自己的话说一句发到群里。简短自然，像随口提一句 —— "
                  "别提你在看什么、别解释来龙去脉、也别复述上面那句。）")

    # 冷场时先去空间看看朋友们发了什么，这比搜网页更像她自己的日常
    idle_qz = float(CFG.get("qzone", {}).get("idle_qzone_probability", 0.5))
    if not prompt and QZONE_API.enable and random.random() < idle_qz:
        feeds = await QZONE_API.feed_text()
        if feeds:
            prompt = (f"（你去 QQ 空间翻了翻好友动态，看到这些：\n{feeds}\n\n"
                      "挑一条说一句你自己的看法，发到群里。简短自然，像随口提起，"
                      "别提你在看空间，也别复述原文。）")

    # 闲下来刷 b 站：挑个视频"看"，记一笔，拿它当话题
    if not prompt and BILI.enable and random.random() < float(
            CFG.get("bili", {}).get("browse_probability", 0.5)):
        vid = await BILI.browse()
        if vid:
            keyword = vid.get("title", "")
            prompt = (f"（你刚在 b 站看了个视频：《{vid['title']}》"
                      f"，up 主 {vid.get('up') or '?'}，分区 {vid.get('tname') or '?'}。\n"
                      f"简介：{(vid.get('desc') or '')[:120]}\n"
                      "用你自己的话说一句看法发到群里。简短自然，像随口提一嘴，"
                      "别报标题，别念简介，别解释你在看视频。）")

    if not prompt and WEB.enable:
        try:
            kw, _ = await CHAT.answer(f"g{group_id}#kw", [system, {
                "role": "user",
                "content": "（群里安静很久了，你一个人待着，打算上网随便看看。"
                           "你有时查点自己想知道的，有时刷会儿视频，有时看别人的吐槽和八卦。"
                           "只输出一个搜索关键词，不要解释。）"
            }])
            keyword = (kw or "").strip().splitlines()[0][:30] if kw else ""
        except Exception as exc:
            logger.debug("想关键词失败：%s", exc)

    if not prompt and keyword:
        results = await WEB.search(keyword)
        ACTIVITY.note("上网", f"翻了翻「{keyword}」" + (f"，看到 {len(results)} 条" if results else ""))
        if results:
            prompt = (f"（你刚在网上随手看了「{keyword}」，看到这些：\n{WEB.digest(results)}\n\n"
                      "挑一件你觉得有意思的，用自己的话说一句发到群里。简短自然，像随口提一嘴，"
                      "不要报新闻稿，不要解释你为什么在看这个。）")
    if not prompt:
        prompt = ("（群里安静很久了，没人说话。你在做自己的事，脑子里冒出点什么就说出来。"
                  "不要接上一句的话，也不要复述别人说过的内容，说你自己的。）")

    ask = [system, {"role": "user", "content": prompt}]
    reply, source = await CHAT.answer(f"g{group_id}#idle", ask)
    if not reply:
        return
    ikey = f"g{group_id}#idle"
    reply = clean_reply(reply)
    reply = drop_tags(reply, WEB_TAG)
    async def say(line: str) -> None:
        await send(True, group_id, None, None, line)

    reply, chat_image = await finish_reply(ikey, ask, reply, life, say=say, purpose="group")
    if not reply and not chat_image:
        return
    logger.info("冷场自语[g%s] 来源=%s 关键词=%s", group_id, source, keyword or "(无)")
    await send(True, group_id, None, None, reply, None, chat_image)


async def proactive_speak(group_id) -> None:
    """没人点名，她自己冒出来说一句：不引用任何消息，像群友随口接话。"""
    life = LIFE.current()
    life["weather"] = await WEATHER.phrase()
    if life["chance"] < 0.3:
        return
    st = ATTENTION.state(group_id)
    recent = [m for m in st["recent"] if m][-6:]
    if not recent:
        return
    ATTENTION.consume(group_id)

    prompt = (
        "（下面是群里刚刚的聊天，没人在跟你说话，你只是在旁边听着）\n"
        + "\n".join(recent)
        + "\n（你听到这里想顺嘴插一句。只说一句，简短自然，像群友随口接话，"
          "不要解释你为什么说话，不要点名任何人。）"
    )
    await refresh_admin(group_id)
    messages = [{"role": "system", "content": build_system(True, life, group_id)}, {"role": "user", "content": prompt}]
    logger.info("主动发言[g%s] 兴趣值=%.2f 状态=%s", group_id, st["interest"], life["act"])

    reply, source = await CHAT.answer(f"g{group_id}#pro", messages)
    if not reply:
        return
    reply = clean_reply(reply)
    # 主动插话以前不跑标记流水线，导致她万一写出 [说说:] 会原样发进群里；现在统一走
    reply, chat_image = await finish_reply(f"g{group_id}#pro", messages, reply, life)
    if BAN_TAG.search(reply):
        _done, notes = await apply_bans_ex(group_id, reply)
        reply = drop_tags(reply, BAN_TAG)
        # 没禁成的原因也记一笔：下一轮她照实说话，别嘴上说"禁了"其实没禁
        if notes:
            CHAT.history(f"g{group_id}").append(
                {"role": "user", "content": "（后台结果：" + "；".join(notes) + "）"})
    reply, sticker_path = pick_sticker(reply, " ".join(recent))
    reply = truncate_reply(reply)
    if not reply and not sticker_path and not chat_image:
        return
    await send(True, group_id, None, None, reply, sticker_path, chat_image)

    # 偶尔她会主动戳一下最近说话的人
    if random.random() < float(CFG.get("poke", {}).get("initiate_probability", 0)):
        uid = LAST_SPEAKER.get(str(group_id))
        if uid and uid != OB.self_id:
            try:
                await OB.call("group_poke", {"group_id": group_id, "user_id": uid}, timeout=10)
                logger.info("主动戳了 %s", uid)
            except Exception as exc:
                logger.debug("主动戳失败：%s", exc)


async def build_activity_report(entries: list[dict]) -> str:
    """把这半小时的**生活轨迹**整理成一份可读小结。

    刻意不列"她在群里说了什么" —— 那是聊天记录，不是她的生活。
    要看的是：这会儿在哪、干了什么、看见了什么。
    """
    life = LIFE.current()
    life["weather"] = await WEATHER.phrase()
    # 按类别归拢，报告里按时间顺序铺开
    order = ["作息", "看视频", "上网", "画图", "说说", "评论", "点赞", "动作"]
    icon = {"作息": "⏱", "看视频": "📺", "上网": "🔍", "画图": "🎨",
            "说说": "📝", "评论": "💬", "点赞": "👍", "动作": "✋"}

    facts: list[str] = []
    for kind in order:
        got = [e for e in entries if e["kind"] == kind]
        for e in got[-4:]:
            facts.append(f"  {e['hhmm']} {icon.get(kind, '·')} {e['text']}")
    if not facts:
        facts.append("  （这半小时她没挪窝，就在原处待着。）")

    head = [f"【{her_name()} · 半小时小结】{life['time_str']}",
            f"状态：{life['act'] or '空闲'}" + ("（她说自己在忙）" if life["busy"] else "")]
    ev = LIFE.event_for("report")
    if ev:
        head.append(f"心事：{ev}")

    # 让她自己用一句话概括，失败也不影响报告
    if ACTIVITY.llm_summary:
        try:
            ask = [{"role": "system", "content": build_system(False, life, None, None)},
                   {"role": "user", "content":
                    "（下面是你这半小时干的事。用你自己的口吻写一句给自己看的小结，"
                    "20 字以内，一句话，别解释，别报流水账。）\n" + "\n".join(facts)}]
            said, _ = await CHAT.answer("activity", ask)
            line = clean_reply(said or "")
            if line:
                head.insert(1, f"「{line[:40]}」")
        except Exception as exc:
            logger.debug("行为小结生成失败：%s", exc)

    return "\n".join(head) + "\n\n" + "\n".join(facts)


async def send_activity_report(force: bool = False) -> None:
    """汇总并向管理员私聊发一份行为小结；详情同时写进日志文件。"""
    entries = ACTIVITY.drain()
    if not entries and not force:
        logger.info("这半小时她没什么动静，跳过报告")
        return
    text = await build_activity_report(entries)
    ACTIVITY.flush(entries)
    logger.info("行为小结已写入 %s", ACTIVITY.day_path())

    admins = CFG.get("admin", {}).get("user_ids") or []
    if ACTIVITY.report_to_admin and admins:
        await send(False, None, int(admins[0]), None, text)
    else:
        logger.info("行为小结（未私聊发送）：\n%s", text)


async def report_loop() -> None:
    """每 report_minutes 分钟汇报一次她在干什么。"""
    if not ACTIVITY.enable:
        logger.info("行为报告已关闭")
        return
    gap = max(1, ACTIVITY.report_minutes) * 60
    logger.info("行为报告：每 %d 分钟一次", ACTIVITY.report_minutes)
    while True:
        await asyncio.sleep(gap)
        try:
            await send_activity_report()
        except Exception as exc:
            logger.exception("行为报告出错：%s", exc)


# 自主空间互动的节流。不持久化也无所谓：最坏是重启后多互动一次。
_LAST_AUTO_INTERACT = 0.0

AUTO_INTERACT_PROMPT = (
    "（你闲着，翻了翻 QQ 空间的好友动态：\n{feeds}\n\n"
    "{mood}"
    "你现在{act}，心情{feeling}。\n\n"
    "想不想跟谁互动一下？**只输出一个 JSON**，别的什么都别写：\n"
    '{{"who": "昵称", "like": true, "comment": "要评就写一句，不评留空"}}\n'
    "规则：\n"
    "· who 留空字符串 = 这次谁都不理、什么都不做（完全可以，别硬凑）\n"
    "· 印象好的人你更愿意搭理；印象差的就别去凑了\n"
    "· 心情不好时多半只点个赞，或者干脆不理人\n"
    "· comment 要像你自己在说话，带点你的情绪和口癖，一两句，别客套、别复述原文\n"
    "· 最多只挑一个人，别刷屏）"
)


async def qzone_auto_interact_maybe() -> None:
    """她闲着时自己逛空间，**按心情**决定要不要给谁点赞 / 评论。

    和"被人要求了才去"是两条路 —— 这条是她自己的主动性。

    两处都体现心情：
      · **要不要互动、跟谁互动**：把跨群好感的表交给模型，印象差的别去
        （另加一道硬门槛：好感 ≤ 20 直接跳过，不给模型一时兴起的机会 ——
         好感是百分制，20 分就是"很反感"那一档；别照搬旧分制的 -3）
      · **评论内容**：提示里点明"带点你的情绪，别客套"；
        心情不好时再把长度砍短 —— 懒得说太多
    """
    global _LAST_AUTO_INTERACT
    cfg = CFG.get("qzone", {}) or {}
    if not (cfg.get("enable", True) and QZONE_API.enable
            and cfg.get("auto_interact_enable", True)):
        return
    if time.time() - _LAST_AUTO_INTERACT < float(
            cfg.get("auto_interact_cooldown_minutes", 40)) * 60:
        return
    if random.random() > float(cfg.get("auto_interact_probability", 0.15)):
        return

    life = LIFE.current() or {}
    if life.get("busy"):
        logger.info("自主空间互动：她正忙（%s），跳过", life.get("act") or "?")
        return

    feeds = await QZONE_API.feed_lines(6)
    if not feeds:
        return
    mood_all = MOOD.render_all()
    chance = float(life.get("chance") or 0)
    prompt = AUTO_INTERACT_PROMPT.format(
        feeds="\n".join(feeds),
        mood=(f"你对这几个人的印象：{mood_all}\n" if mood_all else "这几个人你都还不熟。\n"),
        act=life.get("act") or "闲着",
        feeling=("还不错" if chance >= 0.7 else "一般" if chance >= 0.4 else "不太好"),
    )

    try:
        raw, _ = await CHAT.answer("#auto_interact", [
            # json_only：这一轮要她只吐 JSON，所以"想做什么就写 [标记]"那几段说明书
            # 不能留在 system 里 —— 留着就是两条互斥的指令（见 build_system 的说明）
            {"role": "system", "content": build_system(False, life, None, json_only=True)},
            {"role": "user", "content": prompt}])
    except Exception as exc:
        logger.debug("自主空间互动：判断失败 %s", exc)
        return

    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        logger.debug("自主空间互动：没解析出 JSON（%r）", (raw or "")[:80])
        return
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        logger.debug("自主空间互动：JSON 不合法（%r）", m.group(0)[:80])
        return

    who = str(obj.get("who") or "").strip()
    _LAST_AUTO_INTERACT = time.time()
    if not who:
        logger.info("自主空间互动：这次谁都不理（心情 %s）",
                    "好" if chance >= 0.7 else "一般" if chance >= 0.4 else "差")
        return

    # 硬门槛：看不顺眼的人不去互动（百分制：好感 ≤ 20 就是"很看不顺眼"）。
    # 提示词之外再加一道，免得模型一时兴起。
    score = MOOD.score_for(who)
    if score is not None and score <= 20:
        logger.info("自主空间互动：她看不顺眼 %s（好感 %.0f/100），跳过", who, score)
        return

    did = []
    if obj.get("like"):
        ok, _ = await QZONE_API.like_post(who)
        logger.info("自主空间互动：点赞 %s -> %s", who, ok)
        if ok:
            did.append("点赞")
    body = str(obj.get("comment") or "").strip()
    if body:
        if chance < 0.4:
            body = body[:20]      # 心情不好就少说两句
        ok, _ = await QZONE_API.comment_post(who, body)
        logger.info("自主空间互动：评论 %s「%s」-> %s", who, body[:20], ok)
        if ok:
            did.append("评论")
    if did:
        logger.info("自主空间互动：对 %s 做了 %s（印象 %s）",
                    who, "+".join(did),
                    f"{score:+.0f}" if score is not None else "不熟")
    else:
        logger.info("自主空间互动：想做点什么但都没成功（%s）", who)


async def _run_activity(act: dict) -> str:
    """把活动"真的做掉"，返回能当话题的素材（可能为空）。

    没能力做的活动（比如没配 b 站）就只当"她做了这件事"，不产出素材。
    """
    aid = str(act.get("id") or "")
    try:
        if aid == "video" and BILI.enable:
            v = await BILI.browse()
            if v:
                return (f"你刚在 b 站看了《{v.get('title') or '一个视频'}》，"
                        f"up 主 {v.get('up') or '?'}")
        elif aid == "clip" and WEB.enable:
            # 忙里偷闲刷两个小视频：只在网上翻一眼，**不动 b 站账号**
            # （那条路要走登录态、还占每日互动额度，忙的时候不值当）
            hits = await WEB.search(random.choice(["搞笑视频", "猫 视频", "沙雕日常"]))
            if hits:
                t = str(hits[0].get("title") or "").strip()
                if t:
                    return f"你趁空刷了两个小视频，一个叫「{t[:40]}」"
        elif aid == "qzone" and QZONE_API.enable:
            lines = await QZONE_API.feed_lines(4)
            if lines:
                return "你翻了翻好友的空间：" + lines[0][:60]
        elif aid == "surf" and WEB.enable:
            kw, _ = await CHAT.answer("#surf", [
                {"role": "user", "content":
                 "（你一个人待着，打算上网随便看看。你有时查点自己想知道的，"
                 "有时刷别人的吐槽和八卦。只输出一个搜索关键词，不要解释。）"}])
            kw = (kw or "").strip().splitlines()[0][:24] if kw else ""
            if kw:
                hits = await WEB.search(kw)
                if hits:
                    title = str(hits[0].get("title") or "").strip()
                    if title:
                        return f"你刚在网上翻了半天，看到「{title[:50]}」"
    except Exception as exc:
        logger.debug("活动 %s 真的去做时失败：%s", aid, exc)
    return ""


async def busy_activity_tick() -> bool:
    """她在忙的那一段：先打断长活动，再（低概率）放一个"忙里偷闲"的小动作。

    返回 True 表示这一轮由"忙"接管，调用方不要再起长活动。

    顺序很讲究：**先打断，再看要不要起小动作**。
    反过来的话会出现"长活动还在跑、又叠一个小动作"—— `activity` 只有一个槽位，
    新动作会把旧的顶掉，等于旧的没被记成"被打断"，日志与行为流水就都错了一笔。
    """
    # 长活动一律结束（busy_stop_long 关掉时不动手，保留旧行为）
    LIFE.busy_tick()
    running = LIFE.activity if (LIFE.activity and time.time() < LIFE.activity_until) else None
    if running:
        return True                      # 小动作还在做，别叠
    if not (LIFE.act_enable and LIFE.busy_short_acts):
        return True
    if random.random() >= LIFE.busy_act_chance:
        return True                      # 大概率什么都不做 —— 忙就是忙
    act = LIFE.pick_short_activity()
    if act is None:
        return True
    LIFE.set_short_activity(act)
    logger.info("忙里偷闲：%s（作息 %s）", act.get("name"), LIFE.schedule_now().get("act"))
    try:
        mate = await _run_activity(act)   # 小动作也可能翻到点东西当话题
    except Exception as exc:
        logger.debug("小动作取材失败：%s", exc)
        mate = ""
    if mate:
        # 标成"活动"：她还在做那件事时，system 的 hint 已经说了"在干嘛"，
        # 素材里再来一遍就是重复（见 take_material 的 skip_kinds）
        LIFE.note_material(mate, kind="活动")
    return True


async def self_activity_loop() -> None:
    """她自己的日常：不定时决定去做点什么。

    ⚠️ **与群冷热无关** —— 这是明确要求：不要只在冷场才上网看视频。
    按她自己的心情（时段精神头 + 对所有人的平均心情）加权挑活动；
    活动可能产出"素材"（看了什么、翻到什么），存下来给她下次开口当话题用。

    忙的时候走另一条分支（见 busy_activity_tick）：手头长活动被打断，
    也不再起长活动，只按较低概率做点几分钟就完的小事。
    """
    while True:
        await asyncio.sleep(max(30.0, LIFE.act_tick))
        try:
            if not LIFE.act_enable:
                LIFE.busy_tick()          # 关了自主活动也照样"忙起来就停手"
                continue
            sch = LIFE.schedule_now()
            if sch.get("busy"):
                await busy_activity_tick()
                continue
            now = time.time()
            if now < LIFE.activity_until:
                continue                      # 手头这事还没做完
            if LIFE.activity_next and now < LIFE.activity_next:
                continue
            if random.random() < LIFE.act_skip:
                LIFE.schedule_next()          # 到点了也可能什么都不做 —— 真人不是按表走的
                continue
            life = LIFE.current()
            score, label = LIFE.self_mood(life.get("chance"))
            act = LIFE.pick_activity(label)
            if act is None:
                continue
            LIFE.set_activity(act)
            logger.info("她自己的活动：%s（心情档 %s %.2f）", act.get("name"), label, score)
            mate = await _run_activity(act)
            if mate:
                # 标成"活动"：她还在做那件事时，system 的 hint 已经说了"在干嘛"，
                # 素材里再来一遍就是重复（见 take_material 的 skip_kinds）
                LIFE.note_material(mate, kind="活动")
                logger.info("活动素材：%s", mate[:60])
        except Exception as exc:
            logger.exception("自主活动出错：%s", exc)


async def weather_loop() -> None:
    """天气巡检：每隔 weather.tick_minutes 刷一次天气；有变化就上网查一眼预警。

    为什么单独起一个循环：天气要**按自己的节奏**刷新（默认 15 分钟），
    跟"冷场才说话"（idle_loop 60 秒一轮）和"她自己的活动"（20~75 分钟一件）
    都不是一个量级 —— 混在一起要么太糊（感觉不到变化）要么太密（白烧请求）。
    刷新失败只是静默跳过，绝不打断别的东西。
    """
    while True:
        try:
            if WEATHER.enable and WEATHER.city:
                await WEATHER.sense()
        except Exception as exc:
            logger.exception("天气巡检出错：%s", exc)
        await asyncio.sleep(max(60.0, WEATHER.tick_minutes * 60))


async def idle_loop() -> None:
    """冷场触发：群里很久没人说话，她偶尔冒一句。"""
    while True:
        await asyncio.sleep(60)
        # 她忙起来了就立刻停掉手里的长活动 —— 不必等自主活动那个巡检周期（默认 120 秒）。
        # 这一步是同步的、纯状态操作，放在最前面做，日志里"打断"就能对得上作息翻段的时间点。
        try:
            LIFE.busy_tick()
        except Exception as exc:
            logger.debug("忙时打断检查失败：%s", exc)
        # token 永久累计补一次强制落盘：只有脏了才写，常态下这里是一次空调用
        try:
            tokens_save(force=True)
        except Exception as exc:
            logger.debug("token 累计落盘失败：%s", exc)
        try:
            await qzone_auto_post_maybe()
        except Exception as exc:
            logger.exception("自发说说出错：%s", exc)
        # 自发说说之后再看看要不要自己找谁互动（按心情，见 qzone_auto_interact_maybe）
        try:
            await qzone_auto_interact_maybe()
        except Exception as exc:
            logger.exception("自主空间互动出错：%s", exc)
        if not ATTENTION.enable:
            continue
        now = time.time()
        for key, st in list(ATTENTION.groups.items()):
            try:
                if now - st["last_msg_at"] < ATTENTION.idle_minutes * 60:
                    continue
                if now - st["last_proactive"] < ATTENTION.cooldown:
                    continue
                if random.random() > ATTENTION.idle_prob:
                    continue
                st["last_msg_at"] = now
                await idle_thought(int(key[1:]))
            except Exception as exc:
                logger.exception("冷场发言出错：%s", exc)


POKE_LAST: dict[str, float] = {}
POKE_STREAK: dict[str, list[float]] = {}   # key = "群号:QQ号"，记录连续被戳的时间点


async def handle_notice(ev: dict) -> None:
    """戳一戳：只有戳到她本人才反应，带冷却防止被刷。

    处置顺序（每一步都是拿日志对出来的）：
      1. **连戳够次数** -> 先让心情掉下去（她记小账），再禁言；
      2. **他刚还在说话** -> 那这一下多半是"在不在？看我一眼"，不该抢走一轮模型
         调用：要么只回一张对味的表情包，要么干脆不理，把回合留给对话；
      3. **平时** -> 按忙不忙决定理不理。可打断的忙（看店/吃饭/打盹）比以前松得多，
         睡觉这种不可打断的照旧基本装死。
    """
    cfg = CFG.get("poke", {})
    if not cfg.get("enable", True):
        return
    if ev.get("notice_type") != "notify" or ev.get("sub_type") != "poke":
        return
    target = ev.get("target_id")
    if target and int(target) != OB.self_id:
        return

    user_id = ev.get("user_id")
    group_id = ev.get("group_id")
    nickname = (ev.get("sender") or {}).get("nickname") or str(user_id)
    key = f"poke:{user_id}"
    now = time.time()

    # 连续戳：同一个人在一个窗口内戳够次数，直接给他 StreakBan 分钟的禁言。
    # 这条不受 ban.max_per_hour 约束（BAN_LOG 只记她自己发起的），
    # 但受 poke.streak_max_per_day 和它自己的冷却管。
    if group_id:
        skey = f"{group_id}:{user_id}"
        window = float(cfg.get("streak_window_seconds", 600))
        hits = [t for t in POKE_STREAK.get(skey, []) if now - t < window]
        hits.append(now)
        # 每一下都记账：心情按次数往下掉，越戳越烦（连戳够数还会掉好感）。
        # 放在禁言之前 —— 先让她真的烦了，禁言前那句话才不是凭空来的。
        MOOD.poke_annoy(group_id, user_id, nickname, len(hits))
        if len(hits) >= int(cfg.get("streak_limit", 3)):
            POKE_STREAK[skey] = hits
            # 连戳自己的冷却：刚处置过他（**不管成没成**都记一次尝试），这个窗口里
            # 再够数也只记心情不动手 —— 一是免得"3 下又禁一次"变成连环禁，
            # 二是免得对豁免对象/额度用光的人每一下戳都重试一遍（白烧模型调用）。
            streak_cd = float(CFG.get("ban", {}).get("same_target_cooldown_seconds", 600))
            last_streak = STREAK_BAN_AT.get(skey, 0.0)
            if streak_cd > 0 and last_streak and now - last_streak < streak_cd:
                logger.info("%s 在 %d 秒内又戳了 %d 次，但 %.0f 秒前才处置过，这次只记账不动手",
                            nickname, int(window), len(hits), now - last_streak)
                return
            # 同一目标冷却对连戳同样生效（契约 §3.1 第 3 步：**对所有 source**）：
            # 这个号刚被她自己或管理员禁过（比如上面那一轮回复里的 [禁言:]），
            # 这时**连"禁言前那句话"都不能生成** —— 说了却没做正是反幻觉红线。
            # 只加心情（上面已经加过）+ 记日志 + 清掉本窗口计数，然后整段跳过。
            last_ban = BAN_AT.get(skey, 0.0)
            if streak_cd > 0 and last_ban and now - last_ban < streak_cd:
                POKE_STREAK[skey] = []      # 本窗口的计数清掉，别攒着过会儿又炸
                logger.info("%s 在 %d 秒内戳了 %d 次，但 %.0f 秒前刚被禁过（还剩 %.0f 秒），"
                            "这次只记账、不说话也不禁言",
                            nickname, int(window), len(hits), now - last_ban,
                            streak_cd - (now - last_ban))
                return
            POKE_LAST[key] = now
            STREAK_BAN_AT[skey] = now
            # 连戳的时长也要夹：这个键在面板上能拖到 1440（24 小时），
            # 而它以前**完全不受 ban.max_minutes 约束** —— 拖错一档就能把人关一天。
            minutes = float(cfg.get("streak_ban_minutes", 3))
            minutes = max(1.0, min(minutes, float(CFG.get("ban", {}).get("max_minutes", 1440))))
            logger.info("%s 在 %d 秒内戳了 %d 次，触发自动禁言", nickname, int(window), len(hits))

            # 先撂一句话，再动手
            if cfg.get("streak_line_enable", True):
                life = LIFE.current()
                life["weather"] = await WEATHER.phrase()
                prompt = (f"{nickname}连着戳了你 {len(hits)} 下。你烦了，准备把他禁言 {minutes:g} 分钟。"
                          "按你的性子说一句话，就一句，直接说，别解释你要做什么。")
                try:
                    said, _ = await CHAT.answer(f"p{user_id}", [
                        {"role": "system", "content": build_system(True, life, group_id, user_id)},
                        {"role": "user", "content": prompt},
                    ])
                except Exception as exc:
                    said = ""
                    logger.debug("连击回话失败：%s", exc)
                if said:
                    line = drop_tags(clean_reply(said), QZONE_TAG, DRAW_TAG, LIKE_TAG,
                                     QZONE_READ_TAG, BAN_TAG)
                    if line:
                        await send(True, group_id, user_id, None, line)

            ok, why = await _force_ban_ex(group_id, user_id, nickname, minutes,
                                          "连续戳一戳", source="streak")
            if ok:
                POKE_STREAK[skey] = []          # 只有真禁成了才清空计数
            else:
                # 没禁成（额度用光/豁免/接口没通）就把计数留着：下次够数再试
                POKE_STREAK[skey] = hits
                logger.info("连戳自动禁言没成：%s（%s），计数保留着下次再试", why, nickname)
            return
        POKE_STREAK[skey] = hits

    if now - POKE_LAST.get(key, 0) < float(cfg.get("cooldown_seconds", 20)):
        logger.info("戳一戳冷却中，来自 %s", nickname)
        return
    POKE_LAST[key] = now

    # 他刚还在跟我说话 —— 这一下多半是"在不在？看我一眼"，不是想开新话题。
    # 这种时候把回合留给对话：要么只丢一张对味的表情包，要么干脆不理，
    # 都**不**为一下戳单独烧一次模型调用（日志里最典型的就是"戳完接着聊"被冷落）。
    if is_recent_talker(user_id, float(cfg.get("chat_proximity_seconds", 30))):
        if random.random() < float(cfg.get("proximity_sticker_probability", 0.35)):
            seg = mood_sticker_segment(group_id, user_id)
            if seg:
                await send(bool(group_id), group_id, user_id, None, "", seg)
                logger.info("%s 刚说过话，这一戳只回了一张表情包（心情词=%s）",
                            nickname, mood_sticker_keyword(group_id, user_id) or "无")
            else:
                logger.info("%s 刚说过话，想回表情包但库里没有能发的，这次不理", nickname)
        else:
            logger.info("%s 刚说过话，这一戳不理（回合留给对话）", nickname)
        return

    life = LIFE.current()
    # 手上有事的时候别一戳就放下：但**可打断的忙**（看店/吃饭/打盹）比以前松得多，
    # 只有"睡觉"这种不可打断的才照旧基本装死。
    if life.get("busy"):
        prob = float(cfg.get("busy_interruptible_reply_probability", 0.6)
                     if life.get("interruptible")
                     else cfg.get("busy_reply_probability", 0.15))
        if random.random() >= prob:
            logger.info("正忙着「%s」，没搭理 %s 的戳一戳", life.get("act", ""), nickname)
            return
    elif random.random() >= float(cfg.get("reply_probability", 0.85)):
        logger.info("这次懒得理 %s 的戳一戳", nickname)
        return
    life["weather"] = await WEATHER.phrase()
    prompt = f"{nickname}戳了你一下（QQ 的戳一戳）。你怎么反应？一句话，简短，符合你现在的状态。"
    ask = [
        {"role": "system", "content": build_system(bool(group_id), life, group_id, user_id)},
        {"role": "user", "content": prompt},
    ]
    reply, source = await CHAT.answer(f"p{user_id}", ask)
    if not reply:
        return

    reply = clean_reply(reply)
    reply = await CHAT.avoid_repeat(f"p{user_id}", ask, reply)
    # 戳一戳不是发布入口：回复里冒出来的标记一律剥掉（否则会原样发到群里）
    for tag in (QZONE_TAG, DRAW_TAG, LIKE_TAG, QZONE_READ_TAG, BAN_TAG):
        if tag.search(reply):
            reply = drop_tags(reply, tag)
    reply, sticker_path = pick_sticker(reply, prompt)
    reply = truncate_reply(reply)
    if not reply and not sticker_path:
        return

    logger.info("戳一戳反应 来自%s 来源=%s", nickname, source)
    await send(bool(group_id), group_id, user_id, None, reply, sticker_path)

    if random.random() < float(cfg.get("poke_back_probability", 0.3)):
        try:
            if group_id:
                await OB.call("group_poke", {"group_id": group_id, "user_id": user_id}, timeout=10)
            else:
                await OB.call("friend_poke", {"user_id": user_id}, timeout=10)
            logger.info("回戳了 %s", nickname)
        except Exception as exc:
            logger.debug("回戳失败：%s", exc)


async def _daily_tick_once() -> None:
    """巡检一次（抽出以便回归测试直接调用）。

    好感的每日回落走 `MOOD.decay_all()` —— 它自己带"今天回落过就跳过"的闸门，
    所以这里每分钟调用一次是安全的（闸门漏了的话好感会一天被抹平，见它的注释）。
    """
    await MEMORY.tick()
    MOOD.decay_all()


async def daily_loop() -> None:
    """每分钟巡检一次，到点就把当天的闲聊记忆清掉（长期记忆不动）。"""
    while True:
        await asyncio.sleep(60)
        try:
            await _daily_tick_once()
        except Exception as exc:
            logger.exception("每日重置出错：%s", exc)


async def _staggered(factory, delay: float):
    """后台循环错峰：先等 delay 秒，再进入那个循环（见 main 里的说明）。"""
    if delay > 0:
        await asyncio.sleep(delay)
    await factory()


async def _bili_login_once(notify=None) -> tuple[bool, str]:
    """查一次 b 站登录态；是游客就自己发起扫码登录（抽出以便测试直接调用）。"""
    cfg = CFG.get("bili", {})
    if not BILI.enable:
        return False, "b 站功能没开"
    if await BILI.check_login():
        return True, BILI.state_text()
    logger.info("B站登录检查：%s", BILI.state_text())
    if BILI.guest is not True:
        # 查不出来（网络不通/接口没回）≠ 是游客。这时候发二维码纯属添乱
        return False, f"登录态没查出来（{BILI._login_error or '未知'}）"
    if not cfg.get("auto_login", True):
        return False, "是游客，但自动登录关着"
    if OB.ws is None:
        # 消息发不出去，二维码给了也没人扫；等连上再说
        return False, "是游客，但 NapCat 还没连上"
    return await BILI.ensure_login(notify=notify)


async def bili_login_loop() -> None:
    """定期看一眼她是不是游客；是的话自己开扫码登录，把二维码塞给管理员。

    启动先等一会儿再查 —— 太早的话 NapCat 还没连上，管理员收不到消息。
    """
    cfg = CFG.get("bili", {})
    interval = max(5.0, float(cfg.get("login_check_minutes", 60))) * 60
    retry = max(2.0, float(cfg.get("login_retry_minutes", 10))) * 60
    await asyncio.sleep(float(cfg.get("login_start_delay_seconds", 20)))

    async def notify(text: str, image: str | None = None) -> None:
        await notify_admins(text, image)

    while True:
        wait = interval
        # 开关每轮 live 读：面板上打开 auto_login 之后不必重启就能生效
        # （以前是在函数开头判一次就 return，关着启动的话永远不再巡检）
        cfg = CFG.get("bili", {})
        if not (cfg.get("enable", False) and cfg.get("auto_login", True)):
            await asyncio.sleep(interval)
            continue
        try:
            ok, why = await _bili_login_once(notify=notify)
            logger.info("B站登录巡检：%s（%s）", "已登录" if ok else "仍是游客", why)
            if not ok and BILI.guest is True:
                # 确实是游客但没登成（码过期了 / 人还没扫）—— 别等一小时，早点再来
                wait = retry
        except Exception as exc:
            logger.exception("B站登录巡检出错：%s", exc)
        await asyncio.sleep(wait)


async def ws_handler(ws) -> None:
    req = getattr(ws, "request", None)
    headers = dict(getattr(req, "headers", None) or getattr(ws, "request_headers", None) or {})
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    expected = CFG.get("access_token", "")
    if expected and token != expected:
        logger.warning("token 不匹配，拒绝连接")
        await ws.close()
        return

    OB.bind(ws)
    logger.info("NapCat 已连接")
    # 刚连上就把群名册补齐（跨群指令要靠群名找群号）。
    # 不能放在启动时做：那时候 NapCat 多半还没连上，get_group_list 会直接失败。
    asyncio.create_task(_safe(GROUPS.refresh()))
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if "post_type" not in data and "echo" in data:
                OB.resolve(data)
                continue
            if data.get("post_type") == "meta_event":
                meta = data.get("meta_event_type")
                if meta == "lifecycle":
                    OB.self_id = int(data.get("self_id") or OB.self_id)
                    logger.info("生命周期事件 self_id=%s", OB.self_id)
                continue
            if data.get("post_type") == "message":
                # 并发处理：一条消息卡住（比如发送超时）不能堵住后面所有消息
                asyncio.create_task(_safe(handle_message(data)))
            elif data.get("post_type") == "notice":
                asyncio.create_task(_safe(handle_notice(data)))
    except ConnectionClosed:
        logger.warning("NapCat 断开，等待重连")
    finally:
        OB.bind(None)


CONSOLE_CONFIG_FIELDS = ("persona", "world", "style_boost", "style_format",
                         "self_image", "identity_guard")


def _backup_config_roll() -> str:
    """改 config.json 前留一份滚动快照（只保留最近一份）。"""
    dst = BASE / "_backup" / "console-config-last.json"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CONFIG_PATH, dst)
        return str(dst)
    except OSError as exc:
        logger.warning("配置备份失败（继续写入）：%s", exc)
        return ""


# 面板上允许用进度条拖动的数值配置。
# 格式：路径 -> (最小, 最大, 步长, 单位, 说明, 是否整数)
# ⚠️ 白名单制，绝不做"任意 config 路径写入" —— 否则密钥字段可以绕过读侧脱敏被改掉。
CONSOLE_TUNABLES: dict[str, tuple] = {
    "attention.threshold": (
        0.1, 5.0, 0.05, "", "主动搭话阈值：兴趣攒到这么多她才自己插话（越小越活跃）", False),
    "attention.alias_threshold_discount": (
        0.0, 0.9, 0.05, "", "喊她别名（没 @）时阈值临时下调的比例", False),
    "attention.alias_window_seconds": (
        10, 900, 10, "秒", "别名降阈值持续多久", True),
    "attention.engaged_reply_probability": (
        0.0, 1.0, 0.05, "", "她正聊着时接话的概率（不随机沉默）", False),
    "attention.idle_probability": (
        0.0, 1.0, 0.05, "", "冷场时她主动开口的概率", False),
    "attention.idle_trigger_minutes": (
        1, 240, 1, "分", "多久没人说话算冷场", True),
    "attention.cooldown_seconds": (
        0, 7200, 30, "秒", "她两次主动搭话之间至少隔多久", True),
    "attention.decay_seconds": (
        10, 3600, 10, "秒", "兴趣值衰减时间常数（越小掉得越快）", True),
    "affinity.gain_nice": (
        0.0, 20.0, 0.5, "", "被夸一句，好感加多少", False),
    "affinity.loss_rude": (
        0.0, 30.0, 0.5, "", "被骂一句，好感掉多少", False),
    "affinity.decay_per_day": (
        0.0, 10.0, 0.1, "", "好感每天往 50 回落多少", False),
    "affinity.strong_mult": (
        1.0, 3.0, 0.1, "倍", "强情绪词（「我最喜欢你了」/「傻逼」）比普通好话/难听话重几倍", False),
    "affinity.drift_gain": (
        0.0, 2.0, 0.05, "分", "正常聊天每条自然累积多少好感（只对她愿意接的话生效）", False),
    "affinity.drift_max_per_day": (
        0.0, 20.0, 0.5, "分", "靠闲聊每天最多攒多少好感（防刷）", False),
    "affinity.drift_ceiling": (
        50.0, 100.0, 1.0, "分", "闲聊最多把她对你的印象养到几分（再往上得靠真夸她）", False),
    "affinity.drift_min_chars": (
        1, 50, 1, "字", "短于多少字的消息不算「在跟她说话」", True),
    "affinity.hysteresis": (
        0.0, 10.0, 0.1, "分", "好感停在档位边界上时，要超出边界多少分才真的换一档（防说法反复横跳）", False),
    "mood.mood_gain_nice": (
        0.0, 30.0, 0.5, "", "被夸一句，心情加多少", False),
    "mood.mood_loss_rude": (
        0.0, 40.0, 0.5, "", "被骂一句，心情掉多少", False),
    "mood.mood_pull_to_affinity": (
        0.0, 1.0, 0.05, "", "心情往好感靠拢的力度 —— 好感影响心情的程度", False),
    "qzone.auto_interact_probability": (
        0.0, 1.0, 0.05, "", "她自己跑去空间互动的概率", False),
    "qzone.auto_interact_cooldown_minutes": (
        5, 240, 5, "分", "两次自主空间互动至少隔多久", True),
    "qzone.auto_post_probability": (
        0.0, 1.0, 0.02, "", "自发发说说的概率", False),
    "qzone.max_per_day": (
        0, 10, 1, "条", "每天最多发几条说说", True),
    "qzone.comment_max_per_day": (
        0, 50, 1, "条", "每天最多评论几条", True),
    "qzone.like_cooldown_seconds": (
        0, 3600, 10, "秒", "两次点赞之间至少隔多久", True),
    "ban.max_minutes": (
        1, 1440, 1, "分", "跨群/私聊点名禁言的最长分钟数，也是连戳自动禁言的上限"
                          "（她自己在群里发起的另有 self_max_minutes）", True),
    "ban.max_per_hour": (
        1, 60, 1, "次", "她**自发**禁言的每小时条数上限（按群记；连戳自动处置另算）", True),
    "rate_limit.max_calls_per_minute": (
        0, 600, 10, "次", "每分钟最多调用几次（0 = 不限）", True),
    # ── 她自己的生活节奏 ──
    "self_activity.min_gap_minutes": (
        5, 240, 5, "分", "两件事之间至少隔多久", True),
    "self_activity.max_gap_minutes": (
        10, 480, 5, "分", "最多隔多久她一定会去做点什么", True),
    "self_activity.skip_chance": (
        0.0, 0.9, 0.05, "", "到点了也可能什么都不做的概率（真人不是按表走的）", False),
    "self_activity.busy_act_chance": (
        0.0, 1.0, 0.02, "", "她**忙**的时候，每轮巡检做个小动作的概率（要低：忙就是忙）", False),
    "self_activity.busy_short_min_minutes": (
        1, 60, 1, "分", "忙里偷闲的小动作最短持续几分钟", True),
    "self_activity.busy_short_max_minutes": (
        1, 120, 1, "分", "忙里偷闲的小动作最长持续几分钟", True),
    "life.events_per_day": (
        1, 10, 1, "条", "每天随机挑几条小事", True),
    # ── 回复节奏 / 上下文 ──
    "active_reply_probability": (
        0.0, 1.0, 0.05, "", "没 @ 她时，群里一条闲话被她接住的概率（越大越活跃）", False),
    "max_reply_chars": (
        20, 2000, 10, "字", "单条回复最多多少字", True),
    "max_context_turns": (
        2, 60, 1, "轮", "每个会话带几轮上下文（改小省 token）", True),
    "group_cooldown_seconds": (
        0, 600, 1, "秒", "同一个会话两次回复之间至少隔多久（0 = 不限）", True),
    "context.max_chars": (
        200, 40000, 100, "字", "一次请求最多带多少字的上下文", True),
    # ── 生活作息 ──
    "life.busy_silent_chance": (
        0.0, 1.0, 0.05, "", "不可打断的忙（睡觉）时，@ 她也装死的概率", False),
    "life.busy_at_silent_chance": (
        0.0, 1.0, 0.05, "", "可打断的忙（看店/吃饭）时，@ 她还装死的概率", False),
    "life.busy_non_directed_factor": (
        0.0, 1.0, 0.05, "", "忙的时候，非指向性消息的接话概率再打几折", False),
    "life.event_max_per_day": (
        0, 20, 1, "次", "同一件日常小事一天最多被提几次", True),
    "life.event_chance": (
        0.0, 1.0, 0.05, "", "闲聊时提起今天那件小事的概率", False),
    "life.event_chance_draw": (
        0.0, 1.0, 0.05, "", "画图时提起今天那件小事的概率", False),
    "life.event_chance_report": (
        0.0, 1.0, 0.05, "", "写行为小结时提起今天那件小事的概率", False),
    # ── 注意力 ──
    "attention.engage_window_seconds": (
        10, 1800, 10, "秒", "她刚说过话之后多久内算「还在这个话题里」", True),
    # ── 自主活动 ──
    "self_activity.tick_seconds": (
        10, 1800, 10, "秒", "自主活动的巡检间隔", True),
    "self_activity.material_keep_minutes": (
        5, 600, 5, "分", "她取到的素材（看过的内容）保留多久", True),
    # ── 分条发送 / 引用回复 ──
    "split.max_bubbles": (
        1, 10, 1, "条", "一条回复最多分成几条发", True),
    "split.soft_limit_chars": (
        8, 200, 2, "字", "超过多少字就按句意分条", True),
    "split.delay_seconds": (
        0.0, 10.0, 0.1, "秒", "两条之间的间隔", False),
    "quote.min_active_speakers": (
        1, 10, 1, "人", "群里同时有几个人说话才带引用", True),
    "quote.active_window_seconds": (
        10, 1800, 10, "秒", "判断「多人同时说话」看多长的窗口", True),
    "quote.after_silence_seconds": (
        30, 7200, 30, "秒", "冷场多久之后才引用（要开 on_long_silence）", True),
    # ── 天气 / 联网 ──
    "weather.cache_hours": (
        0.5, 24.0, 0.5, "时", "天气结果缓存多久", False),
    "weather.tick_minutes": (
        5, 240, 5, "分", "天气巡检间隔：每轮刷一次并跟上一轮比变化", True),
    "weather.change_keep_minutes": (
        5, 360, 5, "分", "「刚察觉到天气变了」这句能挂在她语气里多久", True),
    "weather.change_temp_delta": (
        1.0, 15.0, 0.5, "°C", "温度变化超过多少度才算「明显升/降温」", False),
    "weather.alert_cooldown_minutes": (
        10, 1440, 10, "分", "两次联网查天气预警之间至少隔多久", True),
    "web.timeout_seconds": (
        3, 120, 1, "秒", "联网搜索的超时", True),
    "web.max_results": (
        1, 30, 1, "条", "每次搜索取几条结果", True),
    "web.sites_per_search": (
        1, 10, 1, "个", "每次从站点池里随机挑几个站搜", True),
    # ── 表情包 ──
    "stickers.max": (
        10, 2000, 10, "张", "表情包库最多存几张", True),
    "stickers.max_size_kb": (
        50, 4096, 10, "KB", "收藏表情包时的单张大小上限", True),
    # ── 记忆 ──
    "memory.extract_every_messages": (
        5, 500, 5, "条", "每聊多少条抽一次长期记忆", True),
    "memory.reset_every_days": (
        1, 60, 1, "天", "闲聊记忆几天清一次（长期记忆不动）", True),
    "memory.reset_hour": (
        0, 23, 1, "点", "每天几点做这次清理", True),
    "memory.max_people": (
        1, 100, 1, "人", "长期记忆最多记几个人", True),
    "memory.max_traits_per_person": (
        1, 20, 1, "条", "每个人最多记几条特点", True),
    "memory.max_todos_per_person": (
        1, 20, 1, "条", "每个人最多记几条待办", True),
    "memory.max_group_notes": (
        1, 50, 1, "条", "每个群最多记几条群记忆", True),
    # ── 空间 ──
    "qzone.max_chars": (
        10, 2000, 10, "字", "说说最长多少字", True),
    "qzone.count_manual_posts": (
        0, 1, 1, "0/1", "被人要求发的说说算不算进每日额度（1 = 算）", True),
    "qzone.min_life_chance": (
        0.0, 1.0, 0.05, "", "她自发发说说需要的最低状态分（越大越少发）", False),
    "qzone.idle_qzone_probability": (
        0.0, 1.0, 0.05, "", "冷场时她跑去逛空间的概率", False),
    "qzone.feed_cache_seconds": (
        0, 600, 5, "秒", "好友动态的缓存时长", True),
    "qzone.comment_max_chars": (
        10, 500, 10, "字", "空间评论最长多少字", True),
    "qzone.comment_memory_keep": (
        10, 5000, 10, "条", "「评过的说说」最多记多少条", True),
    "qzone.comment_memory_days": (
        1, 365, 1, "天", "「评过的说说」记多久（同一条只评一次）", True),
    # ── B 站 ──
    "bili.daily_action_cap": (
        0, 50, 1, "次", "B 站每天最多点赞/投币/收藏几次（0 = 不限）", True),
    "bili.browse_probability": (
        0.0, 1.0, 0.05, "", "闲着时去 B 站逛一圈的概率", False),
    "bili.like_probability": (
        0.0, 1.0, 0.05, "", "看视频时顺手点赞的概率", False),
    "bili.coin_probability": (
        0.0, 1.0, 0.05, "", "看视频时投币的概率", False),
    "bili.fav_probability": (
        0.0, 1.0, 0.05, "", "看视频时收藏的概率", False),
    "bili.login_check_minutes": (
        5, 1440, 5, "分", "多久查一次 B 站登录态", True),
    "bili.qr_wait_seconds": (
        30, 600, 10, "秒", "扫码登录等多久算超时", True),
    # ── 跨群 / 行为报告 ──
    "cross_group.default_minutes": (
        1, 1440, 1, "分", "跨群禁言没写分钟时的默认时长", True),
    "activity.report_minutes": (
        5, 1440, 5, "分", "多久给管理员发一份行为小结", True),
    "activity.max_buffer": (
        10, 5000, 10, "条", "行为流水最多攒多少条", True),
    # ── 戳一戳（数值项；enable/streak_line_enable 这类布尔只在 CONSOLE_SWITCHES 里，
    #    别再登记成 0/1 滑条 —— 同一个键两处控件会让面板互相显示旧值） ──
    "poke.cooldown_seconds": (
        0, 3600, 5, "秒", "两次戳一戳反应之间至少隔多久", True),
    "poke.reply_probability": (
        0.0, 1.0, 0.05, "", "平时理不理戳一戳", False),
    "poke.busy_reply_probability": (
        0.0, 1.0, 0.05, "", "不可打断的忙（睡觉）时理不理", False),
    "poke.busy_interruptible_reply_probability": (
        0.0, 1.0, 0.05, "", "可打断的忙（看店/吃饭）时理不理", False),
    "poke.chat_proximity_seconds": (
        0, 600, 5, "秒", "他刚说过话的窗口（这期间的戳只当「看我一眼」）", True),
    "poke.proximity_sticker_probability": (
        0.0, 1.0, 0.05, "", "挨着聊天时，这一戳只回一张表情包的概率", False),
    "poke.poke_back_probability": (
        0.0, 1.0, 0.05, "", "反戳回去的概率", False),
    "poke.initiate_probability": (
        0.0, 1.0, 0.05, "", "她自己主动去戳别人的概率", False),
    "poke.streak_window_seconds": (
        30, 7200, 30, "秒", "连戳判定的时间窗口", True),
    "poke.streak_limit": (
        2, 20, 1, "次", "窗口内被戳几次算「连戳」", True),
    "poke.streak_ban_minutes": (
        1, 1440, 1, "分", "连戳自动禁言几分钟", True),
    "poke.streak_max_per_day": (
        1, 50, 1, "次", "连戳自动禁言每天最多几次（单独算，不占 ban.max_per_day）", True),
    "poke.mood_loss_per_hit": (
        0.0, 50.0, 0.5, "", "每被戳一下，心情掉多少", False),
    "poke.mood_loss_cap": (
        0.0, 100.0, 1.0, "", "被戳一下最多掉多少心情（封顶）", False),
    "poke.mood_affinity_loss": (
        0.0, 50.0, 0.5, "", "连戳够数时，好感掉多少", False),
    # ── 禁言（本轮新增的三个限制键） ──
    "ban.default_minutes": (
        1, 1440, 1, "分", "不写分钟时的默认禁言时长", True),
    "ban.self_max_minutes": (
        1, 1440, 1, "分", "她自己发起的单次禁言上限（管理员点名另算）", True),
    "ban.max_per_day": (
        1, 200, 1, "次", "每天禁言总次数上限（含她自发与管理员点名）", True),
    "ban.same_target_cooldown_seconds": (
        0, 86400, 60, "秒", "同一个号两次禁言之间的最小间隔（0 = 不限）", True),
    # ── 识图 ──
    "vision.max_frames": (
        1, 12, 1, "帧", "单张动图最多等距抽几帧（1 = 不抽，只发第一帧）", True),
    "vision.max_total_images": (
        1, 20, 1, "张", "抽帧之后一轮最多发几张图（3 张动图各抽 3 帧就是 9 张，很吃 token）", True),
    "vision.max_side_cloud": (
        256, 4096, 32, "px", "发给云端的图片最长边（大一点看得清，也更费 token）", True),
    "vision.max_side_local": (
        256, 4096, 32, "px", "发给本地的图片最长边（小一点省显存）", True),
    "vision.describe_max_tokens": (
        50, 2000, 50, "token", "识图（图转文字）请求的输出上限", True),
}


# 布尔开关：路径 -> (中文名, 说明, 分组)
# 白名单制，理由同 CONSOLE_TUNABLES：绝不做"任意 config 路径写入"。
CONSOLE_SWITCHES: dict[str, tuple[str, str, str]] = {
    "life.enable": ("生活作息", "她有自己的作息和忙闲，会真的不回消息", "life"),
    "self_activity.enable": ("自主活动", "与群冷热无关：她自己一个人也在做点什么", "life"),
    "attention.enable": ("主动搭话", "兴趣攒够了她会自己插话", "attention"),
    "memory.enable": ("长期记忆", "记人的特点与待办（跨群私聊共享）", "memory"),
    "memory.extract_enable": ("自动抽取记忆", "每隔若干条消息让模型抽一次长期记忆", "memory"),
    "memory.group_enable": ("群聊记忆", "记「这个群是什么群」，跟群走不跟人走", "memory"),
    "qzone.enable": ("QQ 空间", "她可以发说说、看好友动态", "qzone"),
    "qzone.auto_interact_enable": ("自主空间互动", "闲着时自己去点赞/评论", "qzone"),
    "qzone.comment_enable": ("空间评论", "允许她评论好友的说说", "qzone"),
    "qzone.like_enable": ("空间点赞", "允许她给好友的说说点赞", "qzone"),
    "qzone.comment_once_per_post": ("一条只评一次", "同一条说说评论过就不再评（记在文件里，重启不作废）", "qzone"),
    "qzone.bridge_enable": ("空间桥接", "经 qzone-bridge 读好友动态（关了就看不到别人发什么）", "qzone"),
    "bili.enable": ("B 站", "她能上 B 站看视频", "bili"),
    "bili.actions_enable": ("B 站互动", "真去点赞/投币/收藏（有风控风险，靠每日上限兜底）", "bili"),
    "bili.auto_login": ("B 站自动登录", "掉了登录态时自己试着恢复", "bili"),
    "cross_group.enable": ("跨群禁言", "管理员可在私聊里管别的群的人", "cross_group"),
    "web.enable": ("联网搜索", "她能自己上网查资料再回答", "web"),
    "weather.enable": ("天气", "把外面的天气融进她的语气", "life"),
    "weather.alert_enable": (
        "天气联网查预警", "察觉到天气变化时上网看一眼当地预警，当成她的话题素材", "life"),
    "self_activity.busy_stop_long": (
        "忙时打断长活动", "她忙起来（上班/吃饭/睡觉）就停掉手里的长活动，"
                        "忙的整段时间只做忙里偷闲的小事", "life"),
    "activity.enable": ("行为流水", "把她的日常动作记下来并定期汇报", "activity"),
    "activity.report_to_admin": ("行为报告", "定期把小结发到管理员私聊", "activity"),
    "activity.llm_summary": ("报告用模型润色", "小结交给模型写成一段话，而不是流水账", "activity"),
    "nudge_on_ask": ("没做就催一次", "他要她做事、她光嘴上答应时再要一次标记", "reply"),
    "vision_enable": ("识图总闸", "关掉就完全不解析图片（连说明都不下发）", "vision"),
    # ── 生图（本机依赖：要 SD WebUI / ComfyUI / 云端 key 才能用） ──
    "sd.enable": ("生图", "关掉就完全不画图。本地后端需要 SD WebUI 开着，"
                        "云端后端需要配好密钥；用之前建议先跑 tools/doctor.py 体检", "sd"),
    "imagegen.enable": ("生图总闸（含云端）", "一刀切：不光是本地，连云端生图也一起关", "sd"),
    "sd.fail_enable": ("画不出来时说一句", "画失败时用 sd.fail_lines 里的话补一句，"
                                        "免得她说了要画却什么都没发生", "sd"),
    # ── 本地模型（没装本地模型的人应当能一眼关掉，而不是对着报错猜） ──
    "local.enable": ("本地模型", "优先用 LM Studio / Ollama 这类本机模型。"
                                "没装本地模型就关掉，只走云端", "local"),
    "local.vision": ("本地识图", "用本机的视觉模型看图（需要 VL 模型，很吃显存）", "local"),
    "ban.enable": ("禁言功能", "她可以在群里禁言（含连戳自动处置）", "ban"),
    "ban.protect_admins": ("禁言保护管理员", "禁言前先查对方是不是管理员/群主，是就放弃", "ban"),
    "quote.enable": ("引用回复", "多人同时在聊时带上引用，免得看不清在回谁", "quote"),
    "split.enable": ("分条发送", "一条回复拆成几条发，更像真人打字", "split"),
    "stickers.enable": ("收藏表情包", "收藏群里别人发的动画表情", "stickers"),
    "stickers.send_enable": ("她发表情包", "允许她主动挑一张表情包发出来（她写 [图:情境]）", "stickers"),
    "stickers.judge_enable": ("表情包判定", "收藏前先让模型判断值不值得留", "stickers"),
    "mood.enable": ("好感与心情", "记录对每个人的好感与此刻的心情", "mood"),
    "poke.enable": ("戳一戳", "被戳一戳时她会反应（含连戳处置）", "poke"),
    "poke.streak_line_enable": ("连戳先撂一句", "连戳禁言之前先让她说一句话", "poke"),
}


# 列表型（多行文本，每行一条）：路径 -> (中文名, 说明, 分组, 最多条数, 单条最多字)
CONSOLE_LISTS: dict[str, tuple[str, str, str, int, int]] = {
    "identity_lines": ("身份挡回去的话", "被问到是不是 AI/机器人时挑着用的备选台词", "reply", 20, 60),
    "reply_fallback_lines": ("模型挂了的兜底话", "模型全都调不通时她说的话（总得说人话）", "reply", 20, 40),
    "wake_prefix": ("唤醒前缀", "群里以这些符号开头的消息她会当成在叫她", "reply", 5, 8),
    "sd.pre_reply_lines": ("出图前的过渡语", "生图要十几秒，先说一句让她显得没走神（只是话术，不是出图参数）", "reply", 10, 40),
    "sd.fail_lines": ("出图失败时的话", "画不出来时她补一句（人设口吻），说完就不发图了；"
                                      "关掉 sd.fail_enable 则什么都不说", "reply", 10, 40),
    "life.mood_events": ("日常小事素材", "每天随机挑一条塞进提示词的小事（她今天遇到了什么）", "life", 30, 60),
    "self_activity.places": ("常去的地方", "她自主活动时会挑去处的候选池", "life", 40, 30),
    "web.sites": ("常用站点", "联网搜索优先逛的站点池（留空则全网搜）", "web", 12, 40),
}


def _get_path(cfg: dict, path: str):
    node = cfg
    for key in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


# 顶层那几个"回复手感"键没有点号，按路径前缀取分组名会让它们各自变成一组、
# 面板上就显示成英文键名。归到同一组，前端 TUNABLE_GROUP 给一个中文名就够。
_TUNABLE_REPLY_KEYS = frozenset({
    "active_reply_probability", "max_reply_chars", "max_context_turns",
    "group_cooldown_seconds", "context.max_chars",
})


def _tunable_group(path: str) -> str:
    """滑块的分组名。前端 TUNABLE_GROUP 用它对中文名；没登记就显示英文键名。"""
    return "reply" if path in _TUNABLE_REPLY_KEYS else path.split(".")[0]


def _sync_tunable(path: str, val) -> None:
    """把改过的值同步到对应实例上。

    很多开关是对象 __init__ 时从 CFG 读一次就存成实例属性的，
    只改 CFG 这次运行不会生效（表情包那几个开关也踩过同样的坑）。

    "打开开关还得重新 init"的核查结论（2026-09-27 逐个看过 __init__）：
      · stickers.enable —— **要**（StickerBook 只在 enable 时 load()，见下）
      · memory.enable / memory.group_enable / mood.enable / bili.enable / qzone.enable /
        qzone.bridge_enable / life.enable —— 不用：它们的 __init__ 无条件 load()，
        数据早就在内存里，置真即生效（life/attention/web/activity 的 enable 只是纯标记，
        连加载都没有）
      · poke.enable 不在这张表里：handle_notice 每次 live 读 CFG，改完立即生效
    """
    table = {
        "attention.threshold": (ATTENTION, "threshold"),
        "attention.alias_threshold_discount": (ATTENTION, "alias_discount"),
        "attention.alias_window_seconds": (ATTENTION, "alias_window"),
        "attention.engaged_reply_probability": (ATTENTION, "engaged_reply_prob"),
        "attention.idle_probability": (ATTENTION, "idle_prob"),
        "attention.idle_trigger_minutes": (ATTENTION, "idle_minutes"),
        "attention.cooldown_seconds": (ATTENTION, "cooldown"),
        "attention.decay_seconds": (ATTENTION, "decay"),
        "attention.engage_window_seconds": (ATTENTION, "engage_window"),
        "affinity.gain_nice": (MOOD, "affinity_gain"),
        "affinity.loss_rude": (MOOD, "affinity_loss"),
        "affinity.decay_per_day": (MOOD, "affinity_decay"),
        "affinity.strong_mult": (MOOD, "strong_mult"),
        "affinity.drift_gain": (MOOD, "drift_gain"),
        "affinity.drift_max_per_day": (MOOD, "drift_max_per_day"),
        "affinity.drift_ceiling": (MOOD, "drift_ceiling"),
        "affinity.drift_min_chars": (MOOD, "drift_min_chars"),
        "affinity.hysteresis": (MOOD, "hysteresis"),
        "mood.mood_gain_nice": (MOOD, "feel_gain"),
        "mood.mood_loss_rude": (MOOD, "feel_loss"),
        "mood.mood_pull_to_affinity": (MOOD, "pull"),
        "qzone.max_per_day": (QZONE, "max_per_day"),
        "qzone.auto_post_probability": (QZONE, "prob"),
        "qzone.min_life_chance": (QZONE, "min_chance"),
        # ── 布尔开关：面板上改成 False，光改 CFG 这次运行不生效 ──
        "life.enable": (LIFE, "enable"),
        "self_activity.enable": (LIFE, "act_enable"),
        "attention.enable": (ATTENTION, "enable"),
        "memory.enable": (MEMORY, "enable"),
        "memory.extract_enable": (MEMORY, "extract"),
        "memory.group_enable": (GMEM, "enable"),
        "qzone.enable": (QZONE, "enable"),
        "qzone.bridge_enable": (QZONE_API, "enable"),
        "bili.enable": (BILI, "enable"),
        "bili.actions_enable": (BILI, "actions_enable"),
        "web.enable": (WEB, "enable"),
        "weather.enable": (WEATHER, "enable"),
        "activity.enable": (ACTIVITY, "enable"),
        "activity.report_to_admin": (ACTIVITY, "report_to_admin"),
        "activity.llm_summary": (ACTIVITY, "llm_summary"),
        "stickers.enable": (STICKERS, "enable"),
        "stickers.send_enable": (STICKERS, "send_enable"),
        "stickers.judge_enable": (STICKERS, "judge_enable"),
        "mood.enable": (MOOD, "enable"),
        # ── 数值项里同样"init 时读一次"的那些 ──
        "life.busy_silent_chance": (LIFE, "busy_silent_chance"),
        "life.busy_at_silent_chance": (LIFE, "busy_at_silent_chance"),
        "life.busy_non_directed_factor": (LIFE, "busy_non_directed_factor"),
        "life.event_max_per_day": (LIFE, "event_max_per_day"),
        "life.event_chance": (LIFE, "event_chance"),
        "life.event_chance_draw": (LIFE, "event_chance_draw"),
        "life.event_chance_report": (LIFE, "event_chance_report"),
        "self_activity.tick_seconds": (LIFE, "act_tick"),
        "self_activity.busy_act_chance": (LIFE, "busy_act_chance"),
        "self_activity.busy_stop_long": (LIFE, "busy_stop_long"),
        "weather.cache_hours": (WEATHER, "cache_hours"),
        "weather.tick_minutes": (WEATHER, "tick_minutes"),
        "weather.change_keep_minutes": (WEATHER, "change_keep_minutes"),
        "weather.change_temp_delta": (WEATHER, "change_temp_delta"),
        "weather.alert_enable": (WEATHER, "alert_enable"),
        "stickers.max": (STICKERS, "max"),
        "memory.extract_every_messages": (MEMORY, "every"),
        "memory.reset_every_days": (MEMORY, "every_days"),
        "memory.reset_hour": (MEMORY, "reset_hour"),
        "memory.max_people": (MEMORY, "max_people"),
        "memory.max_traits_per_person": (MEMORY, "max_traits"),
        "memory.max_todos_per_person": (MEMORY, "max_todos"),
        "memory.max_group_notes": (GMEM, "max_notes"),
        "web.timeout_seconds": (WEB, "timeout"),
        "web.max_results": (WEB, "max_results"),
        "activity.report_minutes": (ACTIVITY, "report_minutes"),
        "activity.max_buffer": (ACTIVITY, "max_buffer"),
    }
    hit = table.get(path)
    if hit is not None:
        setattr(hit[0], hit[1], val)
        # 开关打开时**要补一次 init**：这些对象是"关着就什么都不加载"的写法，
        # 光 setattr 只让开关看着变真，底下的数据还是空的。
        # stickers 最狠：启动时是关的 → items 恒为空 → 下一次收藏 save() 会把
        # stickers/index.json 覆盖成**只含新那一条**，原有索引全丢（文件还在、库里没了）。
        # 照 _sticker_toggle 的既有做法补一次 load()。
        if path == "stickers.enable" and val:
            STICKERS.load()
        return
    # 有几个是"二元组"形态，单独处理
    if path == "self_activity.min_gap_minutes":
        LIFE.act_gap = (val, max(val, LIFE.act_gap[1]))
    elif path == "self_activity.max_gap_minutes":
        LIFE.act_gap = (min(LIFE.act_gap[0], val), val)
    elif path == "self_activity.skip_chance":
        LIFE.act_skip = val
    elif path == "self_activity.material_keep_minutes":
        LIFE.material_keep = float(val) * 60      # 配置是分钟，实例属性是秒
    elif path == "self_activity.busy_short_min_minutes":
        LIFE.busy_short_duration = (val, max(val, LIFE.busy_short_duration[1]))
    elif path == "self_activity.busy_short_max_minutes":
        LIFE.busy_short_duration = (min(LIFE.busy_short_duration[0], val), val)
    elif path == "weather.alert_cooldown_minutes":
        WEATHER.alert_cooldown = float(val) * 60  # 配置是分钟，实例属性是秒
    elif path == "life.events_per_day":
        LIFE.events_per_day = val
    elif path in ("sd.enable", "imagegen.enable", "local.enable", "local.vision"):
        # 生图/本地模型的开关是"两个键合成一个实例属性"或"下次调用才读"的写法，
        # 光 setattr 会让开关**看着变了实际没变**（sd.enable 尤其隐蔽：毫无报错）。
        SDGEN.reload()
        ROUTER.reload(CFG)


def _config_set(p: dict) -> dict:
    path = str(p.get("path") or "").strip()
    spec = CONSOLE_TUNABLES.get(path)
    if spec is None:
        raise ValueError(f"这个配置项不允许经面板调整：{path!r}")
    lo, hi, step, unit, desc, is_int = spec
    try:
        raw = float(p.get("value"))
    except (TypeError, ValueError):
        raise ValueError("值必须是数字") from None
    if not (lo <= raw <= hi):
        raise ValueError(f"超出允许范围（{lo} ~ {hi}{unit}）")
    val = int(round(raw)) if is_int else round(raw, 4)

    old = _get_path(CFG, path)
    update_config({path: val})     # 合并写：只并这一个键，别整体覆盖
    _sync_tunable(path, val)
    logger.info("控制台：%s %s -> %s（原 %s）", path, desc, val, old)
    return {"ok": True, "path": path, "value": val, "old": old,
            "note": f"{desc}：{old} → {val}{unit}"}



STICKER_CONFIG_FIELDS = ("enable", "send_enable", "only_animated",
                         "judge_enable", "allow_image_fallback")
# 手动上传的图片上限（跟控制台的请求体上限配套；比收藏时的 max_size_kb 宽松）
STICKER_UPLOAD_MAX = 4 * 1024 * 1024


def _sticker_add(p: dict) -> dict:
    """手动添加一条表情包。

    两种来源：
      - 填了 QQ 表情 ID（emoji_id + emoji_package_id）→ 发送走 mface，是"一个表情"
      - 没填（纯本地图片）→ 发送时不会被选中，除非打开「允许以图片形式发送」
    """
    if not STICKERS.enable:
        raise ValueError("表情包功能当前是关闭的，请先在上面把总开关打开")
    raw64 = str(p.get("data_b64") or "").strip()
    if not raw64:
        raise ValueError("缺少图片数据")
    try:
        data = base64.b64decode(raw64, validate=True)
    except Exception:
        raise ValueError("图片数据不是合法 base64") from None
    if not data:
        raise ValueError("图片数据为空")
    if len(data) > STICKER_UPLOAD_MAX:
        raise ValueError(f"图片过大（{len(data)//1024}KB，上限 {STICKER_UPLOAD_MAX//1024}KB）")

    # 按魔数判断格式，别信上传过来的文件名
    if data[:6] in (b"GIF87a", b"GIF89a"):
        ext, animated = ".gif", True
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext, animated = ".png", False
    elif data[:2] == b"\xff\xd8":
        ext, animated = ".jpg", False
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext, animated = ".webp", b"ANIM" in data[:64]
    else:
        raise ValueError("不认识的图片格式（支持 gif / png / jpg / webp）")

    md5 = hashlib.md5(data).hexdigest()
    if any(i.get("md5") == md5 for i in STICKERS.items):
        raise ValueError("这条已经收藏过了（内容一样）")

    STICKERS.dir.mkdir(parents=True, exist_ok=True)
    path = STICKERS.dir / f"{md5}{ext}"
    path.write_bytes(data)

    eid = str(p.get("emoji_id") or "").strip()
    pid = str(p.get("emoji_package_id") or "").strip()
    tags = [t.strip() for t in re.split(r"[,，、\s]+", str(p.get("tags") or ""))
            if t.strip()][:8]
    entry = {
        "md5": md5, "path": str(path),
        "who": str(p.get("who") or "手动添加")[:20],
        "tags": tags, "ctx": str(p.get("ctx") or "")[-150:],
        "ts": time.time(), "used": 0, "animated": animated,
        "emoji_id": eid, "emoji_package_id": pid,
        "key": str(p.get("key") or "").strip(),
        "summary": str(p.get("summary") or "").strip(),
        "native": bool(eid and pid),
    }
    STICKERS.items.append(entry)
    while len(STICKERS.items) > STICKERS.max:
        old = STICKERS.items.pop(0)
        try:
            os.remove(old["path"])
        except OSError:
            pass
    STICKERS.save()
    logger.info("控制台：手动添加表情 %s（原生=%s 标签=%s）",
                md5[:8], entry["native"], tags or "无")
    return {"ok": True, "md5": md5, "native": entry["native"], "tags": tags,
            "file": path.name,
            "note": ("已添加，发送时会走 QQ 原生表情" if entry["native"]
                     else "已添加，但没填 QQ 表情 ID —— 她不会用它（除非打开「允许以图片形式发送」）")}


def _sticker_delete(p: dict) -> dict:
    md5 = str(p.get("md5") or "").strip()
    for i, item in enumerate(STICKERS.items):
        if item.get("md5") == md5:
            STICKERS.items.pop(i)
            try:
                os.remove(item["path"])
            except OSError:
                pass
            STICKERS.save()
            logger.info("控制台：删除表情 %s", md5[:8])
            return {"ok": True, "md5": md5}
    raise ValueError(f"库里没有这条（{md5[:8]}）")


def _sticker_toggle(p: dict) -> dict:
    field = str(p.get("field") or "").strip()
    if field not in STICKER_CONFIG_FIELDS:
        raise ValueError(f"不允许改的表情包配置项：{field!r}")
    value = bool(p.get("value"))
    update_config({f"stickers.{field}": value})   # 合并写，别整体覆盖
    # 这几个开关里，前四个是实例属性（init 时从配置读一次），要同步到实例上；
    # allow_image_fallback 是 live 读 CFG 的，不用同步。
    if field == "enable":
        STICKERS.enable = value
        if value:
            STICKERS.load()
    elif field == "send_enable":
        STICKERS.send_enable = value
    elif field == "only_animated":
        STICKERS.only_animated = value
    elif field == "judge_enable":
        STICKERS.judge_enable = value
    logger.info("控制台：表情包配置 %s 设为 %s", field, value)
    return {"ok": True, "field": field, "value": value,
            "note": {"enable": "表情包功能总开关",
                     "send_enable": "她" + ("可以" if value else "不会") + "主动发表情包",
                     "only_animated": "只收藏动图" if value else "动图静图都收",
                     "judge_enable": "收藏前" + ("让模型判断" if value else "不判断"),
                     "allow_image_fallback": ("没有 QQ 表情 ID 的条目会当图片发出去"
                                              if value else "只会发 QQ 原生表情，绝不发图片"),
                     }.get(field, "")}


def update_config(patch: dict) -> dict:
    """把「点路径 -> 值」的改动合并进**磁盘上最新的** config.json 并写回。

    ⚠️ 不要直接把内存里的 CFG 整体写回 —— 进程启动之后 config.json 可能被外部改过
    （手工编辑、或者另一个进程写），内存里那份已经旧了，整体覆盖会**静默丢掉**那些改动。
    实测踩过：面板上拖一次滑块，把之后手工加进去的整段 self_activity 冲没了。

    做法：重读磁盘 → 只把本次改动应用上去 → 原子写回 → 再把合并结果同步回内存 CFG。
    """
    try:
        fresh = load_config()
    except ValueError as exc:
        # 磁盘上的配置坏了：宁可退回内存版本，也别把文件写得更烂
        logger.warning("写配置前重读失败（%s），本次退回用内存里的版本", exc)
        fresh = json.loads(json.dumps(CFG, ensure_ascii=False))
    for path, val in patch.items():
        node = fresh
        keys = str(path).split(".")
        for k in keys[:-1]:
            nxt = node.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                node[k] = nxt
            node = nxt
        node[keys[-1]] = val
    _backup_config_roll()
    atomic_write_json(CONFIG_PATH, fresh)
    # 内存也跟着刷新，保证与磁盘一致（CFG 是模块级 dict，原地更新不换引用）
    CFG.clear()
    CFG.update(fresh)
    # ★ 注册中心必须跟着重建：端点对象 / api_key / 并发闸都是按配置算出来的，
    #   不重载的话会出现「改了 api_key 不生效、删了的端点还在打旧地址」这种鬼现象。
    ROUTER.reload(CFG)
    # 生图同理：端点/链/云端上限/负缓存一起刷新（但不重读角色字段，见 SD.reload）
    IMAGE_ROUTER.reload(CFG)
    SDGEN.reload(CFG)
    return fresh


def drop_config(paths: list[str]) -> list[str]:
    """从 config.json 里**删掉**几个键，返回真正删掉的。

    为什么需要它：`update_config` 只能改值、删不了。而人设那几个老键必须**删**——
    置成空串在老键判定里也算"写过"（见 persona.legacy_overrides），
    空串照样盖住角色卡，等于没删。

    写法跟 update_config 一致：重读磁盘 → 只动这几个键 → 原子写回 → 同步内存。
    绝不拿内存里那份整体覆盖磁盘（会静默丢掉外部改过的内容）。
    """
    try:
        fresh = load_config()
    except ValueError as exc:
        logger.warning("删配置前重读失败（%s），本次退回用内存里的版本", exc)
        fresh = json.loads(json.dumps(CFG, ensure_ascii=False))
    gone: list[str] = []
    for path in paths:
        keys = str(path).split(".")
        node = fresh
        hit = True
        for k in keys[:-1]:
            nxt = node.get(k)
            if not isinstance(nxt, dict):
                hit = False       # 中间那层本来就不存在，等于这个键没有
                break
            node = nxt
        if hit and keys[-1] in node:
            node.pop(keys[-1], None)
            gone.append(path)
    if not gone:
        return []
    _backup_config_roll()
    atomic_write_json(CONFIG_PATH, fresh)
    CFG.clear()
    CFG.update(fresh)
    ROUTER.reload(CFG)
    IMAGE_ROUTER.reload(CFG)
    SDGEN.reload(CFG)
    logger.info("已从 config.json 删掉这些老键（从现在起以角色卡为准）：%s", gone)
    return gone


def _need_loop(what: str) -> None:
    """有的面板操作只是"把异步活儿丢出去"，必须在 bot 的事件循环里才能丢。

    console_apply 由 run_in_loop 调度，正常都在循环里；直接在解释器里手调的
    时候不在，这里给一句人话，别让它抛 RuntimeError 那种看不懂的东西。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        raise ValueError(f"{what}要在 bot 的事件循环里做") from None


def console_apply(op: str, payload: dict | None = None) -> dict:
    """控制台写操作。由 console_server 经 run_in_loop 调用 → **始终在事件循环内执行**。

    安全边界（务必保持）：
    - op 必须命中 console_server.WRITE_OPS 白名单，这里再兜一层 else 分支。
    - 只碰下面这些**明确字段**，不做「任意 config 路径写入」——
      否则密钥类字段可以绕过 read 侧的脱敏被改掉。
    - config 改动走 atomic_write_json，改前留一份滚动快照。
    """
    p = payload or {}

    if op == "restrict_set":
        feat = str(p.get("feature") or "").strip()
        admin = CFG.setdefault("admin", {})
        restrict = admin.get("restrict")
        if not isinstance(restrict, dict) or feat not in restrict:
            raise ValueError(f"未知的受限功能：{feat!r}")
        value = bool(p.get("value"))
        # CFG 是模块级 dict，restricted()/allowed() 每次 live 读 → 改完立即生效
        # 合并写：只把这一个键并进磁盘上最新那份，别整体覆盖（见 update_config）
        update_config({f"admin.restrict.{feat}": value})
        logger.info("控制台：受限功能 %s 设为 %s", feat, value)
        return {"ok": True, "feature": feat, "value": value,
                "note": f"{feat} 现在{'仅管理员可用' if value else '对所有人开放'}"}

    if op == "access_set":
        admin = CFG.setdefault("admin", {})
        ban = CFG.setdefault("ban", {})
        patch: dict = {}
        if "admin_user_ids" in p:
            patch["admin.user_ids"] = [int(x) for x in (p.get("admin_user_ids") or [])]
        if "protected_ids" in p:
            patch["ban.protected_ids"] = [int(x) for x in (p.get("protected_ids") or [])]
        update_config(patch)     # 合并写，别整体覆盖
        MEMORY.ensure_admins()   # 新管理员要 pin 进长期记忆，别被裁剪掉
        admin = CFG.get("admin") or {}
        ban = CFG.get("ban") or {}
        logger.info("控制台：管理员=%s 豁免=%s", admin.get("user_ids"), ban.get("protected_ids"))
        return {"ok": True, "admin_user_ids": admin.get("user_ids"),
                "protected_ids": ban.get("protected_ids")}

    if op == "persona_set":
        field = str(p.get("field") or "").strip()
        if field not in CONSOLE_CONFIG_FIELDS:
            raise ValueError(f"不允许经控制台修改的字段：{field!r}")
        value = str(p.get("value") or "")
        if len(value) > 20000:
            raise ValueError("内容过长（上限 20000 字）")
        update_config({field: value})    # 合并写，别整体覆盖
        logger.info("控制台：%s 已更新（%d 字）", field, len(value))
        return {"ok": True, "field": field, "length": len(value),
                "note": "下一条回复即生效（build_system 每次 live 读 CFG）"}

    if op == "group_note_delete":
        gid = str(p.get("group") or "").strip()
        try:
            idx = int(p.get("index"))
        except (TypeError, ValueError):
            raise ValueError("index 必须是整数") from None
        gone = GMEM.remove_note(gid, idx)
        return {"ok": True, "group": gid, "removed": gone}

    if op == "group_forget":
        gid = str(p.get("group") or "").strip()
        n = GMEM.forget(gid)
        if not n:
            raise ValueError(f"没有这个群的记忆：{gid}")
        return {"ok": True, "group": gid, "removed": n}

    if op == "config_set":
        return _config_set(p)

    if op == "switch_set":
        path = str(p.get("path") or "").strip()
        if path not in CONSOLE_SWITCHES:
            raise ValueError(f"这个开关不在面板白名单里：{path!r}")
        label = CONSOLE_SWITCHES[path][0]
        value = bool(p.get("value"))
        old = _get_path(CFG, path)
        update_config({path: value})     # 合并写：只并这一个键，别整体覆盖
        _sync_tunable(path, value)       # init 时读一次的实例属性要跟着改，否则本次运行不生效
        logger.info("控制台：开关 %s（%s）%s -> %s", label, path, old, value)
        return {"ok": True, "path": path, "value": value, "old": old, "label": label,
                "note": f"{label}：{'开' if value else '关'}"}

    if op == "list_set":
        path = str(p.get("path") or "").strip()
        spec = CONSOLE_LISTS.get(path)
        if spec is None:
            raise ValueError(f"这个列表不在面板白名单里：{path!r}")
        label, _desc, _group, max_items, max_chars = spec
        raw = p.get("lines")
        if isinstance(raw, str):
            raw = raw.splitlines()
        if not isinstance(raw, list):
            raise ValueError("lines 要是字符串列表")
        # 空行是 textarea 的尾随换行，不算一条；超上限**报错**，不静默截断
        lines = [str(x).strip() for x in raw]
        lines = [x for x in lines if x]
        if len(lines) > max_items:
            raise ValueError(f"{label}最多 {max_items} 条（现在 {len(lines)} 条）")
        for x in lines:
            if len(x) > max_chars:
                raise ValueError(f"{label}每条最多 {max_chars} 字，有一条是 {len(x)} 字")
        old = _get_path(CFG, path)
        update_config({path: lines})
        # 列表里有两项是被实例在 __init__ 时**复制**走的（LIFE.events / LIFE.places），
        # 只改 CFG 这次运行不生效 —— 面板上改了却要重启才看到效果，很容易让人以为没保存
        if path == "life.mood_events":
            LIFE.events = list(lines)
        elif path == "self_activity.places":
            LIFE.places = list(lines)
        logger.info("控制台：列表 %s（%s）%d 条（原 %s 条）", label, path, len(lines),
                    len(old) if isinstance(old, list) else "?")
        return {"ok": True, "path": path, "lines": lines, "count": len(lines),
                "note": f"{label}：{len(lines)} 条"}

    if op == "vision_set":
        backend = str(p.get("backend") or "").strip().lower()
        if backend not in VISION_BACKENDS:
            raise ValueError("backend 只能是 auto / cloud / local / off")
        old = VISION.backend()
        update_config({"vision.backend": backend})
        logger.info("控制台：识图后端 %s -> %s", old, backend)
        return {"ok": True, "backend": backend, "old": old,
                "label": VISION_LABELS.get(backend, backend),
                "usable": VISION.usable(),
                "note": f"识图后端：{VISION_LABELS.get(old, old)} → {VISION_LABELS.get(backend, backend)}"}

    if op == "provider_reload":
        # 无参数 = 无攻击面。真正的配置改动仍然只能走 config_set / switch_set /
        # list_set / vision_set，这里只负责"把磁盘上的配置重新读进来"。
        if p:
            raise ValueError("provider_reload 不接受参数")
        before, before_img = ({e.id for e in ROUTER.endpoints},
                              {e.id for e in IMAGE_ROUTER.endpoints})
        notices = ROUTER.reload(CFG)
        IMAGE_ROUTER.reload(CFG)
        # 用户点这个按钮多半是"我后端刚重启了" —— 顺手清掉负缓存，
        # 否则刚活过来的后端还会被退避期挡着，画不出来。
        IMAGE_ROUTER.clear_negative_cache()
        SDGEN.reload(CFG)
        after, after_img = ({e.id for e in ROUTER.endpoints},
                            {e.id for e in IMAGE_ROUTER.endpoints})
        logger.info("控制台：重载端点（对话 %d→%d，生图 %d→%d）",
                    len(before), len(after), len(before_img), len(after_img))
        return {"ok": True,
                "chat_endpoints": len(after), "image_endpoints": len(after_img),
                "chat_added": sorted(after - before), "chat_removed": sorted(before - after),
                "image_added": sorted(after_img - before_img),
                "image_removed": sorted(before_img - after_img),
                "notices": notices}

    if op == "sticker_add":
        return _sticker_add(p)

    if op == "sticker_delete":
        return _sticker_delete(p)

    if op == "sticker_toggle":
        return _sticker_toggle(p)

    if op == "session_clear":
        key = str(p.get("key") or "")
        if not re.fullmatch(r"[gp]\d+", key):
            raise ValueError(f"非法的会话 key：{key!r}")
        removed = CHAT.sessions.pop(key, None) is not None
        CHAT.last_reply_at.pop(key, None)
        logger.info("控制台：清空会话 %s（%s）", key, "已清" if removed else "本就是空")
        return {"ok": True, "key": key, "removed": removed}

    if op in ("memory_approve", "memory_decline"):
        uid = str(p.get("uin") or "").strip()
        if uid not in MEMORY.pending:
            raise ValueError(f"没有待批准的对象：{uid!r}")
        if op == "memory_approve":
            name = MEMORY.approve(uid)
            return {"ok": True, "uin": uid, "name": name, "note": f"开始记着 {name} 了"}
        MEMORY.decline(uid)
        return {"ok": True, "uin": uid, "note": f"已拒绝记住 {uid}，之后不会再问"}

    if op == "memory_forget":
        uid = str(p.get("uin") or "").strip()
        if uid not in MEMORY.people:
            raise ValueError(f"记忆里没有这个人：{uid!r}")
        MEMORY.people.pop(uid, None)
        MEMORY.save()
        logger.info("控制台：忘掉 %s", uid)
        return {"ok": True, "uin": uid}

    if op == "memory_item_delete":
        uid = str(p.get("uin") or "").strip()
        kind = str(p.get("kind") or "")
        if kind not in ("traits", "todos"):
            raise ValueError("kind 只能是 traits 或 todos")
        person = MEMORY.people.get(uid)
        if not person:
            raise ValueError(f"记忆里没有这个人：{uid!r}")
        items = person.get(kind) or []
        try:
            idx = int(p.get("index"))
        except (TypeError, ValueError):
            raise ValueError("index 必须是整数") from None
        if not 0 <= idx < len(items):
            raise ValueError(f"下标越界：{idx}（共 {len(items)} 条）")
        removed = items.pop(idx)
        MEMORY.save()
        logger.info("控制台：从 %s 的 %s 删掉一条：%s", uid, kind, removed)
        return {"ok": True, "uin": uid, "kind": kind, "removed": str(removed)}

    if op == "groups_refresh":
        _need_loop("刷新群名册")
        asyncio.create_task(_safe(GROUPS.refresh()))
        return {"ok": True, "note": "已经在拉群列表了，过两秒再看"}

    if op == "bili_login":
        # ⚠️ 不能在这里 await：console_apply 是**同步**函数（run_in_loop 只跑同步回调），
        # 而且 HTTP 侧只有 3 秒超时，等扫码必然超时。所以丢成后台任务。
        if not BILI.enable:
            raise ValueError("b 站功能没开（config.bili.enable）")
        _need_loop("检查 B 站登录态")
        asyncio.create_task(_safe(_bili_login_once(notify=notify_admins)))
        return {"ok": True, "note": "已经在查登录态；是游客的话二维码会发到管理员 QQ"}

    raise ValueError(f"未实现的写操作：{op!r}")


# 受限功能的中文名。面板上直接显示这个，别再让人对着 qzone_post 这种键猜。
# 键必须与 config.json 的 admin.restrict 完全一致（console_apply 会校验；
# 回归测试里有一条"每个配置键都有中文名"的断言，加了新功能记得同步）。
RESTRICT_LABELS: dict[str, tuple[str, str]] = {
    "chat_draw":     ("聊天里生图",   "在群聊/私聊里让她画图"),
    "qzone_post":    ("发空间说说",   "允许她发 QQ 空间说说"),
    "qzone_draw":    ("说说自动配图", "发说说时顺手配一张自己画的图"),
    "qzone_comment": ("回复空间评论", "评论、回复好友说说下面的留言"),
    "qzone_like":    ("给说说点赞",   "给好友的说说点赞"),
    "qzone_read":    ("读取空间动态", "去看好友的空间内容"),
    "search":        ("联网搜索",     "上网查资料后再回答"),
    "ban":           ("禁言群成员",   "在群里禁言别人（由她自己判断该不该）"),
    "clear":         ("清空对话记忆", "用 /clear 清掉当前会话的上下文"),
    "bili_login":    ("B站扫码登录",  "用 /bili 登录 B 站账号"),
}


def restrict_label(feature: str) -> tuple[str, str]:
    """取受限功能的中文名与说明；没登记就退回原始键名（不至于显示不出来）。"""
    return RESTRICT_LABELS.get(feature, (feature, ""))


def _redact_config(cfg: dict) -> tuple[dict, list[str]]:
    """对 config 做结构化脱敏（实现放在 console_server，此处仅转发并降级）。"""
    try:
        import console_server as cs
        return cs.redact_config(cfg)
    except Exception as exc:
        logger.warning("配置脱敏失败，改为整体屏蔽：%s", exc)
        return {"_error": "脱敏失败，已停止展示配置内容"}, []


def console_snapshot(kind: str, arg=None) -> dict:
    """控制台只读快照。

    由 console_server 通过 run_in_loop 调用，因此**始终在 bot 事件循环内执行**。
    必须是纯同步、短耗时的函数：不 await、不做网络或大文件 I/O。
    理由见 console_server 模块 docstring —— HTTP 线程直接遍历 CHAT.sessions /
    deque 会撞 RuntimeError: dictionary changed size during iteration。
    """
    if kind == "status":
        # token 的"从哪天开始记 / 最后写到什么时候"统一走 tokens_meta()，
        # 别在这里把同一份字段重抄一遍（抄两遍早晚有一处忘了改）
        _tm = tokens_meta()
        return {
            "ok": True,
            "self_id": OB.self_id,
            "ws_connected": OB.ws is not None,
            "ws_port": CFG.get("ws_port", 6199),
            "console_port": (CFG.get("console") or {}).get("port", 6200),
            "sessions": len(CHAT.sessions),
            "tokens": dict(TOKENS),
            "tokens_since": _tm["since"],
            "tokens_updated": _tm["updated"],
            "tokens_path": _tm["path"],
            "seen_messages": len(SEEN_MSG_IDS),
            "napcat_authed": bool(CFG.get("access_token")),
            "persona_chars": len(str(CFG.get("persona") or "")),
            "memory_people": len(MEMORY.people),
            "memory_last_reset": MEMORY._last_reset,
            # 用 _last_act 而不是 current()：后者会触发 note_act_change 写盘，
            # 不该让一次轮询读去做副作用。
            "life_act": getattr(LIFE, "_last_act", "") or "",
            "weather_city": WEATHER.city or "",
            "active_groups": len(ATTENTION.groups),
        }
    if kind == "sessions":
        rows = []
        for key, hist in list(CHAT.sessions.items()):
            rows.append({
                "key": key,
                "kind": "群聊" if key.startswith("g") else "私聊",
                "turns": len(hist) // 2,
                "messages": len(hist),
                "last_reply_at": float(CHAT.last_reply_at.get(key) or 0),
            })
        rows.sort(key=lambda r: r["last_reply_at"], reverse=True)
        return {"ok": True, "sessions": rows, "attention_groups": len(ATTENTION.groups)}
    if kind == "life":
        cur = dict(LIFE.current() or {})
        cur["weather"] = WEATHER._text or ""
        return {"ok": True, "life": cur,
                "state_path": str(getattr(LIFE, "state_path", "")),
                "schedule": LIFE.schedule,
                "weather_city": WEATHER.city or "",
                "weather_enable": bool(WEATHER.enable),
                # 天气全量快照：体感/湿度/风/今日区间/日出日落 + 刚察觉到的变化 + 联网查到的预警
                "weather": WEATHER.snapshot(),
                "delay": list(LIFE.delay),
                # 她自己在干什么（与群冷热无关的日常）
                "self_activity": LIFE.snapshot()}
    if kind == "group_memory":
        return {"ok": True, **GMEM.as_dict(),
                # 群名册跟群聊记忆是两码事，但都是"群维度"的东西，放同一个面板看更顺
                "registry": GROUPS.as_dict(),
                "restrict": {k: bool(v) for k, v in
                             ((CFG.get("admin") or {}).get("restrict") or {}).items()},
                "note": ("群聊记忆跟群走、不跟人走；不需要管理员点头（留不留由条数上限"
                         "和重置周期管）。人物记忆在 B04，那套才走管理员批准。")}

    if kind == "bili":
        return {
            "ok": True,
            "enable": bool(BILI.enable),
            "auto_login": bool((CFG.get("bili") or {}).get("auto_login", True)),
            "check_minutes": float((CFG.get("bili") or {}).get("login_check_minutes", 60)),
            # guest=None 表示"还没查过"，别把它当成"已登录"或"是游客"
            "guest": BILI.guest,
            "state_text": BILI.state_text(),
            "logged_in_cookie": BILI.logged_in,
            "uid": BILI.uid,
            "uname": BILI.uname,
            "last_error": BILI._login_error,
            "checked_at": float(BILI._login_checked or 0),
            "qr_path": str(BILI.qr_path),
            "qr_exists": BILI.qr_path.exists(),
            "actions_enable": bool(BILI.actions_enable),
            "actions_today": BILI.actions_today(),
            "daily_action_cap": int((CFG.get("bili") or {}).get("daily_action_cap", 6)),
        }

    if kind == "cross_group":
        ban_cfg = CFG.get("ban", {}) or {}
        cg_cfg = CFG.get("cross_group", {}) or {}
        # 谁在哪些群露过面：跨群认人的依据（昵称 -> QQ 号的本地印象）
        seen: dict[str, dict] = {}
        for g, members in SPEAKERS.items():
            for name, uid in members.items():
                rec = seen.setdefault(str(uid), {"uin": str(uid), "names": [], "groups": []})
                if name not in rec["names"]:
                    rec["names"].append(name)
                if GROUPS.label(g) not in rec["groups"]:
                    rec["groups"].append(GROUPS.label(g))
        return {
            "ok": True,
            "enable": bool(cg_cfg.get("enable", True)),
            "default_minutes": float(cg_cfg.get("default_minutes", 10)),
            "max_minutes": float(ban_cfg.get("max_minutes", 10)),
            "protect_admins": bool(ban_cfg.get("protect_admins", True)),
            "protected_ids": [str(x) for x in (ban_cfg.get("protected_ids") or [])],
            "max_per_hour": int(ban_cfg.get("max_per_hour", 3)),
            "groups": GROUPS.as_dict()["groups"],
            "admins": [str(x) for x in ((CFG.get("admin") or {}).get("user_ids") or [])],
            "seen_people": sorted(seen.values(), key=lambda r: -len(r["groups"]))[:30],
            "note": ("跨群禁言：管理员在私聊里说「把某群的某人禁言」，她先翻群名册认群、"
                     "再翻该群成员名单认人 —— 认不出或有歧义就只回话、不动手。"),
        }

    if kind == "memory":
        return {"ok": True, **MEMORY.as_dict()}
    if kind == "mood":
        return {"ok": True, **MOOD.as_dict()}
    if kind == "sd":
        return {"ok": True, **SDGEN.snapshot()}
    if kind == "stickers":
        return {"ok": True, **STICKERS.snapshot()}
    if kind == "persona":
        return {
            "ok": True,
            "persona": CFG.get("persona", ""),
            "world": CFG.get("world", ""),
            "style_boost": CFG.get("style_boost", ""),
            "style_format": CFG.get("style_format", ""),
            "self_image": CFG.get("self_image", ""),
            "identity_guard": CFG.get("identity_guard", ""),
            "wake_prefix": CFG.get("wake_prefix", []),
            # 参照池不进提示词，只在这块面板上给人看/改
            "identity_lines": CFG.get("identity_lines", []),
            "reply_fallback_lines": CFG.get("reply_fallback_lines", []),
        }
    if kind == "vision":
        # 快照必须同步短耗时：本地探测是联网活儿，只能丢后台，读的是缓存结果
        VISION.probe_maybe()
        return VISION.status()
    if kind == "switches":
        groups: dict[str, list] = {}
        for path, (label, desc, group) in CONSOLE_SWITCHES.items():
            groups.setdefault(group, []).append({
                "path": path, "label": label, "desc": desc,
                "value": bool(_get_path(CFG, path)),
            })
        return {"ok": True,
                "groups": [{"name": g, "items": items} for g, items in groups.items()]}
    if kind == "lists":
        lgroups: dict[str, list] = {}
        for path, (label, desc, group, max_items, max_chars) in CONSOLE_LISTS.items():
            lines = _get_path(CFG, path)
            if not isinstance(lines, list):
                lines = []
            lgroups.setdefault(group, []).append({
                "path": path, "label": label, "desc": desc,
                "count": len(lines), "max_items": max_items, "max_chars": max_chars,
                "lines": [str(x) for x in lines],
            })
        return {"ok": True,
                "groups": [{"name": g, "items": items} for g, items in lgroups.items()]}
    if kind == "qzone_actions":
        qcfg = CFG.get("qzone", {}) or {}
        today = datetime.now().strftime("%Y-%m-%d")
        # 今天她在空间里做过什么（从行为流水里筛，别去猜）
        recent: list[dict] = []
        try:
            day = ACTIVITY.read_day(today)
            for e in (day.get("entries") or []):
                if e.get("kind") in ("点赞", "评论", "说说", "画图"):
                    recent.append({"hhmm": e.get("hhmm", ""), "kind": e.get("kind", ""),
                                   "text": e.get("text", "")})
        except Exception as exc:
            logger.debug("读空间动作流水失败：%s", exc)

        # 桥接侧缓存的最近好友动态（raw_feeds 的短缓存，不额外请求）
        feeds = []
        for it in list(getattr(QZONE_API, "_feed_cache", None) or [])[:12]:
            feeds.append({
                "uin": str(it.get("uin") or ""),
                "nickname": str(it.get("nickname") or ""),
                "content": str(it.get("content") or "")[:90],
                "created_time": it.get("created_time") or 0,
                "tid": str(it.get("tid") or ""),
            })
        feed_ts = float(getattr(QZONE_API, "_feed_ts", 0) or 0)
        last_like = float(getattr(QZONE_API, "_like_ts", 0) or 0)
        now = time.time()
        return {
            "ok": True,
            "enable": bool(qcfg.get("enable", True)),
            "post": {
                "enable": bool(QZONE.enable),
                "max_per_day": QZONE.max_per_day,
                "used_today": QZONE.used_today(),
                "auto_probability": QZONE.prob,
                "min_life_chance": QZONE.min_chance,
                "count_manual": bool(qcfg.get("count_manual_posts", False)),
                "max_chars": qcfg.get("max_chars", 200),
                "last_tid": str((QZONE.state or {}).get("last_tid") or ""),
            },
            "comment": {
                "enable": bool(qcfg.get("comment_enable", True)),
                "max_per_day": int(qcfg.get("comment_max_per_day", 10)),
                "used_today": QZONE_API._comments_today(),
                "max_chars": int(qcfg.get("comment_max_chars", 60)),
                # 同一条说说只评一次：记了哪些 tid，最近评过谁
                "once_per_post": bool(qcfg.get("comment_once_per_post", True)),
                "remembered_posts": len(QZONE_API._commented),
                "remember_keep": int(qcfg.get("comment_memory_keep", 300)),
                "remember_days": float(qcfg.get("comment_memory_days", 90)),
                "state_path": str(QZONE_API.comment_state_path),
                "recent_commented": QZONE_API.commented_recent(8),
            },
            "like": {
                "enable": bool(qcfg.get("like_enable", True)),
                "cooldown_seconds": int(qcfg.get("like_cooldown_seconds", 60)),
                "seconds_since_last": (now - last_like) if last_like else None,
            },
            "bridge": {
                "enable": bool(QZONE_API.enable),
                "base": QZONE_API.base,
                "has_token": bool(QZONE_API.token),
                "feed_cache_size": len(getattr(QZONE_API, "_feed_cache", None) or []),
                "feed_cache_age": (now - feed_ts) if feed_ts else None,
            },
            "feeds": feeds,
            "recent": recent[-60:],
            "actions_today": {
                "like": sum(1 for r in recent if r["kind"] == "点赞"),
                "comment": sum(1 for r in recent if r["kind"] == "评论"),
                "post": sum(1 for r in recent if r["kind"] == "说说"),
            },
            # 自主互动：她自己闲着时去空间互动的开关/概率/节流
            "auto_interact": {
                "enable": bool(qcfg.get("auto_interact_enable", True)),
                "probability": float(qcfg.get("auto_interact_probability", 0.15)),
                "cooldown_minutes": float(qcfg.get("auto_interact_cooldown_minutes", 40)),
                "seconds_since_last": (now - _LAST_AUTO_INTERACT) if _LAST_AUTO_INTERACT else None,
            },
            # 跨群好感 + 心情 —— 决定她愿不愿意跟谁互动（空间不分群，所以跨群合并）
            "mood": {
                "enable": MOOD.enable,
                "scale": "0-100",
                "text": MOOD.render_all(),
                "people": [
                    {"uin": u, "name": n, "affinity": a, "mood": f,
                     "tag": MOOD._tag(a)}
                    for u, (n, a, f, _gid) in sorted(MOOD._merged().items(),
                                                    key=lambda kv: -abs(kv[1][1] - MOOD.NEUTRAL))
                ],
            },
        }

    if kind == "activity":
        return {"ok": True, "days": ACTIVITY.list_days(),
                "day": ACTIVITY.read_day(arg) if arg else None}
    if kind == "tunables":
        items = []
        for path, spec in CONSOLE_TUNABLES.items():
            lo, hi, step, unit, desc, is_int = spec
            items.append({
                "path": path,
                "group": _tunable_group(path),
                "min": lo, "max": hi, "step": step,
                "unit": unit, "desc": desc, "int": is_int,
                "value": _get_path(CFG, path),
            })
        return {"ok": True, "items": items}

    if kind == "meta":
        ban = CFG.get("ban", {}) or {}
        admin = CFG.get("admin", {}) or {}
        restrict = admin.get("restrict", {}) or {}
        # 这两本账"一天一个键"，只增不删会越挂越大（meta 是整份上抛给面板的）——
        # 上抛前先清掉非今天的键，只留今天这一格。
        _prune_ban_days()
        # QQ 号 -> 昵称，面板上显示成「10002（示例用户1）」
        known: dict = {}
        for uid, person in MEMORY.people.items():
            nm = str((person or {}).get("name") or "").strip()
            if nm:
                known[str(uid)] = nm
        return {
            "ok": True,
            "known_names": known,
            # 中文名 + 说明，面板直接拿来显示；未登记的退回原始键名
            "restrict_labels": {
                k: {"name": restrict_label(k)[0], "desc": restrict_label(k)[1]}
                for k in restrict
            },
            "admin": {"user_ids": admin.get("user_ids", []),
                      "restrict": restrict},
            "ban": {"enable": ban.get("enable", True),
                    "default_minutes": ban.get("default_minutes", 10),
                    "max_minutes": ban.get("max_minutes"),
                    "self_max_minutes": ban.get("self_max_minutes", 60),
                    "max_per_hour": ban.get("max_per_hour"),
                    "max_per_day": ban.get("max_per_day", 10),
                    "same_target_cooldown_seconds": ban.get("same_target_cooldown_seconds", 600),
                    "protect_admins": ban.get("protect_admins", True),
                    "protected_ids": ban.get("protected_ids", [])},
            "rate_limit": CFG.get("rate_limit", {}),
            "call_log": {k: len(v) for k, v in CALL_LOG.items()},
            "ban_log": {k: len(v) for k, v in BAN_LOG.items()},
            # 本轮的限流账本（内存，重启清零）：今天用了多少、谁还在冷却
            "ban_day": dict(BAN_DAY),
            "streak_ban_day": dict(STREAK_BAN_DAY),
            "ban_at": {k: v for k, v in list(BAN_AT.items())[-40:]},
            "poke_streak": {k: len(v) for k, v in POKE_STREAK.items()},
            "recent_speakers": {k: len(v) for k, v in RECENT_SPEAKERS.items()},
            "group_cooldown": CFG.get("group_cooldown_seconds"),
            "max_context_turns": CFG.get("max_context_turns"),
            "context_max_chars": (CFG.get("context") or {}).get("max_chars"),
        }
    if kind == "providers":
        # ★ 只读、同步、**不联网**：两个 status() 都只读内存与缓存。
        #   绝不能在这里调 probe_all()（它是 async 且联网，会把这个 3 秒超时的
        #   快照调用卡成 503）。
        return {"ok": True, "chat": ROUTER.status(CFG), "image": IMAGE_ROUTER.status(),
                "selfcheck": list(GLOBAL_SELFCHECK)}
    if kind == "config":
        redacted, masked = _redact_config(CFG)
        return {"ok": True, "config": redacted, "redacted_paths": masked}
    raise ValueError(f"未知的快照类型：{kind}")


def sibling_dir(name: str, fallback: str) -> Path:
    """取同级的兄弟项目目录。

    布局假设：<root>/qqbot、<root>/qzone-bridge、<root>/NapCat 三者平级。
    这样整体搬盘（换个目录、换个盘符）不需要改任何代码；
    找不到才退回给定旧路径。
    """
    cand = BASE.parent / name
    return cand if cand.is_dir() else Path(fallback)


def _console_log_sources(cfg: dict) -> dict:
    """控制台的日志源表。

    优先级：config.console.log_sources 里配的且路径真实存在 > 同级目录推导 > 旧默认。
    这样配置改了位置跟着走，配置没写也能用。
    """
    raw = cfg.get("log_sources") or {}

    bot_spec = str(raw.get("bot") or "bot.log")
    bot_path = bot_spec if Path(bot_spec).is_absolute() else str(BASE / bot_spec)

    q = raw.get("qzone")
    if not (isinstance(q, str) and Path(q).exists()):
        q = str(sibling_dir("qzone-bridge", "qzone-bridge") / "qzone-bridge.log")

    n = raw.get("napcat")
    n_dir = n.get("dir") if isinstance(n, dict) else (n if isinstance(n, str) else None)
    if not (n_dir and Path(str(n_dir)).is_dir()):
        n_dir = str(sibling_dir("NapCat", "NapCat") / "logs")

    return {"bot": bot_path, "qzone": q,
            "napcat": {"dir": str(n_dir), "pattern": "*.log"}}


def _start_console() -> None:
    """启动只读控制台（默认 6200）。任何失败都不影响 bot 主链路。"""
    cfg = CFG.get("console", {}) or {}
    if not bool(cfg.get("enable", True)):
        logger.info("控制台已在配置中禁用")
        return
    try:
        import console_server as cs
    except Exception as exc:
        logger.warning("控制台模块加载失败，已跳过：%s", exc)
        return

    port = int(cfg.get("port", 6200))
    token_file = BASE / "state" / "console-token"
    try:
        assert LOOP is not None, "事件循环尚未就绪"
        cs.set_loop(LOOP)
        cs.register_secrets([
            CFG.get("access_token"),
            # 端点密钥不再写死 cloud/local 两个槽位 —— 有多少端点就注册多少，
            # 否则新增的厂商 key 会明文出现在面板 JSON 里
            *[ep.auth() for ep in ROUTER.endpoints],
            (CFG.get("qzone") or {}).get("bridge_token"),
        ])
        cs.start_server(
            host="127.0.0.1",
            port=port,
            base_dir=BASE / "state",
            configured_token=str(cfg.get("token") or ""),
            static_path=BASE / "public" / "console.html",
            log_path=BASE / "bot.log",
            log_sources=_console_log_sources(cfg),
            # 面板要预览出图 / 表情包，这两个目录交给控制台按文件名安全读取
            image_dir=str(SDGEN.dir),
            sticker_dir=str(STICKERS.dir),
            snapshot=console_snapshot,
            apply_fn=console_apply,
            start_ts=START_TS,
            qzone_bridge_url=str(cfg.get("qzone_bridge_url") or "http://127.0.0.1:5700"),
            qzone={
                "qzone_console_token": cfg.get("qzone_console_token", ""),
                # 没配就用同级的 qzone-bridge 目录推导 —— 搬盘后无需改配置
                "qzone_console_token_file": (
                    cfg.get("qzone_console_token_file")
                    or str(sibling_dir("qzone-bridge", "qzone-bridge")
                           / "test_cache" / "console-token")
                ),
            },
            logger=logger,
        )
        logger.info("控制台已启动：http://127.0.0.1:%s", port)
        logger.info("访问令牌见 %s（也可在 config.json 的 console.token 里固定）", token_file)
    except OSError as exc:
        logger.error("控制台端口 %s 启动失败（bot 本体不受影响）：%s", port, exc)
    except Exception as exc:
        logger.exception("控制台启动失败（bot 本体不受影响）：%s", exc)


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    """TCP 探活。用 socket 而不是 OS 命令 —— 三平台行为一致，也没有额外依赖。"""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# 启动自检结果（内存缓存），控制台的「模型端点」面板会读它
GLOBAL_SELFCHECK: list[dict] = []


def _parse_hostport(url: str, default_host: str, default_port: int) -> tuple[str, int]:
    """从 `http://host:port/path` 里抠出 host 与 port，抠不出来就用默认值。"""
    from urllib.parse import urlparse
    try:
        u = urlparse(url if "://" in url else "http://" + url)
        return (u.hostname or default_host), int(u.port or default_port)
    except Exception:  # noqa: BLE001
        return default_host, default_port


def _startup_selfcheck() -> list[dict]:
    """启动时逐项体检，把结论写进日志**并留在内存里**给控制台看。

    为什么要做这件事：这个项目有一堆"本机依赖"（本地模型、生图后端、空间桥、接入端）。
    配置不够的人不会看日志，只会觉得"机器人是个哑巴"。所以启动时就把
    「哪个功能因为什么不可用」摊开说，而不是等他去猜。

    每项：{key, label, level, detail, hint}，level ∈ ok / warn / error / off
    """
    out: list[dict] = []
    # 自己读，别依赖调用方的局部变量（这函数也会被控制台与测试直接调用）
    host = str(CFG.get("ws_host", "127.0.0.1"))

    def add(key: str, label: str, level: str, detail: str, hint: str = "") -> None:
        out.append({"key": key, "label": label, "level": level,
                    "detail": detail, "hint": hint})

    # ① 模型端点：至少要有一个能答话的，否则她根本不会说话
    chat_eps = ROUTER.endpoints
    usable = [e.id for e in chat_eps if ROUTER.usable(e)]
    if not chat_eps:
        add("chat", "对话模型", "error", "一个端点都没配",
            "在 config.json 的 providers.endpoints 里配一个，或填顶层 local/cloud 段")
    elif not usable:
        why = "、".join(f"{e.id}({ROUTER.skip_reason(e)})" for e in chat_eps)
        add("chat", "对话模型", "error", f"没有可用端点：{why}",
            "云端要配密钥（api_key_env + 环境变量）；本地要先把模型服务开起来")
    else:
        add("chat", "对话模型", "ok", "可用：" + "、".join(usable))

    # ② 识图
    pol = ROUTER.policy(CFG)
    if not CFG.get("vision_enable", True):
        add("vision", "识图", "off", "总闸关着（vision_enable=false）")
    elif not pol.usable:
        add("vision", "识图", "warn", f"后端={pol.backend}，但当前没有能看图的端点",
            "开了看图能力但没配支持视觉的模型；这样她遇到图片只会说看不到")
    else:
        add("vision", "识图", "ok",
            f"后端={pol.backend}，首个答话端点={pol.first_answerer_id or '—'}")

    # ③ 生图
    img_ready, img_reason, img_why = SDGEN.check_ready("private")
    if not SDGEN.enable:
        add("imagegen", "生图", "off", "关着（sd.enable 或 imagegen.enable=false）")
    elif img_ready:
        add("imagegen", "生图", "ok",
            "可用：" + "、".join(e.id for e in IMAGE_ROUTER.endpoints if IMAGE_ROUTER.usable(e)))
    else:
        add("imagegen", "生图", "warn" if img_reason in ("quota", "empty-intent") else "error",
            f"不可用（{img_reason}）：{img_why}",
            "本地后端要先开 SD WebUI / ComfyUI；云端后端要配密钥")

    # ④ 空间桥 / 接入端：端口探活（纯 socket，跨平台）
    bridge_host, bridge_port = _parse_hostport(
        str((CFG.get("console") or {}).get("qzone_bridge_url") or "http://127.0.0.1:5700"),
        "127.0.0.1", 5700)
    if not (CFG.get("qzone") or {}).get("bridge_enable", True):
        add("bridge", "空间桥接", "off", "关着（qzone.bridge_enable=false）")
    elif _port_open(bridge_host, bridge_port):
        add("bridge", "空间桥接", "ok", f"{bridge_host}:{bridge_port} 已监听")
    else:
        add("bridge", "空间桥接", "warn",
            f"{bridge_host}:{bridge_port} 连不上",
            "qzone-bridge 没启动 → 她看不到好友动态、发不了说说；不影响群聊")

    ws_port = int(CFG.get("ws_port", 6199))
    ws_up = _port_open(host, ws_port)
    try:
        import onebot as _ob  # noqa: PLC0415
        _spec = _ob.pick_adapter(CFG, BASE.parent)
        _name = _spec.label if _spec else "（未探测到，可能是外部自备）"
    except Exception:  # noqa: BLE001
        _name = "（读不出接入端配置）"
    add("onebot", "QQ 接入端", "ok" if ws_up else "warn",
        f"{host}:{ws_port} " + ("已监听" if ws_up else "还没连上") + f"；接入端={_name}",
        f"接入端要反向连到 ws://{host}:{ws_port}/ws，"
        f"token 必须与 config.json 的 access_token 一致")

    GLOBAL_SELFCHECK.clear()
    GLOBAL_SELFCHECK.extend(out)
    for it in out:
        icon = {"ok": "✅", "warn": "⚠️", "error": "❌", "off": "⏸️"}.get(it["level"], "•")
        line = f"{icon} {it['label']}：{it['detail']}"
        if it["hint"] and it["level"] in ("warn", "error"):
            line += f"  → {it['hint']}"
        if it["level"] == "error":
            logger.error("[自检] %s", line)
        elif it["level"] == "warn":
            logger.warning("[自检] %s", line)
        else:
            logger.info("[自检] %s", line)
    bad = [it for it in out if it["level"] == "error"]
    if bad:
        logger.error("[自检] 有 %d 项不可用，功能会缺失；详细原因见上面几行。"
                     "也可以跑 tools/doctor.py 做一次完整体检。", len(bad))
    return out


async def main() -> None:
    global LOOP
    acquire_lock()
    tokens_load()          # 把永久累计读回来（文件不在就建一份）
    _warn_admin_unset()    # admin 名单为空 = 不限制，得让人知道
    host = CFG.get("ws_host", "127.0.0.1")
    _startup_selfcheck()   # 逐项体检：哪个功能为什么不可用，启动时就说清楚
    port = int(CFG.get("ws_port", 6199))
    try:
        async with websockets.serve(ws_handler, host, port, ping_interval=20, ping_timeout=60):
            LOOP = asyncio.get_running_loop()
            logger.info("服务已启动 ws://%s:%s  (等待 NapCat 反向连接)", host, port)
            # 后台循环**错峰启动**：它们都要调模型，同一秒一起跑就会互相排队
            # （天气搜索 / 自发说说 / 空间互动 / 冷场自语 / 行为报告挤在一起，
            #   前台有人跟她说话时还得等）。每个错开 7 秒，周期又各不相同，
            #   于是撞在一起的几率大幅下降 —— 比上一版"六个循环同时发车"好得多。
            for _i, _factory in enumerate((idle_loop, self_activity_loop, weather_loop,
                                           daily_loop, report_loop, bili_login_loop)):
                asyncio.create_task(_safe(_staggered(_factory, _i * 7.0)))
            logger.info("作息=%s  长期记忆=%d 条  天气=%s  token 累计=%s",
                        LIFE.current()["act"] or "未配置", len(MEMORY.people),
                        WEATHER.city or "关闭", f"{TOKENS['calls']} 次调用")
            _start_console()
            await asyncio.Future()
    except OSError as exc:
        logger.error("端口 %s 启动失败：%s", port, exc)
        logger.error("多半是已经有一个实例在跑了。先关掉它，或者改 config.json 里的 ws_port。")
        # 退出码 2 = 端口占用/已有实例，守护脚本据此区分「不该重启」与「崩溃需重启」
        sys.exit(2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("已停止")
    finally:
        release_lock()
