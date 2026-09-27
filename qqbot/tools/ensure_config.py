"""补齐 config.json 里缺失的配置项（幂等，只加不改）。

为什么需要这个：
  控制台写配置时，如果 bot 进程的 CFG 是旧的（启动之后 config.json 又被手工改过），
  早期实现会把内存里那份**整体覆盖**回磁盘 —— 于是启动后新加的整段配置被静默冲掉。
  （已经踩过：self_activity / memory.group_* 被冲没。）
  写回逻辑现已改成「重读磁盘 + 只合并本次改动」，但**已经被冲掉的键仍然缺失**，
  这个脚本负责把它们按默认值补回来。

用法：
    .venv\\Scripts\\python.exe tools\\ensure_config.py          # 只补缺失的
    .venv\\Scripts\\python.exe tools\\ensure_config.py --check   # 只看不改
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parents[1]
CFG = BASE / "config.json"

ACTS = [
    ("surf", "上网瞎逛", "any", 3, "你在网上乱翻，翻到一堆没用的东西", 0.70, False),
    ("video", "刷视频", "good", 3, "你在看 b 站，一会儿笑一会儿骂", 0.80, False),
    ("qzone", "翻空间", "any", 2, "你在翻好友的空间，边翻边撇嘴", 0.75, False),
    ("read", "翻旧书", "calm", 2, "你在书店角落翻一本没意思的旧书，翻两页就走神", 0.45, True),
    ("nap", "找个地方蜷着", "bad", 2, "你懒得动，找了个暖和地方蜷着，眼睛半睁半闭", 0.30, True),
    ("window", "趴窗台", "calm", 2, "你在窗台看外面，什么也不想", 0.50, False),
    ("stare", "发呆", "bad", 2, "你什么也没干，就坐在那儿发呆", 0.40, False),
    ("groom", "舔毛", "calm", 2, "你在整理自己，谁说话都懒得搭", 0.40, False),
    ("hunt", "盯虫子", "any", 2, "你盯着一只虫子看了很久，尾巴尖一抖一抖", 0.60, False),
    ("eat", "找吃的", "any", 2, "你去厨房翻了一圈，没找到想吃的", 0.65, False),
    ("mess", "拨东西", "good", 1, "你刚把桌上什么东西拨到地上，正在装作无事发生", 0.70, False),
    ("stretch", "伸懒腰", "good", 1, "你刚睡醒，伸了个很长的懒腰", 0.75, False),
    ("walk", "出门走走", "good", 2, "你出门随便走走，没什么目的地", 0.60, False),
    ("hide", "躲起来", "bad", 1, "你找了个没人看得见的地方躲起来，不想被找到", 0.25, True),
    ("watch", "看人", "any", 2, "你蹲在一边看人，谁也不说话", 0.55, False),
]

# 点路径 -> 默认值。只在缺失时补，已存在的一律不动。
DEFAULTS = {
    "self_activity.enable": True,
    "self_activity.tick_seconds": 120,
    "self_activity.min_gap_minutes": 20,
    "self_activity.max_gap_minutes": 75,
    "self_activity.duration_minutes": [15, 70],
    "self_activity.skip_chance": 0.25,
    "self_activity.material_keep_minutes": 90,
    "self_activity.acts": [
        {"id": i, "name": n, "mood": m, "weight": w, "hint": h, "reply_chance": rc, "busy": b}
        for i, n, m, w, h, rc, b in ACTS
    ],
    "self_activity.places": [
        "窗台", "书堆上", "旧书店的角落", "便利店门口", "楼下花坛边", "天台上", "楼梯口",
        "空纸箱里", "老槐树底下", "公交站牌下", "谁家阳台边上", "巷子深处", "图书馆台阶",
        "洗衣店门口", "小区长椅",
    ],
    "life.events_per_day": 3,
    "affinity.gain_nice": 2.0,
    "affinity.loss_rude": 6.0,
    "affinity.decay_per_day": 1.5,
    "mood.mood_gain_nice": 4.0,
    "mood.mood_loss_rude": 8.0,
    "mood.mood_pull_to_affinity": 0.35,
    "attention.alias_threshold_discount": 0.35,
    "attention.alias_window_seconds": 120,
    "memory.group_enable": True,
    "memory.max_group_notes": 6,
    "memory.group_path": "group_memory.json",
    "qzone.auto_interact_enable": True,
    "qzone.auto_interact_probability": 0.15,
    "qzone.auto_interact_cooldown_minutes": 40,
    "stickers.allow_image_fallback": False,
    "console.enable": True,
    "console.port": 6200,
}


def get_path(cfg: dict, path: str):
    node = cfg
    for k in path.split("."):
        if not isinstance(node, dict) or k not in node:
            return None, False
        node = node[k]
    return node, True


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    missing = []
    for path, val in DEFAULTS.items():
        _, exists = get_path(cfg, path)
        if not exists:
            missing.append(path)

    print(f"配置项总数：{len(DEFAULTS)}")
    if not missing:
        print("✅ 没有缺失，无需补齐。")
        return 0
    print(f"缺失 {len(missing)} 项：")
    for p in missing:
        print("   +", p)
    if check_only:
        print("（--check 模式，未修改）")
        return 1

    for path, val in DEFAULTS.items():
        _, exists = get_path(cfg, path)
        if exists:
            continue
        node = cfg
        keys = path.split(".")
        for k in keys[:-1]:
            nxt = node.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                node[k] = nxt
            node = nxt
        node[keys[-1]] = val

    # 原子写回（缩进/换行与原文件一致，避免无谓的 diff）
    fd, tmp = tempfile.mkstemp(prefix="config.json.", suffix=".tmp", dir=str(CFG.parent))
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CFG)
    print(f"✅ 已补齐 {len(missing)} 项。")
    print("   注意：如果 bot 正在运行，它的内存里仍是旧配置 —— 需要重启一次才生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
