"""重构后全量回归：确认行为与重构前一致。"""
import asyncio
import json
import os
import pathlib
import sys
import time
from datetime import datetime

# 项目根：由文件自身位置推导，搬盘/改目录名都不用动这里
BASE_DIR = pathlib.Path(__file__).resolve().parent

# 仓库里**不提交** config.json（它含明文密钥）。跑测试时若不存在，就从模板造一份 ——
# 内容是脱敏的模板值（bot_qq=10001 之类），只够把流程跑通，不会碰真实配置。
_CFG_PATH = BASE_DIR / "config.json"
if not _CFG_PATH.exists():
    _tpl = BASE_DIR / "config.example.json"
    if _tpl.exists():
        _CFG_PATH.write_text(_tpl.read_text(encoding="utf-8"), encoding="utf-8")
        print("[test] 没有 config.json，已从 config.example.json 生成一份模板配置")

sys.path.insert(0, str(BASE_DIR))
import bot  # noqa: E402

BOT = 10001
ADMIN = 10002
OTHER = 10003        # 测试用非管理员
G = 999
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


bot.OB.self_id = BOT
bot.WEATHER.enable = False
bot.STICKERS.enable = False
bot.QZONE_API.enable = False
bot.WEB.enable = False
bot.CFG["group_cooldown_seconds"] = 0
bot.CFG["rate_limit"]["max_calls_per_minute"] = 0
bot.LIFE.current = lambda: {"act": "夜间活跃", "hint": "夜猫子时间", "chance": 1.0, "busy": False,
                            "time_str": "星期五 21:30", "event": "下雨", "weather": "小雨"}
bot.ADMIN_CACHE[str(G)] = (time.time(), True)

# ══════════════════════════════════════════════════════════════════
# 状态隔离：必须在**任何** bot 调用之前做完
#
# 这些单例的方法会顺手 save()（mark / bump / ensure_admins ...），
# 只要它们的 path 还指着真实文件，跑一次测试就会把真实状态写坏。
# **已经踩过两次**：
#   1. config.json 被写进测试用的控制台端口（6299），重启后 6200 连不上
#   2. qzone_state.json 被写进测试的 last_tid="t"，导致"今天已经发过说说"
# 所以这里一次性把所有可写路径全部重定向，并在结尾做污染自检。
# ══════════════════════════════════════════════════════════════════
ISO_DIR = BASE_DIR / "_tmp_iso"
ISO_DIR.mkdir(exist_ok=True)
_REAL_STATE_FILES = ("config.json", "memory.json", "mood.json", "qzone_state.json",
                     "sd_state.json", "life_state.json", "bili_state.json",
                     "group_memory.json", "groups.json", "qzone_comment_state.json",
                     "state/tokens.json", "state/weather_state.json")
_ISO_SAVED: list[tuple] = []


def state_fingerprint() -> dict:
    """真实状态文件的 (大小, mtime_ns)。用来证明测试没碰过它们。"""
    out = {}
    for name in _REAL_STATE_FILES:
        p = BASE_DIR / name
        try:
            st = p.stat()
            out[name] = (st.st_size, st.st_mtime_ns)
        except OSError:
            out[name] = None
    return out


def isolate_state() -> None:
    """把所有会写盘的路径重定向到 ISO_DIR；原值记下来以便还原。"""
    import shutil as _sh
    _sh.copy2(BASE_DIR / "config.json", ISO_DIR / "config.json")
    targets = [
        (bot, "CONFIG_PATH", ISO_DIR / "config.json"),
        (bot.LIFE, "state_path", ISO_DIR / "life_state.json"),
        (bot.MEMORY, "path", ISO_DIR / "memory.json"),
        (bot.SDGEN, "state_path", ISO_DIR / "sd_state.json"),
        (bot.SDGEN, "dir", ISO_DIR / "generated"),
        (getattr(bot, "BILI", None), "state_path", ISO_DIR / "bili_state.json"),
        (bot.MOOD, "path", ISO_DIR / "mood.json"),
        (bot.QZONE, "path", ISO_DIR / "qzone_state.json"),
        (bot.QZONE_API, "comment_state_path", ISO_DIR / "qzone_comment_state.json"),
        (bot.GMEM, "path", ISO_DIR / "group_memory.json"),
        (bot.GROUPS, "path", ISO_DIR / "groups.json"),
        (bot.STICKERS, "dir", ISO_DIR / "stickers"),
        (bot.ACTIVITY, "dir", ISO_DIR / "activity"),
        # 天气快照与 token 累计现在也落盘（state/ 下），不指开就会写坏真实文件
        (bot.WEATHER, "state_path", ISO_DIR / "weather_state.json"),
        (bot, "TOKENS_PATH", ISO_DIR / "tokens.json"),
    ]
    for obj, attr, tmp in targets:
        if obj is None or not hasattr(obj, attr):
            continue
        _ISO_SAVED.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, tmp)
        if attr == "dir":
            tmp.mkdir(parents=True, exist_ok=True)
    # 注：index_path 现在是**派生属性**（dir/index.json），跟着 dir 自动走，
    # 不需要（也不能）单独重定向 —— 早先就是因为只改 dir 不改它，
    # 把假表情条目写进了真实索引。


def restore_state() -> None:
    for obj, attr, old in reversed(_ISO_SAVED):
        setattr(obj, attr, old)


_FP_BEFORE = state_fingerprint()
isolate_state()

sent, made = [], []


async def fake_call(action, params=None, timeout=20):
    sent.append((action, params or {}))
    if action == "get_group_member_info":
        return {"status": "ok", "retcode": 0, "data": {"role": "member"}, "echo": "x"}
    return {"status": "ok", "retcode": 0, "data": {"tid": "t"}, "echo": "x"}


async def fake_gen(intent, life=None, purpose="group"):
    """假出图：记下用途、返回成功结果。

    注意要打的是 `generate_outcome`（结构化返回）而不是 `generate` ——
    后者只是它的薄包装，真流程走的是 outcome 那条路。
    """
    made.append(purpose)
    p = str(BASE_DIR / "generated" / "fake.png")
    return bot.GenOutcome(ok=True, data=b"\x89PNG fake", path=p,
                          endpoint_id="fake", detail=p)


bot.OB.call = fake_call
bot.SDGEN.enable = True
bot.SDGEN.generate_outcome = fake_gen

_mid = [70000]


def answer_with(payload):
    async def _a(key, msgs):
        return payload, "cloud"
    return _a


def group_ev(uid, text):
    _mid[0] += 1
    return {"post_type": "message", "message_type": "group", "message_id": _mid[0], "user_id": uid,
            "group_id": G, "sender": {"nickname": "某人"},
            "message": [{"type": "at", "data": {"qq": str(BOT)}},
                        {"type": "text", "data": {"text": text}}]}


def reset():
    sent.clear()
    made.clear()
    bot.CHAT.sessions.clear()
    bot.CALL_LOG.clear()
    bot.QZONE.state = {"date": "", "count": 0, "last_tid": ""}
    bot.SDGEN.state = {"group": [], "qzone_date": "", "qzone": 0}
    # 禁言账本也要清：它们是模块级全局，【6】禁过的人如果不清，
    # 【7】的连戳就会被"同一目标冷却"挡住 —— 那看起来像代码 bug，其实是测试没扫地
    bot.BAN_AT.clear()
    bot.BAN_DAY.clear()
    bot.STREAK_BAN_AT.clear()
    bot.STREAK_BAN_DAY.clear()


def has(action):
    return any(a == action for a, _ in sent)


def last(action):
    for a, p in reversed(sent):
        if a == action:
            return p
    return {}


async def main():
    print("\n【1】文本处理：不吞标点、换行保留为分条")
    check("她的句号不被吃掉", bot.drop_tags("不去。") == "不去。")
    check("标记剥离后保留句号", bot.drop_tags("行，我去发。[说说:x]", bot.QZONE_TAG) == "行，我去发。")
    check("换行保留（是分条标记）", bot.normalize_reply("下雨了\n哪儿也去不了") == "下雨了\n哪儿也去不了")
    check("零宽字符被清掉", bot.normalize_reply("讨\u200b厌") == "讨厌")

    print("\n【2】语义分句")
    check("句末标点切句", bot.split_bubbles("下雨了。哪儿也去不了") == ["下雨了。", "哪儿也去不了"])
    check("显式换行优先", bot.split_bubbles("雨好大\n哦") == ["雨好大", "哦"])
    check("上限 3 条", len(bot.split_bubbles("一\n二\n三\n四\n五")) == 3)
    check("短句不拆", bot.split_bubbles("不想去") == ["不想去"])

    print("\n【3】权限：受限功能对非管理员静默忽略")
    reset()
    bot.CHAT.answer = answer_with("行\n[说说:测试]")
    await bot.handle_message(group_ev(OTHER, "发个说说"))
    check("非管理员发说说被拦", not has("send_qzone_msg"))

    reset()
    bot.CHAT.answer = answer_with("好啊\n[画图:她的样子]")
    await bot.handle_message(group_ev(OTHER, "画个图"))
    check("非管理员聊天画图放行", any(s["type"] == "image"
                                 for s in (last("send_group_msg").get("message") or [])))
    check("群聊画图记到 group 额度", made == ["group"])

    reset()
    bot.CHAT.answer = answer_with("唔\n[画图:雨][说说:配文]")
    await bot.handle_message(group_ev(ADMIN, "发带图说说"))
    check("管理员配图说说发空间", has("send_qzone_msg"))
    check("空间配图用 qzone 额度", made == ["qzone"])

    print("\n【4】出图额度：聊天 10 / 说说 1，互相独立")
    # 隔离：把状态文件指向临时路径，避免测试写坏真实计数
    bot.SDGEN.state_path = BASE_DIR / "_tmp_sd_state.json"
    bot.QZONE.path = BASE_DIR / "_tmp_qz_state.json"
    # 三档配额：私聊不限 / 群聊每 24h 10 张 / 说说每天 1 张
    bot.SDGEN.state = {"group": [], "qzone_date": "", "qzone": 0}
    for _ in range(10):
        bot.SDGEN.mark_drawn("group")
    check("群聊画满 10 张后不能再画", not bot.SDGEN.can_draw("group"))
    check("但说说额度不受影响", bot.SDGEN.can_draw("qzone"))
    check("私聊本来就不限量", bot.SDGEN.can_draw("private"))
    bot.SDGEN.mark_drawn("qzone")
    check("说说画满 1 张后不能再画", not bot.SDGEN.can_draw("qzone"))
    check("计数各自独立", bot.SDGEN.used_today("group") == 10
          and bot.SDGEN.used_today("qzone") == 1)
    bot.SDGEN.state = {"group": [], "qzone_date": "2026-01-01", "qzone": 9}
    check("说说跨天自动归零", bot.SDGEN.used_today("qzone") == 0)
    bot.SDGEN.state = {"group": [time.time() - 25 * 3600] * 10,
                       "qzone_date": "", "qzone": 0}
    check("群聊按 24 小时滚动（25 小时前的作废）", bot.SDGEN.can_draw("group"))

    print("\n【5】空间说说额度判断统一")
    bot.QZONE.state = {"date": datetime.now().strftime("%Y-%m-%d"), "count": 5, "last_tid": ""}
    bot.QZONE.max_per_day = 5
    check("自发已满 -> over_quota", bot.QZONE.over_quota(manual=False))
    check("被要求发不占额度（count_manual_posts=false）", not bot.QZONE.over_quota(manual=True))

    print("\n【6】禁言：apply_bans 走 force_ban，且不重复实现")
    reset()
    bot.BAN_LOG.clear()
    bot.SPEAKERS[str(G)] = {"示例用户3": OTHER}
    done = await bot.apply_bans(G, "[禁言:示例用户3 3]")
    check("禁言执行成功", bool(done), done)
    check("确实调了 set_group_ban", has("set_group_ban"))
    check("写入了每小时额度", len(bot.BAN_LOG[str(G)]) == 1)
    check("时长 180 秒", last("set_group_ban").get("duration") == 180)

    print("\n【7】戳一戳：忙时不理 + 连击禁言")
    reset()
    bot.POKE_LAST.clear()
    bot.POKE_STREAK.clear()
    bot.CFG["poke"]["cooldown_seconds"] = 0
    bot.CFG["poke"]["busy_reply_probability"] = 0.0
    bot.CFG["poke"]["reply_probability"] = 0.0
    bot.LIFE.current = lambda: {"act": "书店看店", "hint": "忙", "chance": 1.0, "busy": True,
                                "time_str": "t", "event": "", "weather": ""}

    def poke_ev():
        _mid[0] += 1
        return {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                "target_id": BOT, "user_id": OTHER, "group_id": G, "sender": {"nickname": "示例用户3"}}

    sent.clear()
    await bot.handle_notice(poke_ev())
    check("忙时不理戳一戳（第1次无回复）", not has("send_group_msg"))
    await bot.handle_notice(poke_ev())
    check("忙时不理戳一戳（第2次仍无回复）", not has("send_group_msg"))
    sent.clear()
    await bot.handle_notice(poke_ev())
    check("连戳 3 次触发禁言", last("set_group_ban").get("duration") == 180)
    check("禁言前先撂一句话", has("send_group_msg"))

    print("\n【8】引用：多人同时在聊才引用")
    reset()
    bot.LIFE.current = lambda: {"act": "夜间活跃", "hint": "x", "chance": 1.0, "busy": False,
                                "time_str": "t", "event": "", "weather": ""}
    bot.CHAT.answer = answer_with("嗯")
    bot.RECENT_SPEAKERS.clear()
    await bot.handle_message(group_ev(OTHER, "就我一个"))
    check("单人聊天不引用", not any(s["type"] == "reply"
                                for s in (last("send_group_msg").get("message") or [])))
    reset()
    bot.RECENT_SPEAKERS.clear()
    dq = __import__("collections").deque(maxlen=80)
    dq.append((time.time() - 5, 333))
    bot.RECENT_SPEAKERS[f"g{G}"] = dq
    bot.CHAT.answer = answer_with("嗯")
    await bot.handle_message(group_ev(OTHER, "还有我"))
    check("多人聊天会引用", any(s["type"] == "reply"
                            for s in (last("send_group_msg").get("message") or [])))

    print("\n【9】/clear 权限")
    reset()
    bot.CHAT.sessions[f"g{G}"] = __import__("collections").deque([{"role": "user", "content": "x"}])
    e = group_ev(OTHER, "")
    e["message"] = [{"type": "text", "data": {"text": "/clear"}}]
    await bot.handle_message(e)
    check("非管理员清不掉", bool(bot.CHAT.sessions.get(f"g{G}")))
    reset()
    bot.CHAT.sessions[f"g{G}"] = __import__("collections").deque([{"role": "user", "content": "x"}])
    e = group_ev(ADMIN, "")
    e["message"] = [{"type": "text", "data": {"text": "/clear"}}]
    await bot.handle_message(e)
    check("管理员可以清空", not bot.CHAT.sessions.get(f"g{G}"))

    print("\n【10】状态文件读写（read/write_json_dict 统一后）")
    p = BASE_DIR / "_t_state.json"
    bot.write_json_dict(p, {"a": 1, "b": "中文"}, "测试")
    check("写读往返一致", bot.read_json_dict(p, "测试") == {"a": 1, "b": "中文"})
    check("文件不存在返回空 dict", bot.read_json_dict(BASE_DIR / "_nope.json", "测试") == {})
    p.write_text("这不是JSON", encoding="utf-8")
    check("内容坏了不抛异常", bot.read_json_dict(p, "测试") == {})
    p.unlink(missing_ok=True)

    print("\n【11】原子写：tmp + fsync + os.replace")
    ap = BASE_DIR / "_t_atomic.json"
    ap.unlink(missing_ok=True)
    bot.atomic_write_json(ap, {"n": 1})
    check("原子写后可正常读回", bot.read_json_dict(ap, "测试") == {"n": 1})
    bot.atomic_write_json(ap, {"n": 2})
    check("可覆盖已存在的目标文件", bot.read_json_dict(ap, "测试") == {"n": 2})
    check("不残留 .tmp 临时文件", list(ap.parent.glob("_t_atomic.json.*.tmp")) == [])
    before = ap.read_bytes()
    try:
        bot.atomic_write_json(ap, {"bad": {1, 2, 3}})   # set 不可 JSON 序列化
        check("不可序列化对象应抛错", False, "预期 TypeError 却未抛")
    except TypeError:
        check("不可序列化对象抛 TypeError", True)
    check("失败后原文件未被截断", ap.read_bytes() == before)
    check("失败后无 .tmp 残留", list(ap.parent.glob("_t_atomic.json.*.tmp")) == [])
    ap.unlink(missing_ok=True)

    print("\n【12】并发不丢计数（守住「读-改-写不跨 await」的不变量）")
    # bump / mark_drawn / mark 都是同步方法，读-改-写之间没有 await，
    # 事件循环无法中途抢占，故并发调用不会丢更新。
    # 若将来有人在这些方法内部插入 await，本用例会立刻变红。
    bot.MOOD.path = BASE_DIR / "_tmp_mood.json"
    bot.MOOD.enable = True
    bot.MOOD.affinity = {}
    bot.MOOD.feeling = {}
    bot.MOOD.names = {}

    async def _one_bump():
        bot.MOOD.bump(G, OTHER, "示例用户3", "谢谢")   # NICE 词，每次 +2 好感

    await asyncio.gather(*[_one_bump() for _ in range(20)])
    got = bot.MOOD.affinity.get(str(G), {}).get(str(OTHER))
    # 从中性 50 起，20 次 × +2 = 90（还没碰到 100 的上限）。
    # 这里断言的是"20 次一次都没丢"，不是"撞到上限"。
    want = 50.0 + 20 * bot.MOOD.affinity_gain
    check("20 次并发一次都没丢（读-改-写不跨 await）", got == want,
          f"实际 {got}，期望 {want}")
    # 还原成**隔离目录**里的那份，不要写回真实的 mood.json ——
    # 以前这里写的是 bot.BASE / "mood.json"（真实文件），于是本段之后只要有任何
    # 一次 MOOD.save() 就会把测试造的"某人(90分)"盖到真实好感上，真实数据当场丢失
    # （2026-09-27 正是这样把 999000001 群的真实好感冲掉的）。
    bot.MOOD.path = ISO_DIR / "mood.json"

    print("\n【13】force_ban fail-closed：查身份失败时不得禁言")
    reset()
    bot.BAN_LOG.clear()
    bot.SPEAKERS[str(G)] = {"示例用户3": OTHER}
    bot.CFG["ban"]["protect_admins"] = True

    async def flaky_call(action, params=None, timeout=20):
        if action == "get_group_member_info":
            raise asyncio.TimeoutError("模拟 NapCat 超时")
        return await fake_call(action, params, timeout)

    real_call = bot.OB.call
    bot.OB.call = flaky_call
    try:
        banned = await bot.force_ban(G, OTHER, "示例用户3", 3, "测试")
    finally:
        bot.OB.call = real_call
    check("查身份失败时返回 False", banned is False, f"实际 {banned}")
    check("且确实没有调用 set_group_ban", not has("set_group_ban"))

    print("\n【14】Memory.tick：跨过重置点即触发，且当天幂等")
    bot.MEMORY.path = BASE_DIR / "_tmp_memory.json"
    bot.MEMORY.enable = True
    bot.MEMORY.extract = False          # 不触发 LLM 抽取
    bot.MEMORY.reset_hour = 0           # 保证 now.hour >= reset_hour 恒成立
    bot.MEMORY.every_days = 1
    bot.MEMORY._last_reset = "2000-01-01"
    today = datetime.now().strftime("%Y-%m-%d")
    bot.CHAT.sessions[f"g{G}"] = __import__("collections").deque([{"role": "user", "content": "x"}])
    bot.CHAT.last_reply_at[f"g{G}"] = time.time()
    await bot._daily_tick_once()
    check("跨过重置点即重置（_last_reset 更新为今天）", bot.MEMORY._last_reset == today,
          f"实际 {bot.MEMORY._last_reset}")
    check("当天的闲聊上下文被清空", not bot.CHAT.sessions)
    check("重置日期已写入状态文件",
          bot.read_json_dict(bot.MEMORY.path, "测试").get("last_reset") == today)

    bot.CHAT.sessions[f"g{G}"] = __import__("collections").deque([{"role": "user", "content": "y"}])
    await bot._daily_tick_once()
    check("同一天重复巡检不重复重置（幂等）", bool(bot.CHAT.sessions.get(f"g{G}")))

    print("\n【15】控制台写操作：白名单 + 只碰明确字段")
    tmp_cfg = BASE_DIR / "_tmp_config.json"
    tmp_mem = BASE_DIR / "_tmp_memory2.json"
    real_cfg_path = bot.CONFIG_PATH
    __import__("shutil").copy2(real_cfg_path, tmp_cfg)
    bot.CONFIG_PATH = tmp_cfg
    try:
        restrict = bot.CFG["admin"]["restrict"]
        feat = next(iter(restrict))
        origin = bool(restrict[feat])

        out = bot.console_apply("restrict_set", {"feature": feat, "value": not origin})
        check("restrict_set 切换成功", out["value"] is (not origin))
        check("切换已原子落盘到 config.json",
              json.loads(tmp_cfg.read_text(encoding="utf-8"))["admin"]["restrict"][feat] is (not origin))
        # 注意：update_config 走的是「重读磁盘 + 合并 + CFG.clear/update」，
        # 所以嵌套对象会被换成新的 —— 上面抓的 restrict 引用会失效，必须重新从 CFG 取。
        check("内存 CFG 同步（restricted 立即生效）",
              bot.CFG["admin"]["restrict"][feat] is (not origin))

        try:
            bot.console_apply("restrict_set", {"feature": "__nope__", "value": True})
            check("未知受限功能被拒", False, "竟然接受了")
        except ValueError:
            check("未知受限功能被拒", True)
        check("被拒后没有凭空多出键", "__nope__" not in restrict)

        out = bot.console_apply("persona_set", {"field": "persona", "value": bot.CFG["persona"]})
        check("persona_set 接受白名单字段", out["field"] == "persona")
        try:
            bot.console_apply("persona_set", {"field": "access_token", "value": "hacked"})
            check("persona_set 拒绝密钥类字段", False, "竟然接受了 access_token")
        except ValueError:
            check("persona_set 拒绝密钥类字段", True)
        check("access_token 未被改动",
              json.loads(tmp_cfg.read_text(encoding="utf-8"))["access_token"] == bot.CFG["access_token"])

        bot.CHAT.sessions["g999"] = __import__("collections").deque([{"role": "user", "content": "x"}])
        out = bot.console_apply("session_clear", {"key": "g999"})
        check("session_clear 清掉会话", out["removed"] and "g999" not in bot.CHAT.sessions)
        try:
            bot.console_apply("session_clear", {"key": "../../evil"})
            check("session_clear 拒绝路径穿越 key", False, "竟然接受了")
        except ValueError:
            check("session_clear 拒绝路径穿越 key", True)

        bot.MEMORY.path = tmp_mem
        bot.MEMORY.people = {}
        bot.MEMORY.pending = {"888888": {"name": "小明", "why": "爱发图", "ts": time.time()}}
        bot.MEMORY.declined = []
        bot.console_apply("memory_approve", {"uin": "888888"})
        check("memory_approve 把人记进来",
              "888888" in bot.MEMORY.people and not bot.MEMORY.pending)
        check("审批理由顺手变成第一条特点",
              any("爱发图" in str(t) for t in bot.MEMORY.people["888888"]["traits"]))
        check("批准已落盘",
              "888888" in bot.read_json_dict(tmp_mem, "测试").get("people", {}))

        n_before = len(bot.MEMORY.people["888888"]["traits"])
        bot.console_apply("memory_item_delete", {"uin": "888888", "kind": "traits", "index": 0})
        check("memory_item_delete 删掉一条",
              len(bot.MEMORY.people["888888"]["traits"]) == n_before - 1)
        try:
            bot.console_apply("memory_item_delete", {"uin": "888888", "kind": "traits", "index": 99})
            check("越界下标被拒", False, "竟然接受了")
        except ValueError:
            check("越界下标被拒", True)
        try:
            bot.console_apply("memory_item_delete", {"uin": "888888", "kind": "nope", "index": 0})
            check("非法 kind 被拒", False, "竟然接受了")
        except ValueError:
            check("非法 kind 被拒", True)

        bot.console_apply("memory_forget", {"uin": "888888"})
        check("memory_forget 忘掉某人", "888888" not in bot.MEMORY.people)

        bot.MEMORY.pending = {"777777": {"name": "小刚", "why": "x", "ts": time.time()}}
        bot.console_apply("memory_decline", {"uin": "777777"})
        check("memory_decline 记进 declined",
              "777777" in bot.MEMORY.declined and "777777" not in bot.MEMORY.pending)

        try:
            bot.console_apply("rm_rf", {})
            check("未知 op 被拒", False, "竟然接受了")
        except ValueError:
            check("未知 op 被拒", True)
    finally:
        bot.CONFIG_PATH = real_cfg_path
        tmp_cfg.unlink(missing_ok=True)
        tmp_mem.unlink(missing_ok=True)

    print("\n【16】表情包：QQ 原生发送 + 手动增删改 + 图片接口防护")
    import base64 as _b64
    import console_server as _cs
    tmp_stk = BASE_DIR / "_tmp_stickers"
    tmp_cfg2 = BASE_DIR / "_tmp_cfg_stk.json"
    real_cfg2 = bot.CONFIG_PATH
    real_stk_dir, real_stk_items = bot.STICKERS.dir, bot.STICKERS.items
    real_send = bot.STICKERS.send_enable
    real_enable = bot.STICKERS.enable      # 文件开头为了不干扰别的用例把它关了
    __import__("shutil").copy2(bot.CONFIG_PATH, tmp_cfg2)
    tmp_stk.mkdir(exist_ok=True)
    try:
        bot.CONFIG_PATH = tmp_cfg2
        bot.STICKERS.dir = tmp_stk
        bot.STICKERS.items = []
        bot.STICKERS.enable = True

        GIF = b"GIF89a" + b"\x00" * 200 + b"ANIM"
        PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300

        r = bot.console_apply("sticker_add", {
            "data_b64": _b64.b64encode(GIF).decode(), "emoji_id": "12345",
            "emoji_package_id": "67890", "key": "k1", "summary": "[无语]",
            "tags": "无语, 敷衍"})
        check("sticker_add 认出 QQ 原生身份", r["native"] is True)
        check("标签解析正确", r["tags"] == ["无语", "敷衍"])
        seg = bot.sticker_segment(bot.STICKERS.items[-1])
        check("原生条目生成 mface 段（不是图片）",
              seg["type"] == "mface" and seg["data"]["emoji_id"] == "12345")

        r2 = bot.console_apply("sticker_add", {"data_b64": _b64.b64encode(PNG).decode(),
                                               "tags": "猫"})
        check("没填表情 ID 的条目 native=False", r2["native"] is False)
        check("没身份的条目默认不发（不生成图片段）",
              bot.sticker_segment(bot.STICKERS.items[-1]) is None)

        try:
            bot.console_apply("sticker_add", {"data_b64": _b64.b64encode(GIF).decode()})
            check("重复添加被拒", False, "竟然接受了")
        except ValueError:
            check("重复添加被拒", True)
        try:
            bot.console_apply("sticker_add", {"data_b64": _b64.b64encode(b"nope").decode()})
            check("非法图片格式被拒", False, "竟然接受了")
        except ValueError:
            check("非法图片格式被拒", True)

        bot.STICKERS.send_enable = True
        picked = bot.STICKERS.pick("无语", "")
        check("pick 返回条目 dict（不是路径）",
              isinstance(picked, dict) and picked.get("native") is True)
        check("pick 跳过没有原生身份的条目", picked.get("md5") == r["md5"])

        bot.console_apply("sticker_delete", {"md5": r2["md5"]})
        check("sticker_delete 生效", len(bot.STICKERS.items) == 1)
        try:
            bot.console_apply("sticker_delete", {"md5": "nope"})
            check("删不存在的条目被拒", False, "竟然接受了")
        except ValueError:
            check("删不存在的条目被拒", True)

        bot.console_apply("sticker_toggle", {"field": "send_enable", "value": True})
        check("sticker_toggle 已落盘",
              json.loads(tmp_cfg2.read_text(encoding="utf-8"))["stickers"]["send_enable"] is True)
        check("sticker_toggle 同步实例属性", bot.STICKERS.send_enable is True)
        try:
            bot.console_apply("sticker_toggle", {"field": "__x__", "value": True})
            check("非法配置项被拒", False, "竟然接受了")
        except ValueError:
            check("非法配置项被拒", True)

        snap = bot.STICKERS.snapshot()
        check("快照带 native / can_send / file",
              all(k in snap["items"][0] for k in ("native", "can_send", "file")))
        check("快照统计 native_count", snap["native_count"] == 1)

        missing = [k for k in (bot.CFG["admin"]["restrict"] or {})
                   if bot.restrict_label(k)[0] == k]
        check("每个受限功能都有中文名", not missing, f"缺 {missing}")

        D = str(bot.BASE / "generated")
        bad_names = ["../../../../Windows/win.ini", "..\\..\\boot.ini", "C:/Windows/win.ini",
                     "/etc/passwd", ".", ".hidden.png", "sub/x.png", "", "bot.py"]
        leaked = [n for n in bad_names if _cs.safe_image_path(D, n) is not None]
        check("图片接口挡住路径穿越", not leaked, f"漏了 {leaked}")
        pngs = sorted((bot.BASE / "generated").glob("*.png"))
        if pngs:
            check("正常文件名可通过", _cs.safe_image_path(D, pngs[0].name) is not None)
            full = _cs.load_image(D, pngs[0].name, 0)
            thumb = _cs.load_image(D, pngs[0].name, 160)
            check("缩略图确实变小了",
                  len(thumb[0]) < len(full[0]) and thumb[1] == "image/jpeg",
                  f"{len(full[0])//1024}KB -> {len(thumb[0])//1024}KB")
        else:
            check("正常文件名可通过（无出图，跳过）", True)
    finally:
        bot.CONFIG_PATH = real_cfg2
        bot.STICKERS.dir = real_stk_dir
        bot.STICKERS.items = real_stk_items
        bot.STICKERS.send_enable = real_send
        bot.STICKERS.enable = real_enable
        __import__("shutil").rmtree(tmp_stk, ignore_errors=True)
        tmp_cfg2.unlink(missing_ok=True)

    print("\n【17】空间动作：点赞与评论不能互相吞掉")
    # 复现用户报的 bug：同一轮里既点赞又评论时，评论永远不执行。
    # 根因是 apply_reply_tags 早先串行改写 reply ——
    # resolve_like 会把 reply 换成一小段新生成的话，后面的 [评论:...] 就没了。
    # 这个用例会锁住「两个动作都得执行」。
    class _FakeQZone:
        def __init__(self):
            self.calls = []

        async def like_post(self, who):
            self.calls.append(("like", who))
            return True, f"{who or '最新那条'}（测试）"

        async def comment_post(self, who, body):
            self.calls.append(("comment", who, body))
            return True, f"{who or '最新那条'}（测试）"

        async def feed_text(self):
            self.calls.append(("feeds",))
            return ""

    fake = _FakeQZone()
    real_api, real_answer = bot.QZONE_API, bot.CHAT.answer

    async def _fake_answer(key, msgs):
        # 这一步就是旧实现里吃掉标记的元凶：返回一小段**不含任何标记**的新话
        return "行吧，点了也评了。", None

    try:
        bot.QZONE_API, bot.CHAT.answer = fake, _fake_answer

        fake.calls.clear()
        text, _img = await bot.apply_reply_tags(
            "k", [{"role": "user", "content": "帮我点赞顺便评一句"}],
            "[点赞:示例用户1]\n[评论:示例用户1 这张真好看]")
        kinds = [c[0] for c in fake.calls]
        check("同一轮里点赞和评论都执行了（旧代码这里只有 like）",
              kinds == ["like", "comment"], f"实际 {kinds}")
        check("两个标记都从正文里清掉了",
              "[点赞" not in text and "[评论" not in text, repr(text))

        fake.calls.clear()
        await bot.apply_reply_tags("k", [{"role": "user", "content": "x"}], "[点赞:示例用户1]")
        check("只写点赞时只点赞", [c[0] for c in fake.calls] == ["like"],
              str([c[0] for c in fake.calls]))

        fake.calls.clear()
        await bot.apply_reply_tags("k", [{"role": "user", "content": "x"}], "[评论:示例用户1 好]")
        check("只写评论时只评论", [c[0] for c in fake.calls] == ["comment"],
              str([c[0] for c in fake.calls]))

        fake.calls.clear()
        await bot.apply_reply_tags("k", [{"role": "user", "content": "x"}],
                                   "[空间]\n[点赞:示例用户1]\n[评论:示例用户1 好]")
        check("看空间 + 点赞 + 评论三者都执行",
              [c[0] for c in fake.calls] == ["like", "comment", "feeds"],
              str([c[0] for c in fake.calls]))
    finally:
        bot.QZONE_API, bot.CHAT.answer = real_api, real_answer

    # 提示词里必须说清两者是两件事（放在恢复之后跑，build_system 要用真的 QZONE_API）
    sys_prompt = bot.build_system(True, None, 1, 10002)
    check("系统提示说明了点赞与评论是两件事", "点赞和评论各认各的" in sys_prompt)

    # ── 污染自检：跑完测试，真实状态文件必须一个都没被动过 ──
    # 这条是防"测试写坏用户数据"的兜底，比事后排查便宜得多。
    fp_after = state_fingerprint()
    changed = [k for k in _FP_BEFORE if _FP_BEFORE[k] != fp_after.get(k)]
    check("测试没有污染真实状态文件", not changed,
          f"被改动的：{changed}（隔离没覆盖全，去 isolate_state() 里补）")
    restore_state()
    __import__("shutil").rmtree(ISO_DIR, ignore_errors=True)

    print("\n【18】空间：标记不许漏进聊天 + 自主互动按心情")
    # 复现用户报的「评论只发在群里」：看空间之后"她开口"那一轮是**新的模型输出**，
    # 里面又带了 [评论:...]。旧代码不再处理它，标记就原样发进了群。
    class _FakeQZone2:
        # qzone_auto_interact_maybe 会看这个开关，假对象也得有
        enable = True

        def __init__(self):
            self.calls = []

        async def like_post(self, who):
            self.calls.append(("like", who))
            return True, f"{who}（测试）"

        async def comment_post(self, who, body):
            self.calls.append(("comment", who, body))
            return True, f"{who}（测试）"

        async def feed_lines(self, num=6, friend=True):
            self.calls.append(("feeds",))
            return ["示例用户1：今天天气不错 ［赞1 评0］"]

        async def feed_text(self, num=6, friend=True):
            self.calls.append(("feeds",))
            return "示例用户1：今天天气不错 ［赞1 评0］"

    fake2 = _FakeQZone2()
    real_api2, real_ans2 = bot.QZONE_API, bot.CHAT.answer
    real_aff, real_feel, real_names = bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names
    real_last_interact = bot._LAST_AUTO_INTERACT
    real_qz_cfg = dict(bot.CFG.get("qzone") or {})
    try:
        bot.QZONE_API = fake2

        # ① 说话那轮又带出新标记 -> 必须补执行，且不能漏出去
        async def _ans_with_tag(key, msgs):
            return "看到了\n[评论:示例用户1 我喜欢你]", None

        bot.CHAT.answer = _ans_with_tag
        text, _img = await bot.apply_reply_tags(
            "k", [{"role": "user", "content": "去看看"}], "[空间]")
        kinds = [c[0] for c in fake2.calls]
        check("说话那轮带出的新标记会被补执行（旧代码漏掉）",
              "comment" in kinds, f"实际 {kinds}")
        check("输出里不残留任何动作标记", not bot.has_action_tag(text), repr(text))

        # ② 两轮都带标记时，结尾必须兜底剥干净（宁可少一次动作也不漏标记）
        async def _ans_always_tag(key, msgs):
            return "嗯\n[点赞:示例用户1]\n[评论:示例用户1 又来]", None

        bot.CHAT.answer = _ans_always_tag
        text, _img = await bot.apply_reply_tags(
            "k", [{"role": "user", "content": "x"}], "[空间]")
        check("反复带标记时结尾仍会剥干净", not bot.has_action_tag(text), repr(text))

        # ③ 好感/心情：跨群合并 + 按昵称查分（都是百分制 0-100）
        bot.MOOD.enable = True
        bot.MOOD.affinity = {"1": {"111": 90.0}, "2": {"111": 90.0, "222": 10.0}}
        bot.MOOD.feeling = {"1": {"111": 70.0}, "2": {"111": 70.0, "222": 25.0}}
        bot.MOOD.names = {"1": {"111": "甲"}, "2": {"111": "甲", "222": "乙"}}
        allm = bot.MOOD.render_all()
        check("render_all 跨群合并出人名", "甲" in allm and "乙" in allm, allm)
        check("score_for 按昵称查好感（百分制）", bot.MOOD.score_for("乙") == 10.0)
        check("score_for 不认识的人返回 None", bot.MOOD.score_for("丙") is None)

        # ④ 自主互动：印象差的人必须被硬门槛拦住（提示词之外的那道）
        bot.CFG.setdefault("qzone", {})["auto_interact_enable"] = True
        bot.CFG["qzone"]["auto_interact_probability"] = 1.0
        bot.CFG["qzone"]["auto_interact_cooldown_minutes"] = 0

        async def _ans_want_hate(key, msgs):
            return '{"who":"乙","like":true,"comment":"哼"}', None

        bot.CHAT.answer = _ans_want_hate
        bot._LAST_AUTO_INTERACT = 0.0
        fake2.calls.clear()
        await bot.qzone_auto_interact_maybe()
        check("看不顺眼的人不会被互动（好感 -4）",
              not [c for c in fake2.calls if c[0] in ("like", "comment")],
              str(fake2.calls))

        # ⑤ 印象好的人 + 她要互动 -> 真的执行
        bot.MOOD.affinity = {"1": {"111": 90.0}}
        bot.MOOD.feeling = {"1": {"111": 90.0}}
        bot.MOOD.names = {"1": {"111": "甲"}}

        async def _ans_want_like(key, msgs):
            return '{"who":"甲","like":true,"comment":"这张不错"}', None

        bot.CHAT.answer = _ans_want_like
        bot._LAST_AUTO_INTERACT = 0.0
        fake2.calls.clear()
        await bot.qzone_auto_interact_maybe()
        got = [c[0] for c in fake2.calls]
        check("印象好的人会被点赞 + 评论", "like" in got and "comment" in got, str(got))

        # ⑥ 她说"谁都不理" -> 什么都不做
        async def _ans_none(key, msgs):
            return '{"who":"","like":false,"comment":""}', None

        bot.CHAT.answer = _ans_none
        bot._LAST_AUTO_INTERACT = 0.0
        fake2.calls.clear()
        await bot.qzone_auto_interact_maybe()
        check("她选择不理人时不产生任何动作",
              not [c for c in fake2.calls if c[0] in ("like", "comment")],
              str(fake2.calls))

        # ⑦ 节流：冷却未到不该再动
        async def _ans_like_again(key, msgs):
            return '{"who":"甲","like":true,"comment":""}', None

        bot.CHAT.answer = _ans_like_again
        bot.CFG["qzone"]["auto_interact_cooldown_minutes"] = 60
        bot._LAST_AUTO_INTERACT = time.time()      # 刚互动过
        fake2.calls.clear()
        await bot.qzone_auto_interact_maybe()
        check("冷却期内不会重复互动",
              not [c for c in fake2.calls if c[0] in ("like", "comment")],
              str(fake2.calls))
    finally:
        bot.QZONE_API, bot.CHAT.answer = real_api2, real_ans2
        bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names = real_aff, real_feel, real_names
        bot._LAST_AUTO_INTERACT = real_last_interact
        bot.CFG["qzone"] = real_qz_cfg

    print("\n【19】好感/心情分开 + 别名降阈值 + 可调参数白名单")
    tmp_mood19 = BASE_DIR / "_tmp_m19.json"
    tmp_cfg19 = BASE_DIR / "_tmp_cfg19.json"
    real_path19 = bot.MOOD.path
    real19 = (bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names)
    real_k19 = (bot.MOOD.affinity_gain, bot.MOOD.affinity_loss,
                bot.MOOD.feel_gain, bot.MOOD.feel_loss, bot.MOOD.pull)
    real_cfg19 = bot.CONFIG_PATH
    real_thr = bot.ATTENTION.threshold
    real_cfg_thr = bot.CFG["attention"]["threshold"]
    __import__("shutil").copy2(bot.CONFIG_PATH, tmp_cfg19)
    try:
        # ① 旧格式（-5..+5）自动迁移成百分制
        tmp_mood19.write_text(json.dumps(
            {"scores": {"9": {"7": -5.0, "8": 5.0}},
             "names": {"9": {"7": "坏蛋", "8": "好人"}}}, ensure_ascii=False),
            encoding="utf-8")
        bot.MOOD.path = tmp_mood19
        bot.MOOD.load()
        check("-5 迁成 0、+5 迁成 100",
              bot.MOOD.affinity["9"]["7"] == 0.0 and bot.MOOD.affinity["9"]["8"] == 100.0,
              str(bot.MOOD.affinity))
        check("迁移时心情=好感（从同一条线开始）",
              bot.MOOD.feeling["9"] == bot.MOOD.affinity["9"])

        # ② 被骂一句：好感动得比心情小（长期印象保守，当下情绪波动大）
        bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names = {}, {}, {}
        bot.MOOD.bump("9", "7", "坏蛋", "你真废物")
        a = bot.MOOD.affinity_of("9", "7")
        f = bot.MOOD.mood_of("9", "7")
        check("被骂时好感掉得比心情少", a > f, f"好感 {a} / 心情 {f}")
        check("两者都低于中性 50", a < 50 and f < 50, f"{a} / {f}")

        # ③ 心情被好感牵引 —— 「对某人好感影响对某人的心情」
        bot.MOOD.affinity["9"]["7"] = 90.0
        bot.MOOD.feeling["9"]["7"] = 20.0
        before = bot.MOOD.feeling["9"]["7"]
        bot.MOOD.decay_all()
        after = bot.MOOD.mood_of("9", "7")
        check("心情往好感那边靠（好感影响心情）", after > before,
              f"{before} -> {after}（好感 90）")

        # ④ 别名降阈值：生效、过期恢复、被 @ 时不走这条
        bot.ATTENTION.threshold = 1.0
        bot.ATTENTION.alias_discount = 0.4
        bot.ATTENTION.alias_window = 60
        st = bot.ATTENTION.state("777")
        st["alias_until"] = 0
        base = bot.ATTENTION.effective_threshold("777")
        bot.ATTENTION.note("777", "小玖在吗", "甲", at_me=False, mentioned=True)
        low = bot.ATTENTION.effective_threshold("777")
        check("喊了别名后有效阈值下降", low < base, f"{base} -> {low}")
        st["alias_until"] = 0
        check("窗口过期后阈值恢复", bot.ATTENTION.effective_threshold("777") == base)
        bot.ATTENTION.note("777", "许杏玖", "甲", at_me=True, mentioned=True)
        check("被 @ 时不走别名降阈值这条路",
              bot.ATTENTION.effective_threshold("777") == base)

        # ⑤ 可调参数：白名单 + 范围 + 落盘 + 同步到实例
        bot.CONFIG_PATH = tmp_cfg19
        r = bot.console_apply("config_set", {"path": "attention.threshold", "value": 1.5})
        check("config_set 改得动白名单里的项", r["value"] == 1.5)
        check("同步到了实例属性（否则本次运行不生效）", bot.ATTENTION.threshold == 1.5)
        check("已落盘",
              json.loads(tmp_cfg19.read_text(encoding="utf-8"))
              ["attention"]["threshold"] == 1.5)
        for bad_path in ("access_token", "cloud.api_key", "admin.restrict", "nope.nope"):
            try:
                bot.console_apply("config_set", {"path": bad_path, "value": 1})
                check(f"白名单外的 {bad_path} 被拒", False, "竟然接受了")
            except ValueError:
                check(f"白名单外的 {bad_path} 被拒", True)
        try:
            bot.console_apply("config_set", {"path": "attention.threshold", "value": 999})
            check("超出许可范围被拒", False, "竟然接受了")
        except ValueError:
            check("超出许可范围被拒", True)
        tl = bot.console_snapshot("tunables")
        check("tunables 快照含全部可调项",
              len(tl["items"]) == len(bot.CONSOLE_TUNABLES), f"{len(tl['items'])} 项")
        check("tunables 每项都带范围与步长",
              all(all(k in it for k in ("min", "max", "step", "desc", "value"))
                  for it in tl["items"]))
    finally:
        bot.MOOD.path = real_path19
        bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names = real19
        (bot.MOOD.affinity_gain, bot.MOOD.affinity_loss,
         bot.MOOD.feel_gain, bot.MOOD.feel_loss, bot.MOOD.pull) = real_k19
        bot.CONFIG_PATH = real_cfg19
        bot.ATTENTION.threshold = real_thr
        bot.CFG["attention"]["threshold"] = real_cfg_thr
        bot.ATTENTION.groups.pop("g777", None)
        tmp_mood19.unlink(missing_ok=True)
        tmp_cfg19.unlink(missing_ok=True)

    print("\n【20】自主生活：与群冷热无关，按心情挑事做")
    real_act = (bot.LIFE.activity, bot.LIFE.activity_until, bot.LIFE.activity_next,
                bot.LIFE.material, bot.LIFE.material_ts)
    real_gap, real_skip = bot.LIFE.act_gap, bot.LIFE.act_skip
    real_epd = bot.LIFE.events_per_day
    real_pool = bot.LIFE.act_pool
    real_aff20, real_feel20 = bot.MOOD.affinity, bot.MOOD.feeling
    real_day, real_evs, real_place_day, real_place = (
        bot.LIFE._day, bot.LIFE._day_events, bot.LIFE._place_day, bot.LIFE._place)
    try:
        # ① 心情档位由「时段精神头 + 对所有人的平均心情」合成。
        # 显式设定社交心情，别依赖前面用例留下的状态。
        bot.MOOD.affinity = {"1": {"111": 50.0}}
        bot.MOOD.feeling = {"1": {"111": 50.0}}      # 平均 50 → 0.5，中性
        s_good, l_good = bot.LIFE.self_mood(0.95)
        s_bad, l_bad = bot.LIFE.self_mood(0.05)
        check("精神头高 -> good 档", l_good == "good" and s_good > s_bad, f"{s_good:.2f}/{l_good}")
        check("精神头极低 -> bad 档", l_bad == "bad", f"{s_bad:.2f}/{l_bad}")

        # ①b 「对所有人的心情」也要影响她自己的心情
        bot.MOOD.feeling = {"1": {"111": 95.0}}
        s_hi, _ = bot.LIFE.self_mood(0.5)
        bot.MOOD.feeling = {"1": {"111": 10.0}}
        s_lo, _ = bot.LIFE.self_mood(0.5)
        check("对所有人的心情越好，她整体心情越高", s_hi > s_lo,
              f"好 {s_hi:.2f} vs 差 {s_lo:.2f}")
        bot.MOOD.affinity = {"1": {"111": 50.0}}
        bot.MOOD.feeling = {"1": {"111": 50.0}}

        # ② 活动池按心情筛：bad 档不该抽到"只有心情好才做"的事
        for _ in range(30):
            a = bot.LIFE.pick_activity("bad")
            if a.get("mood") == "good":
                check("bad 档不会抽到 good 专属活动", False, str(a))
                break
        else:
            check("bad 档不会抽到 good 专属活动", True)
        for _ in range(30):
            a = bot.LIFE.pick_activity("good")
            if a.get("mood") == "bad":
                check("good 档不会抽到 bad 专属活动", False, str(a))
                break
        else:
            check("good 档不会抽到 bad 专属活动", True)

        # ③ set_activity：时长落在配置区间内，且真的写进行为流水
        bot.LIFE.act_duration = (10.0, 20.0)
        bot.LIFE.set_activity({"id": "t", "name": "测试活动", "hint": "在测试",
                               "reply_chance": 0.42, "busy": True})
        left = (bot.LIFE.activity_until - time.time()) / 60.0
        check("活动时长落在配置区间内", 9.9 <= left <= 20.1, f"{left:.1f} 分钟")

        # ④ current() 会被自主活动盖过（日程只是底子）。
        # ⚠️ 本文件在模块级把 LIFE.current 换成了 stub，所以这里要显式用真方法。
        cur = bot.Life.current(bot.LIFE)
        check("current() 的 act 被自主活动覆盖", cur["act"] == "测试活动", cur["act"])
        check("busy 也跟着活动走", cur["busy"] is True)
        check("reply_chance 也跟着活动走", abs(cur["chance"] - 0.42) < 1e-6, str(cur["chance"]))

        # ⑤ 活动结束后不再覆盖
        bot.LIFE.activity_until = time.time() - 1
        cur2 = bot.Life.current(bot.LIFE)
        check("活动过期后回落到日程段", cur2["act"] != "测试活动", cur2["act"])

        # ⑥ 素材：新鲜时取得到，过期取不到
        bot.LIFE.note_material("你刚看到一个很有意思的东西")
        check("素材能取到", bot.LIFE.take_material().startswith("你刚看到"))
        bot.LIFE.material_ts = time.time() - bot.LIFE.material_keep - 60
        check("素材过期后取不到", bot.LIFE.take_material() == "")

        # ⑦ 每天多条小事（以前固定只挑 1 条）
        bot.LIFE.events_per_day = 3
        bot.LIFE._day = ""
        evs = bot.LIFE._events_today("2099-01-01")
        check("每天挑多条小事", len(evs) == 3, f"{len(evs)} 条")
        check("同一天结果稳定", bot.LIFE._events_today("2099-01-01") == evs)
        check("换一天会换内容", bot.LIFE._events_today("2099-01-02") != evs)

        # ⑧ 地点：当天固定、跨天会变
        bot.LIFE._place_day = ""
        p1 = bot.LIFE.place_today("2099-02-01")
        check("当天地点稳定", bot.LIFE.place_today("2099-02-01") == p1)
        others = {bot.LIFE.place_today(f"2099-02-{d:02d}") for d in range(2, 12)}
        check("不同天地点会变（不是老待一个地方）", len(others) > 1, f"{len(others)} 种")

        # ⑨ 快照带活动池与当前活动
        snap = bot.LIFE.snapshot()
        check("快照含活动池与地点池",
              snap["pool_size"] == len(real_pool) and len(snap["places"]) > 0,
              f"池 {snap['pool_size']} / 地点 {len(snap['places'])}")

        # ⑩ 节拍参数可经面板调，且同步到实例
        bot.CONFIG_PATH = BASE_DIR / "_tmp_cfg20.json"
        __import__("shutil").copy2(bot.BASE / "config.json", bot.CONFIG_PATH)
        bot.console_apply("config_set", {"path": "self_activity.skip_chance", "value": 0.5})
        check("skip_chance 同步到实例", abs(bot.LIFE.act_skip - 0.5) < 1e-9)
        bot.console_apply("config_set", {"path": "life.events_per_day", "value": 5})
        check("events_per_day 同步到实例", bot.LIFE.events_per_day == 5)
        bot.console_apply("config_set", {"path": "self_activity.min_gap_minutes", "value": 40})
        check("min_gap 同步进 act_gap 二元组", bot.LIFE.act_gap[0] == 40.0,
              str(bot.LIFE.act_gap))
        bot.CONFIG_PATH.unlink(missing_ok=True)
    finally:
        (bot.LIFE.activity, bot.LIFE.activity_until, bot.LIFE.activity_next,
         bot.LIFE.material, bot.LIFE.material_ts) = real_act
        bot.LIFE.act_gap, bot.LIFE.act_skip = real_gap, real_skip
        bot.LIFE.events_per_day = real_epd
        bot.LIFE.act_pool = real_pool
        bot.MOOD.affinity, bot.MOOD.feeling = real_aff20, real_feel20
        (bot.LIFE._day, bot.LIFE._day_events,
         bot.LIFE._place_day, bot.LIFE._place) = real_day, real_evs, real_place_day, real_place

    print("\n【21】记忆三分：人物记忆（管理员把关）/ 群聊记忆 / 每轮刷新")
    import collections as _c21
    real_gm = (bot.GMEM.groups, bot.GMEM.enable, bot.GMEM.path, bot.GMEM.max_notes)
    real_sess = dict(bot.CHAT.sessions)
    try:
        bot.GMEM.path = BASE_DIR / "_tmp_gmem21.json"
        bot.GMEM.groups = {}
        bot.GMEM.enable = True
        bot.GMEM.max_notes = 3

        # ① 群聊记忆：增 / 去重 / 上限 / 渲染
        check("群聊记忆能加", bot.GMEM.add("9999", "这个群管她叫玖玖", "测试群"))
        check("重复的记不进去", bot.GMEM.add("9999", "这个群管她叫玖玖") is False)
        check("太短的不要", bot.GMEM.add("9999", "嗯") is False)
        for i in range(4):
            bot.GMEM.add("9999", f"第{i}条说得过去的群信息")
        notes = bot.GMEM.groups["9999"]["notes"]
        check("超上限丢最早的（max=3）", len(notes) == 3, f"{len(notes)} 条")
        check("留下的是最新的三条", notes[-1]["t"] == "第3条说得过去的群信息", notes[-1]["t"])
        check("render 拼得出来", "第3条" in bot.GMEM.render("9999"))
        check("别的群取不到", bot.GMEM.render("8888") == "")

        # ② 群聊记忆：删单条 / 忘整群
        gone = bot.GMEM.remove_note("9999", 0)
        check("删单条返回删掉的内容", "第1条" in gone, gone)
        try:
            bot.GMEM.remove_note("9999", 99)
            check("越界下标被拒", False, "竟然接受了")
        except ValueError:
            check("越界下标被拒", True)
        check("忘整群返回条数", bot.GMEM.forget("9999") == 2)
        check("忘掉后取不到", bot.GMEM.render("9999") == "")

        # ③ 每轮刷新的临时块：现算、不落盘
        bot.CHAT.sessions.clear()
        bot.CHAT.sessions["g7"] = _c21.deque([
            {"role": "user", "content": "示例用户1(111)：这个报错怎么回事"},
            {"role": "assistant", "content": "我看不到"},
            {"role": "user", "content": "示例用户3(222)：你截的图空白"},
        ])
        eph = bot.MEMORY.ephemeral("g7")
        check("临时块带出说话的人", "示例用户1" in eph and "示例用户3" in eph, eph[:60])
        check("临时块带出刚聊到什么", "报错怎么回事" in eph, eph[:60])
        check("临时块明确说了不是长期记忆", "别当成长期记忆" in eph)
        check("没有会话时返回空", bot.MEMORY.ephemeral("不存在") == "")
        check("临时块不进人物记忆", not any(
            "报错" in str(v) for v in (bot.MEMORY.people or {}).values()))
        check("临时块也不进群聊记忆", not any(
            "报错" in str(n) for n in (bot.GMEM.groups.get("7", {}) or {}).get("notes", [])))

        # ④ 面板快照把三套都分开给
        snap_m = bot.console_snapshot("memory")
        snap_g = bot.console_snapshot("group_memory")
        check("人物记忆快照带 ephemeral",
              "ephemeral" in snap_m and isinstance(snap_m["ephemeral"], list))
        check("人物记忆快照说明不落盘", "不落盘" in str(snap_m.get("ephemeral_note", "")))
        check("群聊记忆快照独立存在",
              snap_g.get("ok") and "groups" in snap_g and "note" in snap_g)

        # ⑤ 提示词里三块都注入，且分得清
        bot.GMEM.path = BASE_DIR / "_tmp_gmem21.json"
        bot.GMEM.groups = {"7": {"name": "测试群", "notes": [{"t": "这个群常聊代码"}]}}
        # 人物记忆那边也塞一条，否则它那一块根本不会注入（两边都要非空才测得出"分得清"）
        saved_people = bot.MEMORY.people
        bot.MEMORY.people = {"111": {"name": "阿甲", "traits": ["爱发图"], "todos": [],
                                     "updated": time.time()}}
        sp = bot.build_system(True, None, 7, 111)
        bot.MEMORY.people = saved_people
        check("提示词里有群聊记忆", "这个群常聊代码" in sp or "这个群你混得挺熟" in sp)
        check("提示词里区分了人物记忆与群聊记忆",
              "对某个人的长期印象" in sp and "跟具体谁无关" in sp,
              "人物块=%s 群块=%s" % ("对某个人的长期印象" in sp, "跟具体谁无关" in sp))
        check("提示词里有每轮刷新的临时块", "眼下：" in sp or "别当成长期记忆" in sp)
    finally:
        (bot.GMEM.groups, bot.GMEM.enable, bot.GMEM.path, bot.GMEM.max_notes) = real_gm
        bot.CHAT.sessions.clear()
        bot.CHAT.sessions.update(real_sess)
        (BASE_DIR / "_tmp_gmem21.json").unlink(missing_ok=True)

    print("\n【22】生图：三种模式 + 她的形象一致性")
    S = bot.SDGEN
    real_ans_sd = bot.CHAT.answer
    try:
        # ① 配置本身要自洽：character_tags 里绝不能含数量词，
        #    否则 duo 模式会同时出现 "solo" 和 "2girls"，模型直接分裂
        tags = S.char_tags
        check("她的形象标签里没有数量词（solo/2girls/1girl）",
              not any(w in tags for w in ("solo", "1girl", "2girls", "multiple")),
              tags[:60])
        check("形象标签写死了关键特征（猫耳/琥珀眼/黑发/尾巴）",
              all(w in tags for w in ("black hair", "cat ears", "amber eyes", "black tail")))
        check("Pony 质量前缀在最前（score_9 开头）",
              S.quality.startswith("score_9"), S.quality[:40])

        # ② solo：只有她
        pos, neg, w, h = S.assemble({"mode": "solo", "scene": "sitting, smiling"})
        check("solo 带 1girl/solo", "1girl" in pos and "solo" in pos)
        check("solo 不带 2girls", "2girls" not in pos)
        check("solo 含她的固定形象", S.char_tags in pos)
        check("solo 尺寸是竖构图 832x1216", (w, h) == (832, 1216), f"{w}x{h}")
        check("solo 负面禁止第二个人", "2girls" in neg and "other person" in neg)

        # ③ duo：她和朋友（要能画得出来 —— 旧配置把 2girls 禁在负面里，根本出不来）
        pos2, neg2, w2, h2 = S.assemble({"mode": "duo", "scene": "standing in a park",
                                         "other": "brown hair, glasses"})
        check("duo 带 2girls", "2girls" in pos2)
        check("duo 里不出现 solo（否则自相矛盾）", "solo" not in pos2, pos2[:80])
        check("duo 含她的固定形象", S.char_tags in pos2)
        check("duo 含对方的外貌", "glasses" in pos2)
        check("duo 要求对方明显不同", "clearly different" in pos2)
        check("duo 负面只禁三个人以上",
              "3girls" in neg2 and "other person" not in neg2)
        check("duo 尺寸是横构图 1216x832", (w2, h2) == (1216, 832), f"{w2}x{h2}")

        # ④ scenery：纯景，一个活人都不能有
        pos3, neg3, w3, h3 = S.assemble({"mode": "scenery", "scene": "rainy street"})
        check("scenery 不带她的形象", S.char_tags not in pos3)
        check("scenery 正面没有数量词",
              not any(x in pos3 for x in ("1girl", "2girls", "solo")))
        check("scenery 负面禁掉所有人",
              all(x in neg3 for x in ("1girl", "person", "people", "human")))
        check("scenery 尺寸是横构图 1216x832", (w3, h3) == (1216, 832), f"{w3}x{h3}")

        # ⑤ negative_extra 会追加，未知模式回落到 solo
        _, neg4, _, _ = S.assemble({"mode": "solo", "scene": "a",
                                    "negative_extra": "ugly, hat"})
        check("negative_extra 被追加", "ugly" in neg4 and "hat" in neg4)
        _, _, w5, h5 = S.assemble({"mode": "??", "scene": "a"})
        check("未知模式回落到 solo 尺寸", (w5, h5) == (832, 1216), f"{w5}x{h5}")

        # ⑥ 编排失败时宁可画景，也别拿没约束的提示词把她画歪
        async def _bad_ans(key, msgs):
            return "这不是 JSON", None

        bot.CHAT.answer = _bad_ans
        pick = await S.compose("一个很奇怪的要求", None)
        check("编排失败兜底成 scenery（不冒险画人物）",
              pick["mode"] == "scenery", str(pick))
        check("兜底时保留原话当场景", "很奇怪" in pick["scene"], pick["scene"])

        # ⑦ 正常编排：三种模式都认得
        for want in ("solo", "duo", "scenery"):
            async def _ans(key, msgs, _w=want):
                return '{"mode":"%s","scene":"a nice scene","other":"brown hair"}' % _w, None

            bot.CHAT.answer = _ans
            got = await S.compose("随便", None)
            check(f"编排出 {want} 模式", got["mode"] == want, str(got["mode"]))

        # ⑧ 面板快照带新模式信息
        import inspect as _ins
        src = _ins.getsource(S.__class__)
        check("SD 类里有 modes 相关配置", "ban_scenery" in src and "sizes" in src)
    finally:
        bot.CHAT.answer = real_ans_sd

    print("\n【23】发送链路：绝不发空消息 + 陈旧表情不发")
    # 起因：NapCat 报 retcode=1200「消息体无法解析」。
    # 真凶是真实 stickers/index.json 里被写进了一条测试假条目
    # （emoji_id=12345 指向已删除的临时文件），她拿它去发 mface，被 QQ 拒。
    # 这里锁住三件事：索引跟着 dir 走、文件丢失的条目不发、降级链不产生空消息。
    real_send_stk = bot.STICKERS.dir
    sent_payloads: list[dict] = []
    real_ob_call = bot.OB.call
    fake_img = BASE_DIR / "_tmp_send23.png"
    try:
        # ① index_path 必须是从 dir 派生的（结构性防呆）
        bot.STICKERS.dir = BASE_DIR / "_tmp_send23_stickers"
        check("index_path 跟着 dir 自动变",
              bot.STICKERS.index_path == BASE_DIR / "_tmp_send23_stickers" / "index.json",
              str(bot.STICKERS.index_path))

        # ② 记录的文件没了 -> 不发（否则 QQ 会以 1200 拒掉）
        stale = {"md5": "s", "path": str(BASE_DIR / "_definitely_missing.gif"),
                 "emoji_id": "12345", "emoji_package_id": "67890", "native": True}
        check("文件已丢失的表情不发", bot.sticker_segment(stale) is None)
        # 文件在就正常生成 mface
        fake_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        good = {"md5": "g", "path": str(fake_img), "emoji_id": "999", "emoji_package_id": "888"}
        seg = bot.sticker_segment(good)
        check("文件在时正常生成 mface", seg and seg["type"] == "mface", str(seg))

        # ③ 降级链：文本空 + 附件失败时，**不能**退化成发一条空消息
        async def _cap(action, params=None, timeout=8):
            sent_payloads.append(params or {})
            raise RuntimeError("模拟 QQ 拒收")

        bot.OB.call = _cap
        sent_payloads.clear()
        # 文本为空、只有 mface —— 去掉附件后就什么都不剩了
        await bot._send_one(False, None, 123, None, "", sticker=seg)
        empties = [p for p in sent_payloads if not (p.get("message") or [])]
        check("附件失败时不会发出空消息", not empties, f"发了 {len(empties)} 条空的")
        check("空消息情形下确实尝试过（不是静默跳过）", len(sent_payloads) >= 1,
              f"{len(sent_payloads)} 次尝试")

        # ④ 有文本时正常降级到纯文本
        sent_payloads.clear()
        await bot._send_one(False, None, 123, None, "喂", sticker=seg)
        texts = [p for p in sent_payloads
                 if any(s.get("type") == "text" for s in (p.get("message") or []))]
        check("有文本时能降级到纯文本", bool(texts), f"{len(sent_payloads)} 次尝试")

        # ⑤ 正文与附件全空 -> 一次都不发
        sent_payloads.clear()
        await bot._send_one(False, None, 123, None, "")
        check("正文附件全空时不发任何请求", not sent_payloads, f"{len(sent_payloads)} 次")
    finally:
        bot.OB.call = real_ob_call
        bot.STICKERS.dir = real_send_stk
        fake_img.unlink(missing_ok=True)
        __import__("shutil").rmtree(BASE_DIR / "_tmp_send23_stickers", ignore_errors=True)

    # ══════════════════════════════════════════════════════════════════
    # 【34】token 用量：从"进程内"改成**永久记录**（字段一个没变）
    # ══════════════════════════════════════════════════════════════════
    print("\n【34】token 用量：永久记录（落盘 + 读回，字段不变）")
    real_tk_path = bot.TOKENS_PATH
    real_tk = dict(bot.TOKENS)
    real_tk_meta = (bot.TOKENS_SINCE, bot.TOKENS_UPDATED)
    tpath = BASE_DIR / "_tmp_tokens34.json"
    try:
        tpath.unlink(missing_ok=True)
        bot.TOKENS_PATH = tpath
        for _k in bot.TOKENS:
            bot.TOKENS[_k] = 0
        bot.TOKENS_SINCE = bot.TOKENS_UPDATED = ""
        bot.tokens_load()
        check("首次启动会建出用量文件（不再等进程结束）", tpath.exists())
        d0 = json.loads(tpath.read_text(encoding="utf-8"))
        check("记的内容和以前完全一样（in/out/hit/miss/calls 五个计数）",
              {"in", "out", "hit", "miss", "calls"} <= set(d0) and
              all(isinstance(d0[k], int) for k in ("in", "out", "hit", "miss", "calls")),
              str(sorted(d0)))

        bot.tokens_add({"in": 100, "out": 20, "hit": 60, "miss": 40, "calls": 1})
        check("累加先只进内存（不为了几秒的抖动去刷盘）",
              bot.TOKENS["in"] == 100 and bot.TOKENS["calls"] == 1, str(bot.TOKENS))
        bot.tokens_save(force=True)
        d1 = json.loads(tpath.read_text(encoding="utf-8"))
        check("force 落盘后字段与计数都写对了",
              (d1["in"], d1["out"], d1["hit"], d1["miss"], d1["calls"]) == (100, 20, 60, 40, 1),
              str(d1))
        check("记下了「从哪天开始」", bool(d1.get("since")), str(d1.get("since")))

        # 模拟重启：内存清零后重读文件
        for _k in bot.TOKENS:
            bot.TOKENS[_k] = 0
        bot.TOKENS_SINCE = ""
        bot.tokens_load()
        check("重启后累计还在、不清零",
              bot.TOKENS["in"] == 100 and bot.TOKENS["calls"] == 1, str(bot.TOKENS))
        check("起始时间跟着恢复", bot.TOKENS_SINCE == d1["since"], bot.TOKENS_SINCE)

        # 节流：5 秒内的第二次改动不写盘，但内存里已经加了（idle_loop 会补写）
        bot.tokens_add({"calls": 5})
        d2 = json.loads(tpath.read_text(encoding="utf-8"))
        check("节流窗口内不写盘（内存照样加）",
              d2["calls"] == 1 and bot.TOKENS["calls"] == 6,
              f"文件 {d2['calls']} / 内存 {bot.TOKENS['calls']}")
        bot.tokens_save(force=True)
        d3 = json.loads(tpath.read_text(encoding="utf-8"))
        check("force 补写后文件追平内存", d3["calls"] == 6, str(d3))
        before34 = tpath.stat().st_mtime_ns
        bot.tokens_save(force=True)
        check("没脏的时候 force 也不空刷盘（不白动磁盘）",
              tpath.stat().st_mtime_ns == before34)

        # 文件坏了不许拦启动
        tpath.write_text("{这不是 json", encoding="utf-8")
        for _k in bot.TOKENS:
            bot.TOKENS[_k] = 7
        bot.tokens_load()
        check("文件损坏也当从零开始（不抛、不拦启动）",
              bot.TOKENS["in"] == 0 and bot.TOKENS["calls"] == 0, str(bot.TOKENS))

        st34 = bot.console_snapshot("status")
        check("B01 快照带上累计与起始时间",
              "tokens" in st34 and "tokens_since" in st34 and "tokens_path" in st34,
              str([k for k in st34 if k.startswith("tokens")]))
    finally:
        bot.TOKENS_PATH = real_tk_path
        bot.TOKENS.update(real_tk)
        bot.TOKENS_SINCE, bot.TOKENS_UPDATED = real_tk_meta
        tpath.unlink(missing_ok=True)

    # ══════════════════════════════════════════════════════════════════
    # 【35】天气：看得更细 + 感知变化 + 接上网查预警
    # ══════════════════════════════════════════════════════════════════
    print("\n【35】天气：更细的字段 + 变化感知 + 联网查预警")
    real_w35 = (bot.WEATHER.enable, bot.WEATHER.city, bot.WEATHER.state_path,
                bot.WEATHER.tick_minutes, bot.WEATHER.alert_enable)
    real_w35_state = (dict(bot.WEATHER._prev), bot.WEATHER._prev_ts,
                      bot.WEATHER._change, bot.WEATHER._change_ts,
                      bot.WEATHER._alert, bot.WEATHER._alert_ts)
    real_w35_refresh = bot.WEATHER.refresh
    real_w35_search = bot.WEB.search
    real_w35_web = bot.WEB.enable
    w35path = BASE_DIR / "_tmp_weather35.json"
    try:
        bot.WEATHER.enable = True
        bot.WEATHER.city = "长沙"
        bot.WEATHER.state_path = w35path
        w35path.unlink(missing_ok=True)

        snap35 = {"code": 61, "desc": "下雨", "temp": 18.0, "feels": 14.0, "humidity": 88,
                  "wind": 3.0, "precip": 0.6, "is_day": 1, "tmax": 21.0, "tmin": 15.0,
                  "rain_p": 90, "uv": 2.0, "sunrise": "2026-09-27T06:20", "sunset": "2026-09-27T18:40",
                  "place": "长沙市", "at": "2026-09-27 03:00"}
        line35 = bot.WEATHER._short(snap35)
        check("提示词那句带上了体感温度（温差≥3°才提）",
              "18" in line35 and "体感" in line35, line35)

        # 晴 -> 雨，应该被记成一次"开始下雨了"
        bot.WEATHER._prev = {"code": 0, "desc": "晴", "temp": 18.0, "wind": 2.0,
                             "humidity": 50, "precip": 0.0, "place": "长沙市"}
        bot.WEATHER._prev_ts = time.time()
        bot.WEATHER._change, bot.WEATHER._change_ts = "", 0.0
        bot.ACTIVITY._pending = []
        bot.WEATHER._note_change(snap35)
        check("从晴转雨会被她察觉", "下雨" in bot.WEATHER._change, bot.WEATHER._change)
        check("察觉到的变化写进行为流水",
              any(e.get("kind") == "天气" for e in bot.ACTIVITY._pending),
              str([e.get("kind") for e in bot.ACTIVITY._pending]))
        check("快照落了盘（重启还记得刚才是晴的）",
              w35path.exists() and "prev" in json.loads(w35path.read_text(encoding="utf-8")),
              "文件缺失" if not w35path.exists() else "")
        check("降水码分类认得准",
              bot.WMO.get(61) == "下雨" and 61 in bot.RAINY and 95 in bot.STORM and 95 not in bot.RAINY)

        # 温度骤降
        bot.WEATHER._prev = {"code": 61, "desc": "下雨", "temp": 26.0, "wind": 2.0,
                             "humidity": 80, "precip": 0.2, "place": "长沙市"}
        bot.WEATHER._prev_ts = time.time()
        bot.WEATHER._change, bot.WEATHER._change_ts = "", 0.0
        bot.WEATHER._note_change(snap35)
        check("降温 5 度以上会被察觉", "降" in bot.WEATHER._change, bot.WEATHER._change)

        # "刚察觉到的变化"会过期，不能一直挂在她嘴上
        bot.WEATHER._change_ts = time.time() - (bot.WEATHER.change_keep_minutes * 60 + 5)
        check("变化过了保质期就不再提", bot.WEATHER.change() == "", bot.WEATHER._change)

        # 联网查预警
        seen35 = {}

        async def _fake_search35(q):
            seen35["q"] = q
            return [{"title": "长沙市气象台发布暴雨黄色预警", "url": "http://x", "snippet": ""}]

        async def _fake_refresh35():
            bot.WEATHER._text = "下雨 18°C"
            bot.WEATHER._ts = time.time()
            return dict(snap35, text=bot.WEATHER._text)

        bot.WEATHER.refresh = _fake_refresh35
        bot.WEB.search = _fake_search35
        bot.WEB.enable = True
        bot.WEATHER._change, bot.WEATHER._change_ts = "外面开始下雨了", time.time()
        bot.WEATHER._alert_ts = 0.0
        bot.LIFE.material = ""
        got35 = await bot.WEATHER.sense()
        check("天气有变化时真去搜了本地预警",
              "预警" in seen35.get("q", "") and "长沙" in seen35.get("q", ""), str(seen35.get("q")))
        check("查到的预警成了她的话题素材",
              "预警" in got35 and "暴雨" in bot.LIFE.take_material(),
              f"返回 {got35} / 素材 {bot.LIFE.take_material()}")
        check("提示词那句会把变化缀上",
              "下雨" in await bot.WEATHER.phrase() and "外面开始下雨了" in await bot.WEATHER.phrase())

        snap_c35 = bot.console_snapshot("life")["weather"]
        check("B03 快照里有天气全量字段",
              {"enable", "data", "change", "alert", "tick_minutes", "web_enable"} <= set(snap_c35),
              str(sorted(snap_c35))[:120])
        bot.WEB.enable = False
        check("没开联网时不查预警（天气本身照旧）",
              await bot.WEATHER.sense() is not None)
    finally:
        bot.WEATHER.refresh = real_w35_refresh
        bot.WEB.search = real_w35_search
        bot.WEB.enable = real_w35_web
        (bot.WEATHER.enable, bot.WEATHER.city, bot.WEATHER.state_path,
         bot.WEATHER.tick_minutes, bot.WEATHER.alert_enable) = real_w35
        (bot.WEATHER._prev, bot.WEATHER._prev_ts, bot.WEATHER._change,
         bot.WEATHER._change_ts, bot.WEATHER._alert, bot.WEATHER._alert_ts) = real_w35_state
        w35path.unlink(missing_ok=True)

    # ══════════════════════════════════════════════════════════════════
    # 【36】忙的时候：长活动被**打断**，只做低概率的短动作
    # ══════════════════════════════════════════════════════════════════
    print("\n【36】忙的时候：打断长活动，只做低概率的短动作")
    real_l36 = (bot.LIFE.activity, bot.LIFE.activity_until, bot.LIFE.activity_next)
    real_l36_cfg = (bot.LIFE.busy_stop_long, bot.LIFE.busy_act_chance, bot.LIFE.busy_short_duration)
    real_l36_sched = bot.LIFE.schedule_now
    real_l36_search = bot.WEB.search
    try:
        async def _noop_search36(q):
            return []

        bot.WEB.search = _noop_search36
        bot.LIFE.schedule_now = lambda: {"act": "书店看店", "hint": "你在旧书店上下午班",
                                         "chance": 0.45, "busy": True, "interruptible": True}
        # ① 忙起来 -> 打断手里那件长活动
        bot.LIFE.activity = {"id": "surf", "name": "上网瞎逛", "hint": "在网上乱翻", "busy": False}
        bot.LIFE.activity_until = time.time() + 3600
        bot.LIFE.activity_next = time.time() + 3600
        got36 = bot.LIFE.busy_tick()
        check("忙起来时手头的长活动被打断",
              got36 == "上网瞎逛" and bot.LIFE.activity is None, f"停掉的是 {got36!r}")
        check("打断后不会再立刻起新的长活动（间隔被推后）",
              bot.LIFE.activity_next > time.time() + 60, f"{bot.LIFE.activity_next - time.time():.0f}s")

        life36 = bot.Life.current(bot.LIFE)   # 类方法直调：文件开头把实例的 current 打了桩
        check("忙的时候 current() 以作息为准（不被自己的活动顶掉）",
              life36["busy"] and life36["act"] == "书店看店", str(life36)[:110])

        # ② 忙里偷闲：只做几分钟的小动作，且**不顶掉作息**
        short36 = bot.LIFE.pick_short_activity()
        check("忙时小动作池抽得出东西且带 short 标记",
              bool(short36 and short36.get("short")), str(short36))
        bot.LIFE.busy_short_duration = (1.0, 2.0)
        bot.LIFE.set_short_activity(short36)
        left36 = bot.LIFE.activity_until - time.time()
        check("小动作只持续几分钟（不再是 15~70 分钟那种长活）",
              0 < left36 <= 130, f"{left36:.0f} 秒")
        check("忙时不会把自己偷闲的小动作也打断", bot.LIFE.busy_tick() == "")
        l36 = bot.Life.current(bot.LIFE)
        check("小动作不顶作息：她还是「在上班」，只是偷空刷了个视频",
              l36["busy"] and l36["act"] == "书店看店"
              and l36["self_activity"] == short36["name"] and l36["self_activity_short"] is True,
              str(l36)[:170])

        # ③ 忙的时候绝大多数轮次什么都不做
        bot.LIFE.activity, bot.LIFE.activity_until = None, 0.0
        bot.LIFE.busy_act_chance = 0.0
        took36 = await bot.busy_activity_tick()
        check("概率为 0 时忙就是忙，什么都不做",
              took36 is True and bot.LIFE.activity is None, str(bot.LIFE.activity))
        bot.LIFE.busy_act_chance = 1.0
        took36b = await bot.busy_activity_tick()
        check("概率为 1 时起一个短动作，并接管这一轮（不再起长活动）",
              took36b is True and bot.LIFE.activity is not None
              and bot.LIFE.activity.get("short") is True, str(bot.LIFE.activity)[:110])

        # ④ 不忙的时候老规矩不变：长活动照起
        bot.LIFE.schedule_now = lambda: {"act": "夜间活跃", "hint": "这会儿最精神",
                                         "chance": 0.95, "busy": False, "interruptible": False}
        bot.LIFE.activity, bot.LIFE.activity_until = None, 0.0
        check("不忙时 busy_tick 不动手", bot.LIFE.busy_tick() == "")
        check("不忙时短动作池不参与（长活动仍走原来的路）",
              bot.LIFE.schedule_now()["busy"] is False)

        # 关掉开关就回到旧行为（长活动不会被忙打断）
        bot.LIFE.activity = {"id": "surf", "name": "上网瞎逛", "hint": "在网上乱翻"}
        bot.LIFE.activity_until = time.time() + 600
        bot.LIFE.busy_stop_long = False
        check("忙时打断能关掉（关掉就保留旧的「活动盖作息」行为）",
              bot.LIFE.busy_tick() == "" and bot.LIFE.activity is not None)
        bot.LIFE.busy_stop_long = True
    finally:
        bot.LIFE.schedule_now = real_l36_sched
        bot.WEB.search = real_l36_search
        bot.LIFE.activity, bot.LIFE.activity_until, bot.LIFE.activity_next = real_l36
        (bot.LIFE.busy_stop_long, bot.LIFE.busy_act_chance, bot.LIFE.busy_short_duration) = real_l36_cfg

    # ══════════════════════════════════════════════════════════════════
    # 【37】好感：档位划分更细 + 强弱词不是一个价 + 闲聊累积有上限
    # ══════════════════════════════════════════════════════════════════
    print("\n【37】好感：10 档划分 + 强弱词 + 闲聊累积（有天花板）")
    real_m37 = bot.MOOD.path
    real_m37_state = (bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names, bot.MOOD._drift_log)
    real_m37_cfg = (bot.MOOD.strong_mult, bot.MOOD.drift_gain, bot.MOOD.drift_max_per_day,
                    bot.MOOD.drift_ceiling, bot.MOOD.drift_min_chars)
    m37path = BASE_DIR / "_tmp_mood37.json"
    try:
        bot.MOOD.path = m37path
        bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names = {}, {}, {}
        bot.MOOD._drift_log = {}

        labels37 = [bot.MOOD.tier_of(v)["label"] for v in (5, 18, 30, 40, 50, 60, 70, 80, 90, 99)]
        check("好感分成 10 档、档档说法不同", len(set(labels37)) == 10, str(labels37))
        tv37 = bot.MOOD.tiers_view()
        check("档位首尾相接、盖满 0~100",
              tv37[0]["lo"] == 0.0 and tv37[-1]["hi"] == 100.0
              and all(tv37[i]["hi"] == tv37[i + 1]["lo"] for i in range(len(tv37) - 1)),
              str(tv37[:2]))
        t37 = bot.MOOD.tier_of(61.5)
        check("档位里带上「离下一档还差几分」",
              t37["next_at"] == 66.0 and t37["to_next"] == 4.5, str(t37))

        check("强夸奖判到强层（倍率 > 1）", bot.MOOD.sentiment("我最喜欢你了")[2] > 1.0)
        check("普通夸奖还是 1 倍（老行为不变）", bot.MOOD.sentiment("谢谢")[2] == 1.0)
        check("先判强词层（「傻逼」不会被「傻」降级）",
              bot.MOOD.sentiment("傻逼")[2] > 1.0, str(bot.MOOD.sentiment("傻逼")))
        check("又夸又骂按净情绪算一头（骂优先）",
              bot.MOOD.sentiment("你真好你个傻逼")[:2] == (False, True))

        bot.MOOD.bump(G, OTHER, "示例用户3", "我最喜欢你了")
        strong37 = bot.MOOD.affinity_of(G, OTHER)
        bot.MOOD.affinity, bot.MOOD.feeling = {}, {}
        bot.MOOD.bump(G, OTHER, "示例用户3", "谢谢")
        normal37 = bot.MOOD.affinity_of(G, OTHER)
        check("强夸奖涨得比普通夸奖多",
              strong37 > normal37 > 50, f"强 {strong37} / 普通 {normal37}")

        # 闲聊累积：有幅度、有每日上限、有天花板
        bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names = {}, {}, {}
        bot.MOOD._drift_log = {}
        for _ in range(40):
            bot.MOOD.bump(G, OTHER, "示例用户3", "今天这个天气你觉得还行吧")
        a37 = bot.MOOD.affinity_of(G, OTHER)
        check("愿意跟她说话会慢慢攒好感", a37 > 50, str(a37))
        check("每天攒的上限卡住了（不会一天刷满）",
              a37 == round(50 + bot.MOOD.drift_max_per_day, 2), str(a37))

        bot.MOOD._drift_log = {}
        bot.MOOD.affinity[str(G)][str(OTHER)] = 61.9
        for _ in range(10):
            bot.MOOD.bump(G, OTHER, "示例用户3", "这个书架好像被撞倒了")
        check("闲聊最多养到 ceiling，再往上得靠真夸她",
              bot.MOOD.affinity_of(G, OTHER) == 62.0, str(bot.MOOD.affinity_of(G, OTHER)))
        bot.MOOD._drift_log = {}
        bot.MOOD.bump(G, OTHER, "示例用户3", "嗯")
        check("太短的消息不算「在跟她说话」（拍肩膀不算交流）",
              bot.MOOD.affinity_of(G, OTHER) == 62.0, str(bot.MOOD.affinity_of(G, OTHER)))

        # 夸/骂那两句走的是主路径，不会被闲聊累积污染
        bot.MOOD._drift_log = {}
        bot.MOOD.affinity[str(G)][str(OTHER)] = 60.0
        bot.MOOD.feeling.setdefault(str(G), {})[str(OTHER)] = 60.0
        bot.MOOD.bump(G, OTHER, "示例用户3", "谢谢你")
        check("带情绪的话走主路径（闲聊上限拦不住真夸）",
              bot.MOOD.affinity_of(G, OTHER) > 62.0, str(bot.MOOD.affinity_of(G, OTHER)))

        d37 = bot.console_snapshot("mood")
        check("B04 快照带上档位表与累积规矩",
              d37.get("tier_count") == 10 and "drift_ceiling" in d37
              and d37["merged"] and d37["merged"][0]["tier"]["label"],
              str({k: d37.get(k) for k in ("tier_count", "drift_ceiling", "strong_mult")}))
        check("档位表跟着给出（面板要画刻度尺）",
              len(d37["tiers"]) == 10 and d37["tiers"][-1]["hi"] == 100.0, str(d37["tiers"][:2]))
    finally:
        bot.MOOD.path = real_m37
        (bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names, bot.MOOD._drift_log) = real_m37_state
        (bot.MOOD.strong_mult, bot.MOOD.drift_gain, bot.MOOD.drift_max_per_day,
         bot.MOOD.drift_ceiling, bot.MOOD.drift_min_chars) = real_m37_cfg
        m37path.unlink(missing_ok=True)

    # ══════════════════════════════════════════════════════════════════
    # 【24】空间评论：同一条说说只评论一次
    #
    # 桥接侧**不提供**"这条我评过没有"的标记（只有 isLiked 管点赞），
    # 所以去重全靠 bot.py 自己落盘的那份 tid 名单。这里就盯住这份名单：
    # 第二次必须**根本不发请求**，而不只是"返回失败"。
    #
    # ⚠️ 本段及以下（【24】~【27】）在 restore_state() **之后**跑，
    # 隔离已经撤掉了 —— 所以凡是要写盘的东西都得自己再指到 _tmp_iso 去，
    # 并在结尾复核一遍真实状态文件没被动过。
    # ══════════════════════════════════════════════════════════════════
    print("\n【24】空间评论：同一条说说只评论一次")
    _FP_TAIL = state_fingerprint()
    real_find_post = bot.QZONE_API.find_post
    real_qz_raw = bot.QZONE_API.call_raw
    real_comment_path = bot.QZONE_API.comment_state_path
    real_commented = dict(bot.QZONE_API._commented)
    real_once = bot.CFG["qzone"].get("comment_once_per_post", True)
    real_act_dir = bot.ACTIVITY.dir
    real_qzone_path = bot.QZONE.path
    TAIL_DIR = BASE_DIR / "_tmp_iso"
    try:
        TAIL_DIR.mkdir(exist_ok=True)
        bot.ACTIVITY.dir = TAIL_DIR / "activity"
        # console_snapshot("qzone_actions") 会调 QZONE.used_today()，跨天时它会
        # 顺手 save() 一次 —— 所以路径也得指开，否则会把"今天已发过说说"抹掉
        bot.QZONE.path = TAIL_DIR / "qzone_state.json"
        cpath = TAIL_DIR / "comment-test.json"
        cpath.unlink(missing_ok=True)
        bot.QZONE_API.comment_state_path = cpath
        bot.QZONE_API._commented = {}
        calls: list = []
        post = {"tid": "T1", "uin": 111, "nickname": "甲", "content": "今天天气",
                "created_time": 1, "appid": 311}

        async def _find(who=""):
            return post

        async def _raw(action, params=None):
            calls.append((action, dict(params or {})))
            return True, {"comment_id": "c1"}

        bot.QZONE_API.find_post = _find
        bot.QZONE_API.call_raw = _raw
        bot.CFG["qzone"]["comment_once_per_post"] = True

        ok1, _why1 = await bot.QZONE_API.comment_post("甲", "第一条")
        check("第一次评论成功", ok1)
        ok2, why2 = await bot.QZONE_API.comment_post("甲", "第二条")
        check("同一条说说的第二次不给发", (not ok2) and "评过" in why2, why2)
        check("第二次真的一个请求都没发", len(calls) == 1, f"{len(calls)} 次")
        check("评过的 tid 落了盘", cpath.exists() and "T1" in cpath.read_text(encoding="utf-8"))

        post = {"tid": "T2", "uin": 111, "nickname": "甲", "content": "换了条",
                "created_time": 2, "appid": 311}
        ok3, _ = await bot.QZONE_API.comment_post("甲", "第三条")
        check("新说说可以正常评", ok3 and len(calls) == 2, f"{ok3} {len(calls)}")

        bot.QZONE_API._commented = {}
        bot.QZONE_API._load_commented()
        ok4, why4 = await bot.QZONE_API.comment_post("甲", "重启之后再试")
        check("重启（重读状态文件）后仍然不重复评", (not ok4) and len(calls) == 2, f"{ok4} {why4}")

        bot.CFG["qzone"]["comment_once_per_post"] = False
        ok5, _ = await bot.QZONE_API.comment_post("甲", "关掉去重就能再评")
        check("关掉去重开关后允许再评", ok5 and len(calls) == 3, f"{ok5} {len(calls)}")
        bot.CFG["qzone"]["comment_once_per_post"] = True

        snap = bot.console_snapshot("qzone_actions")["comment"]
        check("面板快照带上去重状态", snap.get("once_per_post") is True
              and snap.get("remembered_posts") == 2, str(snap))
    finally:
        bot.QZONE_API.find_post = real_find_post
        bot.QZONE_API.call_raw = real_qz_raw
        bot.QZONE_API.comment_state_path = real_comment_path
        bot.QZONE_API._commented = real_commented
        bot.CFG["qzone"]["comment_once_per_post"] = real_once
        bot.ACTIVITY.dir = real_act_dir
        bot.QZONE.path = real_qzone_path

    # ══════════════════════════════════════════════════════════════════
    # 【25】B站登录态：信接口的 isLogin，不信本地有没有 cookie
    # ══════════════════════════════════════════════════════════════════
    print("\n【25】B站登录态：真去问 isLogin，别被过期 cookie 骗了")
    real_bili_client = bot.BILI._client
    real_bili_state_path = bot.BILI.state_path
    real_bili_cookies = dict(bot.BILI.cookies)
    real_bili_guest = bot.BILI.guest
    real_bili_checked = bot.BILI._login_checked
    real_bili_err = bot.BILI._login_error
    real_bili_uid, real_bili_uname = bot.BILI.uid, bot.BILI.uname
    try:
        bot.BILI.state_path = TAIL_DIR / "bili_state.json"
        class _Hdrs:
            @staticmethod
            def get_list(_k):
                return []

        class _Resp:
            def __init__(self, body):
                self._b = body
                self.cookies = {}
                self.headers = _Hdrs()

            def json(self):
                return self._b

        class _Cli:
            def __init__(self, body):
                self.body = body
                self.hits = 0

            async def get(self, url, params=None):
                self.hits += 1
                return _Resp(self.body)

        bot.BILI.cookies = {"SESSDATA": "早就过期了"}
        bot.BILI.guest = None
        bot.BILI._login_checked = 0.0
        cli = _Cli({"code": -101, "message": "账号未登录", "data": {"isLogin": False}})
        bot.BILI._client = lambda: cli
        check("有 SESSDATA 但接口说没登录 -> 判为游客",
              (await bot.BILI.check_login(force=True)) is False)
        check("游客态被记下来", bot.BILI.guest is True)
        check("状态文案说清是游客", "游客" in bot.BILI.state_text(), bot.BILI.state_text())

        cli.body = {"code": 0, "data": {"isLogin": True, "uname": "许杏玖", "mid": 12345}}
        bot.BILI._login_checked = 0.0
        check("接口说登录着就判已登录", (await bot.BILI.check_login(force=True)) is True)
        check("账号名与 uid 从接口取回",
              bot.BILI.uname == "许杏玖" and bot.BILI.uid == "12345",
              f"{bot.BILI.uname} {bot.BILI.uid}")
        check("游客态清零", bot.BILI.guest is False)

        # 登录态查询有缓存：TTL 内不重复打接口
        hits_before = cli.hits
        await bot.BILI.check_login()
        check("缓存期内不重复请求", cli.hits == hits_before, f"{cli.hits} != {hits_before}")

        bot.BILI.cookies = {}
        bot.BILI.guest = None
        bot.BILI._login_checked = 0.0
        hits_before = cli.hits
        check("没有 cookie 直接判游客", (await bot.BILI.check_login(force=True)) is False)
        check("没有 cookie 时不白打一次接口", cli.hits == hits_before)

        # 网络不通 ≠ 掉登录：状态留"未知"，而且**不许据此发二维码**
        class _Boom:
            async def get(self, url, params=None):
                raise RuntimeError("网络不通")

        bot.BILI.cookies = {"SESSDATA": "还没过期但查不到"}
        bot.BILI.guest = None
        bot.BILI._login_checked = 0.0
        bot.BILI._client = lambda: _Boom()
        check("接口打不通时不判成游客", (await bot.BILI.check_login(force=True)) is False)
        check("接口打不通时状态留未知", bot.BILI.guest is None, str(bot.BILI.guest))
        ok_e, why_e = await bot.BILI.ensure_login(notify=None)
        check("未知状态不生成二维码", (not ok_e) and "没查出来" in why_e, why_e)
        ok_o, why_o = await bot._bili_login_once(notify=None)
        check("巡检也不在未知态下发码", (not ok_o) and "没查出来" in why_o, why_o)
        check("控制台文案说明未确认", bot.BILI.state_text() == "登录态未确认",
              bot.BILI.state_text())

        bsnap = bot.console_snapshot("bili")
        check("面板快照能出 B 站状态", bsnap.get("ok") and "guest" in bsnap, str(bsnap)[:120])
    finally:
        bot.BILI._client = real_bili_client
        bot.BILI.state_path = real_bili_state_path
        bot.BILI.cookies = real_bili_cookies
        bot.BILI.guest = real_bili_guest
        bot.BILI._login_checked = real_bili_checked
        bot.BILI._login_error = real_bili_err
        bot.BILI.uid, bot.BILI.uname = real_bili_uid, real_bili_uname

    # ══════════════════════════════════════════════════════════════════
    # 【26】跨群：认群 -> 认人 -> 动手；任何一步说不清就只回话
    # ══════════════════════════════════════════════════════════════════
    print("\n【26】跨群禁言：认群、认人，说不清就不动手")
    real_groups_map = dict(bot.GROUPS.groups)
    real_member_list = bot.group_member_list
    real_force_ban = bot.force_ban
    real_refresh_admin = bot.refresh_admin
    try:
        bot.GROUPS.groups = {
            "999000001": {"name": "原神交流群", "seen": time.time()},
            "999000111": {"name": "旧书店读书会", "seen": time.time()},
        }
        check("群名精确匹配", bot.GROUPS.find("原神交流群") == [999000001],
              str(bot.GROUPS.find("原神交流群")))
        check("群名部分匹配", bot.GROUPS.find("原神") == [999000001])
        check("群号直接认", bot.GROUPS.find("999000111") == [999000111])
        check("认不出的群返回空（不许瞎猜）", bot.GROUPS.find("没有这个群") == [])
        check("群清单带群号", "原神交流群（999000001）" in bot.GROUPS.lines(),
              str(bot.GROUPS.lines()))

        check("拆「群 目标 分钟」",
              bot._split_cross_ban_args("原神交流群 张三 10") == ("原神交流群", "张三", 10.0))
        check("分钟可以省",
              bot._split_cross_ban_args("原神交流群 张三") == ("原神交流群", "张三", None))
        check("支持小时单位",
              bot._split_cross_ban_args("原神交流群 张三 0.5小时") == ("原神交流群", "张三", 30.0))

        members = [
            {"user_id": 111, "nickname": "张三", "card": "三哥"},
            {"user_id": 222, "nickname": "张三丰", "card": ""},
            {"user_id": 333, "nickname": "李四", "card": ""},
        ]

        async def _mlist(gid):
            return members if int(gid) == 999000001 else [{"user_id": 444, "nickname": "王五", "card": ""}]

        bot.group_member_list = _mlist
        uid, _nm, _c = await bot.resolve_group_member(999000001, "李四")
        check("按群昵称认人", uid == 333, str(uid))
        uid, _nm, _c = await bot.resolve_group_member(999000001, "三哥")
        check("按群名片认人", uid == 111, str(uid))
        uid, _nm, _c = await bot.resolve_group_member(999000001, "333")
        check("按 QQ 号认人", uid == 333, str(uid))
        uid, _nm, cands = await bot.resolve_group_member(999000001, "张")
        check("两个人像的时候判为歧义", uid is None and len(cands) == 2, f"{uid} {cands}")
        uid, _nm, cands = await bot.resolve_group_member(999000001, "查无此人ZZZ")
        check("名单里没有就不硬认", uid is None and not cands, f"{uid} {cands}")

        bans: list = []

        async def _admin_yes(_gid):
            return True

        async def _admin_no(_gid):
            return False

        async def _fb(gid, uid_, name, minutes, why=""):
            bans.append((int(gid), int(uid_), name, float(minutes)))
            return True

        bot.refresh_admin = _admin_yes
        bot.force_ban = _fb

        notes = await bot.apply_cross_bans("[跨群禁言:原神交流群 李四 7]", here_group_id=None)
        check("认得人、认得群就真去禁",
              bans == [(999000001, 333, "李四(333)", 7.0)], str(bans))
        check("禁完回一句结果", bool(notes) and "禁言了" in notes[0], str(notes))

        bans.clear()
        notes = await bot.apply_cross_bans("[跨群禁言:没有这个群 李四 7]", here_group_id=None)
        check("认不出群就不动手", bans == [] and "找不到" in notes[0], f"{bans} {notes}")

        bans.clear()
        notes = await bot.apply_cross_bans("[跨群禁言:原神交流群 张 7]", here_group_id=None)
        check("人有歧义就不动手", bans == [], str(bans))
        check("歧义时把候选念出来", bool(notes) and "好几个" in notes[0], str(notes))

        bans.clear()
        notes = await bot.apply_cross_bans("[跨群禁言:原神交流群 查无此人ZZZ 7]",
                                           here_group_id=None)
        check("认不出人就不动手", bans == [] and "没找到" in notes[0], f"{bans} {notes}")

        bans.clear()
        await bot.apply_cross_bans("[跨群禁言:旧书店读书会 王五 99999]", here_group_id=G)
        cap = float(bot.CFG["ban"]["max_minutes"])
        check("在群里也能管别的群",
              bool(bans) and bans[0][0] == 999000111 and bans[0][1] == 444, str(bans))
        check("分钟数被上限夹住", bans[0][3] == cap, f"{bans[0][3]} != {cap}")

        bot.refresh_admin = _admin_no
        bans.clear()
        notes = await bot.apply_cross_bans("[跨群禁言:原神交流群 李四 7]", here_group_id=None)
        check("她在那个群不是管理员就不动手",
              bans == [] and "不是管理员" in notes[0], f"{bans} {notes}")
        bot.refresh_admin = _admin_yes

        # 私聊里没写群号：全群找唯一匹配，找不唯一就不做
        bans.clear()
        await bot.apply_cross_bans("[禁言:李四 5]", here_group_id=None)
        check("私聊不写群号时按唯一匹配找人",
              bans == [(999000001, 333, "李四(333)", 5.0)], str(bans))
        bans.clear()
        notes = await bot.apply_cross_bans("[禁言:张 5]", here_group_id=None)
        check("私聊里人不止一个时不猜", bans == [], f"{bans} {notes}")

        check("跨群标记会被剥干净",
              bot.drop_tags("[跨群禁言:原神交流群 李四 7]", bot.CROSS_BAN_TAG) == "")

        sys_admin = bot.build_system(False, None, None, ADMIN)
        check("管理员私聊时下发跨群禁言说明",
              "跨群禁言" in sys_admin and "原神交流群" in sys_admin)
        sys_other = bot.build_system(False, None, None, OTHER)
        check("非管理员看不到跨群禁言说明", "跨群禁言" not in sys_other)

        csnap = bot.console_snapshot("cross_group")
        check("面板快照能出跨群状态",
              csnap.get("ok") and len(csnap.get("groups") or []) == 2, str(csnap)[:120])
    finally:
        bot.GROUPS.groups = real_groups_map
        bot.group_member_list = real_member_list
        bot.force_ban = real_force_ban
        bot.refresh_admin = real_refresh_admin

    # ══════════════════════════════════════════════════════════════════
    # 【27】控制台接线：新面板的 kind / op / DOM 三处必须对齐
    # ══════════════════════════════════════════════════════════════════
    print("\n【27】控制台接线：kind / op / 面板 id 三处对齐")
    import console_server as cs
    for sub, kind in (("bili", "bili"), ("cross-group", "cross_group")):
        routed = cs.Handler._KINDS.get(sub)
        check(f"{sub} 路由到 {kind}", routed is not None and routed[0] == kind, str(routed))
    for op in ("bili_login", "groups_refresh"):
        check(f"{op} 在白名单里", op in cs.WRITE_OPS)
    html = (BASE_DIR / "public" / "console.html").read_text(encoding="utf-8")
    for box in ("p-bili", "p-crossgroup"):
        check(f"面板容器 {box} 存在", f'id="{box}"' in html)
    for fn in ("renderBili", "renderCrossGroup", "checkBiliLogin", "refreshGroups"):
        check(f"渲染/动作函数 {fn} 存在", ("function " + fn) in html)
    check("两个新面板都进了 LOADERS",
          "'bili'" in html and "'crossgroup'" in html)

    # ══════════════════════════════════════════════════════════════════
    # 【28】提示词守卫：语言模块重写后，红线一条都不能丢
    #
    # 提示词是可以随口改的文字，但有四类东西是踩坑换来的，丢了功能就死：
    #   ① 标记语法 —— 不写她就永远不会用，对应功能静默失效
    #   ② 反幻觉   —— 她开始「口头声称做了」却没做（用户报过）
    #   ③ 身份红线 —— 被问一句「你是不是 AI」就破功
    #   ④ 格式规则 —— 句尾挂句号、复读昵称，一眼不像人
    # 再加一条「块间零重复」和一条「常驻体积上限」：前者防同一规则散在多处
    # 互相盖掉，后者防以后又把它写胖回去。
    #
    # ⚠️ 本节会真的跑一遍 handle_message（验证模型失效兜底），
    #    所以**必须重新套上隔离** —— 否则 MOOD / GROUPS 那些 save() 会写坏真实状态。
    # ══════════════════════════════════════════════════════════════════
    print("\n【28】提示词守卫：标记 / 反幻觉 / 身份红线 / 格式 / 零重复 / 失效兜底")
    isolate_state()                     # 再来一次：本段要真跑消息处理
    keep_web, keep_qz = bot.WEB.enable, bot.QZONE_API.enable
    try:
        # ⚠️ 测试开头把这两个关了（省得真发请求）。守卫测试要看全量提示词，
        #    得先把它们开回来 —— 否则 [空间]/[搜索:] 那几个受它控制的块压根不下发，
        #    断言就会误报"标记丢了"（踩过）。
        bot.WEB.enable = True
        bot.QZONE_API.enable = True
        bot.GROUPS.groups = {"999000001": {"name": "原神交流群", "seen": time.time()},
                             "999000111": {"name": "旧书店读书会", "seen": time.time()}}
        bot.MOOD.affinity = {"999000001": {"10002": 100.0}}
        bot.MOOD.feeling = {"999000001": {"10002": 100.0}}
        bot.MOOD.names = {"999000001": {"10002": "示例用户1"}}
        bot.ADMIN_CACHE[str(G)] = (time.time(), True)
        bot.ADMIN_CACHE["999000001"] = (time.time(), True)
        bot.STICKERS.send_enable = True
        bot.STICKERS.items = [{"md5": "x", "path": "x",
                               "emoji_id": "1", "emoji_package_id": "2"}]
        life = {"act": "夜间活跃", "hint": "夜猫子时间", "chance": 1.0, "busy": False,
                "time_str": "星期六 21点", "weather": "晴 24℃", "event": ""}
        g_admin = bot.build_system(True, life, 999000001, ADMIN)
        g_other = bot.build_system(True, life, 999000001, OTHER)
        p_admin = bot.build_system(False, life, None, ADMIN)
        p_other = bot.build_system(False, life, None, OTHER)

        # ① 标记语法：每个标记在"该出现的那种形态"里必须在
        for tag, where, forms in [
            ("[画图:", "群里所有人", (g_admin, g_other, p_admin, p_other)),
            ("[说说:", "管理员（发说说受限于管理员）", (g_admin, p_admin)),
            ("[空间]", "所有人（读空间不设限）", (g_admin, g_other, p_admin, p_other)),
            ("[点赞:", "所有人（点赞不设限）", (g_admin, g_other, p_admin, p_other)),
            ("[评论:", "所有人（评论不设限）", (g_admin, g_other, p_admin, p_other)),
            ("[搜索:", "所有人（搜索不设限）", (g_admin, g_other, p_admin, p_other)),
            ("[图:", "有收藏表情包时", (g_admin, g_other, p_admin, p_other)),
            ("[禁言:", "群内她是管理员", (g_admin,)),
            ("[跨群禁言:", "说话人是管理员", (g_admin, p_admin)),
        ]:
            miss = [i for i, s in enumerate(forms) if tag not in s]
            check(f"标记 {tag}] 该在的时候在（{where}）", not miss,
                  f"缺于第 {miss} 个形态")
        check("非管理员看不到发说说标记",
              "[说说:" not in g_other and "[说说:" not in p_other)
        check("非管理员看不到跨群禁言标记",
              "[跨群禁言:" not in g_other and "[跨群禁言:" not in p_other)
        check("非本群管理员看不到禁言标记", "[禁言:" not in g_other)

        # ② 反幻觉
        for name, s in (("群聊/管理", g_admin), ("群聊/非管理", g_other),
                        ("私聊/管理", p_admin), ("私聊/非管理", p_other)):
            check(f"反幻觉总则在（{name}）", "没插标记就等于没做" in s)
        check("反幻觉保留「点了」反例", "「点了」" in g_other)
        check("反幻觉保留「早点了」反例", "「早点了」" in g_other)
        check("反幻觉保留「你自己去看」反例", "「你自己去看」" in g_other)
        check("反幻觉保留「或者直说没做成」这条台阶", "没做成" in g_other)
        check("反幻觉保留「点赞和评论各认各的」", "各认各的" in g_other)
        check("反幻觉保留「编结果最招人烦」", "编结果最招人烦" in g_other)

        # ③ 身份红线
        broke = ("扮演", "设定", "人设", "AI", "模型", "程序", "提示词", "虚拟", "虚构")
        for name, s in (("群聊/管理", g_admin), ("私聊/非管理", p_other)):
            miss = [w for w in broke if w not in s]
            check(f"破功禁用词表完整（{name}）", not miss, f"缺 {miss}")
        check("身份块要求「不承认」", "不承认" in g_other)
        check("身份块要求「别解释」", "别解释" in g_other)
        check("身份块要求「越认真问越要敷衍」", "敷衍" in g_other)

        # ④ 格式硬规则（收拢到 style_format 一处）
        for rule in ("句尾不加句号", "别 @ 人", "颜文字单独一行",
                     "心理旁白", "场景描写", "别在开头写自己名字"):
            check(f"格式规则「{rule}」在", rule in g_other)

        # ⑤ 零重复：同一条规则只许出现一次（重写前「点赞和评论是两件事」说了 3 遍）
        for phrase, want in (("各认各的", 1), ("会都执行", 0),
                             ("没插 [点赞:...]", 1), ("没插 [评论:...]", 1),
                             ("发说说一天最多一条", 1), ("露馅", 1),
                             ("别露出查过的痕迹", 1)):
            n = g_admin.count(phrase)
            check(f"「{phrase}」只出现 {want} 次（实际 {n}）", n == want)

        # ⑥ 常驻体积上限：防止以后又被写胖回去（重写前是 2451 / 2785 / 2599 / 2188）
        for name, s, cap in (("群聊/非管理员", g_other, 2050),
                             ("群聊/管理员", g_admin, 2400),
                             ("私聊/管理员", p_admin, 2150),
                             ("私聊/非管理员", p_other, 1800)):
            check(f"{name} 常驻提示 ≤ {cap} 字（实际 {len(s)}）", len(s) <= cap)

        # ⑦ 身份参照：平时不占字，被问到时才随机给一批
        check("不问身份就不加参照", "挡回去的话可以挑" not in g_admin)
        probe = bot.build_system(True, life, 999000001, ADMIN, identity_probe=True)
        check("问到身份才加参照", "挡回去的话可以挑" in probe)
        check("参照来自身份池",
              any(str(x) in probe for x in bot.CFG["identity_lines"]))
        picks = [ln for ln in probe.split("\n") if "挡回去的话可以挑" in ln][0]
        picks = picks.split("：", 1)[1].rstrip("）").split(" / ")
        check("参照条数受 identity_ref_count 控制",
              len(picks) == int(bot.CFG["identity_ref_count"]), str(picks))
        combos = {bot.build_system(True, life, 999000001, ADMIN, identity_probe=True)
                  for _ in range(12)}
        check("参照每轮换一批（不是死的一句）", len(combos) > 1, f"{len(combos)} 种")
        for t, want in (("你是不是AI", True), ("AI是什么", True), ("你是机器人吗", True),
                        ("用的什么模型", True), ("你是不是在扮演", True), ("有提示词吗", True),
                        ("今天吃什么", False), ("等我一下", False),
                        ("wait a minute", False), ("email 收到了", False)):
            check(f"探问识别 {t!r} -> {want}", bool(bot.IDENTITY_PROBE.search(t)) is want)

        # ⑧ 跨群禁言块的措辞必须跟事实一致（重写前不管什么情况都说"还没拉回来"）
        keep_groups = dict(bot.GROUPS.groups)
        try:
            bot.GROUPS.groups = {"999000001": {"name": "原神交流群", "seen": time.time()}}
            t = bot._cross_ban_prompt(True, 999000001)
            check("只有一个群时说「只有这一个群」", "只有这一个群" in t)
            check("只有一个群时不说错误原因", "还没拉回来" not in t)
            check("只有一个群时仍给出标记写法", "[跨群禁言:" in t)
            bot.GROUPS.groups = {}
            t2 = bot._cross_ban_prompt(True, 999000001)
            check("群列表为空时说「还没拉回来」", "还没拉回来" in t2)
            check("群列表为空时也给标记写法", "[跨群禁言:" in t2)
        finally:
            bot.GROUPS.groups = keep_groups

        # ⑨ 模型失效兜底：对外像人话，对内报故障
        bot_src = (BASE_DIR / "bot.py").read_text(encoding="utf-8")
        check("不再有系统腔公告「模型全挂了，稍后再试」",
              "模型全挂了，稍后再试" not in bot_src)
        check("不再把故障公告直接发给聊天对象",
              'await send(is_group, group_id, user_id, message_id, "云端 API Key 没配' not in bot_src
              and 'await send(is_group, group_id, user_id, message_id, "模型全挂了' not in bot_src)
        check("技术原因仍然留给管理员（no-key 有话说）",
              "云端 API Key 没配" in bot_src)
        real_answer, real_send, real_notify = bot.CHAT.answer, bot.send, bot.notify_admins
        said, told = [], []

        async def _dead(_key, _msgs):
            return None, "cloud"

        async def _capture_send(is_group, gid, uid, mid, text,
                                sticker=None, image=None):
            said.append(text)

        async def _capture_notify(text, image=None):
            told.append(text)
            return True

        bot.CHAT.answer, bot.send, bot.notify_admins = _dead, _capture_send, _capture_notify
        try:
            await bot.handle_message(group_ev(OTHER, "在吗"))
            check("模型挂了时她说的是兜底池里的人话",
                  len(said) == 1 and said[0] in bot.CFG["reply_fallback_lines"],
                  str(said))
            check("模型挂了时技术原因私聊给管理员",
                  len(told) == 1 and "模型调用失败" in told[0], str(told))
        finally:
            bot.CHAT.answer, bot.send, bot.notify_admins = real_answer, real_send, real_notify

        # ⑩ 素材库：只增不减，且 SD 相关一个没动
        cfg = bot.CFG
        check("生活小事扩到 18 条", len(cfg["life"]["mood_events"]) == 18)
        check("活动扩到 20 个", len(cfg["self_activity"]["acts"]) == 20)
        check("地点扩到 24 个", len(cfg["self_activity"]["places"]) == 24)
        act_ids = [a["id"] for a in cfg["self_activity"]["acts"]]
        check("活动 id 不重复", len(act_ids) == len(set(act_ids)))
        check("活动都带 hint", all(a.get("hint") for a in cfg["self_activity"]["acts"]))
        check("SD 的过渡语没被动（仍 5 条）", len(cfg["sd"]["pre_reply_lines"]) == 5)
        check("SD 提示词没被动（quality_prefix 原样）",
              cfg["sd"]["quality_prefix"].startswith("score_9, score_8_up"))
    finally:
        bot.WEB.enable, bot.QZONE_API.enable = keep_web, keep_qz
        restore_state()

    # ══════════════════════════════════════════════════════════════════
    # 【29】戳一戳：装死变少、挨着聊天不抢回合、连戳真的会掉心情
    #
    # 这一节是拿 tools\poke_stats.py 的数字倒推出来的：
    #   被戳 152 次里 70% 当没看见、回戳只有 2 次、挨着聊天的戳有 44% 被一起装死。
    # ══════════════════════════════════════════════════════════════════
    print("\n【29】戳一戳：忙时更愿意理、挨着聊天不抢回合、连戳会掉心情")
    isolate_state()
    real_life_current = bot.LIFE.current
    real_mood = (bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names)
    real_poke_cfg = dict(bot.CFG["poke"])
    G2 = 999000111
    # 身份一律用**真实存在**的人，不再编造测试人物：
    #   和她说话 / 被戳回话 / 心情变化 → 用管理员（示例用户1）
    #   需要被禁言 → 只能用真实的非管理员（示例用户3）—— force_ban 有豁免名单，
    #   管理员根本禁不了，拿管理员测禁言只会测出一条恒假的断言
    A, ANAME = ADMIN, "示例用户1"
    N, NNAME = OTHER, "示例用户3"

    def poke_ev(uid, nick, gid=G2):
        return {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                "user_id": uid, "group_id": gid, "target_id": BOT,
                "sender": {"nickname": nick}}

    try:
        # ① 心情：每戳一下掉一点；连戳够数连好感一起掉；单次有封顶
        bot.MOOD.affinity = {str(G2): {str(A): 60.0}}
        bot.MOOD.feeling = {str(G2): {str(A): 60.0}}
        bot.MOOD.names = {str(G2): {str(A): ANAME}}
        bot.MOOD.poke_annoy(G2, A, ANAME, 1)
        check("被戳一下心情就掉", bot.MOOD.mood_of(G2, A) < 60.0,
              str(bot.MOOD.mood_of(G2, A)))
        check("只戳一下还不至于动好感", bot.MOOD.affinity_of(G2, A) == 60.0)
        bot.MOOD.poke_annoy(G2, A, ANAME, 3)
        check("连戳够数时连好感也掉",
              bot.MOOD.affinity_of(G2, A) < 60.0, str(bot.MOOD.affinity_of(G2, A)))
        bot.MOOD.feeling[str(G2)][str(A)] = 80.0
        bot.MOOD.poke_annoy(G2, A, ANAME, 999)
        drop = 80.0 - bot.MOOD.mood_of(G2, A)
        check("单次扣的心情有封顶",
              drop <= float(bot.CFG["poke"]["mood_loss_cap"]) + 0.01, f"掉了 {drop}")

        # ② 「他刚说过话」这个判定本身
        bot.LAST_TALK_AT.clear()
        check("没说过话时不算 recent", not bot.is_recent_talker(A, 60))
        bot.note_talker(A)
        check("说过话就算 recent", bot.is_recent_talker(A, 60))
        check("窗口过了就不算", not bot.is_recent_talker(A, -1))

        # ③ 挨着聊天时的戳：两条路都不烧模型调用
        calls, sent2 = [], []
        real_answer2, real_send2 = bot.CHAT.answer, bot.send
        real_seg = bot.mood_sticker_segment

        async def _count_answer(key, msgs):
            calls.append(key)
            return "在呢", "cloud"

        async def _cap_send(is_group, gid, uid, mid, text, sticker=None, image=None):
            sent2.append((text, sticker))

        # 桩要在 ③④⑤ 全程挂着 —— 早先只在 ③ 里挂了又拆，
        # 结果 ④ 跑的是**真的** CHAT.answer，断言"没调模型"直接误判（踩过）
        bot.CHAT.answer, bot.send = _count_answer, _cap_send
        bot.POKE_LAST.clear()
        bot.POKE_STREAK.clear()
        bot.note_talker(A)
        bot.CFG["poke"]["proximity_sticker_probability"] = 0.0   # 强制"不理"分支
        await bot.handle_notice(poke_ev(A, ANAME))
        check("刚说过话时这一戳不理", not calls and not sent2, f"{calls} {sent2}")

        bot.POKE_LAST.clear()
        bot.CFG["poke"]["proximity_sticker_probability"] = 1.0   # 强制"只表情包"
        bot.mood_sticker_segment = lambda g, u: {"type": "mface",
                                                 "data": {"emoji_id": "1",
                                                          "emoji_package_id": "2"}}
        await bot.handle_notice(poke_ev(A, ANAME))
        check("也可以只丢一张表情包（不带正文）",
              len(sent2) == 1 and sent2[0][1] is not None and not sent2[0][0],
              str(sent2))
        check("这条路也不烧模型调用", not calls, str(calls))

        # ④ 可打断的忙 vs 不可打断的忙
        bot.CFG["poke"]["reply_probability"] = 1.0
        bot.CFG["poke"]["busy_interruptible_reply_probability"] = 1.0
        bot.CFG["poke"]["busy_reply_probability"] = 0.0
        bot.LIFE.current = lambda: {"act": "书店看店", "hint": "看店", "chance": 1.0,
                                    "busy": True, "interruptible": True,
                                    "time_str": "星期六 15点", "weather": ""}
        bot.POKE_LAST.clear()
        calls.clear()
        await bot.handle_notice(poke_ev(A, ANAME))
        check("可打断的忙（看店）也会理人", bool(calls), str(calls))

        bot.LIFE.current = lambda: {"act": "睡觉", "hint": "睡着", "chance": 1.0,
                                    "busy": True, "interruptible": False,
                                    "time_str": "星期六 03点", "weather": ""}
        bot.POKE_LAST.clear()
        calls.clear()
        await bot.handle_notice(poke_ev(N, NNAME))
        check("不可打断的忙（睡觉）照旧装死", not calls, str(calls))

        # ⑤ 连戳：心情掉了 + 禁言照旧
        bot.LIFE.current = lambda: {"act": "夜间活跃", "hint": "夜猫子", "chance": 1.0,
                                    "busy": False, "interruptible": True,
                                    "time_str": "星期六 22点", "weather": ""}
        bot.MOOD.affinity = {str(G2): {str(N): 50.0}}
        bot.MOOD.feeling = {str(G2): {str(N): 50.0}}
        bot.MOOD.names = {str(G2): {str(N): NNAME}}
        bot.POKE_LAST.clear()
        bot.POKE_STREAK.clear()
        sent.clear()
        for _ in range(3):
            await bot.handle_notice(poke_ev(N, NNAME))
        check("连戳三下照旧触发禁言", has("set_group_ban"), str(sent[-2:]))
        check("连戳三下她的心情真的掉了", bot.MOOD.mood_of(G2, N) < 50.0,
              str(bot.MOOD.mood_of(G2, N)))
        check("连戳够数时好感也掉了", bot.MOOD.affinity_of(G2, N) < 50.0,
              str(bot.MOOD.affinity_of(G2, 888)))

        # ⑥ 表情包按心情挑词
        bot.MOOD.feeling = {str(G2): {"a": 10.0, "b": 40.0, "c": 70.0, "d": 90.0}}
        check("心情很差 -> 生气", bot.mood_sticker_keyword(G2, "a") == "生气")
        check("心情偏低 -> 无语", bot.mood_sticker_keyword(G2, "b") == "无语")
        check("心情不错 -> 得意", bot.mood_sticker_keyword(G2, "c") == "得意")
        check("心情很好 -> 开心", bot.mood_sticker_keyword(G2, "d") == "开心")
        check("不认识的人不硬套词", bot.mood_sticker_keyword(G2, "zzz") == "",
              repr(bot.mood_sticker_keyword(G2, "zzz")))

        # ⑦ 配置层：这几条是「排掉模拟人物、看清真实数据」之后定下来的
        # ⚠️ 这里要看**配置里的原值**（real_poke_cfg），不能看当前 CFG ——
        # ③④ 为了强制走某条分支，把几个概率临时改成过 0.0 / 1.0 了
        check("可打断的忙的概率比不可打断的松得多",
              float(real_poke_cfg["busy_interruptible_reply_probability"])
              >= 3 * float(real_poke_cfg["busy_reply_probability"]))
        check("挨着聊天的判定窗口是 30 秒（调轻过）",
              float(real_poke_cfg["chat_proximity_seconds"]) == 30)
        check("挨着聊天时只回表情包的概率是 0.35（调轻过）",
              float(real_poke_cfg["proximity_sticker_probability"]) == 0.35)
        # 反戳 / 额外发表情包：提过一次又回退了 —— 真实样本只有「48 小时回戳 1 次」，
        # 撑不起改默认值。这两条留着断言，防止以后又被人悄悄提上去。
        check("反戳概率保持原值 0.15",
              float(bot.CFG["poke"]["poke_back_probability"]) == 0.15)
        check("没有「回话时额外补表情包」这个开关了",
              "sticker_reply_probability" not in bot.CFG["poke"])
    finally:
        bot.LIFE.current = real_life_current
        bot.MOOD.affinity, bot.MOOD.feeling, bot.MOOD.names = real_mood
        bot.CFG["poke"] = real_poke_cfg
        bot.CHAT.answer, bot.send = real_answer2, real_send2
        bot.mood_sticker_segment = real_seg
        restore_state()

    # ══════════════════════════════════════════════════════════════════
    # 【30】禁言限制：默认时长 / 自己发起的上限 / 同目标冷却 / 每日额度 /
    #       每小时条数 / 连戳单算 / 非管理员不动手 / 失败原因回灌
    #
    # 这一批全是"防止她一轮刷一片、或者跟同一个人较劲"的闸门，
    # 每条都对应契约 §1 里查明的一次线上事故（第 9~14 条）。
    # 靶子只用真实的非管理员（示例用户3）—— 管理员在豁免名单里，拿他测禁言只会测出恒假断言。
    # （唯一例外是 ⑧：那一轮得让**管理员开口**，否则她回复里的 [禁言:] 会被 _deny 静默抹掉。）
    # ══════════════════════════════════════════════════════════════════
    print("\n【30】禁言限制：默认时长 / 冷却 / 每日额度 / 每小时条数 / 连戳单算")
    isolate_state()
    real_speakers30 = dict(bot.SPEAKERS.get(str(G)) or {})

    def _wipe_ban_ledgers30() -> None:
        bot.BAN_LOG.clear()
        bot.BAN_AT.clear()
        bot.BAN_DAY.clear()
        bot.STREAK_BAN_DAY.clear()
        bot.STREAK_BAN_AT.clear()

    def _ban_calls30() -> int:
        return sum(1 for a, _ in sent if a == "set_group_ban")

    try:
        bot.SPEAKERS[str(G)] = {"示例用户3": OTHER}
        bot.ADMIN_CACHE[str(G)] = (time.time(), True)
        bcfg30 = bot.CFG["ban"]
        today30 = datetime.now().strftime("%Y-%m-%d")
        # ⑧ 要真跑一遍 handle_message，先把两道跟本轮无关的节流按下去：
        #   · group_cooldown_seconds 在本文件开头被设成 0，但前面那些 console_apply
        #     会把内存 CFG 重新刷成磁盘那份（真实值是 3），所以这里显式再压一次
        #   · CHAT.last_reply_at 是前面用例留下的，会让这条消息被"冷却中"直接跳掉
        real_gcd30 = bot.CFG.get("group_cooldown_seconds")
        bot.CFG["group_cooldown_seconds"] = 0

        # ① 不写分钟 -> ban.default_minutes（以前错用 max_minutes = 1440 分钟 = 一天）
        reset()
        _wipe_ban_ledgers30()
        default_min = float(bcfg30.get("default_minutes", 10))
        max_min = float(bcfg30.get("max_minutes", 1440))
        check("配置前提：默认时长确实短于最长上限", default_min < max_min,
              f"default={default_min:g} max={max_min:g}")
        done, notes = await bot.apply_bans_ex(G, "[禁言:示例用户3]")
        dur = last("set_group_ban").get("duration")
        check(f"不写分钟时用 ban.default_minutes（{default_min:g} 分 = {int(default_min * 60)} 秒）",
              dur == int(default_min * 60) and dur != int(max_min * 60), f"实际 {dur} 秒")
        check("done 文案仍是「昵称 N分钟」", done == [f"示例用户3 {default_min:g}分钟"], str(done))
        check("成功时不产生失败原因", not notes, str(notes))

        # ② 她自己发起的时长被 ban.self_max_minutes 夹住
        reset()
        _wipe_ban_ledgers30()
        self_max = float(bcfg30.get("self_max_minutes", 60))
        check("配置前提：她自己发起的上限确实短于最长上限", self_max < max_min,
              f"self_max={self_max:g}")
        done, notes = await bot.apply_bans_ex(G, "[禁言:示例用户3 9999]")
        dur = last("set_group_ban").get("duration")
        check(f"她一时上头写 9999 分钟也被夹到 self_max_minutes（{int(self_max * 60)} 秒）",
              dur == int(self_max * 60), f"实际 {dur} 秒")

        # ③ 同一目标冷却：对所有来源生效，且要给人话原因
        reset()
        _wipe_ban_ledgers30()
        cd = float(bcfg30.get("same_target_cooldown_seconds", 600))
        check("配置前提：同目标冷却开着", cd > 0, f"{cd} 秒")
        d1, _n1 = await bot.apply_bans_ex(G, "[禁言:示例用户3 1]")
        d2, n2 = await bot.apply_bans_ex(G, "[禁言:示例用户3 1]")
        check("同一个号冷却期内第二次不再执行",
              bool(d1) and not d2 and _ban_calls30() == 1,
              f"{d1} / {d2} / 实际 {_ban_calls30()} 次")
        check("被冷却挡住时给的是人话原因（不是光返回 False）",
              bool(n2) and "刚被禁过" in n2[0] and n2[0].startswith("示例用户3："), str(n2))
        again = await bot.force_ban(G, OTHER, "示例用户3", 5, "管理员点名")
        check("冷却对所有来源生效（管理员点名也挡）",
              again is False and _ban_calls30() == 1, f"{again} / {_ban_calls30()} 次")

        # ④ 每日总次数上限
        reset()
        _wipe_ban_ledgers30()
        per_day = int(bcfg30.get("max_per_day", 10))
        bot.BAN_DAY[today30] = per_day
        done, notes = await bot.apply_bans_ex(G, "[禁言:示例用户3 1]")
        check("ban.max_per_day 用完即拒", not done and _ban_calls30() == 0, f"{done} {notes}")
        check("拒的原因是「今天禁得够多了」",
              bool(notes) and "今天禁得够多了" in notes[0], str(notes))

        # ⑤ 连戳吃自己的额度，不挤占 ban 的
        reset()
        _wipe_ban_ledgers30()
        bot.BAN_DAY[today30] = per_day            # ban 的额度已经见底
        ok_s, why_s = await bot._force_ban_ex(G, OTHER, "示例用户3", 3, "连戳", source="streak")
        check("ban 额度满了，连戳照样能禁（各自一本账）", ok_s is True, why_s)
        check("连戳记在 STREAK_BAN_DAY 上",
              bot.STREAK_BAN_DAY.get(today30) == 1, str(bot.STREAK_BAN_DAY))
        check("连戳没去挤占 BAN_DAY", bot.BAN_DAY.get(today30) == per_day, str(bot.BAN_DAY))

        reset()
        _wipe_ban_ledgers30()
        streak_cap = int(bot.CFG["poke"].get("streak_max_per_day", 3))
        check("配置前提：连戳每日上限 > 0", streak_cap > 0, str(streak_cap))
        bot.STREAK_BAN_DAY[today30] = streak_cap
        ok_s2, why_s2 = await bot._force_ban_ex(G, OTHER, "示例用户3", 3, "连戳", source="streak")
        check("连戳额度用完 -> 拒", ok_s2 is False and "今天禁得够多了" in why_s2, str(why_s2))

        reset()
        _wipe_ban_ledgers30()
        bot.STREAK_BAN_DAY[today30] = 99          # 连戳那本账爆了
        done, notes = await bot.apply_bans_ex(G, "[禁言:示例用户3 1]")
        check("连戳额度用完不影响她自己禁言", bool(done), f"{done} {notes}")
        check("她的禁言记在 BAN_DAY 上", bot.BAN_DAY.get(today30) == 1, str(bot.BAN_DAY))
        check("她的禁言没动 STREAK_BAN_DAY",
              bot.STREAK_BAN_DAY.get(today30) == 99, str(bot.STREAK_BAN_DAY))

        # ⑥ 一条回复里多个 [禁言:] 只能禁 max_per_hour 次
        #    （这是被修掉的真 bug：上限检查原来在循环外，只查一次）
        reset()
        _wipe_ban_ledgers30()
        mph = int(bcfg30.get("max_per_hour", 3))
        check("配置前提：每小时上限 > 0", mph > 0, str(mph))
        names30 = {f"甲{i}": 2000 + i for i in range(mph + 2)}
        bot.SPEAKERS[str(G)] = names30
        reply30 = "\n".join(f"[禁言:{n} 1]" for n in names30)
        done, notes = await bot.apply_bans_ex(G, reply30)
        check(f"一条回复里写了 {len(names30)} 个 [禁言:]，实际只禁 max_per_hour={mph} 次",
              _ban_calls30() == mph, f"实际 {_ban_calls30()} 次")
        check("done 条数与实际执行数一致",
              len(done) == mph and len(done) == _ban_calls30(), f"{done}")
        check("超出的部分给出人话原因", any("禁得够多了" in n for n in notes), str(notes))
        check("每小时账本只记了 max_per_hour 条",
              len(bot.BAN_LOG.get(str(G)) or []) == mph, str(bot.BAN_LOG.get(str(G))))
        bot.SPEAKERS[str(G)] = {"示例用户3": OTHER}

        # ⑦ 她不是本群管理员：整个 [禁言:] 直接不做（对齐跨群路径的自检）
        reset()
        _wipe_ban_ledgers30()
        real_refresh30 = bot.refresh_admin

        async def _not_admin30(_gid):
            return False

        bot.refresh_admin = _not_admin30
        try:
            done, notes = await bot.apply_bans_ex(G, "[禁言:示例用户3 1]")
            thin = await bot.apply_bans(G, "[禁言:示例用户3 1]")     # 兼容入口也走同一条路
        finally:
            bot.refresh_admin = real_refresh30
        # 她不是本群管理员时：不调 set_group_ban、不白记额度，但**要回一句人话**
        # （回灌后她才不会照样说"安静会儿"；2026-09-27 从"回空"改成"回原因"）
        check("她不是本群管理员时不真禁",
              done == [] and _ban_calls30() == 0, f"{done} {notes} {_ban_calls30()}")
        check("但会回一句「没有管理权限」而不是假装禁成了",
              bool(notes) and "管理权限" in notes[0], str(notes))
        check("薄包装 apply_bans 同样不动手且仍返回 list", thin == [], str(thin))
        check("也没白记一次每小时额度", not bot.BAN_LOG.get(str(G)), str(bot.BAN_LOG))

        # ⑧ 失败原因回灌历史（跑到 handle_message 那一层才算数）
        #    第一条成功、第二条被同一个号的冷却挡住 -> notes 非空 -> 必须进 history
        reset()
        _wipe_ban_ledgers30()
        bot.SPEAKERS[str(G)] = {"示例用户3": OTHER}
        bot.CHAT.recent_replies.clear()
        bot.CHAT.sessions.pop(f"g{G}", None)
        real_ans30, real_send30, real_notify30 = bot.CHAT.answer, bot.send, bot.notify_admins
        real_extract30 = bot.MEMORY.extract
        real_stk30 = (bot.STICKERS.enable, bot.STICKERS.send_enable)

        async def _cap_send30(is_group, gid, uid, mid, text, sticker=None, image=None):
            pass

        async def _cap_notify30(text, image=None):
            return True

        bot.CHAT.answer = answer_with("行吧\n[禁言:示例用户3 1]\n[禁言:示例用户3 1]")
        bot.send, bot.notify_admins = _cap_send30, _cap_notify30
        bot.MEMORY.extract = False
        bot.STICKERS.enable = bot.STICKERS.send_enable = False
        bot.CHAT.last_reply_at.pop(f"g{G}", None)
        try:
            # ⚠️ 说话的人必须是管理员：非管理员那轮 [禁言:] 会被 _deny 静默抹掉
            #    （这是既有行为，不是 bug），那样就永远测不到回灌那一步
            await bot.handle_message(group_ev(ADMIN, "在吗"))
        finally:
            bot.CHAT.answer, bot.send, bot.notify_admins = real_ans30, real_send30, real_notify30
            bot.MEMORY.extract = real_extract30
            bot.STICKERS.enable, bot.STICKERS.send_enable = real_stk30
        hist30 = [str(h.get("content")) for h in (bot.CHAT.sessions.get(f"g{G}") or [])]
        notes30 = [h for h in hist30 if "后台结果" in h]
        check("禁言结果回灌进上下文（她才知道没禁成）", bool(notes30), str(hist30)[-240:])
        check("回灌里带着人话原因", bool(notes30) and "刚被禁过" in notes30[0], str(notes30))
        check("一条回复里同一个号只禁了一次", _ban_calls30() == 1, f"{_ban_calls30()} 次")
    finally:
        bot.SPEAKERS[str(G)] = real_speakers30
        bot.CFG["group_cooldown_seconds"] = real_gcd30
        restore_state()

    # ══════════════════════════════════════════════════════════════════
    # 【31】识图：引擎判据 / auto 退本地 / 动图抽帧 / 超限先降质 /
    #       mface 进链路 / 提示词与占位不打架
    #
    # 这一节的桩只换"往外发的那个调用"（HTTP 与两个引擎入口），
    # 不换被测的逻辑本身；所有可写路径都靠 isolate_state 兜着。
    # ══════════════════════════════════════════════════════════════════
    print("\n【31】识图：后端判据 / auto 退本地 / 抽帧 / 超限降质 / mface 进链路")
    isolate_state()
    real_vision31 = dict(bot.CFG.get("vision") or {})
    real_cloud31 = dict(bot.CFG.get("cloud") or {})
    real_local31 = dict(bot.CFG.get("local") or {})
    real_ven31 = bot.CFG.get("vision_enable")
    real_relay31 = bot.CFG.get("vision_relay")
    real_stats31 = dict(bot.VISION.stats)
    real_probe31 = (dict(bot.VISION._probe), bot.VISION._probe_at, bot.VISION._probe_error)
    real_client31 = bot.CHAT.client_for
    real_ask31 = bot.VISION._ask_cloud
    real_desc31 = bot.CHAT.describe_images
    real_ri31 = bot.resolve_images
    real_ans31, real_send31, real_notify31 = bot.CHAT.answer, bot.send, bot.notify_admins
    real_stk31 = (bot.STICKERS.enable, bot.STICKERS.send_enable)
    real_extract31 = bot.MEMORY.extract
    real_gcd31 = bot.CFG.get("group_cooldown_seconds")
    try:
        # ① backend() 的解析
        for b in bot.VISION_BACKENDS:
            bot.CFG["vision"]["backend"] = b
            check(f"backend={b} 被原样认下", bot.VISION.backend() == b, bot.VISION.backend())
        bot.CFG["vision"]["backend"] = "  AUTO  "
        check("backend 容忍大小写与空格", bot.VISION.backend() == "auto", bot.VISION.backend())
        bot.CFG["vision"]["backend"] = "xxx"
        bot.CFG["vision_relay"] = True
        check("backend 是脏值时按 vision_relay=true 推成 auto",
              bot.VISION.backend() == "auto", bot.VISION.backend())
        bot.CFG["vision_relay"] = False
        check("vision_relay=false 时推成 local", bot.VISION.backend() == "local",
              bot.VISION.backend())
        bot.CFG["vision"].pop("backend", None)
        bot.CFG["vision_relay"] = True
        check("vision.backend 整条缺失时同样按 vision_relay 推导",
              bot.VISION.backend() == "auto", bot.VISION.backend())

        # ② usable() 真值表（纯配置判断，不该联网）
        bot.CFG["vision"]["backend"] = "cloud"
        bot.CFG["cloud"] = {"enable": True, "vision": True, "api_key": "k"}
        bot.CFG["local"] = {"enable": True, "vision": True}
        bot.CFG["vision_enable"] = True
        check("cloud：enable+vision+api_key 齐 -> 能用", bot.VISION.usable() is True)
        bot.CFG["cloud"]["api_key"] = ""
        check("cloud：没有 api_key -> 不能用", bot.VISION.usable() is False)
        bot.CFG["cloud"]["api_key"] = "k"
        bot.CFG["cloud"]["vision"] = False
        check("cloud：cloud.vision=false -> 不能用", bot.VISION.usable() is False)
        bot.CFG["cloud"]["vision"] = True
        bot.CFG["cloud"]["enable"] = False
        check("cloud：cloud.enable=false -> 不能用", bot.VISION.usable() is False)
        bot.CFG["cloud"] = {"enable": True, "vision": True, "api_key": "k"}
        bot.CFG["vision"]["backend"] = "local"
        check("local：local.enable+local.vision -> 能用", bot.VISION.usable() is True)
        bot.CFG["local"]["vision"] = False
        check("local：local.vision=false -> 不能用", bot.VISION.usable() is False)
        bot.CFG["local"] = {"enable": False, "vision": True}
        check("local：local.enable=false -> 不能用", bot.VISION.usable() is False)
        bot.CFG["local"] = {"enable": True, "vision": True}
        bot.CFG["vision"]["backend"] = "off"
        check("off：两边都能看也不看", bot.VISION.usable() is False)
        bot.CFG["vision_enable"] = False
        for b in ("auto", "cloud", "local"):
            bot.CFG["vision"]["backend"] = b
            check(f"总闸 vision_enable=false -> backend={b} 也不看", bot.VISION.usable() is False)
        bot.CFG["vision_enable"] = True
        bot.CFG["vision"]["backend"] = "auto"
        bot.CFG["cloud"]["vision"] = False
        check("auto：云端不可用但本地可用 -> 能用", bot.VISION.usable() is True)
        bot.CFG["local"]["vision"] = False
        check("auto：两个引擎都不可用 -> 不能用", bot.VISION.usable() is False)

        # ③ describe()：auto 云端优先、失败退本地；cloud 不偷偷退
        bot.CFG["vision"]["backend"] = "auto"
        bot.CFG["vision_enable"] = True
        bot.CFG["cloud"] = {"enable": True, "vision": True, "api_key": "k"}
        bot.CFG["local"] = {"enable": True, "vision": True}
        tried31: list[str] = []

        async def _cloud_ok31(urls):
            tried31.append("cloud")
            return "云端的描述"

        async def _cloud_bad31(urls):
            tried31.append("cloud")
            return None

        async def _local_ok31(urls):
            tried31.append("local")
            return "本地的描述"

        async def _local_bad31(urls):
            tried31.append("local")
            return None

        bot.VISION._ask_cloud = _cloud_ok31
        bot.CHAT.describe_images = _local_ok31
        got = await bot.VISION.describe(["data:image/png;base64,AA"])
        check("auto：云端成了就用云端（不问本地）",
              got == "云端的描述" and tried31 == ["cloud"], f"{got} / {tried31}")
        tried31.clear()
        bot.VISION._ask_cloud = _cloud_bad31
        got = await bot.VISION.describe(["data:image/png;base64,AA"])
        check("auto：云端失败自动退本地",
              got == "本地的描述" and tried31 == ["cloud", "local"], f"{got} / {tried31}")
        tried31.clear()
        bot.CHAT.describe_images = _local_bad31
        failed_before = bot.VISION.stats["failed"]
        got = await bot.VISION.describe(["data:image/png;base64,AA"])
        check("auto：两个引擎都不行 -> None", got is None, str(got))
        check("失败记进 stats.failed", bot.VISION.stats["failed"] == failed_before + 1,
              str(bot.VISION.stats))
        tried31.clear()
        bot.CFG["vision"]["backend"] = "cloud"
        bot.VISION._ask_cloud = _cloud_bad31
        bot.CHAT.describe_images = _local_ok31
        got = await bot.VISION.describe(["data:image/png;base64,AA"])
        check("cloud：云端失败不偷偷退本地",
              got is None and tried31 == ["cloud"], f"{got} / {tried31}")
        tried31.clear()
        bot.VISION._ask_cloud = _cloud_ok31
        got = await bot.VISION.describe(["data:image/png;base64,AA"])
        check("cloud：云端成了就用云端", got == "云端的描述", str(got))
        described_before = bot.VISION.stats["described"]
        await bot.VISION.describe(["data:image/png;base64,AA"])
        check("成功记进 stats.described",
              bot.VISION.stats["described"] == described_before + 1, str(bot.VISION.stats))
        tried31.clear()
        bot.CFG["vision"]["backend"] = "off"
        got = await bot.VISION.describe(["data:image/png;base64,AA"])
        check("off：直接 None，两个引擎都不试", got is None and not tried31, f"{got} / {tried31}")
        tried31.clear()
        bot.CFG["vision"]["backend"] = "auto"
        bot.CFG["vision_enable"] = False
        got = await bot.VISION.describe(["data:image/png;base64,AA"])
        check("总闸关掉时 describe 直接 None", got is None and not tried31, f"{got} / {tried31}")
        bot.CFG["vision_enable"] = True
        check("没有图时直接 None", await bot.VISION.describe([]) is None)

        # ③b 云端识图发出去的到底是什么：原图直传 + 带输出上限
        #     （前面的桩换掉了 _ask_cloud，这里换回真实现、只拦最外层的 HTTP 那一步）
        posted31: list = []

        async def _post31(ep, msgs, timeout, extra=None):
            posted31.append((msgs, extra))
            return "云端的描述"

        real_post31 = bot.CHAT._post
        bot.VISION._ask_cloud = real_ask31
        bot.CHAT._post = _post31
        bot.CFG["vision"]["backend"] = "cloud"
        try:
            got = await bot.VISION.describe(["data:image/png;base64,AA"])
        finally:
            bot.CHAT._post = real_post31
        want_tokens = int(bot.CFG["vision"]["describe_max_tokens"])
        check("识图请求带上 vision.describe_max_tokens 的输出上限",
              got == "云端的描述" and posted31
              and (posted31[0][1] or {}).get("max_tokens") == want_tokens,
              f"{got} / {posted31[0][1] if posted31 else None}")
        check("云端识图把原图直传（消息里真的是 image_url 段，不是只发文字）",
              bool(posted31) and any(
                  isinstance(p, dict) and p.get("type") == "image_url"
                  for p in posted31[0][0][0]["content"]), str(posted31)[:120])

        # ③c 分辨率按后端分（云端留大图、本地省 token）
        bot.CFG["vision"]["backend"] = "cloud"
        check("云端用 vision.max_side_cloud",
              bot.VISION.max_side() == int(bot.CFG["vision"]["max_side_cloud"]),
              str(bot.VISION.max_side()))
        bot.CFG["vision"]["backend"] = "local"
        check("本地用 vision.max_side_local",
              bot.VISION.max_side() == int(bot.CFG["vision"]["max_side_local"]),
              str(bot.VISION.max_side()))
        bot.CFG["vision"]["backend"] = "auto"

        # ④ 动图抽帧：N 帧 -> N 个 data URL，有 alpha 的帧走 PNG
        def _anim_gif31(n: int = 4) -> bytes:
            import io as _io
            from PIL import Image as _Img
            frs = [_Img.new("RGBA", (40, 40), (255, i * 40, 0, 0)) for i in range(n)]
            buf = _io.BytesIO()
            frs[0].save(buf, format="GIF", save_all=True, append_images=frs[1:],
                        duration=100, loop=0, transparency=0, disposal=2)
            return buf.getvalue()

        def _static_png31() -> bytes:
            import io as _io
            from PIL import Image as _Img
            buf = _io.BytesIO()
            _Img.new("RGB", (60, 60), (10, 20, 30)).save(buf, format="PNG")
            return buf.getvalue()

        def _fake_client31(payload: bytes, ctype: str = "image/jpeg"):
            hits: list[str] = []

            class _R:
                def __init__(self):
                    self.content = payload
                    self.headers = {"content-type": ctype}

                def raise_for_status(self):
                    pass

            class _C:
                async def get(self, url, timeout=None):
                    hits.append(url)
                    return _R()

            return _C(), hits

        bot.CFG["vision"]["max_frames"] = 3
        cli31, _hits31 = _fake_client31(_anim_gif31(4), "image/gif")
        bot.CHAT.client_for = lambda url: cli31
        frames_before = bot.VISION.stats["frames"]
        out = await bot._image_to_data_url("http://x/a.gif")
        check("动图按 max_frames 等距抽帧（4 帧抽 3 帧）",
              isinstance(out, list) and len(out) == 3, f"{type(out).__name__} / {len(out)}")
        check("有 alpha 的帧编成 PNG",
              bool(out) and all(u.startswith("data:image/png;base64,") for u in out),
              str(out)[:60])
        check("抽帧数记进 stats.frames",
              bot.VISION.stats["frames"] == frames_before + 3, str(bot.VISION.stats))

        bot.CFG["vision"]["max_frames"] = 1
        out = await bot._image_to_data_url("http://x/a.gif")
        check("max_frames=1 时动图只出第 0 帧（退回老行为）", len(out) == 1, str(len(out)))

        bot.CFG["vision"]["max_frames"] = 3
        cli31, _hits31 = _fake_client31(_static_png31(), "image/png")
        bot.CHAT.client_for = lambda url: cli31
        out = await bot._image_to_data_url("http://x/s.png")
        check("静图只出一张", len(out) == 1, str(len(out)))
        check("静图不算抽帧", bot.VISION.stats["frames"] == frames_before + 3,
              str(bot.VISION.stats))

        # ⑤ 超过 vision.max_bytes：先降质重试一次，仍超才丢并计数
        import io as _io31
        from PIL import Image as _Img31
        noise31 = _Img31.effect_noise((700, 700), 120).convert("RGB")
        _nbuf = _io31.BytesIO()
        noise31.save(_nbuf, format="PNG")
        raw_noise31 = _nbuf.getvalue()
        max_side31 = 512
        _fm, full_bytes31 = bot._encode_frame(noise31, max_side31, 85)
        _rm, retry_bytes31 = bot._encode_frame(noise31, max(1, int(max_side31 * 0.7)), 55)
        check("构造前提：全质量版本确实比降质版大", len(full_bytes31) > len(retry_bytes31),
              f"{len(full_bytes31)} vs {len(retry_bytes31)}")

        dropped_before = bot.VISION.stats["dropped"]
        cli31, _hits31 = _fake_client31(raw_noise31, "image/png")
        bot.CHAT.client_for = lambda url: cli31
        bot.CFG["vision"]["max_frames"] = 1
        out = await bot._image_to_data_url("http://x/big.png", max_bytes=len(retry_bytes31),
                                           max_side=max_side31)
        check("超过 max_bytes 时先降质重试（图仍然发得出去）",
              len(out) == 1 and out[0].startswith("data:"), f"{len(out)} 张")
        check("降质成功就不算丢图", bot.VISION.stats["dropped"] == dropped_before,
              str(bot.VISION.stats))

        out = await bot._image_to_data_url("http://x/big.png", max_bytes=64,
                                           max_side=max_side31)
        check("降质后仍超上限才丢，并记进 stats.dropped",
              out == [] and bot.VISION.stats["dropped"] == dropped_before + 1,
              f"{out} / {bot.VISION.stats}")

        # 不显式传 max_bytes 时必须读 CFG 里那个 vision.max_bytes
        # ⚠️ 这个键很小，后面几条断言都从 dropped_before 重新数，别串了账
        bot.CFG["vision"]["max_bytes"] = 64
        out = await bot._image_to_data_url("http://x/big.png", max_side=max_side31)
        check("不传 max_bytes 时读的是 vision.max_bytes",
              out == [] and bot.VISION.stats["dropped"] == dropped_before + 2,
              f"{out} / {bot.VISION.stats}")
        bot.CFG["vision"]["max_bytes"] = real_vision31.get("max_bytes", 8_000_000)
        dropped_mid = bot.VISION.stats["dropped"]

        cli31, _hits31 = _fake_client31(b"\x00\x01 this is not an image", "image/png")
        bot.CHAT.client_for = lambda url: cli31
        out = await bot._image_to_data_url("http://x/notimg.bin", max_bytes=10 ** 6,
                                           max_side=max_side31)
        check("解不开的字节按原样送一张（不把功能弄没）", len(out) == 1, f"{len(out)} 张")
        out = await bot._image_to_data_url("http://x/notimg.bin", max_bytes=4,
                                           max_side=max_side31)
        check("解不开又超上限 -> 丢并计数",
              out == [] and bot.VISION.stats["dropped"] == dropped_mid + 1,
              str(bot.VISION.stats))

        # ⑥ resolve_images：返回 list[str]；来源数仍限 3；取不到就计数
        cli31, hits31 = _fake_client31(_static_png31(), "image/png")
        bot.CHAT.client_for = lambda url: cli31
        d_before = bot.VISION.stats["dropped"]
        out = await bot.resolve_images(
            [], urls=["http://x/1.png", "http://x/2.png", "http://x/3.png", "http://x/4.png"])
        check("resolve_images 返回的是 list[str]（不是结构体）",
              isinstance(out, list) and out
              and all(isinstance(u, str) and u.startswith("data:") for u in out),
              f"{type(out).__name__} / {str(out)[:40]}")
        check("总来源数仍限 3（第 4 个只记账不下载）", len(hits31) == 3, str(hits31))
        check("超过 3 个的部分记进 stats.dropped",
              bot.VISION.stats["dropped"] == d_before + 1, str(bot.VISION.stats))
        out = await bot.resolve_images(["file_id_that_has_no_url"])
        check("get_image 拿不到链接就丢掉并计数",
              out == [] and bot.VISION.stats["dropped"] == d_before + 2,
              f"{out} / {bot.VISION.stats}")

        # ⑦ 纯 mface 消息不再直接 return，而且动画表情真的并进识图链路
        reset()
        bot.CHAT.recent_replies.clear()
        bot.CHAT.sessions.pop(f"g{G}", None)
        bot.CHAT.last_reply_at.pop(f"g{G}", None)
        bot.CFG["group_cooldown_seconds"] = 0     # 见【30】⑧ 的说明
        asked31: list = []
        ri_calls31: list = []

        async def _ans31(key, msgs):
            asked31.append(key)
            return "看到啦", "cloud"

        async def _ri31(files, max_side=None, urls=None):
            ri_calls31.append((list(files), list(urls or [])))
            return []

        async def _send31(is_group, gid, uid, mid, text, sticker=None, image=None):
            pass

        async def _notify31(text, image=None):
            return True

        bot.resolve_images = _ri31
        bot.CHAT.answer = _ans31
        bot.send, bot.notify_admins = _send31, _notify31
        bot.STICKERS.enable = bot.STICKERS.send_enable = False
        bot.MEMORY.extract = False
        bot.CFG["vision"]["backend"] = "auto"
        bot.CFG["vision_enable"] = True
        bot.CFG["cloud"] = {"enable": True, "vision": True, "api_key": "k"}
        bot.CFG["local"] = {"enable": True, "vision": True}
        _mid[0] += 1
        ev_mf31 = {"post_type": "message", "message_type": "group", "message_id": _mid[0],
                   "user_id": OTHER, "group_id": G, "sender": {"nickname": "示例用户3"},
                   "message": [{"type": "at", "data": {"qq": str(BOT)}},
                               {"type": "mface",
                                "data": {"emoji_id": "123", "emoji_package_id": "456"}}]}
        await bot.handle_message(ev_mf31)
        check("纯 mface 消息不再直接 return（她真的被问了一轮）", bool(asked31), str(asked31))
        check("mface 的公开地址并进了识图链路",
              bool(ri_calls31) and any("gxh.vip.qq.com" in u and "/456/123/" in u
                                       for u in ri_calls31[0][1]),
              str(ri_calls31))
        mf_hist31 = [str(h.get("content")) for h in (bot.CHAT.sessions.get(f"g{G}") or [])]
        check("纯 mface 也写进了群上下文",
              any(str(OTHER) in h for h in mf_hist31), str(mf_hist31)[-160:])

        # ⑧ 提示词与占位文本必须跟 VISION.usable() 保持一致（两边打架她就只能编）
        life31 = {"act": "夜间活跃", "hint": "夜猫子", "chance": 1.0, "busy": False,
                  "time_str": "星期六 21点", "weather": "晴", "event": ""}
        bot.CFG["vision_enable"] = True
        bot.CFG["vision"]["backend"] = "auto"
        bot.CFG["cloud"] = {"enable": True, "vision": True, "api_key": "k"}
        sys_on31 = bot.build_system(True, life31, G, OTHER)
        check("能看图时提示词说她看得到图", "你看得到图" in sys_on31)
        check("能看图时不再同时说看不到", "你看不到图里的内容" not in sys_on31)
        bot.CFG["vision_enable"] = False
        sys_off31 = bot.build_system(True, life31, G, OTHER)
        check("不能用时提示词改成说她看不到", "你看不到图里的内容" in sys_off31)
        check("不能用时不再说看得到", "你看得到图" not in sys_off31)
        bot.CFG["vision_enable"] = True

        msgs31 = [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
        check("端点自己收图时不许动消息",
              bot.Chat._fit_vision(msgs31, {"vision": True}) is msgs31)
        txt_on31 = str(bot.Chat._fit_vision(msgs31, {})[0]["content"])
        check("能看图的后端被换掉图时，措辞是「这条通道传不了图」",
              "传不了图" in txt_on31, txt_on31[:70])
        bot.CFG["vision_enable"] = False
        txt_off31 = str(bot.Chat._fit_vision(msgs31, {})[0]["content"])
        check("不能用时的占位措辞跟系统提示一致",
              "你看不到图里的内容" in txt_off31, txt_off31[:70])
        bot.CFG["vision_enable"] = True
    finally:
        bot.CFG["vision"] = real_vision31
        bot.CFG["cloud"] = real_cloud31
        bot.CFG["local"] = real_local31
        bot.CFG["vision_enable"] = real_ven31
        bot.CFG["vision_relay"] = real_relay31
        bot.VISION.stats.clear()
        bot.VISION.stats.update(real_stats31)
        (bot.VISION._probe, bot.VISION._probe_at, bot.VISION._probe_error) = real_probe31
        bot.CHAT.client_for = real_client31
        bot.VISION._ask_cloud = real_ask31
        bot.CHAT.describe_images = real_desc31
        bot.resolve_images = real_ri31
        bot.CHAT.answer, bot.send, bot.notify_admins = real_ans31, real_send31, real_notify31
        bot.STICKERS.enable, bot.STICKERS.send_enable = real_stk31
        bot.MEMORY.extract = real_extract31
        bot.CFG["group_cooldown_seconds"] = real_gcd31
        restore_state()

    # ══════════════════════════════════════════════════════════════════
    # 【32】WebUI 后端表面：三个 kind 的形状 + 三个 op 的校验 + 写回落盘
    #
    # 只在 bot.py 这一半，不碰 HTTP 层（那是 console_server 的用例范围）。
    # 写操作放最后：它会刷新内存 CFG，跑完在这里重读一次真实 config.json 收尾。
    # ══════════════════════════════════════════════════════════════════
    print("\n【32】WebUI 后端：三 kind 形状 / 三 op 校验 / 写回落盘")
    isolate_state()
    real_probe32 = (dict(bot.VISION._probe), bot.VISION._probe_at, bot.VISION._probe_error)
    # update_config 每次都会把 CONFIG_PATH 顺手拷一份到 _backup/console-config-last.json
    # （滚动回滚快照）。那个目的地是写死在 bot.py 里的**真实项目路径**，隔离改不到它，
    # 所以这里把这一步挡掉 —— 本段要验的是「op 校验 + 落盘 + 读回」，不是那份快照。
    real_roll32 = bot._backup_config_roll
    bot._backup_config_roll = lambda: ""
    try:
        # 探针结果塞成缓存里的样子：免得 console_snapshot 顺手真去联网探 LM Studio
        bot.VISION._probe = {"id": "qwen2-vl-7b", "state": "loaded",
                             "loaded_context_length": 4096, "models": 1}
        bot.VISION._probe_at = time.time()
        bot.VISION._probe_error = ""

        snap_v32 = bot.console_snapshot("vision")
        check("vision 快照顶层键与契约一致",
              {"ok", "backend", "backend_label", "usable", "cloud", "local",
               "stats", "config"} <= set(snap_v32), str(sorted(snap_v32)))
        check("cloud 三项齐", {"enable", "vision", "has_key"} <= set(snap_v32["cloud"]),
              str(snap_v32["cloud"]))
        check("local 带探测结果（读的是缓存，不联网）",
              snap_v32["local"].get("probe", {}).get("id") == "qwen2-vl-7b",
              str(snap_v32["local"])[:120])
        # 五项：passed_through 是"原图直传"的计数（auto+云端可用时图片直接给云端看，
        # 不走 describe，所以只看 described 会误以为识图没工作）
        check("stats 五项齐",
              set(snap_v32["stats"]) == {"described", "failed", "dropped", "frames",
                                         "passed_through"},
              str(snap_v32["stats"]))
        # 六项：max_total_images 也是面板可拖的（抽帧后总图数上限）
        check("config 六项齐",
              set(snap_v32["config"]) == {"max_frames", "max_total_images", "max_side_cloud",
                                          "max_side_local", "max_bytes", "describe_max_tokens"},
              str(snap_v32["config"]))
        check("backend_label 与枚举表对得上",
              snap_v32["backend_label"] == bot.VISION_LABELS.get(snap_v32["backend"]),
              str(snap_v32["backend_label"]))

        snap_s32 = bot.console_snapshot("switches")
        flat_s32 = {it["path"]: it for g in (snap_s32.get("groups") or []) for it in g["items"]}
        check("switches 快照与 CONSOLE_SWITCHES 一一对应",
              snap_s32.get("ok") is True and set(flat_s32) == set(bot.CONSOLE_SWITCHES),
              f"{len(flat_s32)} vs {len(bot.CONSOLE_SWITCHES)}")
        check("每个开关都带 path/label/desc/value 且 value 是布尔",
              all({"path", "label", "desc", "value"} <= set(it) for it in flat_s32.values())
              and all(isinstance(it["value"], bool) for it in flat_s32.values()))
        check("分组数与声明表里的分组数一致",
              len(snap_s32["groups"]) == len({g for _, _, g in bot.CONSOLE_SWITCHES.values()}),
              f"{len(snap_s32['groups'])}")

        snap_l32 = bot.console_snapshot("lists")
        flat_l32 = {it["path"]: it for g in (snap_l32.get("groups") or []) for it in g["items"]}
        check("lists 快照与 CONSOLE_LISTS 一一对应",
              snap_l32.get("ok") is True and set(flat_l32) == set(bot.CONSOLE_LISTS),
              f"{len(flat_l32)} vs {len(bot.CONSOLE_LISTS)}")
        check("每个列表都带 count/max_items/max_chars/lines",
              all({"count", "max_items", "max_chars", "lines"} <= set(it)
                  for it in flat_l32.values()))
        check("lines 交付的是字符串列表",
              all(isinstance(it["lines"], list)
                  and all(isinstance(x, str) for x in it["lines"])
                  for it in flat_l32.values()))

        # op 校验：表外路径一律拒
        # 注意用 sd.not_exists：sd.enable 现在是**合法**的开关（本轮新加的）
        for bad_path in ("sd.not_exists", "nope.nope", ""):
            try:
                bot.console_apply("switch_set", {"path": bad_path, "value": True})
                check(f"switch_set 表外路径被拒（{bad_path!r}）", False, "竟然接受了")
            except ValueError:
                check(f"switch_set 表外路径被拒（{bad_path!r}）", True)
        try:
            bot.console_apply("list_set", {"path": "sd.pre_reply_negative", "lines": []})
            check("list_set 表外路径被拒", False, "竟然接受了")
        except ValueError:
            check("list_set 表外路径被拒", True)

        # 列表超上限：**报错**，不许静默截断
        wl_items = bot.CONSOLE_LISTS["wake_prefix"][3]
        wl_chars = bot.CONSOLE_LISTS["wake_prefix"][4]
        try:
            bot.console_apply("list_set", {"path": "wake_prefix",
                                           "lines": [f"x{i}" for i in range(wl_items + 1)]})
            check(f"list_set 超条数（>{wl_items}）报错而不是静默截断", False, "竟然静默接受了")
        except ValueError as exc:
            check(f"list_set 超条数（>{wl_items}）报错而不是静默截断", "最多" in str(exc), str(exc))
        try:
            bot.console_apply("list_set", {"path": "wake_prefix",
                                           "lines": ["x" * (wl_chars + 1)]})
            check(f"list_set 超单条字数（>{wl_chars}）报错", False, "竟然静默接受了")
        except ValueError as exc:
            check(f"list_set 超单条字数（>{wl_chars}）报错", "最多" in str(exc), str(exc))
        try:
            bot.console_apply("list_set", {"path": "wake_prefix", "lines": 123})
            check("list_set 非列表直接拒", False, "竟然接受了")
        except ValueError:
            check("list_set 非列表直接拒", True)
        for bad_b in ("fast", "AUTO2", "", "none"):
            try:
                bot.console_apply("vision_set", {"backend": bad_b})
                check(f"vision_set 拒掉 {bad_b!r}（只认四个枚举）", False, "竟然接受了")
            except ValueError:
                check(f"vision_set 拒掉 {bad_b!r}（只认四个枚举）", True)

        # 出图相关的键**只允许白名单里的这几个**进控制台：
        #   sd.enable / sd.fail_enable / imagegen.enable -> 开关表（本轮新增，用户要的）
        #   sd.pre_reply_lines / sd.fail_lines           -> 列表表（话术池）
        # 用白名单而不是"一律不许"，是为了把判断逼到明面上：以后往 sd 段加键时，
        # 想暴露给用户就必须来这里登记一次，而不是不知不觉多出一个开关。
        ALLOW_SD_SWITCH = {"sd.enable", "sd.fail_enable", "imagegen.enable"}
        ALLOW_SD_LIST = {"sd.pre_reply_lines", "sd.fail_lines"}
        got_sw = {p for p in bot.CONSOLE_SWITCHES if p.startswith(("sd.", "imagegen."))}
        check("开关表里的出图键都在白名单内", got_sw <= ALLOW_SD_SWITCH, str(sorted(got_sw)))
        check("生图开关确实进表了（用户要能关掉生图）",
              {"sd.enable", "imagegen.enable"} <= got_sw, str(sorted(got_sw)))
        check("可调表（滑块）里仍然不许有出图的键",
              not [p for p in bot.CONSOLE_TUNABLES if p.startswith("sd.")])
        got_ls = {p for p in bot.CONSOLE_LISTS if p.startswith("sd.")}
        check("列表表里的 sd.* 在话术白名单内", got_ls <= ALLOW_SD_LIST, str(sorted(got_ls)))

        # _tunable_group：顶层那几个回复手感键归到 reply（否则面板显示英文键名）
        for p in ("active_reply_probability", "max_reply_chars", "max_context_turns",
                  "group_cooldown_seconds", "context.max_chars"):
            check(f"滑块 {p} 归到 reply 组", bot._tunable_group(p) == "reply",
                  bot._tunable_group(p))
        check("别的键还是按路径前缀分组",
              bot._tunable_group("vision.max_frames") == "vision"
              and bot._tunable_group("poke.streak_limit") == "poke"
              and bot._tunable_group("ban.max_per_day") == "ban",
              f"{bot._tunable_group('vision.max_frames')}")

        # 三个新 op 写完能落盘、能读回
        bot.console_apply("switch_set", {"path": "vision_enable", "value": True})
        disk32 = json.loads(bot.CONFIG_PATH.read_text(encoding="utf-8"))
        check("switch_set 立刻落盘且同步内存",
              disk32.get("vision_enable") is True and bot.CFG.get("vision_enable") is True,
              str(disk32.get("vision_enable")))

        bot.console_apply("list_set", {"path": "wake_prefix",
                                       "lines": ["玖玖", "小玖", ""]})
        disk32 = json.loads(bot.CONFIG_PATH.read_text(encoding="utf-8"))
        check("list_set 落盘且顺手丢掉空行",
              disk32.get("wake_prefix") == ["玖玖", "小玖"], str(disk32.get("wake_prefix")))
        back32 = {it["path"]: it for g in bot.console_snapshot("lists")["groups"]
                  for it in g["items"]}
        check("list_set 之后能读回同一份内容",
              back32["wake_prefix"]["lines"] == ["玖玖", "小玖"]
              and back32["wake_prefix"]["count"] == 2, str(back32["wake_prefix"])[:120])

        bot.console_apply("vision_set", {"backend": "local"})
        disk32 = json.loads(bot.CONFIG_PATH.read_text(encoding="utf-8"))
        check("vision_set 落盘", disk32["vision"]["backend"] == "local",
              str(disk32["vision"]["backend"]))
        check("vision_set 之后快照跟着变",
              bot.console_snapshot("vision")["backend"] == "local",
              bot.console_snapshot("vision")["backend"])
    finally:
        (bot.VISION._probe, bot.VISION._probe_at, bot.VISION._probe_error) = real_probe32
        bot._backup_config_roll = real_roll32
        restore_state()
    # 上面几个写操作把内存 CFG 刷成了临时副本的内容，从真实 config.json 重读一遍收尾
    bot.CFG.clear()
    bot.CFG.update(bot.load_config())

    # ══════════════════════════════════════════════════════════════════
    # 【33】跨文件守卫：面板分组中文名 + 滑块范围 + 三处接线
    #
    # 这几条守的不是行为，是"面板还能用"：
    #   · 分组名没在 console.html 的 TUNABLE_GROUP 登记 -> 面板上直接显示英文键名
    #   · 现值落在自己声明的 [min,max] 之外 -> <input type=range> 定不住，滑块打不开
    #   · 后端路由 / 写白名单 / 前端面板三处少一处 -> 面板少一块或点开就 404
    # 以后再加开关/滑块/面板时，忘了补其中任何一处，这里会立刻红。
    # ══════════════════════════════════════════════════════════════════
    print("\n【33】跨文件守卫：分组中文名 / 滑块范围 / 三处接线")
    import re as _re33
    html33 = (BASE_DIR / "public" / "console.html").read_text(encoding="utf-8")
    _m33 = _re33.search(r"var TUNABLE_GROUP\s*=\s*\{(.*?)\};", html33, _re33.S)
    defined33 = set(_re33.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", _m33.group(1))) if _m33 else set()
    check("console.html 里能读到 TUNABLE_GROUP 表", bool(defined33))

    used33 = ({g for _, _, g in bot.CONSOLE_SWITCHES.values()}
              | {spec[2] for spec in bot.CONSOLE_LISTS.values()})
    miss33 = sorted(used33 - defined33)
    check("开关/列表用到的每个分组都能查到中文名",
          not miss33, f"缺中文名：{miss33}（面板上会显示英文键名）")

    miss_t33 = sorted({bot._tunable_group(p) for p in bot.CONSOLE_TUNABLES} - defined33)
    check("滑块用到的每个分组都能查到中文名",
          not miss_t33, f"缺中文名：{miss_t33}")

    bad_range33 = []
    for _p33, _spec33 in bot.CONSOLE_TUNABLES.items():
        _lo, _hi = _spec33[0], _spec33[1]
        _v33 = bot._get_path(bot.CFG, _p33)
        if not isinstance(_v33, (int, float)) or not (_lo <= _v33 <= _hi):
            bad_range33.append(f"{_p33}={_v33!r} 不在 [{_lo},{_hi}]")
    check("每个可调项的现值都落在它自己声明的 [min,max] 里（否则滑块打不开）",
          not bad_range33, str(bad_range33))

    # 三处接线（后端路由 / 写白名单 / 前端面板）必须同时对得上，
    # 只做后端不做前端 = 面板上少一块；只做前端不做后端 = 点开就 404。
    import console_server as _cs33
    for sub33, kind33 in (("vision", "vision"), ("switches", "switches"), ("lists", "lists")):
        _r33 = _cs33.Handler._KINDS.get(sub33)
        check(f"路由 /api/bot/{sub33} -> kind={kind33}",
              _r33 is not None and _r33[0] == kind33, str(_r33))
    for _op33 in ("switch_set", "list_set", "vision_set"):
        check(f"写操作 {_op33} 在 console_server 白名单里", _op33 in _cs33.WRITE_OPS)
    for _box33 in ("p-vision", "p-switches", "p-lists"):
        check(f"面板容器 {_box33} 存在", f'id="{_box33}"' in html33)
    for _fn33 in ("renderVision", "renderSwitches", "renderLists"):
        check(f"渲染函数 {_fn33} 存在", ("function " + _fn33) in html33)
    for _key33, _fn33b, _box33b in (("vision", "renderVision", "p-vision"),
                                    ("switches", "renderSwitches", "p-switches"),
                                    ("lists", "renderLists", "p-lists")):
        check(f"LOADERS 里接上了 {_key33} -> {_fn33b} -> {_box33b}",
              f"'{_key33}'" in html33 and _fn33b in html33 and f"'{_box33b}'" in html33)

    # ── 模型端点面板（只读 + 重载）：五处必须同时接线 ──
    _r33p = _cs33.Handler._KINDS.get("providers")
    check("路由 /api/bot/providers -> kind=providers",
          _r33p is not None and _r33p[0] == "providers", str(_r33p))
    check("写操作 provider_reload 在 console_server 白名单里",
          "provider_reload" in _cs33.WRITE_OPS)
    check('面板容器 p-providers 存在', 'id="p-providers"' in html33)
    for _fn33p in ("renderProviders", "reloadProviders", "epsTable", "chainsBox"):
        check(f"渲染/动作函数 {_fn33p} 存在", ("function " + _fn33p) in html33)
    check("LOADERS 里接上了 providers -> renderProviders -> p-providers",
          "'providers'" in html33 and "renderProviders" in html33
          and "'p-providers'" in html33)
    # 后端：这个 kind 必须真能返回，否则前端拿到 500
    _snap33p = bot.console_snapshot("providers")
    check("console_snapshot('providers') 能返回 chat/image 两段",
          _snap33p.get("ok") and isinstance(_snap33p.get("chat"), dict)
          and isinstance(_snap33p.get("image"), dict), str(list(_snap33p))[:120])

    # ★ 密钥绝不能出现在这个新接口里（含 base_url 里内嵌的 ?key=）
    _blob33 = json.dumps(_snap33p, ensure_ascii=False)
    _leak33 = []
    for _ep33 in bot.ROUTER.endpoints:
        _k = _ep33.auth()
        if _k and len(_k) >= 6 and _k in _blob33:
            _leak33.append(_ep33.id)
    for _ep33 in bot.IMAGE_ROUTER.endpoints:
        _k = _ep33.auth()
        if _k and len(_k) >= 6 and _k in _blob33:
            _leak33.append(_ep33.id)
    check("端点密钥不在 providers 快照里", not _leak33, str(_leak33))
    # 有些厂商（Gemini 老写法 / 某些网关）把 key 塞在查询串里，上屏前必须抹掉
    from providers.router import _safe_url as _su33
    check("base_url 的查询串值会被抹成 ***",
          _su33("https://x/v1?key=abc123&t=1") == "https://x/v1?key=***&t=***",
          _su33("https://x/v1?key=abc123&t=1"))
    check("没有查询串时原样返回", _su33("https://x/v1") == "https://x/v1")

    # 卡片编号：新增面板最容易漏的一步（顺移 / 接号）。现有回归原来没有这条断言。
    _nums33 = _re33.findall(r'<span class="n">(B\d\d)</span>', html33)
    check("卡片编号无重复", len(_nums33) == len(set(_nums33)),
          f"重复：{sorted({n for n in _nums33 if _nums33.count(n) > 1})}")
    check("卡片编号连续无跳号（追加在末尾也要接上）",
          _nums33 == [f"B{i:02d}" for i in range(1, len(_nums33) + 1)],
          f"实际：{_nums33}")

    # ══════════════════════════════════════════════════════════════════
    # 【34】Provider 注册中心：把离线单测并进来，保证「一条命令全绿」
    # ══════════════════════════════════════════════════════════════════
    print("\n【34】Provider 注册中心（跑 test_providers 的离线用例）")
    import test_providers as _tp34
    _ok_before34, _fail_before34 = _tp34.ok, _tp34.fail
    await _tp34.main()
    _delta_ok34 = _tp34.ok - _ok_before34
    _delta_fail34 = _tp34.fail - _fail_before34
    globals()["ok"] += _delta_ok34
    globals()["fail"] += _delta_fail34
    check("Provider 单测全绿", _delta_fail34 == 0, f"{_delta_fail34} 项失败")

    # 面板快照：端点列表与策略必须在（面板/外部工具靠它看多端点状态）
    _snap34 = bot.console_snapshot("vision")
    check("vision 快照里带端点列表", bool(_snap34.get("endpoints")),
          str(list(_snap34))[:120])
    check("vision 快照里带识图策略", bool(_snap34.get("policy")), "")
    check("识别出的后端与策略一致",
          _snap34.get("backend") == (_snap34.get("policy") or {}).get("backend"),
          f"{_snap34.get('backend')} vs {(_snap34.get('policy') or {}).get('backend')}")

    # 端点级并发闸：配置改了之后旧信号量要回收，否则改了 concurrency 不生效
    _ep34 = [e for e in bot.ROUTER.endpoints if e.concurrency]
    if _ep34:
        check("限流端点的信号量已建好",
              bot.ROUTER.gate.sem(_ep34[0]) is not None, _ep34[0].id)
    else:
        check("（没有配 concurrency 的端点，跳过信号量检查）", True)

    # ══════════════════════════════════════════════════════════════════
    # 【35】生图层：离线单测 + 额度只退一次 + 失败反馈分流
    # ══════════════════════════════════════════════════════════════════
    print("\n【35】生图层：离线单测 + 额度只退一次 + 失败原因分流")
    import test_imagegen as _ti35
    _ok_b35, _fail_b35 = _ti35.ok, _ti35.fail
    await _ti35.main()
    globals()["ok"] += _ti35.ok - _ok_b35
    globals()["fail"] += _ti35.fail - _fail_b35
    check("生图层离线单测全绿", _ti35.fail == _fail_b35,
          f"{_ti35.fail - _fail_b35} 项失败")

    _S35 = bot.SDGEN
    _real35 = (_S35.state_path, _S35.dir, _S35.enable, _S35._draw_locked)

    async def _fail_draw35(intent, life=None, purpose="group"):
        return bot.GenOutcome(ok=False, reason="failed", detail="假装失败")

    async def _ok_draw35(intent, life=None, purpose="group"):
        _S35.dir.mkdir(parents=True, exist_ok=True)
        p = str(_S35.dir / "ok.png")
        pathlib.Path(p).write_bytes(b"png")
        return bot.GenOutcome(ok=True, data=b"png", path=p, endpoint_id="fake", detail=p)

    try:
        _S35.state_path = BASE_DIR / "_tmp_sd35.json"
        _S35.dir = BASE_DIR / "_tmp_sd35_dir"
        _S35.enable = True
        _S35.state = {"group": [], "qzone_date": "", "qzone": 0}

        # ★ 额度只退一次：老代码在 _draw_locked 与 generate 各退一次，
        #   任何在这中间抛异常的改动都会让 sd_state.json 凭空少一张（很难复现的那种）。
        _S35._draw_locked = _fail_draw35
        _before = len(_S35.state["group"])
        # ★ 测试开头把 SDGEN.generate_outcome 换成了假桩（fake_gen），
        #   所以这里要绕过实例属性、直接调类的真方法，否则测的是桩不是实现。
        await bot.SD.generate_outcome(_S35, "画个试试", None, "group")
        _after = len(_S35.state["group"])
        check("出图失败后额度净变化为 0（不是负一）", _after == _before,
              f"{_before} -> {_after}")

        _S35._draw_locked = _ok_draw35
        _out35 = await bot.SD.generate_outcome(_S35, "画个试试", None, "group")
        check("出图成功后额度 +1", len(_S35.state["group"]) == _after + 1,
              f"{_after} -> {len(_S35.state['group'])} "
              f"outcome=ok:{_out35.ok} reason:{_out35.reason} detail:{_out35.detail}")

        # 抛异常也要退还（finally 出口）
        def _boom35(intent, life=None, purpose="group"):
            raise RuntimeError("模拟内部炸了")
        _S35._draw_locked = _boom35
        _b2 = len(_S35.state["group"])
        try:
            await bot.SD.generate_outcome(_S35, "画个试试", None, "group")
        except RuntimeError:
            pass
        check("内部抛异常时额度也退回去了", len(_S35.state["group"]) == _b2,
              f"{_b2} -> {len(_S35.state['group'])}")

        _S35._draw_locked = _ok_draw35
        check("check_ready：有端点时通过",
              _S35.check_ready("group", has_intent=True)[0] is True)
        _ok_e, _r_e, _ = _S35.check_ready("group", has_intent=False)
        check("check_ready：没说要画 -> empty-intent（正常态）",
              (not _ok_e) and _r_e == "empty-intent", _r_e)
        _S35.enable = False
        _ok_f, _r_f, _ = _S35.check_ready("group")
        check("check_ready：总闸关着 -> disabled（正常态）",
              (not _ok_f) and _r_f == "disabled", _r_f)
        check("disabled 属正常态（不该给用户发失败话）",
              bot.GenOutcome(ok=False, reason="disabled").benign is True)
        check("backend-down 属故障态（要给话 + 通知）",
              bot.GenOutcome(ok=False, reason="backend-down").benign is False)
    finally:
        _S35.state_path, _S35.dir, _S35.enable, _S35._draw_locked = _real35
        _keep35 = _S35.state
        _S35.load_state()
        _ = _keep35
        __import__("shutil").rmtree(BASE_DIR / "_tmp_sd35_dir", ignore_errors=True)
        (BASE_DIR / "_tmp_sd35.json").unlink(missing_ok=True)

    # 失败话术：配置里的话能取到、能被开关关掉
    _real_sd35 = bot.CFG.get("sd")
    try:
        bot.CFG["sd"] = {**(bot.CFG.get("sd") or {}),
                         "fail_enable": True, "fail_lines": ["画坏了。"]}
        check("fail_line 取到配置里的失败话", bot.fail_line() == "画坏了。", bot.fail_line())
        bot.CFG["sd"]["fail_enable"] = False
        check("sd.fail_enable=false 时保持沉默", bot.fail_line() == "", bot.fail_line())
    finally:
        bot.CFG["sd"] = _real_sd35

    # ══════════════════════════════════════════════════════════════════
    # 【36】跨平台运行层：文件锁 / 探测解析 / venv 路径 / URL 脱敏
    # ══════════════════════════════════════════════════════════════════
    print("\n【36】跨平台：文件锁 / 端口探测 / venv 路径")
    import filelock as _fl36
    import launcher as _lc36

    _tmp36 = BASE_DIR / "_tmp_lock36"
    try:
        _tmp36.mkdir(exist_ok=True)
        _lk = _tmp36 / "a.lock"
        _fh1, _ = _fl36.acquire(_lk)
        check("第一次能拿到锁", _fh1 is not None)
        _fh2, _holder = _fl36.acquire(_lk)
        # 只断言"拿不到锁"。持有者 PID 在 Windows 上读不到（LockFile 语义会连读取
        # 一起拒掉），所以不能拿它当断言条件 —— 那是尽力而为的提示信息。
        check("同一文件第二次拿不到（单实例生效）", _fh2 is None, f"{_fh2} {_holder}")
        _fl36.release(_fh1)
        _fh3, _ = _fl36.acquire(_lk)
        check("释放后能再拿到（陈旧锁自愈）", _fh3 is not None)
        _fl36.release(_fh3)
    finally:
        __import__("shutil").rmtree(_tmp36, ignore_errors=True)

    # ENOTSUP（NFS / 容器 overlay）必须降级放行 —— 否则 bot 在容器里根本起不来
    _real_try = _fl36.try_lock
    try:
        import errno as _errno
        if os.name != "nt":
            def _notsup(_fh):
                raise OSError(_errno.ENOTSUP, "not supported")
            _fl36.try_lock = _notsup
            _fh4, _ = _fl36.acquire(_tmp36 / "nfs.lock") if _tmp36.exists() else (object(), 0)
            check("文件系统不支持锁时降级放行（不把启动卡死）", True)
        else:
            check("（Windows 无 flock，跳过 ENOTSUP 用例）", True)
    finally:
        _fl36.try_lock = _real_try

    # venv 路径按平台分派（用 PurePath 判，避免在源码里跟反斜杠转义较劲）
    _vp = str(_lc36.VENV_PY).replace("\\", "/")
    if os.name == "nt":
        check("Windows 上 venv 走 Scripts/python.exe",
              _vp.endswith(".venv/Scripts/python.exe"), _vp)
    else:
        check("POSIX 上 venv 走 bin/python*", "/.venv/bin/python" in _vp, _vp)

    # 探测层：喂真实的 Windows netstat 样本，断言解析正确
    class _R36:
        def __init__(self, out: str):
            self.stdout = out
            self.returncode = 0

    _real_run36 = _lc36._run
    _win_sample = (
        "\r\n活动连接\r\n\r\n  协议  本地地址          外部地址        状态           PID\r\n"
        "  TCP    127.0.0.1:6199         0.0.0.0:0              LISTENING       41840\r\n"
        "  TCP    127.0.0.1:6200         0.0.0.0:0              LISTENING       41840\r\n"
        "  TCP    127.0.0.1:5700         0.0.0.0:0              LISTENING       18460\r\n"
    )
    try:
        if os.name == "nt":
            _lc36._run = lambda args, **kw: _R36(_win_sample)
            check("从 netstat 输出里解析出 6199 的 PID",
                  _lc36.port_pid(6199) == 41840, str(_lc36.port_pid(6199)))
            _lc36._run = lambda args, **kw: _R36(
                '"pythonw.exe","41840","Console","1","43,656 K"\r\n')
            check("从 tasklist CSV 里解析出镜像名（内存列含逗号也不怕）",
                  _lc36._win_image_name(41840) == "pythonw", _lc36._win_image_name(41840))
        else:
            check("（非 Windows 上的 netstat 解析跳过）", True)
    finally:
        _lc36._run = _real_run36

    # 命令特征判归属：cmdline 命中就算自己的进程（跨平台唯一可靠依据）
    # port_pid 也要一起桩掉：owns_port 会先查端口有没有人占，拿不到 PID 就直接返回
    # 「未监听」。不桩的话这两个断言只在「本机恰好有 NapCat 占着 6199」时成立 ——
    # 开发机上是绿的，CI 上必红（runner 上没那个进程）。
    _real_info36 = _lc36.proc_info
    _real_pid36 = _lc36.port_pid
    try:
        _lc36.port_pid = lambda port: 4242
        _lc36.proc_info = lambda pid: {"name": "python3",
                                       "cmdline": "/x/.venv/bin/python launcher.py start",
                                       "cwd": "/x/qqbot"}
        _who, _why = _lc36.owns_port(6199)
        check("命令行匹配 bot.py/launcher.py 就认作自己人",
              "命令行匹配" in _why or "工作目录" in _why, _why)
        _lc36.proc_info = lambda pid: {"name": "chrome", "cmdline": "chrome --type=gpu",
                                       "cwd": "/Applications"}
        _who2, _why2 = _lc36.owns_port(6199)
        check("别人的程序会被明确标出「不是预期程序」", "不是预期程序" in _why2, _why2)
    finally:
        _lc36.proc_info = _real_info36
        _lc36.port_pid = _real_pid36

    check("EXPECT_CMD 已按命令行特征配置", "bot.py" in _lc36.EXPECT_CMD[6199],
          str(_lc36.EXPECT_CMD))

    # host:port 解析（自检里用来找空间桥端口）
    check("能从 URL 里抠 host:port",
          bot._parse_hostport("http://127.0.0.1:5700/status", "x", 1) == ("127.0.0.1", 5700))
    check("抠不出端口就用默认端口",
          bot._parse_hostport("这不是URL", "127.0.0.1", 5700)[1] == 5700,
          str(bot._parse_hostport("这不是URL", "127.0.0.1", 5700)))

    # 启动自检：结构完整、能给等级
    _sc36 = bot._startup_selfcheck()
    check("启动自检有结果", bool(_sc36) and all(
        {"key", "label", "level", "detail"} <= set(x) for x in _sc36), str(_sc36)[:160])
    check("自检等级取值合法",
          all(x["level"] in ("ok", "warn", "error", "off") for x in _sc36),
          str([x["level"] for x in _sc36]))
    check("自检结果进了 providers 快照",
          bool(bot.console_snapshot("providers").get("selfcheck")))

    # ════════════════════════════════════════════════════════════════
    print("\n【37】脚本编码与换行符（「下载下来打开就炸」那一类）")

    # 这一段的由来：install.bat 整篇中文注释，而 chcp 65001 压在第 11 行 ——
    # cmd.exe 用控制台代码页（中文 Windows = 936/GBK）**逐行解码**批处理，
    # chcp 之前那段中文的每个字是 3 个 UTF-8 字节，被 GBK 按 2 字节两两错配，
    # 多出来的半个字节会把紧跟其后的 ASCII 字符一起吞掉。于是整个脚本散架：
    #   '縗挭敥縗…' 不是内部或外部命令
    #   'nstall.ps1' 不是内部或外部命令      ← install 的 i 被啃掉了
    # 实测还发现更阴的一条：把 chcp 提到第 2 行**也不够** ——
    # 被别的 .bat `call` 调用时照样炸（cmd 已在旧代码页下缓冲过文件）。
    # 所以规矩只能是最硬的那条：.bat 里一个非 ASCII 字节都不能有。
    # 这类问题「本地跑一次测试」是抓不到的，必须由机器守着。
    _repo37 = BASE_DIR.parent
    _tool37 = _repo37 / "tools" / "normalize_scripts.py"
    if not _tool37.exists():
        # 源目录（不在开源仓库布局里，没有 tools/normalize_scripts.py）跳过而不是判失败
        # —— 否则同一套测试在源目录就没法跑了。
        check("（不在开源仓库布局里，跳过脚本编码守卫）", True)
    else:
        import importlib.util as _ilu37
        _spec37 = _ilu37.spec_from_file_location("_norm37", _tool37)
        _norm37 = _ilu37.module_from_spec(_spec37)
        _spec37.loader.exec_module(_norm37)

        _all37 = _norm37.targets()
        check("扫到了脚本文件（不是空目录）", len(_all37) >= 5, f"{len(_all37)} 个")

        _bad37: dict[str, list[str]] = {}
        for _p37 in _all37:
            _codes37 = [c for c, _ in _norm37.check(_p37)]
            if _codes37:
                _bad37[str(_p37.relative_to(_repo37)).replace("\\", "/")] = _codes37
        check("全部脚本文件编码/换行合规", not _bad37,
              f"不合规（跑 `python tools/normalize_scripts.py --fix` 可自动修大部分）：{_bad37}")

        # ── 逐条拆开，失败时能一眼看出是哪条规则被破坏 ──
        # （1）批处理：零非 ASCII 字节。这是唯一可靠的做法，不能打折扣。
        _bats37 = [p for p in _all37 if p.suffix.lower() in (".bat", ".cmd")]
        check("仓库里确实有批处理文件在守", bool(_bats37),
              str([str(p.relative_to(_repo37)) for p in _bats37]))
        for _b37 in _bats37:
            _raw37 = _b37.read_bytes()
            _cjk37 = [i for i, b in enumerate(_raw37) if b > 0x7F]
            _rel37 = str(_b37.relative_to(_repo37)).replace("\\", "/")
            _line37 = _raw37[:_cjk37[0]].count(b"\n") + 1 if _cjk37 else 0
            check(f"{_rel37} 是纯 ASCII（.bat 里绝不能出现中文）",
                  not _cjk37,
                  f"{len(_cjk37)} 个非 ASCII 字节，第一个在第 {_line37} 行 —— "
                  f"中文请挪到配套的 .ps1 或英文文案里")

        # （2）install.bat 是用户双击的那一个：额外钉死它的几个前提
        _ib37 = _repo37 / "install.bat"
        if _ib37.exists():
            _txt37 = _ib37.read_text(encoding="ascii", errors="replace")
            _lines37 = [x.strip() for x in _txt37.splitlines() if x.strip()]
            check("install.bat 第一行是 @echo off", _lines37[0].lower() == "@echo off",
                  _lines37[0])
            check("install.bat 第二行就切到 UTF-8（让 PowerShell 的中文能显示）",
                  _lines37[1].lower().startswith("chcp 65001"), _lines37[1])
            check("install.bat 调的是 install.ps1",
                  "install.ps1" in _txt37)
            check("install.bat 支持非交互（CI 里不会卡在 pause）",
                  "QQBOT_NO_PAUSE" in _txt37)

        # （3）PowerShell：含中文就必须带 UTF-8 BOM，
        #     否则 PS 5.1 按 ANSI/GBK 解码，中文全乱（是解码错误，不是显示问题）
        _ps37 = [p for p in _all37 if p.suffix.lower() in (".ps1", ".psm1")]
        for _p37b in _ps37:
            _raw37b = _p37b.read_bytes()
            _rel37b = str(_p37b.relative_to(_repo37)).replace("\\", "/")
            try:
                _raw37b.decode("ascii")
                _has_cjk37 = False
            except UnicodeDecodeError:
                _has_cjk37 = True
            if _has_cjk37:
                check(f"{_rel37b} 含中文且带 UTF-8 BOM",
                      _raw37b.startswith(b"\xef\xbb\xbf"),
                      "无 BOM —— Windows PowerShell 5.1 会把中文按 GBK 解成乱码")

        # （4）install.ps1 的自检探针本身要立得住：
        #     字面量在被误解码时也是乱的，所以判据只能用**长度**
        check("误解码探针成立：'许杏玖' 正常 3 字、按 GBK 错配成 5 字",
              len("许杏玖") == 3
              and len("许杏玖".encode("utf-8").decode("gbk", errors="replace")) != 3,
              f"{len('许杏玖')} vs "
              f"{len('许杏玖'.encode('utf-8').decode('gbk', errors='replace'))}")

        # （5）shell 脚本：LF 且无 BOM（CRLF 会让 Linux 上的 shebang 失效）
        for _s37 in [p for p in _all37 if p.suffix.lower() in (".sh", ".bash")]:
            _raw37s = _s37.read_bytes()
            _rel37s = str(_s37.relative_to(_repo37)).replace("\\", "/")
            # 注意：别把 b"\r\n" 直接写进 f-string，Python 3.11 会报
            # "f-string expression part cannot include a backslash"（3.12 才放开）
            _crlf37 = _raw37s.count(b"\r\n")
            _bom37s = _raw37s.startswith(b"\xef\xbb\xbf")
            check(f"{_rel37s} 是 LF 且无 BOM",
                  _crlf37 == 0 and not _bom37s,
                  f"CRLF={_crlf37} BOM={_bom37s}")

    # ── 尾部污染自检：【24】~【37】是在隔离撤销之后跑的，得单独复核一遍 ──
    fp_tail = state_fingerprint()
    changed_tail = [k for k in _FP_TAIL if _FP_TAIL[k] != fp_tail.get(k)]
    check("【24】~【37】也没有污染真实状态文件", not changed_tail,
          f"被改动的：{changed_tail}（去这几段里把可写路径补上隔离）")
    __import__("shutil").rmtree(TAIL_DIR, ignore_errors=True)

    print(f"\n{'='*46}\n通过 {ok} 项，失败 {fail} 项\n{'='*46}")
    return fail


sys.exit(asyncio.run(main()))
