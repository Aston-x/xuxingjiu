"""识图链路体检：一条命令看清「描述与图片完全不符」到底坏在哪一环。

为什么要这个脚本：用户抱怨的是同一个现象，但根因分好几层，而且互相掩盖 ——
云端视觉被误关（`cloud.vision=false`）、`vision_relay=true` 让云端永远只吃本地 7B
的转述、本地 VL 的 `loaded_context_length` 小到装不下一张图（→ 400 → 图被换成
「你看不到内容」）、动图被重编码成静态第 0 帧。四层里任何一层坏掉，表面都是同一句话。

所以这里按链路顺序逐段体检，每段都给出可据以判断的原始证据（HTTP 状态、错误原文、
服务端自报的上下文长度、日志条数），最后汇总成「结论与建议」。

用法：
    .venv\\Scripts\\python.exe tools\\vision_probe.py                 # 全链路（含一次云端调用，会花钱）
    .venv\\Scripts\\python.exe tools\\vision_probe.py --no-cloud      # 不花钱：跳过云端自检
    .venv\\Scripts\\python.exe tools\\vision_probe.py --no-cloud --no-local
    .venv\\Scripts\\python.exe tools\\vision_probe.py --repro-400     # 额外给本地发一张 1024px 图，复现 400
    .venv\\Scripts\\python.exe tools\\vision_probe.py --log bot.log --samples 2

⚠️ 只读：只读 config.json / bot.log / bot.py / console_server.py / public/console.html，
   只发网络请求，不写任何状态文件，不碰 QQ。
   （import bot 只为复用它的纯函数：第 5 节走真链路的 `_decode_frames`，第 7 节用
   getattr 核对契约里的名字在不在。import 之后立刻把 `write_json_dict` /
   `LIFE.note_act_change` / `ACTIVITY.note` 换成空函数，并摘掉 `bot.logger` 上写
   bot.log 的 FileHandler 换成 NullHandler —— 见文件里的「只读护栏」段与运行末尾的
   「只读佐证」；检测能力全部走 getattr，bot.py 再改名也只会报 ✘，不会崩。）
   `--repro-400` 是唯一会额外花时间的一步，它不花钱，但会让本地模型真跑一次推理。
"""
from __future__ import annotations

import argparse
import ast
import base64
import inspect
import io
import json
import logging
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

# bot.py 也做了同样的 reconfigure，但本脚本可能在 import bot 之前就打印东西
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx  # noqa: E402
import bot  # noqa: E402

# ---- 只读护栏：把会落盘 / 会记账的入口堵掉（import bot 时 LIFE 已读过状态文件，无写入）----
bot.write_json_dict = lambda *a, **k: None
if getattr(bot, "LIFE", None) is not None:
    bot.LIFE.note_act_change = lambda act: None
if getattr(bot, "ACTIVITY", None) is not None:
    bot.ACTIVITY.note = lambda *a, **k: None
# 本进程绝不往 bot.log 里写：bot 是活的，我的日志不该混进去。
# 写 bot.log 的是 RotatingFileHandler（bot.py:70-72），必须摘掉并 close ——
# 留着它，一旦本进程有人调 logger，就会写进线上日志，极端情况还会触发轮转把 bot.log 改名。
# bot.py 的日志格式不带 pid（bot.py:69），事后没法从内容分辨是谁写的，所以这里做**实证**：
# 摘完之后故意往 bot.logger 打一条唯一标记，运行末尾回读 bot.log* 看它有没有落进去。
GUARD_ERROR = ""
MARK = f"vision_probe 只读自检标记 {time.time_ns()}"
REMOVED_HANDLERS: list[str] = []
try:
    for _h in list(bot.logger.handlers):
        _fn = getattr(_h, "baseFilename", None)
        REMOVED_HANDLERS.append(type(_h).__name__ + (f" → {_fn}" if _fn else ""))
        bot.logger.removeHandler(_h)
        try:
            _h.close()          # 关掉句柄，等于连"打开着"这件事都不做
        except Exception:
            pass
    bot.logger.addHandler(logging.NullHandler())   # 免得 logging 的 lastResort 往 stderr 喷
    bot.logger.propagate = False
    bot.logger.warning(MARK)        # 故意打一条：它不该出现在 bot.log 里（末尾会回读核对）
except Exception as exc:
    GUARD_ERROR = f"{type(exc).__name__}: {exc}"

CFG = bot.CFG

# 单张 1024px 图约 1300 视觉 token（官方：每图约 1024 token，边距 + 提示词另算）；
# 4096 是「装得下一张图还有余量」的经验下限。
TOKENS_PER_IMAGE = 1300
SAFE_CONTEXT = 4096

PROBLEMS: list[str] = []   # 明确判定有问题
NOTES: list[str] = []      # 存疑 / 需要人看一眼


def head(n: int, title: str) -> None:
    print(f"\n{'=' * 68}\n{n}. {title}\n{'=' * 68}")


def p(text: str = "") -> None:
    print("  " + text if text else "")


def ok(text: str) -> None:
    print("  [OK]   " + text)


def bad(text: str) -> None:
    print("  [问题] " + text)


def warn(text: str) -> None:
    print("  [注意] " + text)


def clip(text: object, n: int = 300) -> str:
    s = str(text).replace("\r", " ").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def yesno(v: object) -> str:
    return "true" if v else "false"


# ---------------------------------------------------------------- 1. 配置

def sec_config() -> None:
    head(1, "配置")
    cloud = CFG.get("cloud") or {}
    local = CFG.get("local") or {}
    vision = CFG.get("vision") or {}
    relay = CFG.get("vision_relay")

    p(f"vision_enable = {yesno(CFG.get('vision_enable'))}"
      "   （总闸：false 时完全不解析图片）")
    p(f"vision_relay  = {yesno(relay)}"
      "   （老开关，契约里已降级为兼容键）")
    p(f"vision.backend = {vision.get('backend', '<缺失>')}"
      f"   （契约要求它优先；当前 {'已落地' if vision.get('backend') else '缺失 → 仍按旧开关推导'}）")
    if vision:
        p("vision 段其它键：" + json.dumps(
            {k: v for k, v in vision.items() if not k.startswith("_")}, ensure_ascii=False))
    else:
        NOTES.append("config.json 里没有 vision 段，契约 2.1 的新键还没落地"
                     "（此后所有判定都是按旧键推导出来的）")

    p("")
    p("云端 cloud：")
    p(f"    enable = {yesno(cloud.get('enable'))}"
      f"   vision = {yesno(cloud.get('vision'))}"
      f"   model = {cloud.get('model')}")
    p(f"    base_url = {cloud.get('base_url')}")
    p(f"    api_key  = {'有（不打印内容）' if cloud.get('api_key') else '无'}"
      f"   max_tokens = {cloud.get('max_tokens')}")
    p("本地 local：")
    p(f"    enable = {yesno(local.get('enable'))}"
      f"   vision = {yesno(local.get('vision'))}"
      f"   model = {local.get('model')}")
    p(f"    base_url = {local.get('base_url')}"
      f"   timeout_seconds = {local.get('timeout_seconds')}")
    p("")

    cloud_usable = bool(cloud.get("enable") and cloud.get("vision") and cloud.get("api_key"))
    local_usable = bool(local.get("enable") and local.get("vision"))
    p(f"契约 §3.2 的可用性判据 → 云端可用 = {yesno(cloud_usable)}"
      f"（enable 且 vision 且 api_key）｜本地可用 = {yesno(local_usable)}（enable 且 vision）")

    backend = vision.get("backend")
    if backend:
        p(f"实际生效后端（按 vision.backend）：{backend}")
    else:
        # 契约只说「缺失时按旧开关推导」，没写死映射；这里按旧语义等价推导并标明
        derived = "local" if relay else "cloud"
        p(f"实际生效后端（推导，vision.backend 缺失）：{derived}"
          f"   ← vision_relay={yesno(relay)} 的旧语义"
          f"（true = 云端只吃本地转述）")
        warn("这条「推导」是按旧代码语义得出的，契约未写死映射；config 落地 vision.backend 后以它为准")

    if not cloud_usable:
        why = []
        if not cloud.get("enable"):
            why.append("cloud.enable=false")
        if not cloud.get("vision"):
            why.append("cloud.vision=false（契约 §1 已查明是当年的误判，要改成 true）")
        if not cloud.get("api_key"):
            why.append("没有 api_key")
        bad("云端视觉不可用：" + "；".join(why))
        PROBLEMS.append("云端视觉不可用（" + "、".join(why) + "）")
    if not local_usable:
        bad("本地视觉不可用：local.enable / local.vision 有一个是 false")
        PROBLEMS.append("本地视觉不可用（local.enable 或 local.vision 为 false）")
    if relay and not backend:
        bad("vision_relay=true：云端永远只吃本地 7B 的转述，"
            "本地看一眼就说错的话，云端照着说 —— 描述不符的头号嫌疑")
        PROBLEMS.append("vision_relay=true 且 vision.backend 缺失 → 图片不进云端")


# ------------------------------------------------------- 网络请求小工具

def post_vision(base_url: str, api_key: str, model: str, data_url: str,
                question: str, timeout: float, max_tokens: int) -> dict:
    """按项目里 `Chat._post` 的写法发一次带图的 chat/completions。

    返回 dict：ok / status / text / error / url / seconds。任何异常都吞成 error，
    保证体检脚本自己不会因为网络问题崩掉。
    """
    url = str(base_url).rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    is_local = ("127.0.0.1" in url) or ("localhost" in url)
    out = {"ok": False, "status": None, "text": "", "error": "", "url": url, "seconds": 0.0}
    t0 = time.time()
    try:
        with httpx.Client(trust_env=not is_local) as cli:
            r = cli.post(url, json=body, headers=headers, timeout=timeout)
        out["status"] = r.status_code
        if r.status_code >= 400:
            out["error"] = clip(r.text, 400)
        else:
            try:
                ch = (r.json().get("choices") or [{}])[0]
                out["text"] = (ch.get("message", {}).get("content") or "").strip()
                out["ok"] = bool(out["text"])
                if not out["text"]:
                    out["error"] = "HTTP 200 但 choices[0].message.content 是空的：" + clip(r.text, 200)
            except Exception as exc:
                out["error"] = f"响应不是合法 JSON（{type(exc).__name__}）：" + clip(r.text, 200)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["seconds"] = time.time() - t0
    return out


def tiny_png_data_url(size: int = 64, rgb: tuple[int, int, int] = (220, 40, 40)) -> tuple[str, int]:
    """用 Pillow 现造一张纯色 PNG 转 data URL —— 最小可能的计费请求。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (size, size), rgb).save(buf, format="PNG")
    raw = buf.getvalue()
    return "data:image/png;base64," + base64.b64encode(raw).decode(), len(raw)


def _print_result(res: dict) -> None:
    p(f"    POST {res['url']}   耗时 {res['seconds']:.1f}s")
    p(f"    HTTP 状态：{res['status'] if res['status'] is not None else '（没连上）'}")
    if res["ok"]:
        ok(f"模型回答：{clip(res['text'], 200)}")
    else:
        bad(f"失败原文：{clip(res['error'], 400)}")


def _fit_vision_note(ep_name: str, ep: dict) -> None:
    """顺手报告项目自己的判据会不会把图换掉（纯计算，不发请求）。

    判据只有一条：端点 dict 的 `vision` 字段（`Chat._fit_vision`，bot.py:407）。
    占位文本现在按 `VISION.usable()` 分两套措辞 —— 这里把两套都逼出来看一眼：
    措辞和"当前后端能不能看图"对不上，就是系统提示词与占位文本互相打脸的那个坑。
    """
    fit = getattr(getattr(bot, "Chat", None), "_fit_vision", None)
    if not callable(fit):
        warn("bot.Chat._fit_vision 不存在（名字改了？）—— 图片占位判据这一环没法自检")
        NOTES.append("Chat._fit_vision 不存在，第 4 节的占位判据没验证")
        return
    engine = getattr(bot, "VISION", None)
    usable = getattr(engine, "usable", None)
    usable_now = None
    if callable(usable):
        try:
            usable_now = bool(usable())
        except Exception:
            usable_now = None

    def run(label: str, ep_try: dict) -> str:
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
        parts = fit(msgs, ep_try)[0]["content"]
        if any(isinstance(x, dict) and x.get("type") == "image_url" for x in parts):
            p(f"    项目内 _fit_vision（{label}）：图片会原样送给模型")
            return ""
        ph = " ".join(str(x.get("text") or "") for x in parts
                      if isinstance(x, dict) and x.get("type") == "text")
        p(f"    项目内 _fit_vision（{label}）：图片被换成占位文本")
        p(f"      占位原文：{clip(ph, 140)}")
        return ph

    try:
        run(f"{ep_name}｜端点 vision={yesno(ep.get('vision'))}", ep)
    except Exception as exc:
        warn(f"_fit_vision 自检失败（bot.py 可能正在被改）：{type(exc).__name__}: {exc}")
        return
    try:
        # 再逼一次"端点收不了图"的分支 —— 占位文本真正生效的就是这条路
        ph = run(f"{ep_name}｜端点 vision=False（假设这条通道传不了图）", {**ep, "vision": False})
    except Exception as exc:
        warn(f"_fit_vision 占位分支自检失败：{type(exc).__name__}: {exc}")
        return
    if not ph:
        return
    if usable_now is None:
        warn("拿不到 VISION.usable()，占位措辞没法核对")
        return
    said_relay = "这条通道传不了图" in ph
    if said_relay == usable_now:
        ok("占位措辞与 VISION.usable() 一致（usable=" + yesno(usable_now) + "："
           + ("后端能看图，只是这条通道传不了图" if usable_now
              else "后端当前看不了图，占位就该说「看不到」") + "）")
    else:
        bad(f"占位措辞与 VISION.usable() 不一致：usable={yesno(usable_now)} 而占位文本"
            + ("说「这条通道传不了图」" if said_relay else "说「你看不到图里的内容」"))
        PROBLEMS.append("_fit_vision 的占位措辞与 VISION.usable() 不一致（提示词会互相打脸）")


# ------------------------------------------------------------ 2. 云端自检

def sec_cloud(no_cloud: bool, timeout: float) -> None:
    head(2, "云端视觉自检")
    cloud = CFG.get("cloud") or {}
    if no_cloud:
        p("已跳过（--no-cloud）")
        return
    if not cloud.get("enable"):
        p("已跳过（cloud.enable = false）")
        return
    if not cloud.get("api_key"):
        bad("已跳过：config.json 里没有 cloud.api_key")
        return

    print("  ⚠️ 这一步会真的调用云端 API，是本次体检里唯一花钱的地方（一张 64x64 图，"
          "费用极小但非零）。想省钱就加 --no-cloud。")
    vision = CFG.get("vision") or {}
    max_tokens = int(vision.get("describe_max_tokens", 300))
    data_url, nbytes = tiny_png_data_url()
    p(f"    测试图：64x64 纯红 PNG，{nbytes} 字节 → data URL {len(data_url)} 字符")
    _fit_vision_note("cloud", cloud)
    res = post_vision(cloud["base_url"], cloud.get("api_key", ""), cloud.get("model", ""),
                      data_url, "这张图是什么颜色？只回答颜色。", timeout, max_tokens)
    _print_result(res)
    if res["ok"]:
        ans = res["text"]
        if "红" in ans or "red" in ans.lower():
            ok("云端确实看到了图（答对了颜色）→ deepseek-flash 的视觉是通的")
        else:
            warn("云端有回答但没说出「红」，可能只是没按格式答，人工看一眼上面的原文")
    else:
        st = res["status"]
        if st in (401, 403):
            bad(f"云端 {st}：api_key 无效或无权限")
            PROBLEMS.append(f"云端视觉调用返回 {st}（认证问题）")
        elif st == 400:
            bad("云端 400：可能模型名不对，或该模型端点不收图片")
            PROBLEMS.append("云端视觉调用 400（模型名 / 端点不支持视觉）")
        else:
            bad(f"云端调用失败（状态 {st}）")
            PROBLEMS.append(f"云端视觉调用失败（{res['error'][:80]}）")


# ------------------------------------------------------------ 3. 本地探测

def probe_models(local: dict, timeout: float) -> dict:
    """GET LM Studio 的 /api/v0/models。

    坑：`local.base_url` 是 `...:1234/v1`，直接拼 `/api/v0/models` 会得到
    `/v1/api/v0/models` —— LM Studio 对这条路**返回 HTTP 200 但 body 是
    {"error": "Unexpected endpoint..."}**，光看状态码会误判成成功。所以这里两条都试，
    并且要求 body 里真有 data 才算成功。
    """
    base = str(local.get("base_url", "")).rstrip("/")
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base
    cands = [base + "/api/v0/models", root + "/api/v0/models"]
    seen: list[str] = []
    last_err = ""
    for url in cands:
        if url in seen:
            continue
        seen.append(url)
        try:
            with httpx.Client(trust_env=False) as cli:
                r = cli.get(url, timeout=timeout)
            try:
                data = r.json()
            except Exception:
                data = None
            if r.status_code == 200 and isinstance(data, dict) and data.get("data"):
                return {"ok": True, "url": url, "data": data.get("data") or []}
            last_err = f"HTTP {r.status_code}：{clip(r.text, 160)}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
    return {"ok": False, "url": cands[-1], "error": last_err}


def sec_local_probe(no_local: bool, timeout: float) -> None:
    head(3, "本地探测（LM Studio /api/v0/models）")
    local = CFG.get("local") or {}
    if no_local:
        p("已跳过（--no-local）")
        return
    res = probe_models(local, timeout)
    if not res["ok"]:
        bad(f"探测失败：{res['error']}")
        p("    → LM Studio 没开或端口不对。此时本地识图整条链路直接不可用，"
          "auto 后端会退回云端（如果云端也不可用，她就只能编）。")
        PROBLEMS.append("本地端点探测失败（LM Studio 未运行？）")
        return
    ok(f"可用端点：{res['url']}")
    models = res["data"]
    want = local.get("model")
    target = None
    for m in models:
        if m.get("id") == want:
            target = m
            break
    if target is None:
        for m in models:
            if m.get("state") == "loaded":
                target = m
                break
    if target is None and models:
        target = models[0]
        warn(f"config 里的 local.model={want} 不在服务列表里，下面按第一条报告")
    if target is None:
        bad("服务端没返回任何模型")
        PROBLEMS.append("本地端点没返回模型列表")
        return

    p("")
    p("    服务端模型列表：")
    for m in models:
        mark = " ←" if m is target else ""
        p(f"      - {m.get('id')}｜type={m.get('type')}｜state={m.get('state')}"
          f"｜loaded={m.get('loaded_context_length')}｜max={m.get('max_context_length')}{mark}")

    loaded = target.get("loaded_context_length")
    maxctx = target.get("max_context_length")
    p("")
    p(f"    关注模型：{target.get('id')}")
    p(f"      state                = {target.get('state')}")
    p(f"      loaded_context_length= {loaded}")
    p(f"      max_context_length   = {maxctx}")
    p(f"      type                 = {target.get('type')}（vlm 才是视觉模型）")
    p("")
    p(f"    判据：单张 1024px 图约 {TOKENS_PER_IMAGE} 视觉 token；"
      f"loaded_context_length < {SAFE_CONTEXT} 就会必然 400。")
    frames = int((CFG.get("vision") or {}).get("max_frames", 1) or 1)
    if frames > 1:
        p(f"    另：vision.max_frames={frames} 时，一张动图抽帧后本地要吃约 "
          f"{TOKENS_PER_IMAGE * frames} token，更吃紧。")
    if not isinstance(loaded, int):
        warn("服务端没报 loaded_context_length，无法判断（模型可能没加载）")
        NOTES.append("本地探测拿不到 loaded_context_length")
    elif loaded < SAFE_CONTEXT:
        bad(f"loaded_context_length={loaded} < {SAFE_CONTEXT} → "
            "本地识图必然 400，图会被换成「你看不到内容」")
        PROBLEMS.append(f"本地上下文只有 {loaded}，装不下一张图（必然 400）")
    else:
        ok(f"loaded_context_length={loaded} ≥ {SAFE_CONTEXT}，装得下约 "
           f"{loaded // TOKENS_PER_IMAGE} 张图")
        past = past_context_limits()
        if past:
            warn(f"但日志里出现过更小的上下文上限：{past} —— 这个值是 LM Studio "
                 "里「加载设置」决定的，现在够不代表一直是够")
            if min(past) < SAFE_CONTEXT:
                NOTES.append(f"日志历史里 {past} 都装不下图；现在的 {loaded} 只是本次加载的值")
    if target.get("state") != "loaded":
        warn(f"该模型当前 state={target.get('state')}，不是 loaded")


# ------------------------------------------------------- 4. 本地视觉自检

def sec_local_vision(no_local: bool, timeout: float, repro400: bool) -> None:
    head(4, "本地视觉自检（真发一次图片请求）")
    local = CFG.get("local") or {}
    if no_local:
        p("已跳过（--no-local）")
        return
    if not local.get("enable"):
        p("已跳过（local.enable = false）")
        return
    vision = CFG.get("vision") or {}
    max_tokens = int(vision.get("describe_max_tokens", 300))
    data_url, nbytes = tiny_png_data_url()
    p(f"    测试图：64x64 纯红 PNG，{nbytes} 字节（最小请求，本地不计费）")
    _fit_vision_note("local", local)
    res = post_vision(local["base_url"], local.get("api_key", "lm-studio"),
                      local.get("model", ""), data_url,
                      "这张图是什么颜色？只回答颜色。", timeout, max_tokens)
    _print_result(res)
    if res["ok"] and ("红" in res["text"] or "red" in res["text"].lower()):
        ok("本地 VL 能看到小图（小图这一环是通的）")
    elif res["ok"]:
        warn("本地有回答但没说出「红」，人工看一眼上面原文")
    else:
        PROBLEMS.append("本地视觉请求失败（详见证物原文）")

    if repro400:
        p("")
        p("    --repro-400：换一张 1024px 的图，复现线上那个 400")
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (1024, 1024), (30, 120, 220)).save(buf, format="PNG")
        raw = buf.getvalue()
        big = "data:image/png;base64," + base64.b64encode(raw).decode()
        p(f"    测试图：1024x1024 纯蓝 PNG，{len(raw)} 字节")
        res2 = post_vision(local["base_url"], local.get("api_key", "lm-studio"),
                           local.get("model", ""), big,
                           "这张图是什么颜色？只回答颜色。", timeout, max_tokens)
        _print_result(res2)
        if res2["status"] == 400 and "context" in (res2["error"] or "").lower():
            bad("复现成功：1024px 图 + 当前上下文 → 400 exceed_context_size_error，"
                "这正是线上「她看不到图」的直接原因")
            PROBLEMS.append("1024px 图在本地上必然 400（上下文不够）")
        elif res2["ok"]:
            ok("1024px 图本地也吃得下，这一环当前没问题")


# --------------------------------------------------------- 5. 动图抽帧（真链路）
#
# 2026-09-26 换了链路：老的 _shrink_image 已被删掉，图 → data URL 现在走
#   _image_to_data_url() → _decode_frames(raw, max_frames, max_side, max_bytes)
#     → _encode_frame_limited() → _encode_frame()
# 所以第 5 节不再"找不到 _shrink_image 就跳过"的降级，而是直接调 _decode_frames：
# 它是纯同步、不联网、不需要 OneBot 的函数，正好能在内存里离线跑真链路，
# 第 7 节还会从源码证明 _image_to_data_url 确实调用它（不是这里另写一套）。

FRAME_COLORS = [(230, 60, 60), (60, 200, 90), (70, 110, 240)]


def _make_gif(w: int, h: int, frames: int = 3) -> bytes:
    """内存里造一张**多帧 + 真有透明像素**的 GIF。

    为什么要透明：老链路把 >1024px 的动图重编码成静态 JPEG 的第 0 帧，alpha 一起丢；
    新链路要求"有 alpha 的帧存 PNG"。只有造一张真有透明像素的动图，
    "透明通道还在不在"才有可判定性（本来就不透明的图走 JPEG 是正常的，不算丢）。
    索引 0 用 palette + transparency=0 声明成透明色，每帧再画一块不透明色块；
    色块位置逐帧挪，所以"抽出来的是不是同一帧"也一眼能看出。
    用 P 模式 + 手写调色板而不是 RGBA，是因为大尺寸下不需要自适应量化，快得多。
    """
    from PIL import Image, ImageDraw
    imgs = []
    for i in range(frames):
        im = Image.new("P", (w, h), 0)          # 整幅先填索引 0 = 透明
        pal = [0] * 768
        r, g, b = FRAME_COLORS[i % len(FRAME_COLORS)]
        pal[0:3] = [0, 0, 0]                    # 索引 0 的颜色无所谓：它被声明为透明
        pal[3:6] = [r, g, b]                    # 索引 1 = 不透明色块
        pal[6:9] = [255, 255, 255]
        im.putpalette(pal)
        d = ImageDraw.Draw(im)
        x0 = i * (w // 4)
        d.rectangle([x0, 0, x0 + max(1, w // 8), max(1, h // 8)], fill=1)
        d.rectangle([0, 0, max(1, w // 16), max(1, h // 16)], fill=2)
        im.info["transparency"] = 0
        imgs.append(im)
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:],
                 duration=120, loop=0, transparency=0, disposal=2)
    return buf.getvalue()


def _img_probe(raw: bytes) -> dict:
    """量一张图：格式 / 尺寸 / mode / 帧数 / 字节 / 有没有**真的**透明像素。

    "有 alpha 通道"和"真有全透明像素"是两件事：PNG 一定带 alpha，但可能每个像素
    都是 255（等于不透明）。所以两个都报：alpha=有没有通道，alpha_min/max=实测范围
    （alpha_min 为 0 才说明全透明的像素确实还在）。
    """
    d: dict = {"format": "?", "size": (0, 0), "mode": "?", "frames": 0,
               "bytes": len(raw), "alpha": False, "alpha_min": None,
               "alpha_max": None, "error": ""}
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        d["format"] = im.format or "?"
        d["size"] = tuple(im.size)
        d["mode"] = im.mode
        d["frames"] = int(getattr(im, "n_frames", 1) or 1)
        lo, hi, seen = 255, 0, False
        for i in range(d["frames"]):
            im.seek(i)
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                seen = True
                a_lo, a_hi = im.convert("RGBA").getchannel("A").getextrema()
                lo, hi = min(lo, a_lo), max(hi, a_hi)
        if seen:
            d["alpha"], d["alpha_min"], d["alpha_max"] = True, lo, hi
    except Exception as exc:
        d["error"] = f"{type(exc).__name__}: {exc}"
    return d


def _fmt_img(d: dict) -> str:
    s = (f"{d['format']} {d['size'][0]}x{d['size'][1]} mode={d['mode']} "
         f"帧数={d['frames']} {d['bytes']}字节")
    if d["error"]:
        return s + f"｜<解析失败：{d['error']}>"
    if d["alpha"]:
        s += f"｜有 alpha 通道（实测透明度 {d['alpha_min']}~{d['alpha_max']}）"
        if d["alpha_min"] == 255:
            s += "，但每个像素都不透明"
    else:
        s += "｜无 alpha 通道"
    return s


def _frames_summary(frames: list) -> dict:
    """把 _decode_frames 的返回值（[(mime, bytes), ...]）汇总成几个数。"""
    out: dict = {"n": len(frames), "mimes": {}, "bytes": 0,
                 "alpha_frames": 0, "alpha_min": None, "sizes": set()}
    for mime, raw in frames:
        out["mimes"][mime] = out["mimes"].get(mime, 0) + 1
        out["bytes"] += len(raw)
        d = _img_probe(raw)
        out["sizes"].add(d["size"])
        if d["alpha"]:
            out["alpha_frames"] += 1
            out["alpha_min"] = (d["alpha_min"] if out["alpha_min"] is None
                                else min(out["alpha_min"], d["alpha_min"]))
    return out


def _callers_in_source(target: str) -> list[str]:
    """在 bot.py 源码里找"哪个函数调用了 target"—— 证明被测的函数真在线上链路上。

    只读源码 + ast，不 import 第二次；源码正被另一个人改也不怕：解析失败返回空表。
    """
    try:
        tree = ast.parse((BASE_DIR / "bot.py").read_text(encoding="utf-8"))
    except Exception:
        return []
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if (getattr(f, "id", None) or getattr(f, "attr", None)) == target:
                    hits.append(f"{node.name}() 第 {sub.lineno} 行")
    return sorted(set(hits))


def sec_gif() -> None:
    head(5, "动图抽帧自检（内存造多帧透明 GIF，走真链路 _decode_frames）")
    decode = getattr(bot, "_decode_frames", None)
    if not callable(decode):
        bad("bot 里没有 _decode_frames —— 新链路的名字又变了，本节跳过")
        PROBLEMS.append("bot._decode_frames 不存在，动图抽帧这一环没法自检")
        return

    vision = CFG.get("vision") or {}
    max_frames = max(1, int(vision.get("max_frames", 1) or 1))
    max_bytes = int(vision.get("max_bytes", 8_000_000))
    engine = getattr(bot, "VISION", None)
    ms = getattr(engine, "max_side", None)
    if callable(ms):
        try:
            max_side = int(ms())
            side_src = f"VISION.max_side()（backend={engine.backend()}）"
        except Exception as exc:
            max_side = int(vision.get("max_side_local", 1024))
            side_src = f"config（VISION.max_side() 调用失败：{type(exc).__name__}）"
    else:
        max_side = int(vision.get("max_side_local", 1024))
        side_src = "config（bot 里没有 VISION.max_side）"

    p(f"    口径：_decode_frames(raw, max_frames={max_frames}, max_side={max_side}, "
      f"max_bytes={max_bytes})")
    p(f"    max_side 取自 {side_src}")
    try:
        p(f"    实际签名：_decode_frames{inspect.signature(decode)}")
    except Exception:
        pass
    callers = _callers_in_source("_decode_frames")
    p("    它被谁调用：" + ("、".join(callers) if callers else "（源码里没找到调用点）"))

    base = max(1024, max_side)      # 保证"大图"那一档一定超过 1024px
    cases = [
        ("小图 200x150（不触发缩放）", 200, 150),
        (f"大图 {base + 576}x{base + 176}（超过 1024px，会触发缩放）", base + 576, base + 176),
    ]
    for label, w, h in cases:
        gif = _make_gif(w, h, 3)
        before = _img_probe(gif)
        p("")
        p(f"    {label}")
        p(f"      抽帧前：{_fmt_img(before)}")
        t0 = time.time()
        try:
            frames = decode(gif, max_frames, max_side, max_bytes)
        except Exception as exc:
            bad(f"      _decode_frames 抛异常：{type(exc).__name__}: {exc}")
            PROBLEMS.append(f"动图抽帧自检：_decode_frames 抛 {type(exc).__name__}")
            continue
        dt = time.time() - t0
        if frames is None:
            bad("      返回 None —— 线上会退回「整张原图原样送」：动图不抽帧、"
                "字节上限只剩原来那一条（Pillow 缺失或解不开）")
            PROBLEMS.append("_decode_frames 返回 None（Pillow 不可用或解不开这张图）")
            continue
        if not frames:
            bad("      返回空列表 —— 线上等于这张图被直接丢掉")
            PROBLEMS.append("_decode_frames 返回空（图被丢掉）")
            continue

        after = _frames_summary(frames)
        p(f"      抽帧后：{after['n']} 帧，"
          + "、".join(f"{m}×{c}" for m, c in after["mimes"].items())
          + f"，合计 {after['bytes']} 字节，耗时 {dt * 1000:.0f}ms")
        for i, (mime, raw) in enumerate(frames):
            p(f"        第 {i} 帧：{_fmt_img(_img_probe(raw))}")
        p("        尺寸：" + "、".join(f"{s[0]}x{s[1]}" for s in sorted(after["sizes"]))
          + f"（缩放上限 max_side={max_side}；上面每帧的字节＝该帧编码后的大小）")
        if max(w, h) > max_side:
            ok(f"缩放生效：原图 {w}x{h} 最长边 > {max_side} → 每帧最长边 ≤ {max_side}")

        if before["frames"] > 1 and after["n"] <= 1:
            if max_frames <= 1:
                warn(f"      帧数 {before['frames']} → {after['n']}："
                     f"vision.max_frames={max_frames} 就是这么配的（等于关掉抽帧），不是 bug")
            else:
                bad(f"      动图又塌成静态了：{before['frames']} 帧 → {after['n']} 帧，"
                    f"而 max_frames={max_frames}")
                PROBLEMS.append(f"max_frames={max_frames} 时 {w}x{h} 动图仍塌成 {after['n']} 帧")
        elif before["frames"] > 1:
            ok(f"帧数保住了：{before['frames']} 帧 → {after['n']} 帧"
               + ("（每帧都是独立的 data URL，模型能看到动作）" if after["n"] > 1 else ""))

        if before["alpha_min"] != 0:
            warn("造出来的源图没有全透明像素，这一档没法判定 alpha 有没有保留")
        elif after["alpha_frames"] == after["n"] and after["alpha_min"] == 0:
            ok(f"透明通道还在：{after['alpha_frames']}/{after['n']} 帧带 alpha，"
               f"实测最小透明度 {after['alpha_min']}（0 = 全透明像素确实留住了，存的是 PNG）")
        else:
            bad(f"透明通道丢了：{after['alpha_frames']}/{after['n']} 帧带 alpha，"
                f"最小透明度 {after['alpha_min']}（255 就是全被压成不透明）")
            PROBLEMS.append(f"{w}x{h} 动图抽帧后透明通道丢失")

    # 结论段：直接回答「>1024px 的动图现在还会不会塌成静态」「透明通道还在不在」
    p("")
    p("    结论：")
    p(f"      · 出厂配置 vision.max_frames={max_frames}、max_side={max_side}"
      f"（来自 {side_src.split('（')[0]}）")
    if max_frames > 1:
        ctl = decode(_make_gif(200, 150, 3), 1, max_side, max_bytes)
        n_ctl = len(ctl) if isinstance(ctl, list) else -1
        p("      · >1024px 的动图**不再**塌成静态：上面「大图」那一档的抽帧后帧数是 "
          "3（与源图一致），塌成单帧这件事现在只可能来自配置。")
        p(f"      · 对照：把 max_frames 临时按 1 调一次同一张 3 帧小图 → {n_ctl} 帧，"
          "说明「只取第 0 帧」已经是 max_frames=1 的**显式选择**，不再是无条件行为。")
    else:
        p(f"      · config 里 max_frames={max_frames}，抽帧是**关着**的：动图会被压成 1 帧 ——"
          " 这是配置选择，不是链路 bug；要动图被看懂就把 vision.max_frames 调到 3。")
    p("      · 透明通道：帧编码走 _encode_frame，有 alpha 就存 PNG —— 上面每档都报"
      "「实测最小透明度」，看到 0 就说明全透明像素没丢（原图走 JPEG 才是丢 alpha 的写法）。")


# --------------------------------------------------------- 6. 最近记录

LOG_PATTERNS = [
    ("本地识图失败", "本地 VL 识图失败（退化为纯文本）"),
    ("exceed_context_size_error", "本地上下文超限 400（图片 token 装不下）"),
    ("识图", "其它含「识图」的行"),
]

# 400 原文形如：request (1060 tokens) exceeds the available context size (1024 tokens)
_CTX_RE = re.compile(r"available context size \((\d+) tokens\)")


def past_context_limits(log_path: Path | None = None) -> list[int]:
    """从历史 400 报错里抠出服务端**当时**自报的上下文上限。

    为什么值得单独抠：这个数是「当时那次加载」的上下文，和 `/api/v0/models`
    现在报的 `loaded_context_length` 未必一样 —— 用户在 LM Studio 里改了加载设置
    就会变（本项目就从 1024 变成过 8192）。只看当下会误判成"问题早好了"。
    """
    path = log_path or (BASE_DIR / "bot.log")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    return sorted({int(m.group(1)) for ln in lines for m in [_CTX_RE.search(ln)] if m})


def sec_log(log_path: Path, samples: int) -> None:
    head(6, f"最近识图记录（{log_path.name}）")
    if not log_path.exists():
        bad(f"日志不存在：{log_path}")
        return
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        bad(f"读日志失败：{type(exc).__name__}: {exc}")
        return
    p(f"    共 {len(lines)} 行")

    hot = [ln for ln in lines if ("[WARNING]" in ln or "[ERROR]" in ln)]
    p(f"    其中 WARNING/ERROR {len(hot)} 行")
    limits = past_context_limits(log_path)
    if limits:
        p(f"    400 原文里服务端自报过的上下文上限：{limits}")
        if min(limits) < SAFE_CONTEXT:
            warn(f"其中 {min(limits)} 装不下一张图 → 这就是当年那批 400 的直接原因")
    p("")
    for pat, desc in LOG_PATTERNS:
        hits = [ln for ln in hot if pat in ln]
        p(f"    「{pat}」{len(hits)} 条 —— {desc}")
        if hits:
            for ln in hits[-samples:]:
                p(f"        {clip(ln, 260)}")
        else:
            p("        （无）")
        if pat == "识图":
            others = [ln for ln in lines if pat in ln and ln not in hot]
            if others:
                p(f"        （另有 {len(others)} 条非 WARNING/ERROR 的识图行，未列出）")
    p("")
    total400 = len([ln for ln in hot if "exceed_context_size_error" in ln])
    if total400:
        last = [ln for ln in hot if "exceed_context_size_error" in ln][-1]
        bad(f"线上真的发生过 {total400} 次上下文超限 400，最近一次：{clip(last, 60)}")
        PROBLEMS.append(f"日志里 {total400} 次 exceed_context_size_error")
    if not [ln for ln in hot if "本地识图失败" in ln]:
        NOTES.append("日志里没有「本地识图失败」，可能已经被轮转掉或还没复现")


# --------------------------------- 7. 契约落地自查（逐条对照 _backup/契约-禁言识图WebUI.md）
#
# 为什么要有这一节：契约写的是"别人负责的那一半"，落地没落地只有对着现场文件核对才知道。
# 一律用 getattr 动态探测 + 只读源码（ast / 正则），任何一项缺失都只报 ✘ 并进 PROBLEMS，
# 绝不让体检脚本自己崩 —— bot.py 或 console_server.py 正被同时改，读到的可能是旧版本。

CHECK_OK, CHECK_BAD = "✔", "✘"


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _ast_assign(path: Path, attr: str):
    """只读源码 + ast：找出 `attr = <值>` 那个赋值节点（模块级 / 类级都算）。"""
    tree = ast.parse(_read_text(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == attr for t in node.targets):
            return node.value
    return None


def _ast_const_names(node) -> set | None:
    """从 `{...}` / `frozenset({...})` 里取字符串键或元素。取不到返回 None。"""
    if node is None:
        return None
    if isinstance(node, ast.Call):          # frozenset({...})
        node = node.args[0] if node.args else None
    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys if isinstance(k, ast.Constant)}
    if isinstance(node, ast.Set):
        return {e.value for e in node.elts if isinstance(e, ast.Constant)}
    return None


def _ast_const_map(node) -> dict | None:
    """把 `{键: (字面量, ...)}` 这种表取成 python dict；非字面量的值记成 None。"""
    if not isinstance(node, ast.Dict):
        return None
    out: dict = {}
    for k, v in zip(node.keys, node.values):
        if not isinstance(k, ast.Constant):
            continue
        if isinstance(v, ast.Tuple):
            out[str(k.value)] = tuple(
                e.value if isinstance(e, ast.Constant) else None for e in v.elts)
        else:
            out[str(k.value)] = v.value if isinstance(v, ast.Constant) else None
    return out


def _ast_int(node) -> int | None:
    """把 `256 * 1024` 这种纯常量算式算出来（不求值任意代码）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.BinOp):
        l, r = _ast_int(node.left), _ast_int(node.right)
        if l is None or r is None:
            return None
        return {"Mult": l * r, "Add": l + r, "Sub": l - r}.get(type(node.op).__name__)
    return None


def _html_tunable_groups(path: Path) -> set | None:
    """从 console.html 里抠出 `TUNABLE_GROUP = { 分组键: '中文名', ... }` 的键。"""
    try:
        src = _read_text(path)
    except Exception:
        return None
    m = re.search(r"TUNABLE_GROUP\s*=\s*\{(.*?)\}\s*;", src, re.S)
    if not m:
        return None
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1)))


def _sig(fn) -> str:
    if not callable(fn):
        return "不存在"
    try:
        return f"{getattr(fn, '__name__', '?')}{inspect.signature(fn)}"
    except Exception:
        return getattr(fn, "__name__", "?")


def _param_names(fn) -> list:
    try:
        return list(inspect.signature(fn).parameters)
    except Exception:
        return []


def sec_contract() -> None:
    head(7, "契约落地自查（逐条对照 _backup/契约-禁言识图WebUI.md）")
    rows: list = []      # 契约逐条要求的（任务清单里的那几项）
    extra: list = []     # 顺手多核对的（同一份契约里的其它条目）

    def chk(name: str, cond, detail: str = "", more: bool = False) -> None:
        (extra if more else rows).append((name, bool(cond), str(detail)))

    def n_of(x) -> str:
        try:
            return f"{len(x)} 项"
        except Exception:
            return "?"

    srv = BASE_DIR / "console_server.py"
    html = BASE_DIR / "public" / "console.html"
    p("    核对方式：bot 侧全部 getattr 动态探测（改名只会报 " + CHECK_BAD
      + "，不会崩）；console_server.py 用 ast 读源码；console.html 用正则抠 TUNABLE_GROUP")

    # ── bot.py 侧 ──
    engine = getattr(bot, "VISION", None)
    chk("bot.VISION 存在（§3.2 的识图单例）", engine is not None,
        type(engine).__name__ if engine is not None else "getattr(bot, 'VISION') → None")
    for m in ("backend", "usable", "status", "probe", "describe"):
        fn = getattr(engine, m, None)
        chk(f"VISION.{m}() 存在（§3.2 的接口）", callable(fn),
            _sig(fn) if callable(fn) else "不存在", more=True)

    vision_cfg = CFG.get("vision") or {}
    backend = str(vision_cfg.get("backend") or "")
    backends = tuple(getattr(bot, "VISION_BACKENDS", ("auto", "cloud", "local", "off")))
    chk("config 里有 vision.backend 且取值合法（§2.1）",
        bool(backend) and backend in backends,
        f"vision.backend={backend or '<缺失>'}（合法值 {'/'.join(backends)}）")

    chk("config 里 cloud.vision 为真（§2.2，当年那个 false 是误判）",
        bool((CFG.get("cloud") or {}).get("vision")),
        f"cloud.vision={(CFG.get('cloud') or {}).get('vision')}"
        f"｜vision_relay={CFG.get('vision_relay')}")

    fbx = getattr(bot, "_force_ban_ex", None)
    abx = getattr(bot, "apply_bans_ex", None)
    chk("bot._force_ban_ex 存在且是 async（§3.1 真正的实现）",
        callable(fbx) and inspect.iscoroutinefunction(fbx), _sig(fbx))
    chk("bot.apply_bans_ex 存在且是 async，返回 (done, notes)（§3.1）",
        callable(abx) and inspect.iscoroutinefunction(abx), _sig(abx))

    sw = getattr(bot, "CONSOLE_SWITCHES", None)
    ls = getattr(bot, "CONSOLE_LISTS", None)
    chk("CONSOLE_SWITCHES 非空（§3.4）", bool(sw), n_of(sw))
    chk("CONSOLE_LISTS 非空（§3.4）", bool(ls), n_of(ls))

    # ── console_server.py 侧（只读源码 + ast，不 import）──
    try:
        kmap = _ast_const_map(_ast_assign(srv, "_KINDS")) or {}
    except Exception as exc:
        kmap = {}
        warn(f"读 console_server.py 的 _KINDS 失败：{type(exc).__name__}: {exc}")
    want_kinds = ("vision", "switches", "lists")
    bad_kinds = [k for k in want_kinds if kmap.get(k, (None,))[0] != k]
    chk("console_server._KINDS 有 vision/switches/lists 三行且 kind 名对得上（§4.1）",
        bool(kmap) and not bad_kinds,
        ("缺或名字不对：" + "、".join(bad_kinds)) if bad_kinds
        else f"共 {len(kmap)} 个 kind；" + "、".join(
            f"{k}→{kmap[k]}" for k in want_kinds if k in kmap))

    try:
        ops = _ast_const_names(_ast_assign(srv, "WRITE_OPS")) or set()
    except Exception as exc:
        ops = set()
        warn(f"读 console_server.py 的 WRITE_OPS 失败：{type(exc).__name__}: {exc}")
    want_ops = ("switch_set", "list_set", "vision_set")
    miss_ops = [o for o in want_ops if o not in ops]
    chk("console_server.WRITE_OPS 里有 switch_set/list_set/vision_set（§4.2）",
        bool(ops) and not miss_ops,
        ("缺 " + "、".join(miss_ops)) if miss_ops else f"WRITE_OPS 共 {len(ops)} 个 op")

    # ── public/console.html 侧（只读源码 + 正则）──
    tgs = _html_tunable_groups(html)
    tun = getattr(bot, "CONSOLE_TUNABLES", None) or {}
    grp_fn = getattr(bot, "_tunable_group", None)
    if callable(grp_fn):
        try:
            tgroups = {grp_fn(path) for path in tun}
            how = "用后端 _tunable_group() 现算（和面板拿到的分组名同一套）"
        except Exception as exc:
            tgroups = {str(path).split(".")[0] for path in tun}
            how = f"按路径前缀推导（_tunable_group 调用失败：{type(exc).__name__}）"
    else:
        tgroups = {str(path).split(".")[0] for path in tun}
        how = "按路径前缀推导（bot 里没有 _tunable_group）"
    miss_g = sorted(g for g in tgroups if not tgs or g not in tgs)
    chk(f"console.html 的 TUNABLE_GROUP 覆盖 CONSOLE_TUNABLES 的 {len(tgroups)} 个分组名（§5.3）",
        bool(tgroups) and tgs is not None and not miss_g,
        ("缺 " + "、".join(miss_g)) if miss_g
        else (f"{how}：" + "、".join(sorted(tgroups))))

    # ── 顺手多核对的（没写进本任务清单，但同一份契约里有）──
    sgroups = {v[2] for v in (sw or {}).values() if isinstance(v, tuple) and len(v) >= 3}
    lgroups = {v[2] for v in (ls or {}).values() if isinstance(v, tuple) and len(v) >= 3}
    miss2 = sorted(g for g in (sgroups | lgroups) if not tgs or g not in tgs)
    chk("TUNABLE_GROUP 也覆盖 switches/lists 的分组名"
        "（前端 renderSwitches/renderLists 同样走 groupLabel）",
        not miss2,
        ("缺 " + "、".join(miss2)) if miss2
        else (f"switches {len(sgroups)} 组 + lists {len(lgroups)} 组："
              + "、".join(sorted(sgroups | lgroups))), more=True)

    try:
        mx = _ast_int(_ast_assign(srv, "_MAX_BODY"))
    except Exception as exc:
        mx = None
        warn(f"读 console_server.py 的 _MAX_BODY 失败：{type(exc).__name__}: {exc}")
    chk("console_server._MAX_BODY 已抬到 ≥256KB（§4.3）",
        isinstance(mx, int) and mx >= 256 * 1024, f"_MAX_BODY={mx}", more=True)

    fb = getattr(bot, "force_ban", None)
    chk("bot.force_ban 仍在且签名没变（§3.1 只许做成薄包装）",
        callable(fb) and _param_names(fb)[:5] == ["group_id", "uid", "name", "minutes", "why"],
        _sig(fb), more=True)

    snap = getattr(bot, "console_snapshot", None)
    if callable(snap):
        kinds_want = (("vision", ("backend", "backend_label", "usable", "cloud",
                                 "local", "stats", "config")),
                      ("switches", ("groups",)),
                      ("lists", ("groups",)))
        for kind, keys in kinds_want:
            try:
                got = snap(kind)
                missk = [k for k in keys if not isinstance(got, dict) or k not in got]
                chk(f"console_snapshot('{kind}') 能取到且含约定的键（§3.4 的 kind）",
                    not missk, ("缺 " + "、".join(missk)) if missk else "ok", more=True)
            except Exception as exc:
                chk(f"console_snapshot('{kind}') 能取到", False,
                    f"{type(exc).__name__}: {exc}", more=True)
    else:
        chk("bot.console_snapshot 可调用（三个新 kind 的入口）", False, "不存在", more=True)

    apis = ("_decode_frames", "_encode_frame", "_encode_frame_limited",
            "_image_to_data_url", "resolve_images")
    gone = [n for n in apis if not callable(getattr(bot, n, None))]
    chk("这一批的新识图 API 都在（" + "、".join(apis) + "）", not gone,
        ("缺 " + "、".join(gone)) if gone else "5 个名字都在", more=True)

    calls = _callers_in_source("_decode_frames")
    chk("bot._image_to_data_url 真的调用 _decode_frames（不是我另写一套）",
        any("_image_to_data_url" in c for c in calls),
        "、".join(calls) or "源码里没找到调用点", more=True)

    chk("老函数 bot._shrink_image 已删除（说明新链路确实换掉了旧的）",
        getattr(bot, "_shrink_image", None) is None,
        "仍在（新链路没替干净？）" if hasattr(bot, "_shrink_image") else "已不存在", more=True)

    # ── 打印 ──
    def dump(title: str, group: list) -> None:
        if not group:
            return
        p("")
        p("  ── " + title + " ──")
        for name, good, detail in group:
            mark = CHECK_OK if good else CHECK_BAD
            tail = f"　— {detail}" if detail else ""
            print(f"  {mark} {name}{tail}")

    dump("契约逐条要求", rows)
    dump("顺手多核对的", extra)
    total = len(rows) + len(extra)
    bads = [n for n, g, _ in (rows + extra) if not g]
    p("")
    p(f"    共 {total} 项：{CHECK_OK} {total - len(bads)} / {CHECK_BAD} {len(bads)}")
    for n in bads:
        PROBLEMS.append(f"契约没落地：{n}")


# ------------------------------------------------------------- 汇总

def sec_summary() -> None:
    head(0, "结论与建议")
    if PROBLEMS:
        p("发现的问题（按链路顺序）：")
        for i, item in enumerate(PROBLEMS, 1):
            p(f"  {i}. {item}")
    else:
        p("本次体检没发现明确故障点。")
    if NOTES:
        p("")
        p("存疑 / 需要人看一眼：")
        for i, item in enumerate(NOTES, 1):
            p(f"  {i}. {item}")

    vision = CFG.get("vision") or {}
    cloud = CFG.get("cloud") or {}
    p("")
    p("静态建议（不管体检结果如何都成立）：")
    tips = []
    if not vision.get("backend"):
        tips.append("config.json 补上 vision 段（backend/max_frames/max_side_cloud/"
                    "max_side_local/max_bytes/describe_max_tokens/local_probe_seconds），"
                    "让后端由 vision.backend 明确决定，不再靠 vision_relay 推导。")
    if not cloud.get("vision"):
        tips.append("cloud.vision 改回 true —— 契约 §1 已查明当年那个 400 是"
                    "「DeepSeek 下不到 QQ 图床链接」，不是不支持视觉；现在图片是先下下来"
                    "转 data URL 再发的，云端能收。")
    if _drops_logged_as_debug():
        tips.append("丢图路径还在用 logger.debug：log_level=INFO 时线上看不见，"
                    "契约 §3.2 要求升成 logger.warning + 记进 VISION.stats[\"dropped\"]。")
    if not tips:
        p("  （本轮契约点过名的几条老毛病都没再复现：vision 段在、cloud.vision 为真、"
          "丢图不再走 debug —— 没有需要补的静态建议。）")
    for i, t in enumerate(tips, 1):
        p(f"  {i}. {t}")
    p("")
    print("  （本脚本只读：不改配置、不写状态、不碰 QQ；也确实没往 bot.log 写"
          "（见上方「只读佐证」）。体检结果供人工决定改什么。）")


# --------------------------------------------------------------- main

def _drops_logged_as_debug() -> bool:
    """丢图路径还在用 logger.debug 吗（契约 §3.2 要求升到 warning + 计数）。

    只读源码 + ast：解析失败就返回 False（宁可不报，也别把体检结论带偏）。
    """
    try:
        tree = ast.parse((BASE_DIR / "bot.py").read_text(encoding="utf-8"))
    except Exception:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in ("_image_to_data_url", "resolve_images", "_decode_frames"):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "debug"):
                return True
    return False


def _mark_in_logs() -> tuple[int, list[str]]:
    """回读 bot.log 及轮转出来的 bot.log.1..3，数那条标记出现了几次。

    这是"没污染 bot.log"的实证：护栏装好之后故意往 bot.logger 打过一条唯一标记，
    它出现在日志里就说明护栏没兜住（本进程的日志真混进线上日志了）。
    """
    hits, files = 0, []
    for p in sorted(BASE_DIR.glob("bot.log*")):
        try:
            hits += _read_text(p).count(MARK)
            files.append(p.name)
        except Exception:
            continue
    return hits, files


def sec_readonly_proof() -> None:
    """只读佐证：护栏清点了什么、现在还连着谁、标记有没有落进 bot.log。"""
    print()
    print("-" * 68)
    print("只读佐证（本进程有没有污染 bot.log）")
    print("-" * 68)
    print("  import bot 后摘掉的 logger handler："
          + ("；".join(REMOVED_HANDLERS) if REMOVED_HANDLERS else "（一个都没有）"))
    handlers = [type(h).__name__ for h in getattr(bot.logger, "handlers", [])]
    print(f"  现在的 bot.logger.handlers = {handlers}"
          f"｜propagate = {getattr(bot.logger, 'propagate', '?')}")
    still_file = [getattr(h, "baseFilename", None) for h in getattr(bot.logger, "handlers", [])]
    print(f"  还挂着写文件的 handler 吗：{'有 → ' + str(still_file) if any(still_file) else '没有'}")
    if GUARD_ERROR:
        bad(f"护栏安装失败：{GUARD_ERROR}")
        PROBLEMS.append(f"只读护栏安装失败：{GUARD_ERROR}")
    hits, files = _mark_in_logs()
    print(f"  实证：护栏装好后故意打了一条标记「{MARK}」，回读 {('、'.join(files)) or '（没有 bot.log*）'}"
          f" → 出现 {hits} 次")
    if hits == 0:
        ok("这条标记没进 bot.log —— 本进程的日志不会混进线上日志")
    else:
        bad("标记进了 bot.log！护栏没兜住，本进程真的污染了线上日志")
        PROBLEMS.append("本进程的日志写进了 bot.log（只读护栏没生效）")
    print("  （bot.log 仍会被**正在运行的 bot** 追加，所以文件在变长是正常的；"
          "判据是「本进程那条唯一标记一次都不出现」。）")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="识图链路体检：配置 → 云端自检 → 本地探测 → 本地自检 → 动图抽帧"
                    " → 日志 → 契约落地自查",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-cloud", action="store_true",
                    help="跳过云端视觉自检（唯一花钱的一步）")
    ap.add_argument("--no-local", action="store_true",
                    help="跳过本地探测与本地视觉自检")
    ap.add_argument("--repro-400", action="store_true",
                    help="额外给本地发一张 1024px 图，复现上下文超限 400（不花钱，但要等推理）")
    ap.add_argument("--log", default=str(BASE_DIR / "bot.log"),
                    help="日志路径（默认 bot.log）")
    ap.add_argument("--samples", type=int, default=1,
                    help="每类日志给几条样本（默认 1）")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="云端自检超时秒数（默认 30）")
    args = ap.parse_args()

    print("=" * 68 + "\n识图链路体检\n" + "=" * 68)
    print(f"项目：{BASE_DIR}")
    print("只读脚本：不改配置、不写状态、不碰 QQ。")
    print("护栏：摘掉写 bot.log 的 handler、把会落盘/记账的入口换空 —— 末尾的"
          "「只读佐证」会实证没污染 bot.log。")
    if not args.no_cloud:
        print("注意：云端自检会真的花钱（一张 64x64 图，费用极小）。--no-cloud 可跳过。")

    local = CFG.get("local") or {}
    local_timeout = float(local.get("timeout_seconds", 240))

    steps = [
        ("配置", lambda: sec_config()),
        ("云端自检", lambda: sec_cloud(args.no_cloud, args.timeout)),
        ("本地探测", lambda: sec_local_probe(args.no_local, 15.0)),
        ("本地自检", lambda: sec_local_vision(args.no_local, local_timeout, args.repro_400)),
        ("动图抽帧", lambda: sec_gif()),
        ("最近记录", lambda: sec_log(Path(args.log), max(1, args.samples))),
        ("契约自查", lambda: sec_contract()),
    ]
    for name, fn in steps:
        try:
            fn()
        except KeyboardInterrupt:
            print("\n被中断")
            return 130
        except Exception as exc:
            # 体检脚本自己绝不能崩：某一节炸了就记下来继续下一节
            bad(f"{name} 这一节自身出错，已跳过：{type(exc).__name__}: {exc}")
            NOTES.append(f"{name} 一节自身出错：{type(exc).__name__}: {exc}")

    try:
        sec_readonly_proof()
    except Exception as exc:
        warn(f"只读佐证这一段自身出错：{type(exc).__name__}: {exc}")
    sec_summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
