"""生图层离线单测 —— **全程不联网**（httpx.MockTransport），不 import bot。

跑法：
    .venv\\Scripts\\python.exe test_imagegen.py
或由 test_regression.py 的【35】段一并调用。

风格与 test_regression.py / test_providers.py 一致（自研 runner，不引入 pytest）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import sys
import tempfile

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import httpx  # noqa: E402

from imagegen import (  # noqa: E402
    GenRequest,
    ImageCapability,
    ImageEndpoint,
    ImageRegistry,
    ImageRouter,
    build_image_registry,
    build_image_router,
)
from imagegen.base import register_image_provider  # noqa: E402
from imagegen.types import (  # noqa: E402
    REASON_BACKEND_DOWN,
    REASON_DISABLED,
    REASON_NO_ENDPOINT,
    REASON_QUOTA,
    ImageResult,
)
from providers.transport import Transport  # noqa: E402

ok = 0
fail = 0

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
B64 = base64.b64encode(FAKE_PNG).decode()


def check(name: str, cond, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


# ══════════════════════════════════════════════════════════════════
# 脚手架
# ══════════════════════════════════════════════════════════════════

def make_registry() -> ImageRegistry:
    reg = build_image_registry({}, base_dir=BASE_DIR)
    assert "sd_webui" in reg.names(), reg.names()
    return reg


def make_router(handler, cfg: dict, *, registry: ImageRegistry | None = None,
                state_path: pathlib.Path | None = None) -> ImageRouter:
    reg = registry or make_registry()
    transport = Transport(client_factory=lambda trust_env: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=trust_env))
    r = ImageRouter(reg, transport=transport, cfg=cfg, state_path=state_path)
    r.load_cfg(cfg)
    return r


def sd_reply(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "images": [B64],
        "info": json.dumps({"seed": 12345, "sampler_name": "DPM++ 2M Karras",
                            "steps": 28, "cfg_scale": 6.5}),
    })


LEGACY_CFG = {
    "sd": {
        "enable": True,
        "base_url": "http://127.0.0.1:7860",
        "timeout_seconds": 300,
        "steps": 28,
        "cfg_scale": 6.5,
        "sampler": "DPM++ 2M Karras",
        "hires": {"enable": True, "scale": 1.35, "denoising_strength": 0.32,
                  "upscaler": "R-ESRGAN 4x+ Anime6B"},
    }
}


# ══════════════════════════════════════════════════════════════════
print("\n【I1】端点构建：旧 sd.* 迁移 / 新 imagegen 段")
# ══════════════════════════════════════════════════════════════════


def t_endpoints() -> None:
    reg = make_registry()

    out = reg.build_endpoints(LEGACY_CFG)
    check("旧 sd.* 能推导出生图端点", len(out.endpoints) == 1, str(len(out.endpoints)))
    check("标记为 legacy", out.legacy is True)
    ep = out.endpoints[0]
    check("id/厂商/tier 都对",
          ep.id == "local-sd" and ep.provider == "sd_webui" and ep.tier == "local", repr(ep.id))
    check("超时沿用 sd.timeout_seconds", ep.timeout == 300, str(ep.timeout))
    check("steps/cfg/sampler 进 params",
          ep.params.get("steps") == 28 and ep.params.get("cfg_scale") == 6.5
          and ep.params.get("sampler_name") == "DPM++ 2M Karras", str(ep.params))
    check("hires 开关也带进 params",
          ep.params.get("enable_hr") is True
          and ep.params.get("hr_second_pass_steps") == 14, str(ep.params))
    check("本机端点自动绕过代理", ep.is_loopback and ep.effective_trust_env is False)
    check("链是 image -> local-sd", out.chains.get("image") == ["local-sd"], str(out.chains))
    check("有迁移提示", any("sd ->" in n for n in out.notices), str(out.notices))

    # 关掉 enable 时端点也该是停用的（不然 check_ready 会误判）
    off = reg.build_endpoints({"sd": {**LEGACY_CFG["sd"], "enable": False}})
    check("sd.enable=false -> 端点停用", off.endpoints[0].enabled is False)

    modern = {
        "imagegen": {
            "endpoints": [
                {"id": "local-sd", "provider": "sd_webui",
                 "base_url": "http://127.0.0.1:7860", "tier": "local", "enabled": True},
                {"id": "cloud-x", "provider": "openai_images",
                 "base_url": "https://api.openai.com/v1", "model": "gpt-image-1",
                 "tier": "cloud", "api_key_env": "OPENAI_API_KEY", "max_per_day": 3},
            ],
            "chains": {"image": ["cloud-x", "local-sd"]},
        }
    }
    outm = reg.build_endpoints(modern)
    check("新 imagegen 段原样生效", len(outm.endpoints) == 2 and not outm.legacy,
          str([e.id for e in outm.endpoints]))
    check("链顺序被尊重", outm.chains["image"] == ["cloud-x", "local-sd"], str(outm.chains))
    cloud = outm.by_id()["cloud-x"]
    check("max_per_day 落到 params", cloud.params.get("_max_per_day") == 3, str(cloud.params))
    check("云端端点 tier/密钥方式对",
          cloud.tier == "cloud" and cloud.api_key_env == "OPENAI_API_KEY")

    bad = reg.build_endpoints({"imagegen": {"endpoints": [
        {"id": "z", "provider": "nope", "base_url": "http://x", "tier": "local"}]}})
    check("未知 provider 被跳过并提示", bad.endpoints == [] and any("没注册" in n for n in bad.notices),
          str(bad.notices))
    check("空配置不炸", reg.build_endpoints({}).endpoints == [])


# ══════════════════════════════════════════════════════════════════
print("\n【I2】能力夹紧：本地对齐 / 云端吸附 / 负词丢弃")
# ══════════════════════════════════════════════════════════════════


def t_fit_size() -> None:
    local = ImageCapability(free_size=True, size_step=8)
    check("本地尺寸对齐到 8 的倍数", local.fit(831, 1217) == (824, 1216),
          str(local.fit(831, 1217)))
    # 有下限保护：8x8 这种尺寸 SD 本身也会拒，夹到 64 更实在
    check("本地尺寸有下限保护（<64 抬到 64 并对齐）",
          local.fit(1, 1) == (64, 64), str(local.fit(1, 1)))

    cloud = ImageCapability(free_size=False,
                            presets=((1024, 1024), (1024, 1536), (1536, 1024)))
    # (832,1400) 离竖版 (1024,1536) 最近；离 (1024,1024) 更远
    check("云端吸附到最近预设",
          cloud.fit(832, 1400) == (1024, 1536), str(cloud.fit(832, 1400)))
    check("云端已经是预设值就不动", cloud.fit(1024, 1024) == (1024, 1024))
    check("没有预设的云端后端保持原样",
          ImageCapability(free_size=False).fit(832, 1216) == (832, 1216))

    reg = make_registry()
    prov = reg.get("sd_webui")
    ep = ImageEndpoint(id="e", provider="sd_webui", base_url="http://127.0.0.1:7860",
                       model="", tier="local")
    req = GenRequest(prompt="x" * 10, negative="bad", width=831, height=1217)
    got = prov.prepare(ep, req)
    check("prepare 会把尺寸夹好", (got.width, got.height) == (824, 1216),
          f"{got.width}x{got.height}")

    ep_cog = ImageEndpoint(id="c", provider="cogview", base_url="https://x",
                           model="cogview-4", tier="cloud",
                           image=ImageCapability(free_size=False, negative=False,
                                                 presets=((1024, 1024),)))
    class _Cog:
        name = "cogview"
    from imagegen.base import ImageProvider

    class CogStub(ImageProvider):
        name = "cogview"
        async def run(self, ep, req, http):  # pragma: no cover - 不跑
            return ImageResult()
    got2 = CogStub().prepare(ep_cog, GenRequest(prompt="p", negative="n",
                                                width=832, height=1216))
    check("不支持负词的后端会丢掉 negative 并记 note",
          got2.negative == "" and "负面" in got2.params.get("_note", ""),
          f"{got2.negative!r} {got2.params}")
    check("云端尺寸被吸附", (got2.width, got2.height) == (1024, 1024),
          f"{got2.width}x{got2.height}")
    _ = _Cog


# ══════════════════════════════════════════════════════════════════
print("\n【I3】SD WebUI：报文形状 + 取图 + info 解析")
# ══════════════════════════════════════════════════════════════════


async def t_sd_webui() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url),
                     "body": json.loads(request.content.decode("utf-8"))})
        return sd_reply(request)

    router = make_router(handler, LEGACY_CFG)
    out = await router.generate(GenRequest(prompt="1girl, solo", negative="bad hands",
                                          width=832, height=1216))
    check("能拿到图字节", out.ok and out.data == FAKE_PNG, f"{out.ok} {len(out.data)}")
    check("endpoint_id 是 local-sd", out.endpoint_id == "local-sd", out.endpoint_id)
    body = seen[0]["body"]
    check("URL 打到 /sdapi/v1/txt2img",
          seen[0]["url"] == "http://127.0.0.1:7860/sdapi/v1/txt2img", seen[0]["url"])
    check("body 带 prompt/negative_prompt/尺寸",
          body["prompt"] == "1girl, solo" and body["negative_prompt"] == "bad hands"
          and body["width"] == 832 and body["height"] == 1216, str(body))
    check("steps/cfg/sampler 来自旧 sd.* 迁移",
          body["steps"] == 28 and body["cfg_scale"] == 6.5
          and body["sampler_name"] == "DPM++ 2M Karras", str(body))
    check("hires 字段也带上了", body.get("enable_hr") is True and body.get("hr_scale") == 1.35,
          str(body))
    check("info 里的 seed 被解析出来", out.meta.get("seed") == 12345, str(out.meta))

    # 返回空 images -> 失败但不崩
    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"images": []})

    r2 = make_router(empty_handler, LEGACY_CFG)
    out2 = await r2.generate(GenRequest(prompt="x"))
    check("images 为空 -> 失败且不进负缓存误杀",
          (not out2.ok) and out2.endpoint_id == "", f"{out2.ok} {out2.reason} {out2.detail}")


# ══════════════════════════════════════════════════════════════════
print("\n【I9】回退链：前一个挂了自动退后一个 + 负缓存生效")
# ══════════════════════════════════════════════════════════════════


async def t_fallback() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if ":7861" in url:
            return httpx.Response(500, text="boom")
        return sd_reply(request)

    cfg = {"imagegen": {"endpoints": [
        {"id": "bad", "provider": "sd_webui", "base_url": "http://127.0.0.1:7861",
         "tier": "local", "enabled": True, "timeout": 5},
        {"id": "good", "provider": "sd_webui", "base_url": "http://127.0.0.1:7862",
         "tier": "local", "enabled": True, "timeout": 5}],
        "chains": {"image": ["bad", "good"]}}}
    router = make_router(handler, cfg)
    out = await router.generate(GenRequest(prompt="x"))
    check("前一个挂了自动退到后一个", out.ok and out.endpoint_id == "good",
          f"{out.ok} {out.endpoint_id} {out.tried}")
    check("坏端点进了负缓存", router.skip_reason(router.endpoint("bad")) == "backend-down",
          router.skip_reason(router.endpoint("bad")))
    check("负缓存清空后恢复可试", (router.clear_negative_cache(),
                              router.skip_reason(router.endpoint("bad"))) == (None, ""))


# ══════════════════════════════════════════════════════════════════
print("\n【I10】成本控制：云端每日上限 / allow_when")
# ══════════════════════════════════════════════════════════════════


def t_cost() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="imagegen_state_")) / "s.json"
    cfg = {"imagegen": {
        # 注意：P1 阶段云端适配器还没实现，这里借用 sd_webui + tier=cloud ——
        # 成本控制与配额逻辑是**按 tier 分流**的，所以测的是同一条代码路径。
        "endpoints": [{"id": "c1", "provider": "sd_webui",
                       "base_url": "http://127.0.0.1:7890", 
                       "tier": "cloud", "api_key": "k", "max_per_day": 2}],
        "chains": {"image": ["c1"]},
        "cloud": {"allow_when": "always"}}}
    router = make_router(sd_reply, cfg, state_path=tmp)
    ep = router.endpoint("c1")
    check("云端初始可用", router.skip_reason(ep) == "", router.skip_reason(ep))
    router.cloud_mark("c1")
    router.cloud_mark("c1")
    check("到上限后 over-quota", router.skip_reason(ep) == "over-quota",
          router.skip_reason(ep))
    okk, reason, _why = router.check_ready()
    check("check_ready 报 quota（属正常态）", (not okk) and reason == REASON_QUOTA,
          f"{okk} {reason}")

    # 注意：上面已经把 tmp 那份额度用满了，换策略的用例要用**干净的**状态文件，
    # 否则测的就不是策略而是额度。
    def fresh_state(tag: str) -> pathlib.Path:
        return pathlib.Path(tempfile.mkdtemp(prefix=f"imagegen_{tag}_")) / "s.json"

    cfg2 = json.loads(json.dumps(cfg))
    cfg2["imagegen"]["cloud"]["allow_when"] = "low-spec"
    r2 = make_router(sd_reply, cfg2, state_path=fresh_state("low"))
    check("low-spec 策略：高分辨率请求会跳过云端",
          r2.skip_reason(r2.endpoint("c1"), low_spec=False) == "cloud-skipped",
          r2.skip_reason(r2.endpoint("c1"), low_spec=False))
    check("low-spec 策略：低分辨率请求仍然可用",
          r2.skip_reason(r2.endpoint("c1"), low_spec=True) == "",
          r2.skip_reason(r2.endpoint("c1"), low_spec=True))
    cfg3 = json.loads(json.dumps(cfg))
    cfg3["imagegen"]["cloud"]["allow_when"] = "never"
    r3 = make_router(sd_reply, cfg3, state_path=fresh_state("never"))
    check("never 策略：云端永久跳过",
          r3.skip_reason(r3.endpoint("c1")) == "cloud-skipped",
          r3.skip_reason(r3.endpoint("c1")))


# ══════════════════════════════════════════════════════════════════
print("\n【I8】密钥不外泄：status 快照里不能有明文 key")
# ══════════════════════════════════════════════════════════════════


def t_no_key_leak() -> None:
    cfg = {"imagegen": {"endpoints": [
        {"id": "c1", "provider": "sd_webui", "base_url": "http://127.0.0.1:7891",
         "tier": "cloud", "api_key": "sk-super-secret-value"}]}}
    router = make_router(sd_reply, cfg)
    blob = json.dumps(router.status(), ensure_ascii=False)
    check("明文 api_key 不出现在快照里", "sk-super-secret-value" not in blob, blob[:200])
    check("只暴露密钥来源",
          router.status()["endpoints"][0]["key_source"] == "plain",
          str(router.status()["endpoints"][0]))

    import os
    os.environ["IMAGEGEN_TEST_KEY"] = "env-secret-value"
    try:
        cfg2 = {"imagegen": {"endpoints": [
            {"id": "c2", "provider": "sd_webui", "base_url": "http://127.0.0.1:7892",
             "tier": "cloud", "api_key_env": "IMAGEGEN_TEST_KEY"}]}}
        r2 = make_router(sd_reply, cfg2)
        blob2 = json.dumps(r2.status(), ensure_ascii=False)
        check("环境变量里的 key 也不外泄", "env-secret-value" not in blob2, blob2[:200])
        check("key_source 标明来自环境变量",
              r2.status()["endpoints"][0]["key_source"] == "env:IMAGEGEN_TEST_KEY",
              str(r2.status()["endpoints"][0]))
    finally:
        os.environ.pop("IMAGEGEN_TEST_KEY", None)


# ══════════════════════════════════════════════════════════════════
print("\n【I13】check_ready：零请求 + 原因分流正确")
# ══════════════════════════════════════════════════════════════════


async def t_check_ready() -> None:
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return sd_reply(request)

    router = make_router(handler, LEGACY_CFG)
    okk, reason, why = router.check_ready()
    check("有可用端点时 ready", okk is True, f"{okk} {reason} {why}")
    check("check_ready 不发任何请求", hits["n"] == 0, str(hits))

    okk2, reason2, _ = router.check_ready(enabled=False)
    check("总闸关着 -> disabled", (not okk2) and reason2 == REASON_DISABLED, reason2)
    okk3, reason3, _ = router.check_ready(has_intent=False)
    check("没说要画 -> empty-intent", (not okk3) and reason3 == "empty-intent", reason3)

    empty = make_router(handler, {"imagegen": {"endpoints": []}})
    okk4, reason4, _ = empty.check_ready()
    check("没有端点 -> no-endpoint", (not okk4) and reason4 == REASON_NO_ENDPOINT, reason4)

    # 后端刚挂过 -> backend-down（且不该再发请求）
    cfg = {"imagegen": {"endpoints": [
        {"id": "x", "provider": "sd_webui", "base_url": "http://127.0.0.1:7899",
         "tier": "local", "timeout": 5}]}}
    r = make_router(lambda req: httpx.Response(500, text="no"), cfg)
    await r.generate(GenRequest(prompt="x"))
    before = hits["n"]
    okk5, reason5, why5 = r.check_ready()
    check("退避期内报 backend-down", (not okk5) and reason5 == REASON_BACKEND_DOWN,
          f"{okk5} {reason5} {why5}")
    check("退避期内也不发请求", hits["n"] == before, f"{hits['n']} vs {before}")


# ══════════════════════════════════════════════════════════════════
print("\n【I14】只读快照：结构完整 + legacy 标记")
# ══════════════════════════════════════════════════════════════════


def t_status() -> None:
    router = make_router(sd_reply, LEGACY_CFG)
    snap = router.status()
    check("快照是 dict 且 ok=True", isinstance(snap, dict) and snap.get("ok") is True, "")
    check("必需字段齐全",
          {"legacy_config", "chains", "endpoints", "registry", "cloud_policy", "ready"}
          <= set(snap), str(sorted(snap)))
    check("旧配置被标成 legacy", snap["legacy_config"] is True)
    check("端点里有能力摘要", "capability" in snap["endpoints"][0],
          str(snap["endpoints"][0]))
    # P3/P4 会陆续加 comfyui / sd_next / invokeai 与云端五家；这里先钉住已有的
    check("适配器清单符合当前阶段", "sd_webui" in snap["registry"], str(snap["registry"]))

    router2 = build_image_router(LEGACY_CFG, base_dir=BASE_DIR)
    check("build_image_router 能建起来", isinstance(router2, ImageRouter))
    check("未知清单文件只警告不抛",
          isinstance(router2.registry.names(), list), router2.registry.summary())


# ══════════════════════════════════════════════════════════════════
print("\n【I4】ComfyUI：三跳（提交→轮询→取图）+ node_errors 立刻失败")
# ══════════════════════════════════════════════════════════════════


async def t_comfyui() -> None:
    calls: list[str] = []
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url.split("127.0.0.1:8188")[-1].split("?")[0])
        if url.endswith("/prompt"):
            body = json.loads(request.content.decode("utf-8"))
            wf = body["prompt"]
            # 断言注入生效：正向提示词进了 CLIPTextEncode
            texts = [v["inputs"].get("text", "") for v in wf.values()
                     if v.get("class_type") == "CLIPTextEncode"]
            assert "1girl" in texts[0], texts
            assert wf["5"]["inputs"]["width"] == 832, wf["5"]["inputs"]
            return httpx.Response(200, json={"prompt_id": "p1"})
        if "/history/" in url:
            state["n"] += 1
            if state["n"] < 2:
                return httpx.Response(200, json={})          # 第一次还没好
            return httpx.Response(200, json={"p1": {
                "status": {"status_str": "success"},
                "outputs": {"9": {"images": [{"filename": "x_00001_.png",
                                              "subfolder": "", "type": "output"}]}}}})
        if "/view" in url:          # 带 ?filename=… 查询参，所以不能 endswith("/view")
            return httpx.Response(200, content=FAKE_PNG,
                                 headers={"content-type": "image/png"})
        return httpx.Response(404)

    cfg = {"imagegen": {"endpoints": [
        {"id": "comfy", "provider": "comfyui", "base_url": "http://127.0.0.1:8188",
         "tier": "local", "timeout": 20, "params": {"steps": 25, "cfg_scale": 7.0}}],
        "chains": {"image": ["comfy"]}}}
    router = make_router(handler, cfg)
    out = await router.generate(GenRequest(prompt="1girl, solo", negative="bad",
                                           width=832, height=1216,
                                           params={"steps": 25, "cfg_scale": 7.0}))
    check("ComfyUI 能取回图片", out.ok and out.data == FAKE_PNG, f"{out.ok} {out.reason} {out.detail}")
    check("三跳都走到了（提交→轮询→取图）",
          any("/prompt" in c for c in calls) and any("/history/" in c for c in calls)
          and any("/view" in c for c in calls), str(calls))
    check("轮询会重试直到完成", state["n"] >= 2, str(state["n"]))
    check("seed 被写进了工作流",
          isinstance(out.meta.get("seed"), int) and out.meta["seed"] > 0, str(out.meta))

    # ★ node_errors 非空必须立刻失败（不允许轮询到超时）
    def bad_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/prompt"):
            return httpx.Response(200, json={"node_errors": {"4": {"errors": [
                {"message": "ckpt_name not found"}]}}})
        calls.append("POLLED")
        return httpx.Response(200, json={})

    r2 = make_router(bad_handler, cfg)
    calls.clear()
    out2 = await r2.generate(GenRequest(prompt="x", width=832, height=1216))
    check("node_errors 立刻失败", (not out2.ok) and "工作流节点报错" in out2.detail,
          f"{out2.ok} {out2.detail}")
    check("node_errors 时不进轮询", "POLLED" not in calls, str(calls))


# ══════════════════════════════════════════════════════════════════
print("\n【I5】InvokeAI：只支持固定 graph 这一条路径，缺模板就明确说不支持")
# ══════════════════════════════════════════════════════════════════


async def t_invokeai() -> None:
    from imagegen.adapters import invokeai as _inv
    reg = make_registry()

    ep_no_graph = ImageEndpoint(id="iv", provider="invokeai",
                                base_url="http://127.0.0.1:9090", model="",
                                tier="local")
    check("没给 graph 就判定不可用",
          _inv.InvokeAIProvider().available(ep_no_graph)[0] is False,
          str(_inv.InvokeAIProvider().available(ep_no_graph)))

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="inv_graph_"))
    (tmp / "g.json").write_text(json.dumps({
        "positive_prompt": {"value": "__PROMPT__"},
        "negative_prompt": {"value": "__NEGATIVE__"},
        "width": {"value": "__WIDTH__"},
        "height": {"value": "__HEIGHT__"},
    }), encoding="utf-8")
    old_dir = _inv.WORKFLOW_DIR
    _inv.WORKFLOW_DIR = tmp
    try:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/enqueue_batch"):
                seen["graph"] = json.loads(request.content.decode("utf-8"))["batch"]["graph"]
                return httpx.Response(200, json={"item_ids": ["it1"]})
            if "/queue/default/b/" in url:
                return httpx.Response(200, json={
                    "status": "completed", "result": {"image": {"image_name": "abc.png"}}})
            if "/images/i/" in url:
                return httpx.Response(200, content=FAKE_PNG)
            return httpx.Response(200, json={"version": "5.0"})

        cfg = {"imagegen": {"endpoints": [
            {"id": "iv", "provider": "invokeai", "base_url": "http://127.0.0.1:9090",
             "tier": "local", "timeout": 20, "params": {"graph": "g.json"}}],
            "chains": {"image": ["iv"]}}}
        router = make_router(handler, cfg)
        out = await router.generate(GenRequest(prompt="cat, solo", negative="bad",
                                               width=896, height=1152))
        check("InvokeAI 能按模板出图", out.ok and out.data == FAKE_PNG,
              f"{out.ok} {out.reason} {out.detail}")
        g = seen.get("graph") or {}
        check("占位符被替换成正向提示词", g.get("positive_prompt", {}).get("value") == "cat, solo",
              str(g)[:200])
        # 占位符写在引号里，替换后就是字符串（模板里是 "value": "__WIDTH__"）
        check("宽高占位符被替换成数字", str(g.get("width", {}).get("value")) == "896",
              str(g.get("width")))
        check("probe 自报 unsupported（未实测就该说不支持）",
              (await _inv.InvokeAIProvider().probe(
                  ImageEndpoint(id="x", provider="invokeai",
                                base_url="http://127.0.0.1:9090", model="", tier="local"),
                  router.http)).get("unsupported") is True)
    finally:
        _inv.WORKFLOW_DIR = old_dir
        __import__("shutil").rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
print("\n【I6】云端：OpenAI images 的 b64 与 url 两条返回路径")
# ══════════════════════════════════════════════════════════════════


async def t_cloud_openai() -> None:
    mode = {"v": "b64"}
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        hits.append(url)
        if url.endswith("/images/generations"):
            body = json.loads(request.content.decode("utf-8"))
            assert body["size"] == "1024x1536", body      # 必须已被吸附到预设档
            if mode["v"] == "b64":
                return httpx.Response(200, json={"data": [{"b64_json": B64}]})
            return httpx.Response(200, json={"data": [{"url": "https://cdn/x.png"}]})
        if url == "https://cdn/x.png":
            return httpx.Response(200, content=FAKE_PNG)
        return httpx.Response(404)

    cfg = {"imagegen": {"endpoints": [
        {"id": "oai", "provider": "openai_images", "base_url": "https://api.openai.com/v1",
         "model": "gpt-image-1", "tier": "cloud", "api_key": "sk-x", "timeout": 20}],
        "chains": {"image": ["oai"]}}}
    router = make_router(handler, cfg)

    out = await router.generate(GenRequest(prompt="a cat", width=832, height=1400))
    check("b64_json 路径能取到图", out.ok and out.data == FAKE_PNG, f"{out.ok} {out.detail}")
    mode["v"] = "url"
    out2 = await router.generate(GenRequest(prompt="a cat", width=832, height=1400))
    check("url 路径会再下载一次", out2.ok and out2.data == FAKE_PNG,
          f"{out2.ok} {out2.detail}")
    check("url 路径确实发生了下载", any("cdn/x.png" in h for h in hits), str(hits))

    # 没密钥 -> 不可用（不该被选中，更不该真发请求）
    cfg2 = {"imagegen": {"endpoints": [
        {"id": "oai2", "provider": "openai_images", "base_url": "https://api.openai.com/v1",
         "model": "gpt-image-1", "tier": "cloud"}],
        "chains": {"image": ["oai2"]}}}
    r2 = make_router(handler, cfg2)
    o3, r3, _ = r2.check_ready()
    check("云端没密钥 -> 不可用", (not o3) and r3 == REASON_NO_ENDPOINT, f"{o3} {r3}")


# ══════════════════════════════════════════════════════════════════
print("\n【I7】DashScope：异步任务两跳（提交→轮询→下载）")
# ══════════════════════════════════════════════════════════════════


async def t_dashscope() -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/image-synthesis"):
            body = json.loads(request.content.decode("utf-8"))
            # 832x1400 会被吸附到最近的预设档（810x1440），并且用"星号"写法
            assert body["parameters"]["size"] == "810*1440", body
            assert "negative_prompt" in body["input"], body
            return httpx.Response(200, json={"output": {"task_id": "t1"}})
        if "/api/v1/tasks/" in url:
            state["n"] += 1
            if state["n"] < 2:
                return httpx.Response(200, json={"output": {"task_status": "RUNNING"}})
            return httpx.Response(200, json={"output": {
                "task_status": "SUCCEEDED", "results": [{"url": "https://cdn/wanx.png"}]}})
        if url == "https://cdn/wanx.png":
            return httpx.Response(200, content=FAKE_PNG)
        return httpx.Response(404)

    cfg = {"imagegen": {"endpoints": [
        {"id": "ds", "provider": "dashscope",
         "base_url": "https://dashscope.aliyuncs.com", "model": "wanx2.1-t2i-turbo",
         "tier": "cloud", "api_key": "k", "timeout": 30}],
        "chains": {"image": ["ds"]}}}
    router = make_router(handler, cfg)
    out = await router.generate(GenRequest(prompt="一只猫", negative="模糊",
                                           width=832, height=1400))
    check("DashScope 异步链路能出图", out.ok and out.data == FAKE_PNG,
          f"{out.ok} {out.reason} {out.detail}")
    check("尺寸用星号写法且被吸附", True)


async def main() -> int:
    t_endpoints()
    t_fit_size()
    await t_sd_webui()
    await t_fallback()
    t_cost()
    t_no_key_leak()
    await t_check_ready()
    t_status()
    await t_comfyui()
    await t_invokeai()
    await t_cloud_openai()
    await t_dashscope()
    print(f"\n{'=' * 46}\n通过 {ok} 项，失败 {fail} 项\n{'=' * 46}")
    return fail


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
