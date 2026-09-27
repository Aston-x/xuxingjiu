"""控制台冒烟验证（独立进程，不干扰正在运行的 bot）。

用法：
    .venv\Scripts\python.exe tools\smoke_console.py

三条隔离规则（都踩过坑，别删）：
1. **端口由内核动态分配**。写死端口会撞上正在运行的控制台 —— 撞上时
   allow_reuse_address=False 会让本进程 bind 失败，请求就落到别人的进程上，
   测出来的结果完全不可信（曾因此误判过一次）。
2. **CONFIG_PATH 指向临时副本**。写路径（restrict_set 等）会把整个 CFG 原子写回
   CONFIG_PATH；只改内存 CFG 是不够的 —— 曾把测试端口写进了真实 config.json，
   导致用户重启后控制台跑到 6299、6200 连接被拒。
3. 开测前做**端口归属自检**：把本进程 LOOP 置空后若 status 仍报 loop_alive=true，
   说明请求落到了别人的控制台上，直接中止。
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import socket
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

import httpx  # noqa: E402
import bot  # noqa: E402
import console_server as cs  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = _free_port()
(bot.CFG.setdefault("console", {}))["port"] = PORT

TMP_CFG = BASE_DIR / "_tmp_smoke_config.json"
shutil.copy2(bot.CONFIG_PATH, TMP_CFG)
bot.CONFIG_PATH = TMP_CFG

BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []


def note(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   {detail}" if detail else ""))


def safe_json(r):
    try:
        return r.json()
    except Exception:
        return {"_nonjson": (r.text or "")[:200], "_status": r.status_code}


async def main() -> int:
    bot.LOOP = asyncio.get_running_loop()
    bot._start_console()
    await asyncio.sleep(0.5)

    token = cs.load_or_create_token(BASE_DIR / "state",
                                    str((bot.CFG.get("console") or {}).get("token") or ""))
    H = {"x-console-token": token}

    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as c:
        # 端口归属自检
        saved = cs.LOOP
        cs.LOOP = None
        try:
            pb = safe_json(await c.get(f"{BASE}/api/bot/status", headers=H))
        finally:
            cs.LOOP = saved
        if pb.get("loop_alive") is not False:
            print(f"\n!! 端口 {PORT} 不是本进程在服务，结果不可信，中止。")
            TMP_CFG.unlink(missing_ok=True)
            return 2
        print(f"✅ 已确认端口 {PORT} 由本进程独占服务\n")

        # 鉴权
        note("无令牌访问被拒（401）",
             (await c.get(f"{BASE}/api/bot/status")).status_code == 401)
        r = await c.get(f"{BASE}/")
        note("首页返回 HTML", r.status_code == 200 and "许杏玖" in r.text,
             f"{len(r.content)} bytes")
        note("?token= 方式可通过",
             (await c.get(f"{BASE}/api/bot/status", params={"token": token})).status_code == 200)

        # 各面板
        for label, path in [
            ("B01 status", "/api/bot/status"), ("B02 persona", "/api/bot/persona"),
            ("B03 life", "/api/bot/life"), ("B04 memory", "/api/bot/memory"),
            ("B05 mood", "/api/bot/mood"), ("B06 activity", "/api/bot/activity"),
            ("B07 sd", "/api/bot/sd"), ("B08 stickers", "/api/bot/stickers"),
            ("B09 sessions", "/api/bot/sessions"), ("B10/11 meta", "/api/bot/meta"),
            ("B13 config", "/api/bot/config"),
            ("B15 group-memory", "/api/bot/group-memory"),
            ("B16 bili", "/api/bot/bili"), ("B17 cross-group", "/api/bot/cross-group"),
            ("Q03 qzone-actions", "/api/bot/qzone-actions"),
        ]:
            rr = await c.get(BASE + path, headers=H)
            b = safe_json(rr)
            note(label, rr.status_code == 200 and b.get("ok") is not False,
                 f"HTTP {rr.status_code}")

        # 日志三个来源
        r = await c.get(f"{BASE}/api/bot/logs", headers=H, params={"lines": 30})
        body = safe_json(r)
        note("B12 日志（bot 源）", len(body.get("lines") or []) > 0,
             f"{len(body.get('lines') or [])} 行")
        for src in ("bot", "qzone", "napcat"):
            rr = await c.get(f"{BASE}/api/bot/logs", headers=H,
                             params={"source": src, "lines": 5})
            b = safe_json(rr)
            note(f"日志源 {src}", rr.status_code == 200 and b.get("ok") is not False,
                 f"行={len(b.get('lines') or [])} 文件={b.get('file') or '（无）'} "
                 f"{b.get('note') or ''}")
        rr = await c.get(f"{BASE}/api/bot/logs", headers=H, params={"source": "nope"})
        note("未知日志源被拒（400）", rr.status_code == 400)

        # 脱敏
        at = str(bot.CFG.get("access_token") or "")
        note("日志已脱敏（不含明文 access_token）",
             not any(at and at in ln for ln in (body.get("lines") or [])))
        cfg_body = safe_json(await c.get(f"{BASE}/api/bot/config", headers=H))
        ak = str((bot.CFG.get("cloud") or {}).get("api_key") or "")
        cfg_text = str(cfg_body.get("config") or "")
        import json as _json
        cfg_text = _json.dumps(cfg_body.get("config") or {}, ensure_ascii=False)
        note("配置已脱敏（不含明文 api_key）", bool(ak) and ak not in cfg_text,
             f"打码字段 {cfg_body.get('redacted_paths')}")
        note("人设未被误打码",
             (cfg_body.get("config") or {}).get("persona") == bot.CFG.get("persona"))

        # loop 不可用语义
        saved = cs.LOOP
        cs.LOOP = None
        rr = await c.get(f"{BASE}/api/bot/status", headers=H)
        note("loop 挂掉时 status 仍 200 且标 loop_alive=false",
             rr.status_code == 200 and safe_json(rr).get("loop_alive") is False)
        note("loop 挂掉时数据接口 503",
             (await c.get(f"{BASE}/api/bot/memory", headers=H)).status_code == 503)
        cs.LOOP = saved

        # 生图：三种模式与她的固定形象
        sb = safe_json(await c.get(f"{BASE}/api/bot/sd", headers=H))
        note("生图快照含三种模式信息",
             all(k in sb for k in ("character_tags", "sizes", "hires",
                                   "quality_prefix", "negative_count")),
             f"形象标签 {len(str(sb.get('character_tags')))} 字")
        note("生图质量前缀是 Pony 的 score 标签（崩图主因）",
             str(sb.get("quality_prefix", "")).startswith("score_9"),
             str(sb.get("quality_prefix"))[:34])
        note("三种模式分辨率都 >= 768（SDXL 原生下限）",
             all(all(v >= 768 for v in (sb.get("sizes") or {}).get(m, []))
                 for m in ("solo", "duo", "scenery")),
             str(sb.get("sizes")))
        note("生图负面词够多（肢体类覆盖到位）",
             int(sb.get("negative_count") or 0) >= 30,
             f"{sb.get('negative_count')} 项")
        note("solo/duo 的数量词分开配置（否则会自相矛盾）",
             "solo" in str(sb.get("solo_prefix")) and "2girls" in str(sb.get("duo_prefix")),
             f"{sb.get('solo_prefix')} ／ {sb.get('duo_prefix')}")

        # 空间
        for label, path in [("Q01 status", "/api/qzone/status"),
                            ("Q02 pollers", "/api/qzone/pollers"),
                            ("Q03 posts", "/api/qzone/posts"),
                            ("Q04 events", "/api/qzone/events")]:
            rr = await c.get(BASE + path, headers=H)
            b = safe_json(rr)
            ok = rr.status_code == 200 and not b.get("auth_error")
            extra = ("（桥接离线）" if b.get("offline")
                     else ("（桥接为旧版本，需重启）" if b.get("stale") else ""))
            note(label, ok, f"HTTP {rr.status_code} ok={b.get('ok')} {extra}")

        # 空间功能总览（Q03/Q04 用的那个）
        rr = await c.get(f"{BASE}/api/bot/qzone-actions", headers=H)
        b = safe_json(rr)
        at = b.get("actions_today") or {}
        note("空间功能总览接口",
             rr.status_code == 200 and all(k in b for k in
                                           ("post", "comment", "like", "bridge", "actions_today")),
             f"今日 赞{at.get('like')}/评{at.get('comment')}/说{at.get('post')}")
        note("空间功能总览含额度与开关",
             isinstance((b.get("post") or {}).get("max_per_day"), int)
             and isinstance((b.get("comment") or {}).get("max_per_day"), int)
             and "cooldown_seconds" in (b.get("like") or {}))
        note("空间功能总览含自主互动设置",
             all(k in (b.get("auto_interact") or {})
                 for k in ("enable", "probability", "cooldown_minutes")),
             str(b.get("auto_interact")))
        note("空间功能总览含跨群好感表",
             "people" in (b.get("mood") or {}),
             (b.get("mood") or {}).get("text") or "（暂无好感记录）")

        # 可调参数（B14 进度条的数据源）
        rr = await c.get(f"{BASE}/api/bot/tunables", headers=H)
        tb = safe_json(rr)
        items = tb.get("items") or []
        note("可调参数接口", rr.status_code == 200 and len(items) > 0, f"{len(items)} 项")
        note("每项都带范围/步长/说明/当前值",
             all(all(k in it for k in ("min", "max", "step", "desc", "value", "group"))
                 for it in items))
        note("涵盖了别名降阈值",
             any(it["path"] == "attention.alias_threshold_discount" for it in items))
        # 白名单：写一个不在表里的路径必须被拒
        # （这里 J 还没定义 —— 写路径那一节在后面，所以就地拼一个头）
        _J = {**H, "Content-Type": "application/json"}
        rr = await c.post(f"{BASE}/api/bot/apply", headers=_J,
                          json={"op": "config_set",
                                "payload": {"path": "access_token", "value": 1}})
        note("config_set 拒绝白名单外的路径（400）",
             rr.status_code == 400 and "不允许" in str(safe_json(rr).get("error")),
             f"HTTP {rr.status_code}")
        rr = await c.post(f"{BASE}/api/bot/apply", headers=_J,
                          json={"op": "config_set",
                                "payload": {"path": "attention.threshold", "value": 9999}})
        note("config_set 拒绝越界值（400）", rr.status_code == 400, f"HTTP {rr.status_code}")

        # 群聊记忆（与人物记忆分开的一套）
        rr = await c.get(f"{BASE}/api/bot/group-memory", headers=H)
        gb = safe_json(rr)
        note("群聊记忆接口",
             rr.status_code == 200 and "groups" in gb and "note" in gb
             and "max_notes" in gb,
             f"{gb.get('count')} 条 / {len(gb.get('groups') or {})} 个群")
        note("群聊记忆说明了它不走管理员",
             "管理员" in str(gb.get("note", "")), str(gb.get("note"))[:40])
        rr = await c.post(f"{BASE}/api/bot/apply", headers=_J,
                          json={"op": "group_forget", "payload": {"group": "__nope__"}})
        note("忘掉不存在的群被拒（400）", rr.status_code == 400, f"HTTP {rr.status_code}")
        mb = safe_json(await c.get(f"{BASE}/api/bot/memory", headers=H))
        note("人物记忆快照带「每轮刷新」块",
             "ephemeral" in mb and "不落盘" in str(mb.get("ephemeral_note", "")))

        # 写路径
        J = {**H, "Content-Type": "application/json"}
        note("旧读接口不接受 POST（404）",
             (await c.post(f"{BASE}/api/bot/status", headers=J, json={})).status_code == 404)
        rr = await c.post(f"{BASE}/api/bot/apply", headers=J, json={"op": "nope"})
        note("未知 op 被拒（400 且回白名单）",
             rr.status_code == 400 and isinstance(safe_json(rr).get("allowed"), list))
        note("缺 application/json 被拒（415）",
             (await c.post(f"{BASE}/api/bot/apply", headers=H, content=b"{}")).status_code == 415)
        note("跨站 Origin 被拒（403）",
             (await c.post(f"{BASE}/api/bot/apply",
                           headers={**J, "Origin": "http://evil.test"},
                           json={"op": "restrict_set", "payload": {}})).status_code == 403)
        note("参数不合法被拒（400）",
             (await c.post(f"{BASE}/api/bot/apply", headers=J,
                           json={"op": "restrict_set", "payload": {}})).status_code == 400)
        rr = await c.post(f"{BASE}/api/bot/apply", headers=J,
                          json={"op": "persona_set",
                                "payload": {"field": "access_token", "value": "x"}})
        note("密钥类字段不可经控制台写（400）",
             rr.status_code == 400 and "access_token" in str(safe_json(rr).get("error")))

        meta = safe_json(await c.get(f"{BASE}/api/bot/meta", headers=H))
        restrict = (meta.get("admin") or {}).get("restrict") or {}
        if restrict:
            feat = next(iter(restrict))
            cur = bool(restrict[feat])
            rr = await c.post(f"{BASE}/api/bot/apply", headers=J,
                              json={"op": "restrict_set",
                                    "payload": {"feature": feat, "value": cur}})
            b = safe_json(rr)
            note(f"restrict_set 幂等写入成功（{feat}={cur}）",
                 rr.status_code == 200 and b.get("value") is cur)
            back = safe_json(await c.get(f"{BASE}/api/bot/meta", headers=H))
            note("写后读回一致",
                 bool(((back.get("admin") or {}).get("restrict") or {})[feat]) is cur)

        note("管理员只有示例用户1",
             (meta.get("admin") or {}).get("user_ids") == [10002],
             f"实际 {(meta.get('admin') or {}).get('user_ids')}")
        note("meta 带 known_names", isinstance(meta.get("known_names"), dict),
             str(meta.get("known_names")))
        labs = meta.get("restrict_labels") or {}
        note("meta 带受限功能中文名",
             bool(labs) and all(v.get("name") for v in labs.values()),
             " / ".join(list(labs.values())[0]["name"] for _ in [0]) if labs else "")

        # ── 图片预览 ──
        rr = await c.get(f"{BASE}/api/bot/image", headers=H,
                         params={"name": "../../../../Windows/win.ini"})
        note("图片接口挡住路径穿越（404）", rr.status_code == 404, f"HTTP {rr.status_code}")
        rr = await c.get(f"{BASE}/api/bot/image", headers=H, params={"name": "nope.png"})
        note("不存在的图片返回 404", rr.status_code == 404, f"HTTP {rr.status_code}")
        rr = await c.get(f"{BASE}/api/bot/image", params={"name": "nope.png", "token": token})
        note("图片接口认 ?token=（<img> 带不了请求头）",
             rr.status_code != 401, f"HTTP {rr.status_code}")
        pngs = sorted((pathlib.Path(bot.BASE) / "generated").glob("*.png"))
        if pngs:
            r1 = await c.get(f"{BASE}/api/bot/image", headers=H, params={"name": pngs[0].name})
            r2 = await c.get(f"{BASE}/api/bot/image", headers=H,
                             params={"name": pngs[0].name, "w": 160})
            note("原图可预览",
                 r1.status_code == 200 and r1.content[:4] == b"\x89PNG",
                 f"{len(r1.content)//1024}KB {r1.headers.get('content-type')}")
            note("缩略图更小且转成 jpeg",
                 r2.status_code == 200 and len(r2.content) < len(r1.content)
                 and r2.headers.get("content-type") == "image/jpeg",
                 f"{len(r1.content)//1024}KB -> {len(r2.content)//1024}KB")
        else:
            note("（没有出图，跳过图片预览）", True)

        # ── 表情包接口 ──
        rr = await c.post(f"{BASE}/api/bot/sticker", headers=J,
                          json={"op": "nope", "payload": {}})
        note("未知表情包操作被拒（400 且回白名单）",
             rr.status_code == 400 and isinstance(safe_json(rr).get("allowed"), list),
             f"HTTP {rr.status_code}")
        rr = await c.post(f"{BASE}/api/bot/sticker", headers=J,
                          json={"op": "add_url", "payload": {"url": "http://evil.test/x.png"}})
        note("非 QQ 表情地址被拒（400）",
             rr.status_code == 400 and "gxh.vip.qq.com" in str(safe_json(rr).get("error")),
             f"HTTP {rr.status_code}")
        rr = await c.post(f"{BASE}/api/bot/sticker", headers=J,
                          json={"op": "delete", "payload": {"md5": "nope"}})
        note("删不存在的表情被拒（400）", rr.status_code == 400, f"HTTP {rr.status_code}")
        rr = await c.post(f"{BASE}/api/bot/sticker",
                          headers={**J, "Origin": "http://evil.test"},
                          json={"op": "delete", "payload": {}})
        note("表情包接口同样校验 Origin（403）", rr.status_code == 403, f"HTTP {rr.status_code}")
        rr = await c.post(f"{BASE}/api/bot/sticker", headers=H, content=b"{}")
        note("表情包接口也要求 application/json（415）",
             rr.status_code == 415, f"HTTP {rr.status_code}")

    TMP_CFG.unlink(missing_ok=True)
    bad = [x for x in results if not x[1]]
    print()
    print("=" * 52)
    print(f"通过 {len(results) - len(bad)} 项，失败 {len(bad)} 项")
    for n, _, d in bad:
        print(f"  ✗ {n}  {d}")
    print("=" * 52)
    return len(bad)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
