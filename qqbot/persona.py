"""人设层：角色卡、多套人设、按会话/按群绑定。

为什么要单独一层：人设字段以前散在 config.json 顶层（persona / world / self_image …）
和 sd 段（character_tags …），只能有一套，改一次全局生效、重启才换得掉形象。
现在收口成「一张角色卡 = 一个角色的全部设定」，这样可以：

  · 写多张卡，随时切（`personas/<id>.json`）
  · 按会话或按群绑不同的人设（`persona_bindings.json`，键就是 Chat 的 `g<群号>` / `p<QQ号>`）
  · 开源版里角色卡的**名字和形象也能改**（换个人就是换个角色）
  · 本地版把人设钉死在某一张卡上时，可以用 lock 挡住名字和形象的改动

三条设计约束，改动时别破坏：

1. **零回归**。config.json 里那些老键只要还在，就**优先于**卡里的同名字段
   （见 `_legacy_overrides`）。老安装升级上来，行为一个字都不变；把老键删掉，
   就完全以卡为准。这条是迁移期用的桥，等大家都不用老键了可以撤掉。
2. **形象只有一处定义**。卡里的 `art.character_tags` 是「她长什么样」的唯一出处，
   生图时由 SD.assemble() 统一拼——这条是每张图里必须是同一个人的保证，别绕过。
3. **lock 只挡名字和形象**。`LOCKED_FIELDS` 之外的东西（语气、世界观、话术池…）
   锁着的时候也能改：锁的是「她是谁」，不是「她怎么说话」。

本模块不 import bot，bot 反过来 import 它，这样它可以单独测（见 test_persona.py）。
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

# ── 字段清单 ──────────────────────────────────────────────────────────
# 角色卡里跟提示词有关的字段（build_system 前 6 个片段 + 三组话术池）
PROMPT_FIELDS = (
    "persona",
    "world",
    "self_image",
    "style_boost",
    "style_format",
    "identity_guard",
)
PROMPT_LIST_FIELDS = (
    "identity_lines",
    "reply_fallback_lines",
    "wake_prefix",
)
# 跟「她长什么样」有关的字段。character_tags 是唯一出处，其余是数量词与禁用词。
ART_FIELDS = (
    "character_tags",
    "solo_prefix",
    "duo_prefix",
    "other_person",
    "ban_solo",
    "ban_duo",
    "ban_scenery",
    "size",
)
# 这些字段写在卡顶层，不在 prompt / art 里
META_FIELDS = ("id", "name", "aliases")

# ── 硬约束 ────────────────────────────────────────────────────────────
# 锁开着的卡：名字、别名、长相描述、生图形象串都不许改。
# 别的字段（语气、世界观、话术池、数量词…）照常能改 —— 锁的是"她是谁"。
LOCKED_FIELDS = ("name", "aliases", "self_image", "character_tags")

# 卡 id 的合法写法：给文件名用，别放路径分隔符和中文标点
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

_MISSING = object()


def _field_kind(field: str) -> str | None:
    """字段属于哪一块：meta / prompt / list / art。认不出来返回 None。"""
    if field in META_FIELDS:
        return "meta"
    if field in PROMPT_FIELDS:
        return "prompt"
    if field in PROMPT_LIST_FIELDS:
        return "list"
    if field in ART_FIELDS:
        return "art"
    return None


def blank_card(card_id: str, name: str = "") -> dict:
    """一张空卡的骨架。新卡从这里长出来，字段齐全但不写死任何角色。"""
    card: dict[str, Any] = {
        "id": card_id,
        "name": name or card_id,
        "aliases": [],
        "prompt": {k: "" for k in PROMPT_FIELDS},
        "lists": {k: [] for k in PROMPT_LIST_FIELDS},
        "art": {k: ("" if k != "size" else {}) for k in ART_FIELDS},
        "_说明": (
            "一张角色卡 = 一个角色的全部设定。prompt 进系统提示词，art 进生图提示词。"
            "名字和形象（name / aliases / self_image / character_tags）在锁定模式下不可改；"
            "要换人就新建一张卡，别改别人那张。"
        ),
    }
    return card


def legacy_overrides(cfg: dict) -> dict:
    """从老的扁平配置里把还在的字段捞出来。

    ★ 只要老键在 config.json 里存在，它就压过卡里的同名字段 —— 这是为了让老安装
    升级上来行为完全不变。判断"在不在"用 sentinel，不能用 `cfg.get(k) or default`：
    空字符串也是用户明确写过的值（比如故意把 world 清空）。
    """
    out: dict[str, Any] = {"prompt": {}, "lists": {}, "art": {}}
    for k in PROMPT_FIELDS:
        v = cfg.get(k, _MISSING)
        if v is not _MISSING:
            out["prompt"][k] = v
    for k in PROMPT_LIST_FIELDS:
        v = cfg.get(k, _MISSING)
        if v is not _MISSING:
            out["lists"][k] = v
    sd = cfg.get("sd") or {}
    # 生图字段的老家在 sd 段；other_person 在老配置里叫 other_person，卡里叫 other_person，
    # 保持一致，省得两边对不上。
    for k in ART_FIELDS:
        v = sd.get(k, _MISSING)
        if v is not _MISSING:
            out["art"][k] = v
    return out


def legacy_conflicts(cfg: dict, card: dict) -> list[tuple[str, Any, Any]]:
    """哪些老键正在盖住卡，而且值和卡**不一样**。

    只报"不一样"的：全新安装的 config.json 是从 config.example.json 抄的，和出厂卡
    逐字相同 —— 那种情况不该天天报警告。返回值 [(字段名, config 里的值, 卡里的值)]。
    """
    out: list[tuple[str, Any, Any]] = []
    p = card.get("prompt") or {}
    ls = card.get("lists") or {}
    a = card.get("art") or {}
    sd = cfg.get("sd") or {}
    for k in PROMPT_FIELDS:
        if k in cfg and cfg[k] != p.get(k):
            out.append((k, cfg[k], p.get(k)))
    for k in PROMPT_LIST_FIELDS:
        if k in cfg and cfg[k] != ls.get(k):
            out.append((k, cfg[k], ls.get(k)))
    for k in ART_FIELDS:
        if k in sd and sd[k] != a.get(k):
            out.append((f"sd.{k}", sd[k], a.get(k)))
    return out


def release_legacy(cfg: dict) -> list[str]:
    """把所有老键从 config 字典里删掉（**不落盘**，落盘交给调用方）。

    删干净之后，那些字段就完全以角色卡为准 —— 这是"换张卡就是换个人"成立的前提。
    返回删掉了哪些键，方便日志里说清楚动了什么。
    """
    gone: list[str] = []
    for k in PROMPT_FIELDS + PROMPT_LIST_FIELDS:
        if k in cfg:
            cfg.pop(k, None)
            gone.append(k)
    sd = cfg.get("sd")
    if isinstance(sd, dict):
        for k in ART_FIELDS:
            if k in sd:
                sd.pop(k, None)
                gone.append(f"sd.{k}")
    return gone


class PersonaLibrary:
    """卡库：加载、解析、绑定、编辑。不碰网络也不碰 bot 的状态。

    `root` 是 qqbot/（卡与绑定文件都放它下面）；`cfg` 是已加载的 config 字典，
    只用来做老键迁移，不做别的。
    """

    def __init__(self, root: pathlib.Path, cfg: dict | None = None):
        self.root = pathlib.Path(root)
        self.cfg = cfg or {}
        self.dir = self.root / "personas"
        self.bindings_path = self.root / "persona_bindings.json"
        self.cards: dict[str, dict] = {}
        self.bindings: dict[str, str] = {}
        self.load()

    # ── 加载 ────────────────────────────────────────────────────────
    def load(self) -> None:
        self.cards = {}
        if self.dir.is_dir():
            for p in sorted(self.dir.glob("*.json")):
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue  # 坏卡跳过，不能让一张写坏的卡把整个机器人带崩
                cid = str(raw.get("id") or p.stem)
                if not _ID_RE.match(cid):
                    continue
                raw["id"] = cid
                self.cards[cid] = self._fill(raw)
        self.bindings = {}
        if self.bindings_path.exists():
            try:
                d = json.loads(self.bindings_path.read_text(encoding="utf-8"))
                self.bindings = {str(k): str(v) for k, v in (d.get("bindings") or {}).items()}
            except Exception:
                self.bindings = {}

    @staticmethod
    def _fill(raw: dict) -> dict:
        """补齐缺的块，免得下游到处 .get 判空。

        顶层除 prompt / lists / art 之外的键**原样留着**：`lock`（这张卡要不要跟着
        全局开关一起锁名字和形象）和 `default`（是不是默认卡）都住在那儿，
        _fill 把它们吃掉的话，卡里写了也等于没写。
        """
        card = blank_card(str(raw.get("id") or ""), str(raw.get("name") or ""))
        for k, v in raw.items():
            if k not in ("prompt", "lists", "art"):
                card[k] = v
        for block, fields in (("prompt", PROMPT_FIELDS), ("lists", PROMPT_LIST_FIELDS),
                              ("art", ART_FIELDS)):
            src = raw.get(block) or {}
            for k in fields:
                if k in src:
                    card[block][k] = src[k]
        return card

    # ── 查询 ────────────────────────────────────────────────────────
    def default_id(self) -> str:
        """默认卡：配置指定 > 卡自己声明 default > 唯一的卡 > 按 id 排序第一张。

        ★ 兜底**不能**用「排序第一张」当主要依据：多写一张卡就可能把默认人设悄悄
        换掉（lengdan 排在 xuxingjiu 前面就是个例子）。所以默认卡的归属要显式：
        要么 config 里写 `persona_card`，要么在卡里写 `"default": true`。
        """
        want = str(self.cfg.get("persona_card") or "").strip()
        if want and want in self.cards:
            return want
        flagged = sorted(k for k, v in self.cards.items() if v.get("default"))
        if flagged:
            return flagged[0]
        if len(self.cards) == 1:
            return next(iter(self.cards))
        if self.cards:
            return sorted(self.cards)[0]
        return ""

    def set_default(self, card_id: str) -> tuple[bool, str]:
        """改默认卡。归属写在卡自己身上（"default": true），不藏在别的文件里。"""
        if card_id not in self.cards:
            return False, f"没有这张卡：{card_id}"
        if self.cards[card_id].get("default") and \
                sum(1 for v in self.cards.values() if v.get("default")) == 1:
            return True, f"{card_id} 本来就是默认卡"
        for cid in list(self.cards):
            self.cards[cid]["default"] = (cid == card_id)
            self.save_card(cid)
        return True, f"默认人设已切换为：{card_id}"

    def conflicts(self, card_id: str = "") -> list[tuple[str, Any, Any]]:
        """config.json 里正在盖住这张卡、而且和卡里**不一样**的老键。

        控制台切卡时用它提示"这几项还压着卡"，启动时用它打警告。
        全新安装（config 从 config.example.json 抄的那份和出厂卡逐字相同）返回空。
        """
        cid = card_id or self.default_id()
        return legacy_conflicts(self.cfg, self.cards.get(cid) or {})

    def release_legacy(self) -> list[str]:
        """把 config 那份里的老键全删掉（不落盘，调用方负责写回）。

        删完这些字段就以角色卡为准了 —— "换张卡就是换个人"成立的前提。
        """
        return release_legacy(self.cfg)

    def resolve(self, session_key: str = "") -> str:
        """这个会话用哪张卡：精确绑定的 > 默认卡 > 空。"""
        if session_key and self.bindings.get(session_key) in self.cards:
            return self.bindings[session_key]
        return self.default_id()

    def binding_of(self, session_key: str) -> str:
        """这个会话被显式绑到哪张卡（没绑返回空串）。"""
        return self.bindings.get(session_key, "")

    def card(self, card_id: str) -> dict | None:
        return self.cards.get(card_id)

    def effective(self, session_key: str = "") -> dict:
        """生效的那张卡：卡本身 + 老配置键的覆盖。下游要字段就找它。"""
        cid = self.resolve(session_key)
        base = self.cards.get(cid) or blank_card("")
        card = {
            "id": cid,
            "name": base.get("name", ""),
            "aliases": list(base.get("aliases") or []),
            "prompt": dict(base.get("prompt") or {}),
            "lists": {k: list(v or []) for k, v in (base.get("lists") or {}).items()},
            "art": {k: (dict(v) if isinstance(v, dict) else v)
                    for k, v in (base.get("art") or {}).items()},
        }
        ov = legacy_overrides(self.cfg)
        card["prompt"].update(ov["prompt"])
        card["lists"].update(ov["lists"])
        card["art"].update(ov["art"])
        return card

    def prompt_fields(self, session_key: str = "") -> dict:
        """给 build_system 用：6 个字符串 + 3 组话术池 + 名字。"""
        c = self.effective(session_key)
        out = dict(c["prompt"])
        out.update(c["lists"])
        out["name"] = c.get("name", "")
        out["aliases"] = list(c.get("aliases") or [])
        return out

    def art_fields(self, session_key: str = "") -> dict:
        """给生图用：形象串 + 数量词 + 禁用词 + 尺寸。"""
        return dict(self.effective(session_key)["art"])

    def names(self, session_key: str = "") -> tuple[str, ...]:
        """这个会话那张卡的 name + aliases，用来判断「有没有在喊她」。

        按会话取：多套人设下每张卡的名字不一样，"她认不认自己被喊"必须跟着会话走。
        """
        c = self.effective(session_key)
        out = [str(c.get("name") or "").strip()]
        out += [str(a).strip() for a in (c.get("aliases") or [])]
        return tuple(n for n in out if n)

    # ── 绑定（按会话 / 按群）────────────────────────────────────────
    def bind(self, session_key: str, card_id: str) -> tuple[bool, str]:
        if card_id not in self.cards:
            return False, f"没有这张卡：{card_id}"
        if not session_key:
            return False, "会话键不能为空"
        self.bindings[str(session_key)] = card_id
        self._save_bindings()
        return True, f"{session_key} → {card_id}"

    def unbind(self, session_key: str) -> tuple[bool, str]:
        if str(session_key) in self.bindings:
            del self.bindings[str(session_key)]
            self._save_bindings()
            return True, f"已解除绑定：{session_key}"
        return False, f"本来就没绑：{session_key}"

    def _save_bindings(self) -> None:
        self.bindings_path.write_text(
            json.dumps({
                "bindings": self.bindings,
                "_说明": ("按会话指定人设：键就是会话键 g<群号> / p<QQ号>（见 bot.py 里算 key 的那行），"
                          "值是 personas/ 下的卡 id。没绑的走默认卡。"),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── 编辑（受 lock 约束）─────────────────────────────────────────
    def _lock_enabled(self) -> bool:
        """名字和形象要不要锁死。

        这是「本地版 / 开源版」之间唯一的人设差异：本地版在 config.json 里把
        `persona_lock` 设成 true（她是谁不能动，改的只能是她怎么说话）；开源版留
        false（换个人就是换个角色，名字和设定图都能改）。
        """
        return bool(self.cfg.get("persona_lock", False))

    def locked(self, card_id: str, field: str) -> bool:
        """这张卡的这个字段是不是被锁住。"""
        if field not in LOCKED_FIELDS or not self._lock_enabled():
            return False
        c = self.cards.get(card_id) or {}
        return c.get("lock") is not False  # 卡也可以显式声明自己不锁

    def set_field(self, card_id: str, field: str, value: Any) -> tuple[bool, str]:
        """改一个字段。被锁的字段会被挡下来，并说清为什么。"""
        c = self.cards.get(card_id)
        if not c:
            return False, f"没有这张卡：{card_id}"
        kind = _field_kind(field)
        if kind is None:
            return False, f"不认识的字段：{field}"
        if self.locked(card_id, field):
            return False, f"「{field}」是硬约束，不能改（这张卡锁着名字和形象）"
        if kind == "meta":
            c[field] = value
        elif kind == "list":
            c["lists"][field] = value
        elif kind == "prompt":
            c["prompt"][field] = value
        else:
            c["art"][field] = value
        self.cards[card_id] = c
        self.save_card(card_id)
        return True, f"{card_id}.{field} 已更新"

    def new_card(self, card_id: str, name: str = "") -> tuple[bool, str]:
        if not _ID_RE.match(card_id or ""):
            return False, "卡 id 只能用字母数字、下划线、短横线（1~32 位），且以字母数字开头"
        if card_id in self.cards:
            return False, f"已经有一张叫 {card_id} 的卡了"
        self.cards[card_id] = blank_card(card_id, name)
        self.save_card(card_id)
        return True, f"已新建角色卡：{card_id}"

    def delete_card(self, card_id: str) -> tuple[bool, str]:
        if card_id not in self.cards:
            return False, f"没有这张卡：{card_id}"
        if len(self.cards) <= 1:
            return False, "这是最后一张卡，删了就没有人设了"
        self.cards.pop(card_id)
        p = self.dir / f"{card_id}.json"
        if p.exists():
            p.unlink()
        # 绑到它身上的会话回到默认卡
        for k in [k for k, v in self.bindings.items() if v == card_id]:
            del self.bindings[k]
        self._save_bindings()
        return True, f"已删除角色卡：{card_id}"

    def save_card(self, card_id: str) -> None:
        card = self.cards.get(card_id)
        if not card:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{card_id}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 校验 ────────────────────────────────────────────────────────
    @staticmethod
    def validate(card: dict) -> list[str]:
        """卡片结构自检。返回问题清单，空列表 = 没问题。"""
        bad: list[str] = []
        cid = str(card.get("id") or "")
        if not _ID_RE.match(cid):
            bad.append(f"id 不合法：{cid!r}")
        if not str(card.get("name") or "").strip():
            bad.append("name 不能为空（她得有个名字）")
        for block in ("prompt", "lists", "art"):
            if not isinstance(card.get(block), dict):
                bad.append(f"{block} 必须是对象")
        art = card.get("art") or {}
        if not str(art.get("character_tags") or "").strip():
            # 形象串空着不是错，但一定出不好图 —— 值得提一句
            bad.append("art.character_tags 是空的：生图时她就没有固定长相了")
        size = art.get("size") or {}
        for mode in ("solo", "duo", "scenery"):
            v = size.get(mode)
            if v is None:
                continue
            if (not isinstance(v, list)) or len(v) != 2 or not all(isinstance(x, int) for x in v):
                bad.append(f"art.size.{mode} 要写成 [宽, 高] 两个整数")
        return bad
