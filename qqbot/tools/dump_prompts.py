"""把许杏玖当前所有模型提示词导成文本，用来做「改前 / 改后」对照。

为什么要这个脚本：提示词散在 bot.py 的十几处和 config.json 里，
靠人眼抄一遍必然漏。跑这个脚本得到的快照是可重跑、可 diff 的。

用法：
    .venv\\Scripts\\python.exe tools\\dump_prompts.py            # 打到 stdout
    .venv\\Scripts\\python.exe tools\\dump_prompts.py out.txt    # 写文件
    .venv\\Scripts\\python.exe tools\\dump_prompts.py --ondemand out.txt

⚠️ 只读：跑之前会把 bot 里仅有的两处落盘入口换成空函数，
   避免导出提示词时顺手改了生活状态或行为流水。
"""
import random
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import bot  # noqa: E402

# ── 只读护栏：掐掉落盘入口（导出不该有副作用）──
bot.write_json_dict = lambda *a, **k: None
bot.LIFE.note_act_change = lambda act: None
bot.ACTIVITY.note = lambda *a, **k: None

ADMIN = 10002
OTHER = 111222
GID = 999000001
OPEN = chr(0xFF08)


def blocks(s: str) -> int:
    """常驻提示里「（」起段的块数 —— 每执行一次 parts.append 就多一段。"""
    return s.count(chr(10) + OPEN) + (1 if s.startswith(OPEN) else 0)


def variants(life: dict) -> list[tuple[str, str]]:
    return [
        ("1. 群聊 / 非管理员 / 有群号",
         bot.build_system(True, life, GID, OTHER)),
        ("2. 群聊 / 管理员 / 有群号（含群内禁言说明）",
         bot.build_system(True, life, GID, ADMIN)),
        ("3. 私聊 / 管理员",
         bot.build_system(False, life, None, ADMIN)),
        ("4. 私聊 / 非管理员",
         bot.build_system(False, life, None, OTHER)),
    ]


def fresh_variant(life: dict) -> tuple[str, str]:
    """全新安装：记忆 / 群名册 / 表情包都空。"""
    keep = (dict(bot.MEMORY.people), list(bot.MEMORY.legacy),
            dict(bot.GROUPS.groups), list(bot.STICKERS.items))
    try:
        bot.MEMORY.people.clear()
        bot.MEMORY.legacy.clear()
        bot.GROUPS.groups.clear()
        bot.STICKERS.items.clear()
        return ("5. 群聊 / 全新安装（无记忆无群无表情）",
                bot.build_system(True, life, None, OTHER))
    finally:
        (bot.MEMORY.people.update(keep[0]),
         bot.MEMORY.legacy.extend(keep[1]),
         bot.GROUPS.groups.update(keep[2]),
         bot.STICKERS.items.extend(keep[3]))


def on_demand_source() -> str:
    """按需提示词：直接抄源码，避免为了拿到文本去真的调一次模型。"""
    import inspect
    parts: list[str] = []
    for title, obj in (
        ("模块常量 AUTO_INTERACT_PROMPT", bot.AUTO_INTERACT_PROMPT),
        ("Memory.extract_from（记忆抽取）", bot.Memory.extract_from),
        ("SD.compose（出图编排）", bot.SD.compose),
        ("Chat.describe_images（本地识图）", bot.Chat.describe_images),
        ("StickerBook.judge（表情包判定）", bot.StickerBook.judge),
        ("StickerBook.tag（表情包打标签）", bot.StickerBook.tag),
        ("handle_message（含看图 look / 催标记 nudge）", bot.handle_message),
        ("_action_note（空间动作收尾）", bot._action_note),
        ("idle_thought（冷场取材）", bot.idle_thought),
        ("proactive_speak（主动插话）", bot.proactive_speak),
        ("build_activity_report（行为小结）", bot.build_activity_report),
        ("resolve_web（搜索后重答）", bot.resolve_web),
        ("_run_activity（自发活动）", bot._run_activity),
        ("handle_notice（戳一戳 / 连击禁言台词）", bot.handle_notice),
        ("handle_qzone_reply（配文 #cap）", bot.handle_qzone_reply),
        ("qzone_auto_post_maybe（自发说说 #qzone）", bot.qzone_auto_post_maybe),
        ("Chat.avoid_repeat（防复读）", bot.Chat.avoid_repeat),
        ("Chat._fit_vision（看不到图提示）", bot.Chat._fit_vision),
        ("pre_draw_line（画图前过渡语）", bot.pre_draw_line),
        ("ASK_TO_TAG（催标记文案）", bot.ASK_TO_TAG),
        ("ASK_BAN_RULE（催禁言标记文案）", bot.ASK_BAN_RULE),
    ):
        try:
            src = obj if isinstance(obj, str) else inspect.getsource(obj)
        except Exception as exc:                      # noqa: BLE001
            src = f"（取源码失败：{exc}）"
        parts.append(f"{'=' * 70}\n{title}\n{'=' * 70}\n{src.rstrip()}\n")
    return "\n".join(parts)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_ondemand = "--ondemand" in sys.argv
    out = Path(args[0]) if args else None

    random.seed(0)                      # 今天的小事是按概率+seed 给的，固定住才能对比
    life = bot.LIFE.current()
    life["weather"] = bot.WEATHER._text or ""

    if want_ondemand:
        text = (f"# 许杏玖 · 按需提示词源码快照\n"
                f"# 导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"# 来源：{BASE_DIR}\\bot.py（源码原文，未经过模型）\n\n"
                + on_demand_source())
    else:
        rows: list[tuple[str, str]] = []
        for name, s in variants(life):
            rows.append((name, s))
        rows.append(fresh_variant(life))

        chunks = [f"# 许杏玖 · 常驻系统提示词快照（build_system）",
                  f"# 导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                  f"# 条件：random.seed(0)；life 来自 LIFE.current()；weather={life.get('weather')!r}",
                  "",
                  "| 形态 | 字符数 | 块数（（起段） |",
                  "|---|---|---|"]
        for name, s in rows:
            chunks.append(f"| {name} | {len(s)} | {blocks(s)} |")
        chunks.append("")
        for name, s in rows:
            chunks.append(f"\n{'=' * 70}\n{name}  —  {len(s)} 字符 / {blocks(s)} 块\n{'=' * 70}")
            chunks.append(s)
        chunks.append("\n" + "=" * 70 + "\n配置里的语言字段\n" + "=" * 70)
        for k in ("persona", "world", "style_boost", "style_format", "self_image"):
            v = bot.CFG.get(k) or ""
            chunks.append(f"\n--- {k}（{len(v)} 字）---\n{v}")
        text = "\n".join(chunks)

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"已写出 {out}（{len(text)} 字符）")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
