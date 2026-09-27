#!/usr/bin/env python3
"""校验 install.sh / install.ps1 的 `--detect --json` 环境报告。

为什么需要它：

  1. 环境探测是这一版安装脚本的**新入口**，也是唯一一个「CI 能在三种系统上真跑」
     的部分（装依赖那半段没法在 CI 里真跑）。
  2. 探测有**两份实现**（bsh 一份、PowerShell 一份），不比对就会悄悄长歪 ——
     某天只改了 .sh，Windows 用户拿到的报告就少一个键，谁也不会发现。
  3. 探测代码很容易「静默失败」：把某个字段拼成空串、把数字写成字符串、
     把不可达写成 0，报告照样是一坨合法 JSON，看着一切正常。

用法：
    python tools/check_env_report.py report.json          # 校验一份
    python tools/check_env_report.py --compare a.json b.json   # 两份实现是否互相认同
    cat report.json | python tools/check_env_report.py -

退出码：0 通过 / 1 不通过。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

# 输出统一成 UTF-8。CI 的 Windows runner 是英文系统，stdout 走 cp1252，
# 这个脚本的输出全是中文，不重设就直接 UnicodeEncodeError —— 校验环境报告的工具
# 自己先崩，比报告有问题更难查。三段式和 qqbot/bot.py 一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

SCHEMA = 1
OS_FAMILIES = {"windows", "linux", "macos", "unix", "unknown"}

# 必填的键路径 → 允许的类型。故意只查「两个实现都必须给」的项，
# 平台特有的（例如 msys / wsl）不强求另一方也有。
REQUIRED: dict[str, tuple[type, ...]] = {
    "schema": (int,),
    "os.family": (str,),
    "os.arch": (str,),
    "python.found": (bool,),
    "python.ok": (bool,),
    "python.cmd": (str,),
    "python.version": (str,),
    "node.found": (bool,),
    "git.found": (bool,),
    "dir.writable": (bool,),
    "pkg.name": (str,),
}

# --compare 只比「事实」，不比「选中的那个解释器」——
# 两份实现按各自的优先级挑 Python，挑到 3.12 还是 3.11 都对，
# 但「有没有」和「操作系统是哪个」必须一致。网络耗时同理，不比。
COMPARE_KEYS = [
    "os.family",
    "python.found",
    "node.found",
    "git.found",
    "dir.writable",
]


def get(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def load(path: str) -> dict:
    if path == "-":
        text = sys.stdin.read()
    else:
        raw = pathlib.Path(path).read_bytes()
        text = _decode(raw)
    return json.loads(text)


def _decode(raw: bytes) -> str:
    """把报告文件解成字符串。

    要容忍三种编码，因为它们都真实出现过：
      · UTF-8（install.sh 用 curl/printf 直接写出来的）；
      · UTF-8 with BOM（含中文的 .ps1 必须带 BOM，顺手写出来的 JSON 也可能带）；
      · UTF-16 LE（Windows PowerShell 5.1 的 `>` 重定向默认就是 UTF-16LE ——
        CI 里跑的是 pwsh 7（UTF-8），但用户在 5.1 里手工跑一遍再把文件交给工具，
        是很正常的事，不该因此报"不是合法 JSON"）。
    """
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def check_one(path: str) -> list[str]:
    fails: list[str] = []
    try:
        d = load(path)
    except Exception as exc:  # noqa: BLE001
        return [f"{path}: 读不了 / 不是合法 JSON：{exc}"]
    if not isinstance(d, dict):
        return [f"{path}: 顶层不是对象"]

    for key, types in REQUIRED.items():
        val, present = get(d, key)
        if not present:
            fails.append(f"{path}: 缺键 {key}")
            continue
        if isinstance(val, bool) and bool not in types and int in types:
            # bool 是 int 的子类，别让 True 冒充数字
            fails.append(f"{path}: {key} 是布尔，不该是数字：{val!r}")
            continue
        if not isinstance(val, types):
            fails.append(f"{path}: {key} 类型不对，要 {types}，实际 {type(val).__name__}")
            continue
        if isinstance(val, str) and val == "" and key not in ("python.cmd", "python.version"):
            fails.append(f"{path}: {key} 是空串")

    # 取值合理性 —— 这几条都是只有真跑起来才可能满足的
    schema, _ = get(d, "schema")
    if schema != SCHEMA:
        fails.append(f"{path}: schema 应为 {SCHEMA}，实际 {schema!r}")
    fam, _ = get(d, "os.family")
    if fam not in OS_FAMILIES:
        fails.append(f"{path}: os.family 不认识：{fam!r}（允许 {sorted(OS_FAMILIES)}）")
    pkg, _ = get(d, "pkg.name")
    if not isinstance(pkg, str) or not pkg:
        fails.append(f"{path}: pkg.name 空了（没有包管理器也要写 'none'）")

    py_found, _ = get(d, "python.found")
    py_ver, _ = get(d, "python.version")
    if py_found:
        if not re.match(r"^\d+\.\d+\.\d+$", str(py_ver)):
            fails.append(f"{path}: python.found=true 但 version 不是 x.y.z：{py_ver!r}")
        else:
            maj, minor = (int(x) for x in str(py_ver).split(".")[:2])
            if maj != 3 or minor < 11:
                fails.append(f"{path}: 报告里的 Python {py_ver} 不满足 >=3.11 —— 探测没筛干净")
    elif py_ver:
        fails.append(f"{path}: python.found=false 却给了 version：{py_ver!r}")

    node_found, _ = get(d, "node.found")
    node_ver, _ = get(d, "node.version")
    if node_found and not str(node_ver).startswith("v"):
        fails.append(f"{path}: node.version 一般是 vX.Y.Z 形式，实际 {node_ver!r}")

    # 网络探测：要么是数字（秒），要么是 null（不可达）。写 0 或 -1 是错的
    for key in ("net.pypi", "net.tuna", "net.npm", "net.npmmirror"):
        val, present = get(d, key)
        if not present:
            continue
        if val is None:
            continue
        if not isinstance(val, (int, float)) or isinstance(val, bool) or val <= 0:
            fails.append(f"{path}: {key} 应为正数秒数或 null，实际 {val!r}")

    # 镜像决策不能自相矛盾：选了镜像就必须同时给出地址
    pip, _ = get(d, "net.mirror_pip")
    if pip is not None and not str(pip).startswith("http"):
        fails.append(f"{path}: net.mirror_pip 不是 URL：{pip!r}")
    return fails


def compare(a_path: str, b_path: str) -> list[str]:
    fails: list[str] = []
    try:
        a, b = load(a_path), load(b_path)
    except Exception as exc:  # noqa: BLE001
        return [f"compare: 读不了其中一个：{exc}"]
    for key in COMPARE_KEYS:
        va, oka = get(a, key)
        vb, okb = get(b, key)
        if not oka or not okb:
            fails.append(f"compare: {key} 有一边没给（{(a_path, oka)} / {(b_path, okb)}）")
            continue
        if va != vb:
            fails.append(f"compare: {key} 两份实现不一致：{a_path}={va!r}  {b_path}={vb!r}")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reports", nargs="*", help="JSON 文件；'-' 表示从 stdin 读")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"),
                    help="比对两份实现（install.sh 与 install.ps1）的事实是否一致")
    args = ap.parse_args()

    fails: list[str] = []
    if args.compare:
        a, b = args.compare
        fails += check_one(a)
        fails += check_one(b)
        fails += compare(a, b)
    elif args.reports:
        for r in args.reports:
            fails += check_one(r)
    else:
        fails += check_one("-")

    if fails:
        print("环境报告校验未通过：")
        for f in fails:
            print(f"  ✘ {f}")
        return 1
    n = len(args.compare) if args.compare else len(args.reports) or 1
    print(f"环境报告校验通过（{n} 份，schema={SCHEMA}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
