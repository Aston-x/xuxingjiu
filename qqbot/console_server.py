"""qqbot 只读控制台后端（零新增依赖）。

设计约束（改动前务必先读）：

1. **本模块顶层绝不 import bot。** 对 bot 内部状态的一切访问都通过注入的
   snapshot 回调完成。这样 `import console_server` 无副作用，也不会与 bot
   形成循环导入；`import bot` 更不会顺带起 6200 监听。

2. **HTTP 服务跑在独立 daemon 线程**（ThreadingHTTPServer）。因此**不能**
   直接迭代 bot 的 dict / deque —— 主循环里 handle_message 是 create_task
   并发跑的，遍历中途被改会抛 RuntimeError: dictionary changed size /
   deque mutated during iteration。一切**内存态**读取都必须经 run_in_loop
   回到事件循环里执行。

3. **落在磁盘上的状态文件可以直读**：bot 侧统一走 atomic_write_json
   （tmp + os.replace），读方要么看到旧文件要么看到新文件，不会读到半截。

4. 所有进 loop 的调用都带硬超时，且被调函数必须是**短同步函数**：
   run_coroutine_threadsafe 返回的是 concurrent.futures.Future（超时异常是
   concurrent.futures.TimeoutError），而且同步函数不响应 cancel()，
   所以长活（HTTP 代理、日志尾读）一律留在线程侧做。

5. 只监听 127.0.0.1。面板会展示（脱敏后的）config.json，绝不能暴露到局域网。
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import io
import json
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

# ──────────────────────────────────────────────
# 事件循环桥
# ──────────────────────────────────────────────


class LoopUnavailable(RuntimeError):
    """bot 事件循环不可用（未启动 / 正在重启 / 已崩溃）。"""


class LoopTimeout(RuntimeError):
    """进 loop 的回调超时（loop 被长同步调用卡住）。"""


LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_BOUND_AT: float = 0.0


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """由 bot 在 main() 内、拿到 running loop 之后调用。"""
    global LOOP, _LOOP_BOUND_AT
    LOOP = loop
    _LOOP_BOUND_AT = time.time()


def loop_alive() -> bool:
    return LOOP is not None and not LOOP.is_closed() and LOOP.is_running()


async def _call_sync(fn, args):
    """在 loop 里跑一个纯同步回调；不 await 任何东西，立即返回。"""
    return fn(*args)


def run_in_loop(fn, *args, timeout: float = 3.0):
    """线程侧调用：把同步回调丢进 bot 的 loop 执行并取回结果。

    抛 LoopUnavailable / LoopTimeout，其余异常原样上抛。
    """
    if not loop_alive():
        raise LoopUnavailable("bot 事件循环不可用（未启动、正在重启或已崩溃）")
    try:
        fut = asyncio.run_coroutine_threadsafe(_call_sync(fn, args), LOOP)
    except RuntimeError as exc:  # loop 在两次检查之间被关闭
        raise LoopUnavailable(str(exc)) from exc
    try:
        return fut.result(timeout)
    except concurrent.futures.TimeoutError as exc:
        fut.cancel()  # 尽力而为：已在跑的同步函数无法真正取消
        raise LoopTimeout(f"回调超时（{timeout}s），事件循环可能被阻塞") from exc


# ──────────────────────────────────────────────
# 脱敏
# ──────────────────────────────────────────────

# 文本层：凭据关键词**必须带赋值关系**才命中，避免误伤正常聊天内容
_TEXT_SENSITIVE_RE = re.compile(
    r"(?:token|密码|密钥|口令|password|passwd|secret|api[_-]?key|authorization"
    r"|bearer|access[_-]?key|credential)"
    r"[\"']?(?:\s*(?:[:=：]|是|为)\s*[\"']?[^\s，。；、\"']{3,}|\s+[A-Za-z0-9_\-./]{6,})",
    re.IGNORECASE,
)

# 结构化层：按 key 名判断（$ 锚定，避免误伤 base_url / tokenizer 之类）
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|bridge[_-]?token|console[_-]?token"
    r"|secret|password|passwd|cookie|authorization|token)$",
    re.IGNORECASE,
)

KNOWN_SECRETS: set[str] = set()


def register_secrets(values) -> None:
    """由 bot 启动时把已知密钥字面量登记进来，用于兜掉日志里的明文。"""
    for v in values:
        s = str(v or "")
        if len(s) >= 6:  # 太短的值做全文替换会误伤正文
            KNOWN_SECRETS.add(s)


def redact_text(s: str) -> str:
    """文本脱敏：正则命中的凭据 + 已知密钥字面量，一律换成 ***。"""
    raw = str(s or "")
    if not raw:
        return raw
    try:
        raw = _TEXT_SENSITIVE_RE.sub("***", raw)
    except Exception:
        pass
    for tok in KNOWN_SECRETS:
        if tok in raw:
            raw = raw.replace(tok, "***")
    return raw


def _mask(value: str) -> str:
    s = str(value)
    if not s:
        return ""  # 空值原样保留，让用户看得出"没配"
    if len(s) <= 8:
        return "*" * len(s)
    return "*" * 6 + s[-4:]


def redact_config(obj, prefix: str = ""):
    """递归脱敏配置对象。

    返回 (脱敏后的对象, 被打码的路径列表)。路径列表用于"写回保护"：
    前端把打码值原样 PUT 回来时，必须认得出这是打码值而不是新密钥。
    """
    if isinstance(obj, dict):
        out: dict = {}
        masked: list[str] = []
        for k, v in obj.items():
            path = f"{prefix}.{k}".lstrip(".")
            if isinstance(v, str) and v and _SECRET_KEY_RE.search(str(k)):
                out[k] = _mask(v)
                masked.append(path)
            else:
                out[k], sub = redact_config(v, path)
                masked.extend(sub)
        return out, masked
    if isinstance(obj, list):
        out_list: list = []
        masked = []
        for i, v in enumerate(obj):
            vv, sub = redact_config(v, f"{prefix}[{i}]")
            out_list.append(vv)
            masked.extend(sub)
        return out_list, masked
    return obj, []


# ──────────────────────────────────────────────
# 令牌
# ──────────────────────────────────────────────


def load_or_create_token(base_dir: Path, configured: str) -> str:
    """优先用配置里的令牌；留空则生成并持久化到 state/console-token。"""
    import secrets as _secrets

    token = (configured or "").strip()
    if token:
        return token
    f = Path(base_dir) / "console-token"
    try:
        existing = f.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = _secrets.token_urlsafe(24)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(token + "\n", encoding="utf-8")
        try:
            f.chmod(0o600)
        except OSError:
            pass  # Windows 上 chmod 语义有限，失败可接受
    except OSError:
        pass
    return token


# ──────────────────────────────────────────────
# 日志尾读
# ──────────────────────────────────────────────


def _rotated_chain(log_path: Path) -> list[Path]:
    """按「最旧 -> 最新」返回日志文件链：bot.log.2, bot.log.1, bot.log"""
    rotated: list[Path] = []
    for p in log_path.parent.glob(log_path.name + ".*"):
        try:
            idx = int(p.suffix.lstrip("."))
        except ValueError:
            continue
        rotated.append((idx, p))  # type: ignore[arg-type]
    rotated.sort(key=lambda t: t[0], reverse=True)  # type: ignore[arg-type]
    return [p for _, p in rotated] + [log_path]


def read_log_tail(log_path: Path, limit: int = 200, level: str = "") -> list[str]:
    """读取日志尾部若干行。带轮转处理、级别过滤，输出前已脱敏。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 2000))

    buf: deque[str] = deque(maxlen=limit)
    for p in _rotated_chain(Path(log_path)):
        if not p.exists():
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                buf.extend(line.rstrip("\n\r") for line in f)
        except OSError:
            continue

    levels = {x.strip().upper() for x in str(level or "").split(",") if x.strip()}
    out = list(buf)
    if levels:
        out = [ln for ln in out if any(f"[{lv}]" in ln for lv in levels)]
    return [redact_text(ln) for ln in out]


# ──────────────────────────────────────────────
# 图片预览（出图 / 表情包）
# ──────────────────────────────────────────────

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_CTYPE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}

# 缩略图内存缓存：(绝对路径, 宽度, mtime) -> (字节, content-type)
_THUMB_CACHE: dict[tuple, tuple[bytes, str]] = {}
_THUMB_CACHE_MAX = 240


def content_type_for(suffix: str) -> str:
    return _CTYPE.get(str(suffix).lower(), "application/octet-stream")


def safe_image_path(image_dir, name: str) -> Path | None:
    """把「文件名」安全地解析成目录内的真实路径。

    三层防护（预览接口是能把任意路径读出来的口子，必须收紧）：
      1. 只接受纯文件名：name 必须等于 Path(name).name，且不含 / \\ 和上跳
      2. 扩展名必须在白名单里
      3. resolve 之后必须真的落在 image_dir 内（防符号链接/短名绕过）
    """
    if not image_dir or not name:
        return None
    name = str(name).strip()
    if not name or name != Path(name).name or "/" in name or "\\" in name:
        return None
    if name in (".", "..") or name.startswith("."):
        return None
    base = Path(image_dir)
    p = base / name
    if p.suffix.lower() not in _IMAGE_EXTS:
        return None
    try:
        rp = p.resolve()
        rb = base.resolve()
    except (OSError, ValueError):
        return None
    if rb != rp.parent:
        return None
    return rp if rp.is_file() else None


def load_image(image_dir, name: str, width: int = 0) -> tuple[bytes, str] | None:
    """取一张图。width>0 时用 Pillow 压成 JPEG 缩略图并缓存。

    出图动辄几百 KB～MB，面板一次要铺十几二十张；不缩略图会白烧带宽。
    """
    p = safe_image_path(image_dir, name)
    if p is None:
        return None

    if width <= 0:
        try:
            return p.read_bytes(), content_type_for(p.suffix)
        except OSError:
            return None

    width = max(32, min(int(width), 1200))
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None

    key = (str(p), width, mtime)
    hit = _THUMB_CACHE.get(key)
    if hit is not None:
        return hit

    try:
        from PIL import Image
        with Image.open(p) as im:
            im.thumbnail((width, max(1, int(im.height * width / max(1, im.width)))))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=82, optimize=True)
        out = (buf.getvalue(), "image/jpeg")
    except Exception as exc:
        logger.warning("缩略图生成失败（%s），回退原图：%s", name, exc)
        try:
            out = (p.read_bytes(), content_type_for(p.suffix))
        except OSError:
            return None

    if len(_THUMB_CACHE) >= _THUMB_CACHE_MAX:
        _THUMB_CACHE.clear()
    _THUMB_CACHE[key] = out
    return out


def resolve_log_source(sources: dict, name: str) -> tuple[Path | None, str]:
    """把日志源配置解析成一个具体文件。

    支持三种写法：
      "bot.log"                        -> 单个文件
      "D:/x/y.log"                     -> 单个文件
      {"dir": "...", "pattern": "*.log"} -> 目录里**按 mtime 取最新**那个
                                          （NapCat 每次启动生成一个新文件）
    返回 (路径, 备注)；路径为 None 表示取不到（备注里说明原因）。
    """
    spec = sources.get(name)
    if spec is None:
        return None, f"未配置日志源：{name}"
    if isinstance(spec, str):
        p = Path(spec)
        return (p, "") if p.exists() else (None, f"文件不存在：{p}")
    if isinstance(spec, dict):
        d = spec.get("dir")
        if d:
            dp = Path(str(d))
            if not dp.is_dir():
                return None, f"目录不存在：{dp}"
            pattern = str(spec.get("pattern") or "*.log")
            files = [p for p in dp.glob(pattern) if p.is_file()]
            if not files:
                return None, f"{dp} 下没有匹配 {pattern} 的文件"
            newest = max(files, key=lambda p: p.stat().st_mtime)
            return newest, f"目录内最新：{newest.name}"
        f = spec.get("file")
        if f:
            fp = Path(str(f))
            return (fp, "") if fp.exists() else (None, f"文件不存在：{fp}")
    return None, f"日志源配置无法识别：{name}"


# ──────────────────────────────────────────────
# qzone-bridge 代理
# ──────────────────────────────────────────────

_QZONE_CLIENT: httpx.Client | None = None
_QZONE_CLIENT_LOCK = threading.Lock()


def _qzone_http() -> httpx.Client:
    global _QZONE_CLIENT
    if _QZONE_CLIENT is None:
        with _QZONE_CLIENT_LOCK:
            if _QZONE_CLIENT is None:
                _QZONE_CLIENT = httpx.Client(
                    timeout=httpx.Timeout(5.0, connect=3.0), trust_env=False
                )
    return _QZONE_CLIENT


def read_qzone_console_token(cfg: dict) -> str:
    """qzone-bridge 的控制台令牌：配置优先，否则读它的 console-token 文件。"""
    direct = str(cfg.get("qzone_console_token") or "").strip()
    if direct:
        return direct
    path = str(cfg.get("qzone_console_token_file") or "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def proxy_qzone(base_url: str, token: str, endpoint: str, params: dict) -> dict:
    """把 /api/qzone/* 转发到 qzone-bridge 的 /console/api/*（服务端代理）。

    这样浏览器既不需要跨域，也拿不到 5700 的令牌。桥接没起时返回离线标记，
    面板显示离线卡片而不是整页报错。
    """
    url = base_url.rstrip("/") + "/console/api/" + endpoint.lstrip("/")
    headers = {"x-console-token": token} if token else {}
    try:
        r = _qzone_http().get(url, params=params, headers=headers)
    except Exception as exc:  # 连接失败 / 超时 / DNS —— 一律当作离线
        return {"ok": False, "offline": True, "error": f"{type(exc).__name__}: {exc}"}
    if r.status_code == 401:
        return {
            "ok": False,
            "offline": False,
            "auth_error": True,
            "error": "qzone-bridge 控制台令牌不匹配：请检查 qzone_console_token / qzone_console_token_file",
        }
    try:
        body = r.json()
    except ValueError:
        return {"ok": False, "offline": False, "error": f"上游返回非 JSON（HTTP {r.status_code}）"}
    if isinstance(body, dict):
        # 桥接进程还跑着旧版本时会走到这里：/console/api/* 被 OneBot action
        # 分发接管，返回 {status, retcode, data} 而不是我们的 {ok, ...}。
        # 明确区分出来，免得面板只显示一片空白让人以为是没数据。
        if body.get("ok") is None and body.get("retcode") is not None:
            return {
                "ok": False,
                "stale": True,
                "error": "qzone-bridge 正在运行旧版本（改动后未重启），"
                         "/console/api/* 被当成 OneBot action 处理了。请重启 qzone-bridge。",
            }
        return body
    return {"ok": True, "data": body}


# ──────────────────────────────────────────────
# HTTP 服务
# ──────────────────────────────────────────────

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
    ),
}

# 控制台允许的写操作白名单。不在表里的一律拒绝 ——
# 这样即便前端被改坏、或有人直接打接口，也无法借控制台碰到密钥类字段。
WRITE_OPS = frozenset({
    "restrict_set",        # 受限功能开关（admin.restrict.*）
    "access_set",          # 管理员名单 / 禁言豁免名单
    "persona_set",         # 人设五个字段之一
    "session_clear",       # 清掉某个会话的上下文
    "memory_approve",      # 批准记住某人
    "memory_decline",      # 拒绝记住某人
    "memory_forget",       # 忘掉某人
    "memory_item_delete",  # 删掉某人的一条特点 / 待办
    "sticker_add",         # 手动添加表情包
    "sticker_delete",      # 删除表情包
    "sticker_toggle",      # 表情包配置开关
    "config_set",          # 面板进度条调整数值配置（白名单）
    "group_note_delete",   # 删掉某条群聊记忆
    "group_forget",        # 整个群的记忆忘掉
    "groups_refresh",      # 重新拉一次群名册（跨群指令靠它认群）
    "bili_login",          # 查一次 B 站真登录态；是游客就开扫码并通知管理员
    "switch_set",          # 切一个功能开关（CONSOLE_SWITCHES 里的布尔项）
    "list_set",            # 写一个话术 / 列表（CONSOLE_LISTS 里的一项，按上限校验）
    "vision_set",          # 切换识图后端（auto / cloud / local / off）
    "provider_reload",     # 重载模型 / 生图端点（重读磁盘 + 重建端点，不改磁盘）
})

# /api/bot/sticker 上允许的"前端操作名" -> 实际交给 bot 执行的 op
STICKER_CLIENT_OPS = {
    "add_url": "sticker_add",      # 贴 QQ 表情资源地址
    "add_upload": "sticker_add",   # 上传本地图片
    "delete": "sticker_delete",
    "toggle": "sticker_toggle",
}

# QQ 商城表情的资源地址形如：
#   https://gxh.vip.qq.com/club/item/parcel/item/<emoji_package_id>/<emoji_id>/raw300.gif
# 从地址里就能反解出原生身份，用户不用手抄两个 ID。
MFACE_URL_RE = re.compile(r"gxh\.vip\.qq\.com/club/item/parcel/item/(\d+)/([^/?#\s]+)")

# 单次请求体上限，防止误传大文件把内存打满
# （话术池是多行长文本，persona_set 单个字段本来就允许 20000 字，64KB 已贴边）
_MAX_BODY = 256 * 1024
# 表情包上传是二进制图片，单独放宽（base64 后约 4/3 倍，6MB 够放 4MB 的图）
_MAX_UPLOAD = 6 * 1024 * 1024


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    # 刻意**不**开 SO_REUSEADDR：Windows 上开了它，第二个进程绑同一端口会
    # 「成功」而不报错，于是两个控制台并存、请求被谁接走不确定（实测踩过）。
    # 关掉后重复绑定会直接抛 OSError，start_server 的调用方会明确记一条错误日志。
    allow_reuse_address = False
    request_queue_size = 32

    def __init__(self, addr, handler_cls, ctx: dict):
        super().__init__(addr, handler_cls)
        self.ctx = ctx


class Handler(BaseHTTPRequestHandler):
    server_version = "qqbot-console"
    sys_version = ""
    protocol_version = "HTTP/1.1"  # 必须配 Content-Length，否则浏览器会挂住

    # ---------- 基础设施 ----------

    def log_message(self, fmt, *args):  # 别往 stderr 裸打
        logger = self.server.ctx.get("logger")  # type: ignore[attr-defined]
        if logger is not None:
            logger.debug("[console] " + fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(code)
            for k, v in _SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器提前断开，正常现象

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_raw(self, limit: int = _MAX_BODY) -> tuple[bytes, str]:
        """把请求体字节**读干净**。

        ⚠️ 这是必须的：HTTP/1.1 keep-alive 下，如果错误路径没读完 body 就返回，
        残留字节会被当成下一个请求的开头，之后同一条连接上的请求全部错位
        （表现为返回非 JSON、解析失败）。所以 POST 一律先读净再校验。
        返回 (原始字节, 错误说明)；错误说明非空表示应当拒绝。
        """
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return b"", "Content-Length 非法"
        if length <= 0:
            return b"", ""
        if length > limit:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.close_connection = True
            return b"", f"请求体过大（{length} > {limit} 字节）"
        return self.rfile.read(length), ""

    def _apply_json(self, body: dict) -> None:
        """写操作总入口。白名单外的 op 一律拒绝。body 已解析为 dict。"""
        ctx = self.server.ctx  # type: ignore[attr-defined]
        op = str(body.get("op") or "").strip()
        if op not in WRITE_OPS:
            self._json(400, {"ok": False, "error": f"不支持的写操作：{op or '(空)'}",
                             "allowed": sorted(WRITE_OPS)})
            return
        payload = body.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "payload 必须是 JSON 对象"})
            return

        apply_fn = ctx.get("apply_fn")
        if apply_fn is None:
            self._json(503, {"ok": False, "error": "控制台未接上写通道（apply_fn 缺失）"})
            return
        try:
            result = run_in_loop(apply_fn, op, payload, timeout=float(ctx.get("timeout", 3.0)))
        except LoopUnavailable:
            self._json(503, {"ok": False, "loop_alive": False,
                             "error": "bot 事件循环不可用，写操作已拒绝"})
            return
        except LoopTimeout:
            self._json(503, {"ok": False, "loop_alive": True,
                             "error": "bot 事件循环繁忙，写操作未执行，请重试"})
            return
        except Exception as exc:
            # 参数类错误统一回 400，让前端能把原因显示出来
            self._json(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        if isinstance(result, dict):
            result.setdefault("ok", True)
            self._json(200, result)
        else:
            self._json(200, {"ok": True, "result": result})

    # ---------- 鉴权 / CSRF ----------

    def _sticker_json(self, body: dict) -> None:
        """表情包写操作（单独一个路由，不并进 /api/bot/apply）。

        两个原因：
          1. 要放开请求体上限（上传图片）
          2. add_url 得在**本线程**先把图抓下来 —— 抓取是阻塞 IO，
             绝不能带进 bot 的事件循环（进 loop 的必须是短同步函数）
        """
        ctx = self.server.ctx  # type: ignore[attr-defined]
        op = str(body.get("op") or "").strip()
        payload = body.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        payload = dict(payload)

        if op not in STICKER_CLIENT_OPS:
            self._json(400, {"ok": False,
                             "error": f"不支持的表情包操作：{op or '(空)'}",
                             "allowed": sorted(STICKER_CLIENT_OPS)})
            return

        if op == "add_url":
            url = str(payload.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            m = MFACE_URL_RE.search(url)
            if not m:
                self._json(400, {"ok": False, "error":
                                 "这不是 QQ 表情资源地址。正确形如：\n"
                                 "https://gxh.vip.qq.com/club/item/parcel/item/"
                                 "<表情包ID>/<表情ID>/raw300.gif"})
                return
            # 反解出原生身份 —— 有它才能按"表情"发，而不是当图片发
            payload["emoji_package_id"], payload["emoji_id"] = m.group(1), m.group(2)
            try:
                with httpx.Client(timeout=20.0, follow_redirects=True,
                                  trust_env=False) as cli:
                    r = cli.get(url)
                r.raise_for_status()
                data = r.content
            except Exception as exc:
                self._json(400, {"ok": False,
                                 "error": f"下载表情失败：{type(exc).__name__}: {exc}"})
                return
            payload["data_b64"] = base64.b64encode(data).decode("ascii")
            payload["filename"] = m.group(2)

        apply_fn = ctx.get("apply_fn")
        if apply_fn is None:
            self._json(503, {"ok": False, "error": "控制台未接上写通道（apply_fn 缺失）"})
            return
        try:
            result = run_in_loop(apply_fn, STICKER_CLIENT_OPS[op], payload,
                                 timeout=float(ctx.get("timeout", 3.0)))
        except LoopUnavailable:
            self._json(503, {"ok": False, "loop_alive": False, "error": "bot 事件循环不可用"})
            return
        except LoopTimeout:
            self._json(503, {"ok": False, "loop_alive": True,
                             "error": "bot 事件循环繁忙，写操作未执行，请重试"})
            return
        except Exception as exc:
            self._json(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if isinstance(result, dict):
            result.setdefault("ok", True)
            self._json(200, result)
        else:
            self._json(200, {"ok": True, "result": result})

    def _token_ok(self, params: dict) -> bool:
        token = self.server.ctx.get("token") or ""  # type: ignore[attr-defined]
        if not token:
            return True
        supplied = self.headers.get("x-console-token") or params.get("token")
        return supplied == token

    def _origin_ok(self) -> bool:
        origin = self.headers.get("origin")
        if not origin:
            return True  # 非浏览器（curl / 本地脚本）
        port = self.server.ctx.get("port")  # type: ignore[attr-defined]
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    # ---------- 路由 ----------

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        from urllib.parse import urlparse, parse_qs

        ctx = self.server.ctx  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        try:
            # 静态页（无需令牌，页面自己会提示输入）
            if path in ("/", "/index.html", "/console.html", "/console"):
                self._serve_index()
                return

            # 浏览器总会来要 favicon；我们不提供图标，直接 204 而不是 404，
            # 免得开发者控制台里一直挂着一条红色报错。
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return

            if not path.startswith("/api/"):
                self._json(404, {"ok": False, "error": f"未知路径：{path}"})
                return

            if not self._token_ok(params):
                self._json(401, {"ok": False, "error": "未授权：请提供控制台令牌（x-console-token 头或 ?token= 参数）"})
                return

            if method == "POST":
                is_sticker = path == "/api/bot/sticker"
                # 先把 body 读净再看别的 —— 顺序不能反（见 _read_raw 的说明）
                raw, raw_err = self._read_raw(_MAX_UPLOAD if is_sticker else _MAX_BODY)
                if raw_err:
                    self._json(413 if "过大" in raw_err else 400,
                               {"ok": False, "error": raw_err})
                    return
                if not self._origin_ok():
                    self._json(403, {"ok": False, "error": "跨站请求被拒绝（Origin 不在白名单）"})
                    return
                ctype = (self.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    self._json(415, {"ok": False, "error": "写接口只接受 application/json"})
                    return
                if path not in ("/api/bot/apply", "/api/bot/sticker"):
                    self._json(404, {"ok": False, "error": f"未知写接口：{path}"})
                    return
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError) as exc:
                    self._json(400, {"ok": False, "error": f"请求体不是合法 JSON：{exc}"})
                    return
                if not isinstance(body, dict):
                    self._json(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
                    return
                if is_sticker:
                    self._sticker_json(body)
                else:
                    self._apply_json(body)
                return

            if path == "/api/health":
                self._json(200, {"ok": True, "loop_alive": loop_alive()})
                return

            if path.startswith("/api/bot/"):
                self._route_bot(path[len("/api/bot/"):], params)
                return

            if path.startswith("/api/qzone/"):
                self._route_qzone(path[len("/api/qzone/"):], params)
                return

            self._json(404, {"ok": False, "error": f"未知接口：{path}"})
        except Exception as exc:  # 兜底，绝不让 handler 线程把异常吐到 stderr
            try:
                self._json(500, {"ok": False, "error": f"控制台内部错误：{type(exc).__name__}: {exc}"})
            except Exception:
                pass

    # ---------- 静态 ----------

    def _serve_index(self) -> None:
        ctx = self.server.ctx  # type: ignore[attr-defined]
        p = Path(ctx.get("static_path") or "")
        try:
            body = p.read_bytes()
        except OSError as exc:
            self._json(500, {"ok": False, "error": f"控制台页面缺失：{p}（{exc}）"})
            return
        self._send(200, body, "text/html; charset=utf-8")

    # ---------- /api/bot/* ----------

    _KINDS = {
        "status": ("status", None),
        "sessions": ("sessions", None),
        "life": ("life", None),
        "memory": ("memory", None),
        "mood": ("mood", None),
        "persona": ("persona", None),
        "sd": ("sd", None),
        "stickers": ("stickers", None),
        "meta": ("meta", None),
        "config": ("config", None),
        # 空间功能总览：三个动作的开关/额度/今日用量 + 最近做过什么 + 好友动态缓存
        "qzone-actions": ("qzone_actions", None),
        # 可拖动调整的数值配置（阈值/概率/衰减）
        "tunables": ("tunables", None),
        # 群聊记忆（跟群走不跟人走，独立于人物记忆）
        "group-memory": ("group_memory", None),
        # B 站登录态：真去问 b 站 isLogin，不看本地有没有 cookie
        "bili": ("bili", None),
        # 跨群：群名册 + 跨群禁言的认群认人依据
        "cross-group": ("cross_group", None),
        # 识图：当前后端 / 云端与本地可用性 / 本地探测 / 识图计数（只读）
        "vision": ("vision", None),
        # 模型与生图端点（只读）：两套 Router 的状态合并视图，不含明文密钥
        "providers": ("providers", None),
        # 功能开关清单：按分组列出的布尔项（配合 switch_set 写）
        "switches": ("switches", None),
        # 话术与列表：按分组列出的多行文本项（配合 list_set 写）
        "lists": ("lists", None),
    }

    def _route_bot(self, sub: str, params: dict) -> None:
        ctx = self.server.ctx  # type: ignore[attr-defined]

        if sub == "image":
            # 图片预览。注意 <img> 带不了自定义头，所以前端会用 ?token= 传令牌
            # （_token_ok 本来就支持 query 里的令牌）。
            name = (params.get("name") or "").strip()
            try:
                width = int(params.get("w") or "0")
            except (TypeError, ValueError):
                width = 0
            which = (params.get("dir") or "generated").strip()
            image_dir = ctx.get("image_dir") if which == "generated" else ctx.get("sticker_dir")
            got = load_image(image_dir, name, width)
            if got is None:
                self._json(404, {"ok": False, "error": f"取不到图片：{name!r}"})
                return
            data, ctype = got
            self._send(200, data, ctype)
            return

        if sub == "logs":
            source = (params.get("source") or "bot").strip()
            level = params.get("level", "")
            try:
                n = int(params.get("lines", "200"))
            except (TypeError, ValueError):
                n = 200
            sources = ctx.get("log_sources") or {}
            if source not in sources:
                self._json(400, {"ok": False, "error": f"未知日志源：{source}",
                                 "available": sorted(sources)})
                return
            path, note = resolve_log_source(sources, source)
            if path is None:
                self._json(200, {"ok": True, "source": source, "lines": [],
                                 "file": None, "note": note})
                return
            self._json(200, {"ok": True, "source": source, "file": str(path),
                             "note": note, "lines": read_log_tail(path, n, level)})
            return

        if sub == "activity":
            date = (params.get("date") or "").strip()
            if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                self._json(400, {"ok": False, "error": "date 需为 YYYY-MM-DD"})
                return
            self._snapshot_json("activity", date or None)
            return

        if sub in self._KINDS:
            kind, arg = self._KINDS[sub]
            # status 特殊：loop 挂掉时也要能回，面板才能显示「Bot 离线」而不是报错页
            if kind == "status":
                self._status_json()
                return
            self._snapshot_json(kind, arg)
            return

        self._json(404, {"ok": False, "error": f"未知接口：/api/bot/{sub}"})

    def _snapshot_json(self, kind: str, arg=None) -> None:
        ctx = self.server.ctx  # type: ignore[attr-defined]
        snapshot = ctx.get("snapshot")
        try:
            data = run_in_loop(snapshot, kind, arg, timeout=float(ctx.get("timeout", 3.0)))
        except LoopUnavailable:
            self._send(503, json.dumps({
                "ok": False, "loop_alive": False,
                "error": "bot 事件循环不可用（未启动、正在重启或已崩溃）",
            }, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        except LoopTimeout:
            self._json(503, {"ok": False, "loop_alive": True,
                             "error": "bot 事件循环繁忙，请稍后重试"})
            return
        self._json(200, data)

    def _status_json(self) -> None:
        ctx = self.server.ctx  # type: ignore[attr-defined]
        start_ts = float(ctx.get("start_ts") or 0.0)
        base = {"uptime": (time.time() - start_ts) if start_ts else 0.0}
        if not loop_alive():
            base.update({"ok": True, "loop_alive": False, "ws_connected": False,
                         "note": "bot 事件循环不可用，以下为控制台自身的存活信息"})
            self._json(200, base)
            return
        try:
            data = run_in_loop(ctx.get("snapshot"), "status", None,
                               timeout=float(ctx.get("timeout", 3.0)))
            if isinstance(data, dict):
                data.setdefault("ok", True)
                data["loop_alive"] = True
                data["uptime"] = base["uptime"]
            else:
                data = {"ok": True, "loop_alive": True, "uptime": base["uptime"]}
            self._json(200, data)
        except LoopTimeout:
            self._json(200, {**base, "ok": True, "loop_alive": True,
                             "ws_connected": None, "note": "事件循环繁忙，状态暂不可得"})
        except Exception as exc:
            self._json(200, {**base, "ok": True, "loop_alive": True, "ws_connected": None,
                             "note": f"状态快照失败：{type(exc).__name__}: {exc}"})

    # ---------- /api/qzone/* ----------

    def _route_qzone(self, sub: str, params: dict) -> None:
        ctx = self.server.ctx  # type: ignore[attr-defined]
        base_url = str(ctx.get("qzone_bridge_url") or "")
        token = read_qzone_console_token(ctx.get("qzone") or {})

        if sub == "health":
            try:
                r = _qzone_http().get(base_url.rstrip("/") + "/status",
                                      headers=({"x-console-token": token} if token else {}))
                self._json(200, {"ok": r.status_code < 500, "http": r.status_code,
                                 "online": True})
            except Exception as exc:
                self._json(200, {"ok": False, "online": False,
                                 "error": f"{type(exc).__name__}: {exc}"})
            return

        if not base_url:
            self._json(200, {"ok": False, "offline": True, "error": "未配置 console.qzone_bridge_url"})
            return

        upstream = {"status": "status", "events": "events", "posts": "posts",
                    "pollers": "pollers"}.get(sub)
        if upstream is None:
            self._json(404, {"ok": False, "error": f"未知接口：/api/qzone/{sub}"})
            return
        self._json(200, proxy_qzone(base_url, token, upstream, params))


# ──────────────────────────────────────────────
# 启动
# ──────────────────────────────────────────────


def start_server(
    *,
    host: str,
    port: int,
    base_dir: Path,
    configured_token: str,
    static_path: Path,
    log_path: Path,
    log_sources: dict | None = None,
    image_dir: str = "",
    sticker_dir: str = "",
    snapshot,
    apply_fn=None,
    start_ts: float,
    qzone_bridge_url: str = "",
    qzone: dict | None = None,
    timeout: float = 3.0,
    logger=None,
) -> ConsoleServer:
    """启动控制台 HTTP 服务（daemon 线程），返回 server 实例。

    snapshot 由 bot 提供，签名 `snapshot(kind: str, arg: str | None) -> dict`，
    会在 bot 的事件循环内被调用。
    """
    token = load_or_create_token(base_dir, configured_token)
    ctx = {
        "token": token,
        "port": port,
        "snapshot": snapshot,
        "apply_fn": apply_fn,
        "static_path": str(static_path),
        "log_path": str(log_path),
        # 多日志源：{"bot": "bot.log", "qzone": "...", "napcat": {"dir": "...", "pattern": "*.log"}}
        "log_sources": (log_sources if log_sources else {"bot": str(log_path)}),
        # 面板预览用：出图目录 / 表情包目录
        "image_dir": str(image_dir or ""),
        "sticker_dir": str(sticker_dir or ""),
        "start_ts": start_ts,
        "timeout": timeout,
        "qzone_bridge_url": qzone_bridge_url,
        "qzone": qzone or {},
        "logger": logger,
    }
    srv = ConsoleServer((host, port), Handler, ctx)
    threading.Thread(target=srv.serve_forever, name="qqbot-console", daemon=True).start()
    return srv
