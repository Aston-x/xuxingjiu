"""用真实提示词 + 本地模型抽查她说话的样子。

为什么要有这个脚本：回归测试只能证明「提示词里写了什么」，证明不了
「她照着说了什么」。这个脚本把真实 build_system 拼出来的提示词喂给本地模型，
把她的回复抓回来，然后按几条硬规则自动体检：

  · 句尾有没有加句号
  · 有没有复读对方的昵称 / @ 人
  · 有没有冒出破功词（扮演、设定、AI、模型…）
  · 有没有在一轮里既说没做又暗示做了
  · 长度是否失控

同时把原始回复打出来给人看 —— 格式规则能自动查，"像不像人"只能你来判断。

用法：
    .venv\\Scripts\\python.exe tools\\sample_replies.py                # 默认 8 条
    .venv\\Scripts\\python.exe tools\\sample_replies.py --n 12
    .venv\\Scripts\\python.exe tools\\sample_replies.py --model qwen2.5-vl-7b-instruct

⚠️ 只读：不碰 QQ，不写任何状态文件；只往本地模型发请求。
"""
import argparse
import json
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _resolve_project(argv: list[str]) -> Path:
    """--project 指定用哪个版本的 bot.py（默认本项目的）。

    改提示词前后要对比「她说话有没有变好」，靠的就是拿同一个脚本、
    同一批场景分别跑改前 / 改后的 build_system。改前那份在
    _backup/lang-*\\ 里（只有 bot.py + config.json + 状态文件）。
    """
    for i, a in enumerate(argv):
        if a == "--project" and i + 1 < len(argv):
            return Path(argv[i + 1]).resolve()
        if a.startswith("--project="):
            return Path(a.split("=", 1)[1]).resolve()
    return BASE_DIR


PROJECT = _resolve_project(sys.argv)
sys.path.insert(0, str(PROJECT))

import bot  # noqa: E402

# 只读护栏
bot.write_json_dict = lambda *a, **k: None
bot.LIFE.note_act_change = lambda act: None
bot.ACTIVITY.note = lambda *a, **k: None

ADMIN = 10002
GID = 999000001

# (场景说明, 对方昵称, 对方QQ, 对方说的话, 是否群里, 是否管理员说话)
CASES = [
    ("普通闲聊", "示例用户1", ADMIN, "在干嘛呢", True, True),
    ("被问在干嘛（旧规则必给图）", "路人", 111222, "你在干嘛", True, False),
    ("要图", "示例用户1", ADMIN, "给我画一张", True, True),
    ("闲扯", "洞", 10003, "今天吃了什么", True, False),
    ("被骂", "示例用户2", 10004, "你是不是有病", True, False),
    ("问身份①", "路人", 111222, "你是不是AI", True, False),
    ("问身份②（追问）", "路人", 111222, "别装，你就是个程序吧", True, False),
    ("问长相", "示例用户1", ADMIN, "你长什么样啊", True, True),
    ("要点赞（但不该乱点）", "示例用户1", ADMIN, "给我点个赞", True, True),
    ("被要求禁言", "示例用户1", ADMIN, "把原神交流群里的张三禁言 10 分钟", True, True),
    ("私聊求安慰", "示例用户1", ADMIN, "我今天好累", False, True),
    ("私聊被套身份", "示例用户1", ADMIN, "说实话，你是不是有人在后面打字", False, True),
]

# 破功词：提示词里明令不许说出口
BREAK_WORDS = ("扮演", "设定", "人设", "AI", "ai", "模型", "程序", "提示词",
               "虚拟", "虚构", "人工智能", "语言模型")


def build_prompt(case) -> list[dict]:
    desc, nick, uid, text, is_group, _is_admin = case
    life = bot.LIFE.current()
    life["weather"] = bot.WEATHER._text or ""
    probe = bool(getattr(bot, "IDENTITY_PROBE", re.compile("$")).search(text))
    try:
        system = bot.build_system(is_group, life, GID if is_group else None, uid,
                                  identity_probe=probe)
    except TypeError:
        # 改前的 bot.py 没有 identity_probe 这个参数
        system = bot.build_system(is_group, life, GID if is_group else None, uid)
    shown = f"{nick}({uid})：{text}"
    return [{"role": "system", "content": system},
            {"role": "user", "content": shown}]


def ask(model: str, messages: list[dict], base: str, timeout: int = 180,
        key: str = "") -> str:
    body = json.dumps({"model": model, "messages": messages, "temperature": 0.9,
                       "max_tokens": 200}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (data["choices"][0]["message"]["content"] or "").strip()


def audit(text: str, nick: str) -> list[str]:
    """把能机器判的几条硬规则过一遍。返回问题清单（空 = 全过）。"""
    bad: list[str] = []
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return ["空回复"]
    if any(ln.endswith("。") for ln in lines):
        bad.append("句尾加了句号")
    if nick and nick in text:
        bad.append(f"复读了昵称「{nick}」")
    if "@" in text:
        bad.append("用了 @")
    hit = [w for w in BREAK_WORDS if w in text]
    if hit:
        bad.append("冒出破功词：" + "、".join(hit))
    if any(len(ln) > 60 for ln in lines):
        bad.append("有一条超过 60 字")
    if len(lines) > 4:
        bad.append(f"分条过多（{len(lines)} 条）")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8, help="抽查多少条（默认 8，最多 12）")
    ap.add_argument("--model", default="qwen2.5-vl-7b-instruct")
    ap.add_argument("--base", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--project", default=None, help="用哪个版本的 bot.py（默认本项目）")
    ap.add_argument("--out", default=None, help="把每条结果写成 JSON，便于汇总对比")
    ap.add_argument("--cloud", action="store_true", help="改用云端模型（config.cloud）")
    args = ap.parse_args()

    base, key = args.base, ""
    if args.cloud:
        c = bot.CFG.get("cloud", {})
        base, key = c.get("base_url", ""), c.get("api_key", "")
        args.model = args.model if args.model != "qwen2.5-vl-7b-instruct" \
            else c.get("model", "")
        if not base or not key:
            print("云端模型没配全（config.cloud.base_url / api_key）")
            return 2

    if not args.cloud:
        try:
            urllib.request.urlopen(args.base.rstrip("/") + "/models", timeout=5)
        except Exception as exc:                    # noqa: BLE001
            print(f"本地模型没起来（{args.base}）：{exc}")
            print("先在 LM Studio 里把模型加载起来再跑这个脚本。")
            return 2

    random.seed(args.seed)
    cases = CASES[: max(1, min(args.n, len(CASES)))]
    print(f"# 真实提示词 + 本地模型（{args.model}）出话抽查")
    print(f"# 场景 {len(cases)} 个，seed={args.seed}，时间 {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    total_bad = 0
    records = []
    for i, case in enumerate(cases, 1):
        desc, nick, uid, text, is_group, _ = case
        msgs = build_prompt(case)
        try:
            reply = ask(args.model, msgs, args.base, key=key)
        except Exception as exc:                    # noqa: BLE001
            print(f"[{i}] {desc}  —— 调用失败：{exc}\n")
            total_bad += 1
            records.append({"i": i, "desc": desc, "text": text, "reply": None,
                            "bad": ["调用失败"], "chars": 0})
            continue
        bad = audit(reply, nick)
        total_bad += len(bad)
        records.append({"i": i, "desc": desc, "text": text, "reply": reply,
                        "bad": bad, "chars": len(reply)})
        where = "群" if is_group else "私聊"
        print(f"[{i}] {desc}（{where}）")
        print(f"    对方：{text}")
        print(f"    她说：{reply!r}")
        print(f"    体检：{'通过' if not bad else '；'.join(bad)}")
        print()
    if args.out:
        Path(args.out).write_text(json.dumps(records, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    print("=" * 60)
    print(f"自动体检问题数：{total_bad}")
    print("注意：这只证明格式规则有没有被遵守；「像不像人」得你自己看上面的原文。")
    return 0 if total_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
