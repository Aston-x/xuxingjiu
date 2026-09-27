"""戳一戳 · 完整统计。

为什么单独做一个脚本：戳一戳的处置散在日志的六七种打印里，靠 grep 数不清，
而且真正要回答的问题（"戳和聊天挨得太近时她该不该理"）得把**两类事件按时间对齐**
才看得出——这只能交给脚本。

用法：
    .venv\\Scripts\\python.exe tools\\poke_stats.py
    .venv\\Scripts\\python.exe tools\\poke_stats.py --window 30 --log bot.log

只读：只解析日志，不写任何东西。
"""
import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[(\w+)\] (.*)$")
# 她的四种反应（每一种都带被戳人）
POKE = [
    ("回话", re.compile(r"^戳一戳反应 来自(\S+?) 来源=")),
    ("装死(忙)", re.compile(r"^正忙着「[^」]*」，没搭理 (\S+?) 的戳一戳")),
    ("懒得理", re.compile(r"^这次懒得理 (\S+?) 的戳一戳")),
    ("冷却挡掉", re.compile(r"^戳一戳冷却中，来自 (\S+)")),
]
STREAK = re.compile(r"^(\S+?) 在 (\d+) 秒内戳了 (\d+) 次，触发自动禁言")
BAN = re.compile(r"^已禁言 (\S+?)（([\d.]+)\s*分钟，原因：([^，、）]*)")
POKE_BACK = re.compile(r"^回戳了 (\S+)")
# 群里有人说话（她看不看都记）＋ 她真的答了
HEARD = re.compile(r"^监听\[g(\d+)\] ([^:]+): (.*?) \|")
ASKED = re.compile(r"^提问\[g(\d+)\] ([^:]+): ")
# 她自己的回复
REPLIED = re.compile(r"^回复\[(g\d+|p\d+)\].*")
BUSY = re.compile(r"^正忙着「([^」]*)」，没搭理 \S+ 的戳一戳")

# ⚠️ 历史日志里的**模拟人物**：早期是脚本注入的假戳，不是真人。
# 统计时必须排掉 —— 不排的话"她 70% 装死、连戳禁言 97 次"这种结论
# 全是假数据撑起来的（真实值：装死 26%、真人连戳禁言 7 次）。
# 这份名单只用来**排除历史行**，不代表系统里还有这个人物。
SIMULATED = ("阿强",)


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()


def parse(path: Path, exclude: tuple = SIMULATED):
    """把日志扫成几条时间线。

    三个必须处理干净的东西，否则数字会假一倍以上：

    1. **重复行**。这份 bot.log 里同一个事件被写 2~4 遍（历史上挂过多个
       指向同一文件的 handler），所以按"同一条消息 2 秒内再出现就丢掉"去重。
    2. **模拟人物**。日志里有一部分戳一戳是**脚本注入的**，不是真人
       （见 SIMULATED）。不排掉的话"她 X% 装死"这种结论会全被假数据带跑 ——
       这事真发生过一次：真实回话率其实有 54%，被模拟数据压到了 22%。
    3. **昵称 / QQ 号两种身份**。有的行只记昵称（"没搭理 洞 的戳一戳"），
       有的只记 QQ（"戳一戳反应 来自10003"）。用私聊的
       `提问[p<QQ>] <昵称>: ` 把昵称映射到 QQ，两边才对齐。
    """
    pokes: list[tuple[float, str, str]] = []      # (时间, 谁, 结局)
    streaks: list[tuple[float, str, int, int]] = []
    bans: list[tuple[float, str, float, str]] = []
    backs: list[tuple[float, str]] = []
    spoke: list[tuple[float, str]] = []           # 群里的发言 (时间, 谁)
    heard: list[tuple[float, str]] = []
    replied: list[float] = []
    busy: list[tuple[float, str]] = []            # (时间, 当时在干什么)
    nick2qq: dict[str, str] = {}
    seen: dict[str, float] = {}                   # 去重：归一化文本 -> 上次时间
    skipped = dup = sim = 0
    first = last = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE.match(raw)
        if not m:
            continue
        stamp, _lvl, msg = m.group(1), m.group(2), m.group(3)
        if "测试" in msg or msg.startswith("[行为]"):
            skipped += 1
            continue
        t = ts(stamp)
        key = re.sub(r"\d+", "#", msg)[:70]
        if t - seen.get(key, -99) < 2.0:
            dup += 1
            continue
        seen[key] = t
        if first is None:
            first = t
        last = t
        # 昵称 -> QQ 的映射（私聊提问里两个都在）
        mm = re.match(r"^提问\[p(\d+)\] ([^:]+): ", msg)
        if mm:
            nick2qq[mm.group(2).strip()] = "qq:" + mm.group(1)
        hit = False
        mm = BUSY.match(msg)
        if mm:
            busy.append((t, mm.group(1)))
        for kind, rx in POKE:
            mm = rx.match(msg)
            if mm:
                who = mm.group(1)
                who = "qq:" + who if who.isdigit() else who
                if who in exclude:
                    sim += 1
                else:
                    pokes.append((t, who, kind))
                hit = True
                break
        if hit:
            continue
        mm = STREAK.match(msg)
        if mm:
            streaks.append((t, mm.group(1), int(mm.group(3)), int(mm.group(2))))
            continue
        mm = BAN.match(msg)
        if mm:
            bans.append((t, mm.group(1), float(mm.group(2)), mm.group(3)))
            continue
        mm = POKE_BACK.match(msg)
        if mm:
            backs.append((t, mm.group(1)))
            continue
        mm = HEARD.match(msg)
        if mm:
            heard.append((t, mm.group(2).strip()))
            spoke.append((t, mm.group(2).strip()))
            continue
        if ASKED.match(msg):
            continue
        if REPLIED.match(msg):
            replied.append(t)
    # 昵称统一成 QQ（能映射的），两边的身份才可比
    pokes = [(t, nick2qq.get(w, w), k) for t, w, k in pokes]
    spoke = [(t, nick2qq.get(w, w)) for t, w in spoke]
    heard = [(t, nick2qq.get(w, w)) for t, w in heard]
    bans = [(t, nick2qq.get(w, w), m_, r) for t, w, m_, r in bans]
    streaks = [(t, nick2qq.get(w, w), c, w2) for t, w, c, w2 in streaks]
    # 模拟人物要在**每一条时间线**上都排掉 —— 只在戳那里排，
    # 禁言/发言那两条线还会继续把他算进去（第一版就是这样漏的）
    def real(w):
        return w not in exclude

    spoke = [(t, w) for t, w in spoke if real(w)]
    heard = [(t, w) for t, w in heard if real(w)]
    bans = [b for b in bans if real(b[1])]
    streaks = [s for s in streaks if real(s[1])]
    backs = [b for b in backs if real(b[1])]
    # 显示名：QQ -> 最好认的昵称
    qq2nick = {}
    for nick, qq in nick2qq.items():
        qq2nick.setdefault(qq, nick)
    return dict(pokes=pokes, streaks=streaks, bans=bans, backs=backs,
                spoke=spoke, heard=heard, replied=replied, qq2nick=qq2nick,
                busy=busy, first=first, last=last, skipped=skipped, dup=dup,
                sim=sim)


def fmt_gap(x: float) -> str:
    if x < 60:
        return f"{x:.0f} 秒"
    if x < 3600:
        return f"{x/60:.1f} 分"
    return f"{x/3600:.1f} 小时"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="bot.log")
    ap.add_argument("--exclude", default=",".join(SIMULATED),
                    help="要排掉的模拟人物昵称，逗号分隔")
    ap.add_argument("--window", type=int, default=30,
                    help="判定“戳和聊天挨得近”的秒数（默认 30）")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    path = BASE_DIR / args.log
    exclude = tuple(x.strip() for x in args.exclude.split(",") if x.strip())
    d = parse(path, exclude)
    pokes = d["pokes"]
    if not pokes:
        print(f"{path} 里没找到戳一戳记录。")
        return 0
    span = (d["last"] - d["first"]) / 3600

    print(f"# 戳一戳 · 完整统计")
    print(f"来源：`{args.log}`（{span:.1f} 小时，"
          f"{datetime.fromtimestamp(d['first']):%m-%d %H:%M} ~ "
          f"{datetime.fromtimestamp(d['last']):%m-%d %H:%M}）")
    print(f"已排除 {d['skipped']} 行测试/流水噪声、**{d['dup']} 行重复行**"
          f"（这份日志历史上挂过多个 handler，同一事件被写 2~4 遍）")
    if d["sim"]:
        print(f"另外排掉 **{d['sim']} 条模拟人物的戳**"
              f"（{'、'.join(exclude)} —— 早期脚本注入的假数据，不是真人；"
              "禁言/发言那两条时间线也一并排掉了）")
    print()

    # ── 1. 总览 ──
    nm = d["qq2nick"]
    kinds = Counter(k for _t, _w, k in pokes)
    print("## 1. 总览：她有记录的处置")
    print()
    print(f"被戳（有处置记录）**{len(pokes)}** 次")
    print()
    print("| 她的反应 | 次数 | 占比 | 含义 |")
    print("|---|---|---|---|")
    latin = {"回话": "走了一次模型，真回了一句", "装死(忙)": "手里有活，按 busy 概率装死",
             "懒得理": "闲着，但这次随机不回", "冷却挡掉": "距上次被戳不到 20 秒，直接丢"}
    for k, _rx in POKE:
        n = kinds.get(k, 0)
        print(f"| {k} | {n} | {n*100/len(pokes):.0f}% | {latin[k]} |")
    ignored = kinds.get("装死(忙)", 0) + kinds.get("懒得理", 0)
    print(f"\n**合计不理她 = {ignored} 次（{ignored*100/len(pokes):.0f}%），回话只有 "
          f"{kinds.get('回话', 0)} 次（{kinds.get('回话', 0)*100/len(pokes):.0f}%）**")
    print(f"主动回戳 {len(d['backs'])} 次")

    # ── 2. 谁在戳 ──
    print("\n## 2. 谁在戳")
    per = defaultdict(Counter)
    for _t, who, k in pokes:
        per[who][k] += 1
    print()
    print("| 谁 | 被戳 | 回话 | 装死 | 懒得理 | 冷却 |")
    print("|---|---|---|---|---|---|")
    for who, c in sorted(per.items(), key=lambda kv: -sum(kv[1].values()))[:10]:
        tot = sum(c.values())
        print(f"| {nm.get(who, who)} | {tot} | {c.get('回话',0)} | {c.get('装死(忙)',0)} "
              f"| {c.get('懒得理',0)} | {c.get('冷却挡掉',0)} |")

    # ── 3. 连戳与禁言 ──
    print("\n## 3. 连戳与禁言")
    print()
    ban_per = Counter(w for _t, w, _m, _r in d["bans"])
    streak_bans = [b for b in d["bans"] if "连续戳一戳" in b[3]]
    self_bans = [b for b in d["bans"] if "她自己决定的" in b[3]]
    print(f"自动禁言（连戳触发）**{len(streak_bans)}** 次；"
          f"她自己决定禁言 **{len(self_bans)}** 次")
    print(f"涉及的人：{ {nm.get(k, k): v for k, v in ban_per.most_common(5)} }")
    # 禁言后还戳不戳
    after = {"60 秒内又戳": 0, "5 分钟内又戳": 0, "没再戳": 0}
    for t, who, _mins, _why in streak_bans:
        if who not in per:
            continue
        nxt = [pt for pt, pw, _k in pokes if pw == who and pt > t]
        if not nxt:
            after["没再戳"] += 1
        elif nxt[0] - t <= 60:
            after["60 秒内又戳"] += 1
        elif nxt[0] - t <= 300:
            after["5 分钟内又戳"] += 1
        else:
            after["没再戳"] += 1
    print(f"禁言之后他接着戳了没有：{dict(after)}")
    # 同一人相邻两次被戳的间隔
    gaps = []
    bywho = defaultdict(list)
    for t, who, _k in pokes:
        bywho[who].append(t)
    for who, ts_ in bywho.items():
        if len(ts_) < 3:
            continue
        g = [b - a for a, b in zip(ts_, ts_[1:])]
        gaps += g
    if gaps:
        gaps.sort()
        mid = gaps[len(gaps)//2]
        print(f"同一人被戳的间隔：中位数 {fmt_gap(mid)}，"
              f"最短 {fmt_gap(gaps[0])}，最长 {fmt_gap(gaps[-1])}")

    # ── 4. 戳 vs 对话 的时间关系（本次要改的核心依据）──
    print(f"\n## 4. 戳和聊天挨得近不近（判定窗口 ±{args.window} 秒）")
    near = [p for p in pokes
            if any(w == p[1] and abs(st - p[0]) <= args.window for st, w in d["spoke"])]
    print()
    print(f"被戳 **{len(pokes)}** 次里，同一个人前后 {args.window} 秒内**说过话**的有 "
          f"**{len(near)}** 次（{len(near)*100/len(pokes):.0f}%）")
    if near:
        nk = Counter(k for _t, _w, k in near)
        print()
        print("这些「挨着聊天」的戳，她的反应：")
        print()
        print("| 反应 | 次数 | 占比 |")
        print("|---|---|---|")
        for k, _rx in POKE:
            n = nk.get(k, 0)
            if n:
                print(f"| {k} | {n} | {n*100/len(near):.0f}% |")
        ni = nk.get("装死(忙)", 0) + nk.get("懒得理", 0)
        print(f"\n→ **挨着聊天时她仍然不理 {ni} 次（{ni*100/len(near):.0f}%）**，"
              "这正是「戳一下只是想被看见」的典型场景")

    # ── 5. 忙不忙 vs 回不回 ──
    print("\n## 5. 忙 vs 不忙")
    acts = Counter(a for _t, a in d["busy"])
    print()
    print(f"她因为「忙」而装死的时段分布：{dict(acts.most_common(6))}")
    print("\n> 注意：现在的判定是「只要 life.busy 为真」，"
          "**可打断的忙（书店看店/吃饭/打盹）和一睡不醒（睡觉）被一视同仁** —— "
          "这正是「装死太多」的主要来源。")

    print("\n## 6. 结论")
    print()
    print(f"1. 她被戳 {len(pokes)} 次，**{ignored*100/len(pokes):.0f}% 当没看见**；回戳只有 {len(d['backs'])} 次")
    nxt = after.get("60 秒内又戳", 0) + after.get("5 分钟内又戳", 0)
    print(f"2. 连戳禁言触发了 {len(streak_bans)} 次；其中解禁/禁言后 5 分钟内又戳的有 "
          f"{nxt} 次" + ("（禁言没拦住）" if nxt > len(streak_bans) * 0.3 else "（基本拦住了）"))
    print(f"3. 其中 {len(near)} 次戳发生在「这个人刚说过话」的窗口里"
          f"（{len(near)*100/len(pokes):.0f}%）—— 这类多半是「戳一下提醒你看我」，"
          "不是想开新话题，值得区别对待")
    return 0


if __name__ == "__main__":
    sys.exit(main())
