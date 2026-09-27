#!/usr/bin/env python3
"""版本兼容性守卫：防止「在新 Python / 新环境又爆一个错」这类问题复发。

这些都是用血换来的坑，必须机器守住，不能靠人记得：

  1. qqbot/requirements.txt 里依赖只能用 `>=` 下限，不能 `==` 钉死。
     `==` 钉死会让 pillow / websockets 这类带 C 扩展的包在新 Python（如 3.14）
     上往往没有对应预编译 wheel，pip 只能回退源码编译而失败
     —— 表现就是「python3.14 环境就用不了」。
  2. install.sh / install.ps1 的 Python 候选必须含 python3.14，
     否则系统只装了 3.14 且没建 python3 软链时，安装脚本直接「找不到 Python 3.11+」。
  3. CI 矩阵（test.yml / install-dryrun.yml）必须含 3.14，
     否则 3.14 从来没被真机装过、跑过，等于没支持。
  4. qzone-bridge/package.json 必须声明 engines.node 下限，
     给 Node 版本一个早期警告（而不是装到一半才炸）。

退出码非 0 = 有违规；preflight.sh 与 CI 会因此变红。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def check(name: str, ok: bool, fix: str) -> None:
    if ok:
        print(f"  ✔ {name}")
    else:
        print(f"  ✘ {name}")
        if fix:
            print(f"     修：{fix}")
        FAILS.append(name)


def main() -> int:
    # 1) requirements.txt 不能有 == 钉版
    req = ROOT / "qqbot" / "requirements.txt"
    bad_pins: list[str] = []
    if req.exists():
        for i, line in enumerate(req.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if re.match(r"^[A-Za-z0-9_.\-]+\s*==", s):
                bad_pins.append(f"{i}: {s}")
    check(
        "requirements.txt 只用 >= 下限（无 == 钉版）",
        not bad_pins,
        "把 == 改成 >=（保留原版本作最小下限）；要可复现就 pip freeze > requirements.lock.txt",
    )
    if bad_pins:
        for b in bad_pins:
            print(f"      违规行 {b}")

    # 2) install.sh 候选含 python3.14
    sh = ROOT / "install.sh"
    sh_ok = sh.exists() and "python3.14" in sh.read_text(encoding="utf-8")
    check("install.sh 候选含 python3.14", sh_ok,
          "在探测列表里加 python3.14（并保留 python3.11~3.20 兜底扫描）")

    # 3) install.ps1 候选含 python3.14
    ps1 = ROOT / "install.ps1"
    ps1_ok = ps1.exists() and "python3.14" in ps1.read_text(encoding="utf-8-sig")
    check("install.ps1 候选含 python3.14", ps1_ok,
          '在候选列表 @("python","py","python3.14") 里加上')

    # 4) CI 矩阵含 3.14
    for wf in ("test.yml", "install-dryrun.yml"):
        p = ROOT / ".github" / "workflows" / wf
        ok = p.exists() and '"3.14"' in p.read_text(encoding="utf-8")
        check(f"{wf} 的 Python 矩阵含 3.14", ok,
              f'给 {wf} 的 python-version 矩阵加 "3.14"')

    # 5) qzone-bridge engines.node
    pkg = ROOT / "qzone-bridge" / "package.json"
    engines_ok = False
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            engines_ok = bool(data.get("engines", {}).get("node", ""))
        except Exception:
            pass
    check("qzone-bridge/package.json 声明 engines.node 下限", engines_ok,
          '加 "engines": { "node": ">=18" }')

    print()
    if FAILS:
        print(f"兼容性守卫发现 {len(FAILS)} 处违规：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("兼容性守卫全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
