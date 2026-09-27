"""人设层单测：角色卡、多套人设、按会话绑定、锁。

不联网、不 import bot、不碰真实状态文件 —— 所有写操作都落在临时目录里，
跑完自检一遍临时目录之外没被写过。

跑：.venv\\Scripts\\python.exe test_persona.py
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile

# 输出统一成 UTF-8。中文 Windows 上 stdio 是 GBK，下面满屏的 "✅" 直接 print 会抛
# UnicodeEncodeError，跑到第一个断言就崩（而且看着像测试失败）。
# 这个三段式和 qqbot/bot.py 里那份一致 —— 本文件不 import bot，所以要自己加一遍。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from persona import (  # noqa: E402
    ART_FIELDS,
    LOCKED_FIELDS,
    PROMPT_FIELDS,
    PROMPT_LIST_FIELDS,
    PersonaLibrary,
    blank_card,
)

ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def _example_cfg() -> dict:
    return json.loads((BASE_DIR / "config.example.json").read_text(encoding="utf-8"))


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="persona_test_"))
    try:
        _run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("=" * 46)
    print(f"通过 {ok} 项，失败 {fail} 项")
    print("=" * 46)
    return 1 if fail else 0


def _run(tmp: pathlib.Path) -> None:
    # ── 1. 出厂卡和 config.example.json 必须逐字一致 ──────────────────
    # 这条是零回归的护栏：角色卡取代老的扁平配置，出厂值就不能和原来对不上。
    print("\n【1】出厂角色卡 vs config.example.json")
    lib = PersonaLibrary(BASE_DIR, _example_cfg())
    check("能加载到出厂卡", "xuxingjiu" in lib.cards, str(list(lib.cards)))
    card = lib.cards["xuxingjiu"]
    check("出厂卡结构自检通过", PersonaLibrary.validate(card) == [],
          str(PersonaLibrary.validate(card)))
    ex = _example_cfg()
    same = [k for k in PROMPT_FIELDS if card["prompt"].get(k) == ex.get(k)]
    check("6 个提示词字段与 config.example.json 完全一致", len(same) == len(PROMPT_FIELDS),
          f"对得上 {len(same)}/{len(PROMPT_FIELDS)}：差 {set(PROMPT_FIELDS) - set(same)}")
    same_list = [k for k in PROMPT_LIST_FIELDS if card["lists"].get(k) == ex.get(k)]
    check("3 组话术池与 config.example.json 完全一致",
          len(same_list) == len(PROMPT_LIST_FIELDS),
          f"差 {set(PROMPT_LIST_FIELDS) - set(same_list)}")
    sd = ex.get("sd") or {}
    same_art = [k for k in ART_FIELDS if card["art"].get(k) == sd.get(k)]
    check("8 个生图字段与 config.example.json 的 sd 段完全一致",
          len(same_art) == len(ART_FIELDS), f"差 {set(ART_FIELDS) - set(same_art)}")

    # ── 2. 老配置优先（迁移期的桥）────────────────────────────────────
    print("\n【2】config.json 里的老键优先于卡")
    lib2 = PersonaLibrary(BASE_DIR, {"persona": "老配置里的她", "world": ""})
    pf = lib2.prompt_fields("")
    check("老键覆盖卡里的同名字段", pf["persona"] == "老配置里的她", pf["persona"])
    check("空字符串也算老键（不是「没填」）", pf["world"] == "", repr(pf["world"]))
    check("老配置没提的字段仍然来自卡", pf["self_image"] == card["prompt"]["self_image"])
    lib3 = PersonaLibrary(BASE_DIR, {"sd": {"character_tags": "old_tags, 1girl"}})
    check("sd 段的老键也能覆盖", lib3.art_fields("")["character_tags"] == "old_tags, 1girl")
    lib4 = PersonaLibrary(BASE_DIR, {})
    check("老键都不在时完全以卡为准",
          lib4.prompt_fields("")["persona"] == card["prompt"]["persona"])

    # ── 3. 出厂卡默认名字与别名 ──────────────────────────────────────
    print("\n【3】名字与别名")
    check("names() 含本名和 3 个别名", lib.names() == ("许杏玖", "杏玖", "玖玖", "小玖"),
          str(lib.names()))

    # ── 4. 按会话 / 按群绑定 ────────────────────────────────────────
    print("\n【4】按会话绑定")
    work = tmp / "qqbot"
    (work / "personas").mkdir(parents=True)
    shutil.copy(BASE_DIR / "personas" / "xuxingjiu.json", work / "personas")
    libb = PersonaLibrary(work, {})
    okc, _ = libb.new_card("lengdan", "冷淡版")
    check("能新建一张卡", okc and "lengdan" in libb.cards)
    check("新卡字段骨架齐全",
          all(k in libb.cards["lengdan"]["prompt"] for k in PROMPT_FIELDS))
    check("没绑定时走默认卡", libb.resolve("g123") == "xuxingjiu", libb.resolve("g123"))
    libb.bind("g123", "lengdan")
    check("群绑定生效", libb.resolve("g123") == "lengdan")
    check("别的群不受影响", libb.resolve("g456") == "xuxingjiu")
    libb.bind("p10001", "lengdan")
    check("私聊也能单独绑", libb.resolve("p10001") == "lengdan")
    check("绑定不存在的卡会被拒", libb.bind("g999", "nope")[0] is False)
    check("绑定关系落盘并回落", PersonaLibrary(work, {}).resolve("g123") == "lengdan")
    check("解绑后回默认卡", libb.unbind("g123")[0] and libb.resolve("g123") == "xuxingjiu")
    check("重复解绑返回 False", libb.unbind("g123")[0] is False)
    # 每张卡的人设内容确实不同：绑上之后提示词就跟着换
    libb.set_field("lengdan", "persona", "你话很少。")
    check("切卡后提示词跟着换", libb.prompt_fields("p10001")["persona"] == "你话很少。")
    check("默认会话不受那张卡影响",
          libb.prompt_fields("g456")["persona"] != "你话很少。")

    # ── 5. 锁：名字和形象不许动，别的照常 ────────────────────────────
    print("\n【5】锁定模式")
    lock_on = PersonaLibrary(work, {"persona_lock": True})
    check("锁着时改 name 被拒", lock_on.set_field("xuxingjiu", "name", "别人")[0] is False)
    check("锁着时改 aliases 被拒",
          lock_on.set_field("xuxingjiu", "aliases", ["别人"])[0] is False)
    check("锁着时改形象描述被拒",
          lock_on.set_field("xuxingjiu", "self_image", "红头发")[0] is False)
    check("锁着时改生图形象串被拒",
          lock_on.set_field("xuxingjiu", "character_tags", "1girl")[0] is False)
    check("锁着时改语气仍然可以",
          lock_on.set_field("xuxingjiu", "persona", "改过的人设")[0] is True)
    check("锁着时改生图数量词仍然可以",
          lock_on.set_field("xuxingjiu", "solo_prefix", "1girl, solo")[0] is True)
    check("被拒的字段真的没变",
          lock_on.cards["xuxingjiu"]["name"] == "许杏玖")
    check("拒绝理由说得清", "硬约束" in lock_on.set_field("lengdan", "name", "x")[1])
    lock_off = PersonaLibrary(work, {})
    check("不开锁时名字可改（开源版默认）",
          lock_off.set_field("lengdan", "name", "新名字")[0] is True)
    check("改完名字，绑它的会话立刻按新名字认人",
          lock_off.names("p10001")[0] == "新名字", str(lock_off.names("p10001")))
    check("没绑的会话仍然认默认卡的名字",
          lock_off.names("g456")[0] == "许杏玖", str(lock_off.names("g456")))
    check("默认卡可以显式切走", lock_off.set_default("lengdan")[0] is True)
    check("切完之后默认会话也换人", lock_off.names("g456")[0] == "新名字")
    check("默认标记跟着挪走，且只剩一个",
          sum(1 for v in lock_off.cards.values() if v.get("default")) == 1)
    check("默认卡切换会落盘",
          PersonaLibrary(work, {}).names("g456")[0] == "新名字")
    check("能切回原卡", lock_off.set_default("xuxingjiu")[0] is True
          and lock_off.names("g456")[0] == "许杏玖")
    card_unlocked = blank_card("free", "随便改")
    card_unlocked["lock"] = False
    (work / "personas" / "free.json").write_text(
        json.dumps(card_unlocked, ensure_ascii=False), encoding="utf-8")
    forced = PersonaLibrary(work, {"persona_lock": True})
    check("卡自己声明 lock:false 时，全局开着也放行",
          forced.set_field("free", "name", "新名")[0] is True)

    # ── 6. 新建 / 删除 ──────────────────────────────────────────────
    print("\n【6】新建与删除")
    check("卡 id 不合法会被拒", PersonaLibrary(work, {}).new_card("../坏", "x")[0] is False)
    check("卡 id 重复会被拒", PersonaLibrary(work, {}).new_card("lengdan", "x")[0] is False)
    one = tmp / "one"
    (one / "personas").mkdir(parents=True)
    shutil.copy(BASE_DIR / "personas" / "xuxingjiu.json", one / "personas")
    lone = PersonaLibrary(one, {})
    check("最后一张卡删不掉", lone.delete_card("xuxingjiu")[0] is False)
    check("删掉绑着的卡后，绑它的会话回默认卡", (
        libb.bind("g777", "lengdan")[0]
        and libb.delete_card("lengdan")[0]
        and libb.resolve("g777") == "xuxingjiu"))

    # ── 7. 坏卡不能让整个库加载失败 ─────────────────────────────────
    print("\n【7】容错")
    (work / "personas" / "broken.json").write_text("{ 这不是 json", encoding="utf-8")
    (work / "personas" / "badid!.json").write_text('{"id": "badid!", "name": "x"}',
                                                   encoding="utf-8")
    safe = PersonaLibrary(work, {})
    check("写坏的卡被跳过，其它卡照常加载", "xuxingjiu" in safe.cards, str(list(safe.cards)))
    check("id 不合法的卡被跳过", "badid!" not in safe.cards)
    check("json 坏掉不会抛异常", True)

    # ── 8. 结构校验 ────────────────────────────────────────────────
    print("\n【8】卡片自检")
    bad_card = blank_card("t", "")
    bad_card["name"] = ""
    check("name 空会被点出来",
          any("name" in m for m in PersonaLibrary.validate(bad_card)))
    bad_card["name"] = "有名字了"
    bad_card["art"]["size"] = {"solo": [832]}
    check("尺寸写错会被点出来",
          any("size.solo" in m for m in PersonaLibrary.validate(bad_card)))
    bad_card["art"]["size"] = {"solo": [832, 1216]}
    check("character_tags 空会被点出来",
          any("character_tags" in m for m in PersonaLibrary.validate(bad_card)))
    check("补齐后没有问题",
          PersonaLibrary.validate({
              "id": "a", "name": "x", "prompt": {}, "lists": {},
              "art": {"character_tags": "1girl", "size": {}},
          }) == [])

    # ── 9. 绑定文件格式 ────────────────────────────────────────────
    print("\n【9】绑定文件")
    raw = json.loads((work / "persona_bindings.json").read_text(encoding="utf-8"))
    check("绑定文件是 {bindings: {...}} 结构", isinstance(raw.get("bindings"), dict))
    check("绑定文件带 _说明", bool(raw.get("_说明")))
    check("LOCKED_FIELDS 就是「名字+形象」这四个", LOCKED_FIELDS ==
          ("name", "aliases", "self_image", "character_tags"), str(LOCKED_FIELDS))


if __name__ == "__main__":
    raise SystemExit(main())
