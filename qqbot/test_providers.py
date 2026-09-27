"""Provider 注册中心离线单测 —— **全程不联网**（httpx.MockTransport）。

跑法：
    .venv\\Scripts\\python.exe test_providers.py
或由 test_regression.py 的【34】段一并调用。

风格与 test_regression.py 一致（自研 runner，不引入 pytest）。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

# 输出统一成 UTF-8。中文 Windows 上 stdout 是 GBK，下面满屏的 "✅" 直接 print 会抛
# UnicodeEncodeError，跑到第一个断言就崩（而且看着像测试失败）。这个三段式和
# qqbot/bot.py 里那份一致 —— 本文件不 import bot，所以要自己加一遍。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import httpx  # noqa: E402

from providers import (  # noqa: E402
    Endpoint, Registry, Router, Transport,
    VISION_PLACEHOLDER_BLIND, VISION_PLACEHOLDER_NO_CHANNEL,
    build_router,
)
from providers.base import Provider, register_provider  # noqa: E402
from providers.manifest import load_file  # noqa: E402
from providers.types import Capability, ChatResult, Request, UsageKeys  # noqa: E402

ok = 0
fail = 0


def check(name: str, cond, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


# ══════════════════════════════════════════════════════════════════
# 测试脚手架
# ══════════════════════════════════════════════════════════════════

def make_registry() -> Registry:
    reg = Registry()
    reg.load_builtin()
    rep = reg.load_manifests(["providers/manifests/openai-compat.json",
                              "providers/manifests/local-servers.json",
                              "providers/manifests/cn-vendors.json"],
                             base_dir=BASE_DIR)
    assert not rep.errors, rep.errors
    return reg


def make_router(handler, cfg: dict | None = None, *, registry: Registry | None = None) -> Router:
    """用 MockTransport 建一个完全离线的 Router。"""
    reg = registry or make_registry()
    transport = Transport(client_factory=lambda trust_env: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=trust_env))
    router = Router(reg, transport=transport)
    if cfg is not None:
        router.load_notices(cfg)
    return router


def json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


OPENAI_REPLY = {
    "choices": [{"message": {"role": "assistant", "content": "嗯。"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
}


# ══════════════════════════════════════════════════════════════════
print("\n【P1】端点构建：旧 local/cloud 迁移 + 新 providers 段")
# ══════════════════════════════════════════════════════════════════


def t_endpoints() -> None:
    reg = make_registry()

    legacy = {
        "local": {"enable": True, "base_url": "http://127.0.0.1:1234/v1",
                  "api_key": "lm-studio", "model": "qwen2.5-vl-7b-instruct",
                  "timeout_seconds": 240, "concurrency": 1, "vision": True},
        "cloud": {"enable": True, "base_url": "https://api.deepseek.com",
                  "api_key": "sk-test", "model": "deepseek-flash",
                  "thinking": "disabled", "max_tokens": 1024,
                  "timeout_seconds": 90, "vision": True},
        "vision_relay": True,
    }
    out = reg.build_endpoints(legacy)
    check("旧配置能推导出端点", len(out.endpoints) == 2, str(len(out.endpoints)))
    check("标记为 legacy", out.legacy is True)
    by = out.by_id()
    local = by.get("local-default")
    cloud = by.get("cloud-default")
    check("local 端点：tier/并发/超时都对",
          local is not None and local.tier == "local" and local.concurrency == 1
          and local.timeout == 240, repr(local))
    check("local 端点：本机地址被识别为 loopback",
          local.is_loopback and local.effective_trust_env is False)
    check("cloud 端点：tier=cloud 且带 thinking 参数",
          cloud is not None and cloud.tier == "cloud"
          and cloud.params.get("thinking") == {"type": "disabled"}, repr(cloud))

    # vision_relay=true 时 cloud 必须"忽略 enable"（对齐老的 Chat.answer）
    check("中继模式下 cloud 不看 cloud.enable", cloud.require_enabled is False)
    check("中继模式下 default 链是 云端->本地",
          out.chains["default"] == ["cloud-default", "local-default"],
          str(out.chains["default"]))
    check("识图链同样是 云端->本地",
          out.chains["vision"] == ["cloud-default", "local-default"],
          str(out.chains["vision"]))
    check("转述链只看本地",
          out.chains["describe"] == ["local-default"], str(out.chains["describe"]))

    # 非中继：本地优先
    legacy2 = dict(legacy, vision_relay=False)
    out2 = reg.build_endpoints(legacy2)
    check("非中继模式下 default 链是 本地->云端",
          out2.chains["default"] == ["local-default", "cloud-default"],
          str(out2.chains["default"]))
    check("非中继模式下 cloud 恢复看 enable",
          out2.by_id()["cloud-default"].require_enabled is True)

    # local.enable=false 时不该排进链
    legacy3 = dict(legacy, vision_relay=False,
                   local=dict(legacy["local"], enable=False))
    out3 = reg.build_endpoints(legacy3)
    check("local 关掉后链里没有它",
          out3.chains["default"] == ["cloud-default"], str(out3.chains["default"]))

    modern = {
        "providers": {
            "endpoints": [
                {"id": "a", "provider": "openai", "base_url": "https://x/v1",
                 "model": "m1", "tier": "cloud", "api_key_env": "A_KEY"},
                {"id": "b", "provider": "vllm", "base_url": "http://127.0.0.1:8000/v1",
                 "model": "m2", "tier": "local", "concurrency": 2},
            ],
            "chains": {"default": ["b", "a"]},
        }
    }
    outm = reg.build_endpoints(modern)
    check("新 providers 段原样生效", len(outm.endpoints) == 2 and not outm.legacy)
    check("新配置的链被尊重", outm.chains["default"] == ["b", "a"], str(outm.chains["default"]))
    check("清单里的 vllm 适配器已被注册", reg.get("vllm") is not None)
    check("未知 provider 被跳过 + 提示", "没注册" in " ".join(
        reg.build_endpoints({"providers": {"endpoints": [
            {"id": "z", "provider": "nope", "base_url": "http://x", "model": "m"}]}}).notices))

    empty = reg.build_endpoints({})
    check("空配置不炸、端点为空", empty.endpoints == [])
    check("明文 api_key 会触发安全提示",
          any("明文" in n for n in reg.build_endpoints(legacy).notices),
          str(reg.build_endpoints(legacy).notices))


# ══════════════════════════════════════════════════════════════════
print("\n【P2】URL 拼接：与重构前的写法逐字一致（不自动补 /v1）")
# ══════════════════════════════════════════════════════════════════


def t_urls() -> None:
    reg = make_registry()
    cases = [
        ("https://api.deepseek.com", "openai"),
        ("https://api.deepseek.com/", "openai"),
        ("https://api.deepseek.com/v1", "openai"),
        ("http://127.0.0.1:1234/v1", "lmstudio"),
        ("https://api.moonshot.cn/v1", "moonshot"),
    ]
    bad = []
    for base, provider in cases:
        ep = Endpoint(id="e", provider=provider, base_url=base, model="m",
                      api_key="k", tier="local" if "127.0.0.1" in base else "cloud")
        prov = reg.get(provider)
        req = prov.build_request(ep, [{"role": "user", "content": "hi"}], stream=False,
                                 max_tokens=16, temperature=0.5, extra=None)
        expect = base.rstrip("/") + "/chat/completions"     # ← 重构前的原式
        if req.url != expect:
            bad.append(f"{base} -> {req.url} != {expect}")
    check("五个端点的 URL 与老实现逐字相同", not bad, str(bad))


# ══════════════════════════════════════════════════════════════════
print("\n【P3】鉴权：bearer / 环境变量优先 / 无鉴权")
# ══════════════════════════════════════════════════════════════════


def t_auth() -> None:
    reg = make_registry()
    ep = Endpoint(id="e", provider="openai", base_url="https://x/v1", model="m",
                  api_key="plain-key", tier="cloud")
    req = reg.get("openai").build_request(ep, [{"role": "user", "content": "hi"}],
                                          stream=False, max_tokens=8, temperature=0.5,
                                          extra=None)
    check("默认走 Authorization: Bearer",
          req.headers.get("Authorization") == "Bearer plain-key", str(req.headers))

    import os
    os.environ["PROVIDERS_TEST_KEY"] = "env-key"
    try:
        ep2 = Endpoint(id="e2", provider="openai", base_url="https://x/v1", model="m",
                       api_key="plain-key", api_key_env="PROVIDERS_TEST_KEY", tier="cloud")
        req2 = reg.get("openai").build_request(ep2, [{"role": "user", "content": "hi"}],
                                               stream=False, max_tokens=8, temperature=0.5,
                                               extra=None)
        check("api_key_env 优先于明文 api_key",
              req2.headers.get("Authorization") == "Bearer env-key", str(req2.headers))
        check("has_key / auth() 也认环境变量", ep2.has_key() and ep2.auth() == "env-key")
    finally:
        os.environ.pop("PROVIDERS_TEST_KEY", None)

    ep3 = Endpoint(id="e3", provider="vllm", base_url="http://127.0.0.1:8000/v1",
                   model="m", tier="local")
    req3 = reg.get("vllm").build_request(ep3, [{"role": "user", "content": "hi"}],
                                         stream=False, max_tokens=8, temperature=0.5,
                                         extra=None)
    check("无鉴权厂商不带 Authorization 头",
          "Authorization" not in (req3.headers or {}), str(req3.headers))
    check("本机端点自动 trust_env=False", ep3.effective_trust_env is False)
    check("外网端点默认 trust_env=True",
          Endpoint(id="x", provider="openai", base_url="https://api.deepseek.com",
                   model="m", tier="cloud").effective_trust_env is True)


# ══════════════════════════════════════════════════════════════════
print("\n【P4】body 隔离：厂商私有字段不外溢 / max_tokens 字段名 / 温度夹紧")
# ══════════════════════════════════════════════════════════════════


def t_body() -> None:
    reg = make_registry()

    ds = Endpoint(id="ds", provider="openai", base_url="https://api.deepseek.com",
                  model="deepseek-chat", api_key="k", tier="cloud",
                  params={"thinking": {"type": "disabled"}})
    groq = Endpoint(id="gq", provider="groq", base_url="https://api.groq.com/openai/v1",
                    model="llama-3.3-70b", api_key="k", tier="cloud")
    msgs = [{"role": "user", "content": "hi"}]

    b_ds = reg.get("openai").build_request(ds, msgs, stream=False, max_tokens=64,
                                           temperature=0.9, extra=None).json_body
    b_gq = reg.get("groq").build_request(groq, msgs, stream=False, max_tokens=64,
                                         temperature=0.9, extra=None).json_body
    check("thinking 只出现在 DeepSeek 端点上", "thinking" in b_ds and "thinking" not in b_gq,
          f"ds={list(b_ds)} groq={list(b_gq)}")
    check("body 里不混入 _ 开头的内部键",
          not [k for k in b_ds if k.startswith("_")], str(list(b_ds)))

    # max_tokens 的字段名：端点能力覆盖成 max_completion_tokens
    cap = Capability(max_tokens_key="max_completion_tokens")
    o_ep = Endpoint(id="o", provider="openai", base_url="https://api.openai.com/v1",
                    model="gpt-5", api_key="k", tier="cloud", capabilities=cap)
    b_o = reg.get("openai").build_request(o_ep, msgs, stream=False, max_tokens=128,
                                          temperature=0.9, extra=None).json_body
    check("max_tokens_key 覆盖生效", b_o.get("max_completion_tokens") == 128
          and "max_tokens" not in b_o, str(b_o))

    # 点路径（Ollama 的 options.num_predict）由声明式清单驱动 → 见 P6
    cap_nested = Capability(max_tokens_key="options.num_predict")
    v_ep = Endpoint(id="v", provider="openai", base_url="http://127.0.0.1:9/v1",
                    model="m", tier="local", capabilities=cap_nested)
    b_v = reg.get("openai").build_request(v_ep, msgs, stream=False, max_tokens=77,
                                          temperature=0.9, extra=None).json_body
    check("max_tokens_key 支持点路径（写进嵌套 dict）",
          (b_v.get("options") or {}).get("num_predict") == 77, str(b_v))

    # 温度夹紧：Kimi 上限 1.0、MiniMax 下限 0.01
    km = Endpoint(id="km", provider="moonshot", base_url="https://api.moonshot.cn/v1",
                  model="moonshot-v1-32k", api_key="k", tier="cloud")
    b_km = reg.get("moonshot").build_request(km, msgs, stream=False, max_tokens=16,
                                             temperature=1.7, extra=None).json_body
    check("超上限的温度被夹到 1.0", b_km.get("temperature") == 1.0, str(b_km))
    mm = Endpoint(id="mm", provider="minimax", base_url="https://api.minimax.chat/v1",
                  model="abab", api_key="k", tier="cloud")
    b_mm = reg.get("minimax").build_request(mm, msgs, stream=False, max_tokens=16,
                                            temperature=0.0, extra=None).json_body
    check("低于下限的温度被夹到 0.01", b_mm.get("temperature") == 0.01, str(b_mm))


# ══════════════════════════════════════════════════════════════════
print("\n【P5】响应解析：OpenAI 形状 + 定制路径")
# ══════════════════════════════════════════════════════════════════


def t_parse() -> None:
    reg = make_registry()
    ep = Endpoint(id="e", provider="openai", base_url="https://x", model="m",
                  api_key="k", tier="cloud")
    res = reg.get("openai").parse_response(ep, OPENAI_REPLY)
    check("取出文本", res.text == "嗯。", res.text)
    check("取出 finish_reason", res.finish_reason == "stop", res.finish_reason)
    check("usage 归一到 in/out", res.usage["in"] == 10 and res.usage["out"] == 3, str(res.usage))

    null_case = {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
    res2 = reg.get("openai").parse_response(ep, null_case)
    check("content=null 不炸且能触发截断重试", res2.text == ""
          and reg.get("openai").needs_truncation_retry(res2), repr(res2))

    # 定制路径的声明式厂商
    ms = reg.get("moonshot")
    res3 = ms.parse_response(ep, OPENAI_REPLY)
    check("声明式厂商也能解析", res3.text == "嗯。", res3.text)


# ══════════════════════════════════════════════════════════════════
print("\n【P6】识图降级：两套占位措辞必须逐字保留")
# ══════════════════════════════════════════════════════════════════


def t_vision_placeholder() -> None:
    reg = make_registry()
    ep = Endpoint(id="e", provider="openai", base_url="https://x", model="m",
                  api_key="k", tier="cloud", vision=False)
    prov = reg.get("openai")
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "看这个"},
        {"type": "image_url", "image_url": {"url": "https://x/a.jpg"}}]}]

    cap = prov.capability(ep)
    out_blind = prov.prepare_messages(msgs, ep, cap, backend_can_see=False)
    out_relay = prov.prepare_messages(msgs, ep, cap, backend_can_see=True)
    check("后端看不了图 -> 用「你看不到图」那套",
          out_blind[0]["content"][1]["text"] == VISION_PLACEHOLDER_BLIND,
          out_blind[0]["content"][1]["text"])
    check("后端能看图、只是这条通道传不了 -> 用另一套",
          out_relay[0]["content"][1]["text"] == VISION_PLACEHOLDER_NO_CHANNEL,
          out_relay[0]["content"][1]["text"])
    check("文字段没被动过", out_blind[0]["content"][0]["text"] == "看这个")

    ep_see = Endpoint(id="e2", provider="moonshot", base_url="https://x", model="m",
                      api_key="k", tier="cloud", vision=True)
    kept = prov.prepare_messages(msgs, ep_see, prov.capability(ep_see), backend_can_see=True)
    check("能看图的端点原样保留图片段",
          kept[0]["content"][1]["type"] == "image_url", str(kept))


# ══════════════════════════════════════════════════════════════════
print("\n【P7】清单校验：坏清单只记错误，不抛异常")
# ══════════════════════════════════════════════════════════════════


def t_manifest() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="prov_manifest_"))
    good = tmp / "good.json"
    good.write_text(json.dumps({"manifest_version": 1, "providers": [
        {"name": "ok1", "protocol": "declarative",
         "request": {"path": "/chat/completions", "body": {"model": "$model"}},
         "response": {"text": "$.choices[0].message.content"}}]}), encoding="utf-8")
    specs, errors = load_file(good)
    check("合法清单通过", len(specs) == 1 and not errors, str(errors))

    bad = tmp / "bad.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    specs2, errors2 = load_file(bad)
    check("坏 JSON 只报错不抛", specs2 == [] and errors2, str(errors2))

    miss = load_file(tmp / "nope.json")
    check("文件不存在只报错不抛", miss[0] == [] and miss[1])

    v = tmp / "version.json"
    v.write_text(json.dumps({"manifest_version": 99, "providers": []}), encoding="utf-8")
    check("不认识的 manifest_version 被拒", load_file(v)[1])

    novar = tmp / "novar.json"
    novar.write_text(json.dumps({"manifest_version": 1, "providers": [
        {"name": "bad", "request": {"body": {"model": "$modle"}}}]}), encoding="utf-8")
    check("模板变量写错能被抓出来", any("$modle" in e for e in load_file(novar)[1]),
          str(load_file(novar)[1]))

    noname = tmp / "noname.json"
    noname.write_text(json.dumps({"manifest_version": 1, "providers": [
        {"protocol": "declarative"}]}), encoding="utf-8")
    check("缺 name 被拒", load_file(noname)[1])

    # 注释容忍
    cmt = tmp / "cmt.json"
    cmt.write_text('{\n  // 注释\n  "manifest_version": 1,\n  "providers": [\n'
                   '    {"name": "c1", "request": {"path": "/x"}}\n  ]\n}',
                   encoding="utf-8")
    check("JSON 里的 // 注释被容忍", load_file(cmt)[1] == [], str(load_file(cmt)[1]))


# ══════════════════════════════════════════════════════════════════
print("\n【P8】usage 归一化：四家字段名各自映射")
# ══════════════════════════════════════════════════════════════════


def t_usage() -> None:
    reg = make_registry()
    got = reg.get("deepseek").parse_response(
        Endpoint(id="d", provider="deepseek", base_url="https://api.deepseek.com",
                 model="m", api_key="k", tier="cloud"),
        {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                   "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}}).usage
    check("DeepSeek 的缓存字段被取到",
          got == {"in": 100, "out": 20, "hit": 80, "miss": 20}, str(got))

    got2 = reg.get("openrouter").parse_response(
        Endpoint(id="o", provider="openrouter", base_url="https://openrouter.ai/api/v1",
                 model="m", api_key="k", tier="cloud"),
        {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 50, "completion_tokens": 5,
                   "prompt_tokens_details": {"cached_tokens": 30}}}).usage
    check("OpenAI 系嵌套的 cached_tokens 被取到",
          got2["in"] == 50 and got2["hit"] == 30 and got2["miss"] == 0, str(got2))

    got3 = reg.get("openai").parse_response(
        Endpoint(id="n", provider="openai", base_url="https://x", model="m",
                 api_key="k", tier="cloud"), {"choices": [{"message": {"content": "y"}}]}).usage
    check("没有 usage 时全归零且不炸", got3 == {"in": 0, "out": 0, "hit": 0, "miss": 0}, str(got3))


# ══════════════════════════════════════════════════════════════════
print("\n【P9】离线调用：MockTransport 走完整条链路（含截断重试）")
# ══════════════════════════════════════════════════════════════════


async def t_offline_call() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        seen.append({"url": str(request.url), "body": body,
                     "auth": request.headers.get("authorization")})
        return json_response(OPENAI_REPLY)

    cfg = {"providers": {"endpoints": [
        {"id": "c1", "provider": "openai", "base_url": "https://api.deepseek.com",
         "model": "deepseek-chat", "tier": "cloud", "api_key": "k", "timeout": 5},
    ]}}
    router = make_router(handler, cfg)

    async def go():
        text, source = await router.answer([{"role": "user", "content": "在吗"}])
        return text, source

    text, source = await go()
    check("能拿到回复", text == "嗯。", repr(text))
    check("source 用的是 tier 字面量", source == "cloud", source)
    check("请求打到了正确 URL",
          seen and seen[0]["url"] == "https://api.deepseek.com/chat/completions",
          str(seen[:1]))
    check("鉴权头带上了", seen and seen[0]["auth"] == "Bearer k")

    # 截断重试
    calls: list[int] = []

    def truncating(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(int(body.get("max_tokens") or 0))
        if len(calls) == 1:
            return json_response({"choices": [{"message": {"content": ""},
                                               "finish_reason": "length"}]})
        return json_response(OPENAI_REPLY)

    cfg2 = {"providers": {"endpoints": [
        {"id": "c1", "provider": "openai", "base_url": "https://x/v1", "model": "m",
         "tier": "cloud", "api_key": "k", "max_tokens": 32, "timeout": 5}]}}
    router2 = make_router(truncating, cfg2)
    text2, _ = await router2.answer([{"role": "user", "content": "hi"}])
    check("被截断后翻倍重试并成功", text2 == "嗯。" and calls == [32, 64], str(calls))

    # 上限夹紧
    calls2: list[int] = []

    def truncating2(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls2.append(int(body.get("max_tokens") or 0))
        return json_response({"choices": [{"message": {"content": ""},
                                           "finish_reason": "length"}]})

    cfg3 = {"providers": {"endpoints": [
        {"id": "c1", "provider": "moonshot", "base_url": "https://api.moonshot.cn/v1",
         "model": "m", "tier": "cloud", "api_key": "k", "max_tokens": 5,
         "capabilities": {"max_tokens_limit": 8}, "timeout": 5}]}}
    router3 = make_router(truncating2, cfg3)
    await router3.answer([{"role": "user", "content": "hi"}])
    check("翻倍不会突破 max_tokens_limit", calls2 == [5, 8], str(calls2))

    # 全部失败 -> failed
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server on fire")

    router4 = make_router(boom, cfg)
    got4 = await router4.answer([{"role": "user", "content": "hi"}])
    check("全挂时返回 failed", got4 == (None, "failed"), str(got4))

    # 云端没 key -> no-key，且不继续往后试
    cfg5 = {"providers": {"endpoints": [
        {"id": "c1", "provider": "openai", "base_url": "https://x/v1", "model": "m",
         "tier": "cloud", "timeout": 5}]}}
    router5 = make_router(handler, cfg5)
    got5 = await router5.answer([{"role": "user", "content": "hi"}])
    check("云端没密钥时短路返回 no-key", got5 == (None, "no-key"), str(got5))


# ══════════════════════════════════════════════════════════════════
print("\n【P10】并发闸：concurrency 真的限流")
# ══════════════════════════════════════════════════════════════════


async def t_gate() -> None:
    state = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return json_response(OPENAI_REPLY)

    cfg = {"providers": {"endpoints": [
        {"id": "l1", "provider": "vllm", "base_url": "http://127.0.0.1:8000/v1",
         "model": "m", "tier": "local", "concurrency": 1, "timeout": 5}]}}
    router = make_router(handler, cfg)

    async def go():
        await asyncio.gather(*[router.answer([{"role": "user", "content": "hi"}])
                               for _ in range(3)])

    await go()
    check("concurrency=1 时峰值并发就是 1", state["peak"] == 1, f"peak={state['peak']}")


# ══════════════════════════════════════════════════════════════════
print("\n【P11】兼容面：包不依赖 bot / build_router 可用")
# ══════════════════════════════════════════════════════════════════


def t_isolation() -> None:
    src = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (BASE_DIR / "providers").rglob("*.py"))
    check("providers 包不引用 SD 生成器", "SDGEN" not in src)

    # 用 AST 查真实 import，别被文档里的字面量骗了
    import ast
    bad_import = []
    for p in (BASE_DIR / "providers").rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"), str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad_import += [(p.name, a.name) for a in node.names if a.name.split(".")[0] == "bot"]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "bot":
                bad_import.append((p.name, node.module or ""))
    check("providers 包不 import bot", not bad_import, str(bad_import))

    cfg = {"providers": {"endpoints": [
        {"id": "l1", "provider": "lmstudio", "base_url": "http://127.0.0.1:1234/v1",
         "model": "m", "tier": "local"}]}}
    router = build_router(cfg, base_dir=BASE_DIR, load_plugins=False)
    check("build_router 能建起来", isinstance(router, Router))
    missing = [n for n in ("deepseek", "moonshot", "zhipu", "groq", "vllm", "llamacpp",
                           "lmstudio", "openai")
               if not router.registry.get(n)]
    check("内置适配器与清单里的厂商都注册了", not missing, str(missing))
    pol = router.policy(cfg)
    check("policy 能算出来", pol.backend in ("auto", "cloud", "local", "off"), str(pol))
    snap = router.status(cfg)
    check("status 快照结构完整",
          {"ok", "endpoints", "policy", "chains"} <= set(snap), str(list(snap)))
    check("快照里不含明文密钥",
          "k" != (snap["endpoints"][0]["key_source"] if snap["endpoints"] else ""), "")

    notice = reg_notice_check()
    check("旧配置迁移提示会写进日志文案", notice, notice)


def reg_notice_check() -> str:
    reg = make_registry()
    out = reg.build_endpoints({
        "local": {"enable": True, "base_url": "http://127.0.0.1:1234/v1",
                  "model": "m", "vision": True},
        "cloud": {"enable": True, "base_url": "https://api.deepseek.com", "model": "m",
                  "api_key": "x", "vision": True},
    })
    hits = [n for n in out.notices if "providers 段" in n or "迁移" in n]
    return hits[0] if hits else ""


# ══════════════════════════════════════════════════════════════════
print("\n【P12】原生协议适配器：Anthropic / Gemini / Azure / Ollama")
# ══════════════════════════════════════════════════════════════════

SYS_MSG = {"role": "system", "content": "你是许杏玖。"}
USER_MSG = {"role": "user", "content": "在吗"}
IMG_MSG = {"role": "user", "content": [
    {"type": "text", "text": "看这个"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}


def t_anthropic() -> None:
    reg = make_registry()
    prov = reg.get("anthropic")
    ep = Endpoint(id="cl", provider="anthropic",
                  base_url="https://api.anthropic.com/v1", model="claude-sonnet-4",
                  api_key="sk-ant", tier="cloud", vision=True)
    req = prov.build_request(ep, [SYS_MSG, USER_MSG], stream=False, max_tokens=64,
                             temperature=1.7, extra=None)
    b = req.json_body
    check("Anthropic URL 走 /messages",
          req.url == "https://api.anthropic.com/v1/messages", req.url)
    check("system 提到顶层、不进 messages",
          b.get("system") == "你是许杏玖。"
          and all(m["role"] != "system" for m in b["messages"]), str(b.get("messages")))
    check("max_tokens 必填且存在", b.get("max_tokens") == 64, str(b))
    check("温度被夹到 1.0", b.get("temperature") == 1.0, str(b.get("temperature")))
    check("鉴权走 x-api-key", req.headers.get("x-api-key") == "sk-ant"
          and "Authorization" not in req.headers, str(req.headers))
    check("带 anthropic-version 头",
          req.headers.get("anthropic-version", "").startswith("2023-"),
          str(req.headers.get("anthropic-version")))

    req2 = prov.build_request(ep, [USER_MSG, {"role": "user", "content": "再来"}],
                              stream=False, max_tokens=8, temperature=0.5, extra=None)
    check("连续两条 user 被合并（Anthropic 要求交替）",
          len(req2.json_body["messages"]) == 1, str(req2.json_body["messages"]))

    req3 = prov.build_request(ep, [SYS_MSG, IMG_MSG], stream=False, max_tokens=8,
                              temperature=0.5, extra=None)
    blocks = req3.json_body["messages"][0]["content"]
    img = [x for x in blocks if x.get("type") == "image"]
    check("data URL 被转成 base64 source",
          img and img[0]["source"] == {"type": "base64", "media_type": "image/png",
                                       "data": "QUJD"}, str(img))

    res = prov.parse_response(ep, {
        "content": [{"type": "thinking", "thinking": "嗯……"},
                    {"type": "text", "text": "在。"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 2,
                  "cache_read_input_tokens": 8, "cache_creation_input_tokens": 4}})
    check("只拼 text block、丢掉 thinking", res.text == "在。", res.text)
    check("Anthropic 用量归一", res.usage == {"in": 12, "out": 2, "hit": 8, "miss": 4},
          str(res.usage))

    res2 = prov.parse_response(ep, {"content": [], "stop_reason": "max_tokens"})
    check("stop_reason=max_tokens 触发翻倍重试", prov.needs_truncation_retry(res2))


def t_gemini() -> None:
    reg = make_registry()
    prov = reg.get("gemini")
    ep = Endpoint(id="gm", provider="gemini",
                  base_url="https://generativelanguage.googleapis.com/v1beta",
                  model="gemini-2.5-flash", api_key="goog", tier="cloud", vision=True)
    req = prov.build_request(ep, [SYS_MSG, USER_MSG, IMG_MSG], stream=False,
                             max_tokens=128, temperature=0.7, extra=None)
    b = req.json_body
    check("Gemini URL 里带 model 与 :generateContent",
          req.url.endswith("/models/gemini-2.5-flash:generateContent"), req.url)
    check("role 用 model 不是 assistant", [c["role"] for c in b["contents"]] == ["user", "user"],
          str([c["role"] for c in b["contents"]]))
    check("system 走 system_instruction",
          b["system_instruction"]["parts"][0]["text"] == "你是许杏玖。", str(b.get("system_instruction")))
    check("参数在 generationConfig 里",
          b["generationConfig"]["maxOutputTokens"] == 128, str(b.get("generationConfig")))
    check("鉴权走 x-goog-api-key", req.headers.get("x-goog-api-key") == "goog", str(req.headers))
    parts = b["contents"][1]["parts"]
    check("图片转成 inline_data",
          any(p.get("inline_data", {}).get("data") == "QUJD" for p in parts), str(parts))

    res = prov.parse_response(ep, {
        "candidates": [{"content": {"parts": [{"text": "在。"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 3,
                          "cachedContentTokenCount": 15}})
    check("Gemini 文本解析", res.text == "在。", res.text)
    check("Gemini 用量归一", res.usage == {"in": 20, "out": 3, "hit": 15, "miss": 0},
          str(res.usage))
    res2 = prov.parse_response(ep, {"candidates": [{"content": {"parts": []},
                                                    "finishReason": "MAX_TOKENS"}]})
    check("MAX_TOKENS 触发重试", prov.needs_truncation_retry(res2))


def t_azure() -> None:
    reg = make_registry()
    prov = reg.get("azure")
    ep = Endpoint(id="az", provider="azure", base_url="https://myres.openai.azure.com",
                  model="gpt-4o", api_key="azkey", tier="cloud", vision=True,
                  deployment="my-deploy", api_version="2024-10-21")
    req = prov.build_request(ep, [USER_MSG], stream=False, max_tokens=32,
                             temperature=0.5, extra=None)
    check("Azure URL 带 deployment",
          req.url == "https://myres.openai.azure.com/openai/deployments/my-deploy/chat/completions",
          req.url)
    check("Azure 带 api-version 查询参",
          (req.params or {}).get("api-version") == "2024-10-21", str(req.params))
    check("Azure 用 api-key 头、不用 Bearer",
          req.headers.get("api-key") == "azkey" and "Authorization" not in req.headers,
          str(req.headers))
    ep2 = Endpoint(id="az2", provider="azure", base_url="https://r.openai.azure.com/openai",
                   model="m", api_key="k", tier="cloud", deployment="d")
    req2 = prov.build_request(ep2, [USER_MSG], stream=False, max_tokens=8,
                              temperature=0.5, extra=None)
    check("base_url 里多写的 /openai 被容错",
          req2.url == "https://r.openai.azure.com/openai/deployments/d/chat/completions",
          req2.url)
    check("没写 api_version 时有默认值",
          (req2.params or {}).get("api-version"), str(req2.params))


def t_ollama() -> None:
    reg = make_registry()
    prov = reg.get("ollama")
    ep = Endpoint(id="ol", provider="ollama", base_url="http://127.0.0.1:11434",
                  model="qwen2.5:7b", tier="local", vision=True)
    req = prov.build_request(ep, [SYS_MSG, IMG_MSG], stream=False, max_tokens=48,
                             temperature=0.9, extra=None)
    b = req.json_body
    check("Ollama 走 /api/chat", req.url == "http://127.0.0.1:11434/api/chat", req.url)
    check("输出上限写进 options.num_predict",
          (b.get("options") or {}).get("num_predict") == 48, str(b.get("options")))
    check("system 留在 messages 里（Ollama 支持）",
          b["messages"][0]["role"] == "system", str(b["messages"][0]))
    check("图片走 messages[].images（纯 base64、不带 data: 前缀）",
          b["messages"][1].get("images") == ["QUJD"], str(b["messages"][1]))
    check("本机端点 trust_env=False", ep.effective_trust_env is False)

    res = prov.parse_response(ep, {"message": {"role": "assistant", "content": "在。"},
                                   "done": True, "done_reason": "stop",
                                   "prompt_eval_count": 30, "eval_count": 5})
    check("Ollama 文本解析", res.text == "在。", res.text)
    check("Ollama 用量归一", res.usage == {"in": 30, "out": 5, "hit": 0, "miss": 0},
          str(res.usage))
    res2 = prov.parse_response(ep, {"message": {"content": ""}, "done_reason": "length"})
    check("done_reason=length 触发重试", prov.needs_truncation_retry(res2))


def t_cn_vendors() -> None:
    reg = make_registry()
    for name in ("ernie", "spark", "hunyuan", "baichuan", "stepfun"):
        if reg.get(name) is None:
            check(f"国内厂商 {name} 已注册", False, reg.summary())
            return
    check("国内厂商清单全部注册", True)
    ep = Endpoint(id="e", provider="ernie", base_url="https://qianfan.baidubce.com/v2",
                  model="ernie-4.5", api_key="k", tier="cloud")
    req = reg.get("ernie").build_request(ep, [USER_MSG], stream=False, max_tokens=16,
                                         temperature=0.5, extra=None)
    check("文心兼容端点 URL 正确",
          req.url == "https://qianfan.baidubce.com/v2/chat/completions", req.url)


# ══════════════════════════════════════════════════════════════════
print("\n【P13】插件体系：白名单 / 坏插件不崩 / 名字冲突")
# ══════════════════════════════════════════════════════════════════


def t_plugins() -> None:
    import tempfile
    from providers.plugins import discover

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="prov_plugin_"))
    (tmp / "good.py").write_text(
        "from providers.base import Provider, register_provider\n"
        "from providers.types import Capability, ChatResult, Request\n"
        "@register_provider('plug_ok')\n"
        "class P(Provider):\n"
        "    def default_capabilities(self): return Capability()\n"
        "    def build_request(self, ep, messages, *, stream, max_tokens, temperature, extra):\n"
        "        return Request(method='POST', url=ep.base_url, json_body={})\n"
        "    def parse_response(self, ep, data):\n"
        "        return ChatResult(text='x', endpoint_id=ep.id)\n", encoding="utf-8")
    (tmp / "broken.py").write_text("this is not python!!!\n", encoding="utf-8")
    (tmp / "clash.py").write_text(
        "from providers.base import Provider, register_provider\n"
        "from providers.types import Capability\n"
        "@register_provider('openai')\n"
        "class Q(Provider):\n"
        "    def default_capabilities(self): return Capability()\n"
        "    def build_request(self, *a, **k): raise NotImplementedError\n"
        "    def parse_response(self, *a, **k): raise NotImplementedError\n", encoding="utf-8")

    reg = make_registry()
    before = len(reg.names())

    rep = discover(tmp, enabled=[], allow_dir_scan=False, registry=reg)
    check("默认不扫目录：一个插件都不加载", len(reg.names()) == before, str(rep.warnings()))

    rep = discover(tmp, enabled=["good.py"], allow_dir_scan=False, registry=reg)
    check("白名单里的插件被加载", reg.get("plug_ok") is not None, str(rep.warnings()))
    check("加载记录里带 sha256 指纹",
          any("sha256:" in x for x in rep.loaded), str(rep.loaded))

    rep = discover(tmp, enabled=["broken.py"], allow_dir_scan=False, registry=reg)
    check("坏插件只记错误、不抛异常", rep.errors and not rep.loaded, str(rep.errors))

    rep = discover(tmp, enabled=["clash.py"], allow_dir_scan=False, registry=reg)
    check("撞名的插件被跳过而不是覆盖内置",
          rep.skipped and reg.get("openai").name == "openai", str(rep.skipped))

    rep = discover(tmp, enabled=["nope.py"], allow_dir_scan=False, registry=reg)
    check("白名单里写错文件名只报错", rep.errors, str(rep.errors))

    rep = discover(tmp, allow_dir_scan=True, registry=reg)
    check("开目录扫描时会把好插件也扫进来（坏的不影响）",
          reg.get("plug_ok") is not None and rep.errors, str(rep.errors))


# ══════════════════════════════════════════════════════════════════
print("\n【P14】重载：配置一改，端点/并发闸/密钥立刻跟上")
# ══════════════════════════════════════════════════════════════════


def t_reload() -> None:
    cfg = {"providers": {"endpoints": [
        {"id": "a", "provider": "openai", "base_url": "https://a/v1", "model": "m1",
         "tier": "cloud", "api_key": "k1", "concurrency": 1}]}}
    router = make_router(lambda r: json_response(OPENAI_REPLY), cfg)
    check("初始端点就位", [e.id for e in router.endpoints] == ["a"])
    check("初始信号量建好了", router.gate.sem(router.endpoints[0]) is not None)

    cfg2 = {"providers": {"endpoints": [
        {"id": "b", "provider": "openai", "base_url": "https://b/v1", "model": "m2",
         "tier": "cloud", "api_key": "k2"}]}}
    notices = router.reload(cfg2)
    check("reload 后端点换成新的", [e.id for e in router.endpoints] == ["b"], str(notices))
    check("消失端点的信号量被回收", "a" not in router.gate.snapshot())

    snap = router.status(cfg2)
    check("快照里的密钥只暴露来源、不暴露明文",
          snap["endpoints"][0]["key_source"] == "plain"
          and "k2" not in json.dumps(snap, ensure_ascii=False), str(snap["endpoints"][0]))


async def main() -> int:
    t_endpoints()
    t_urls()
    t_auth()
    t_body()
    t_parse()
    t_vision_placeholder()
    t_manifest()
    t_usage()
    await t_offline_call()
    await t_gate()
    t_isolation()
    t_anthropic()
    t_gemini()
    t_azure()
    t_ollama()
    t_cn_vendors()
    t_plugins()
    t_reload()
    print(f"\n{'=' * 46}\n通过 {ok} 项，失败 {fail} 项\n{'=' * 46}")
    return fail


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
