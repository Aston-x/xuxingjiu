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
  5. 安装脚本的「环境探测」入口（install.sh --detect / install.ps1 -DetectOnly）
     必须两份都在：探测是唯一能在三种系统上真跑的安装环节，而两份实现最容易
     悄悄长歪 —— 只改了 .sh，Windows 用户拿到的报告就少东西。
  6. 两个脚本都必须保留「绝不碰系统」的逃生开关（--no-system / -NoSystem）。
     缺了它，脚本就只剩「要么全自动、要么别跑」，没有管理员权限的机器上直接卡死。
  7. install.bat 必须把参数原样转给 install.ps1（%*），否则双击场景下所有新开关失效。

退出码非 0 = 有违规；preflight.sh 与 CI 会因此变红。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

# 输出统一成 UTF-8。中文 Windows 上 stdout 是 GBK，下面的 ✔ / ✘ 直接 print 会抛
# UnicodeEncodeError —— 守卫自己崩掉，比没有守卫还糟（preflight 会把它当成"未通过"）。
# 这个三段式和 qqbot/bot.py 保持一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
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

    # 6) 安装脚本的「环境探测」入口必须两份都在。
    #    探测是唯一一个能在三种系统上真跑的安装环节（装依赖那半段没法在 CI 里真跑），
    #    而两份实现最容易悄悄长歪：只改了 .sh，Windows 用户拿到的报告就少东西。
    #    install-dryrun.yml 会真的跑这两条命令并比对结果，这里只守「有没有」。
    sh_detect = sh.exists() and "--detect" in sh.read_text(encoding="utf-8")
    check("install.sh 有环境探测入口（--detect）", sh_detect,
          "加 --detect / --json，并让它一个文件都不动")
    ps_detect = ps1.exists() and "-DetectOnly" in ps1.read_text(encoding="utf-8-sig")
    check("install.ps1 有环境探测入口（-DetectOnly）", ps_detect,
          "加 -DetectOnly / -Json，并让它一个文件都不动")

    # 7) 「绝不碰系统」的逃生开关。缺了它，脚本就只剩「要么全自动、要么别跑」，
    #    在没有管理员权限的机器上会直接卡死。
    sh_nosys = sh.exists() and "--no-system" in sh.read_text(encoding="utf-8")
    ps_nosys = ps1.exists() and "-NoSystem" in ps1.read_text(encoding="utf-8-sig")
    check("install.sh 有 --no-system（绝不碰系统包管理器）", sh_nosys, "加 --no-system")
    check("install.ps1 有 -NoSystem（绝不碰系统包管理器）", ps_nosys, "加 -NoSystem")

    # 8) 两份探测都要能给机器读的报告 —— CI 靠它比对两份实现是否一致。
    ps_json = ps1.exists() and "-Json" in ps1.read_text(encoding="utf-8-sig")
    check("install.ps1 能输出 JSON 报告（-Json）", ps_json, "加 -Json")

    # 9) 双击入口必须把参数原样转给 install.ps1，否则新加的开关在双击场景下全失效。
    bat = ROOT / "install.bat"
    bat_fwd = bat.exists() and "%*" in bat.read_text(encoding="ascii", errors="replace")
    check("install.bat 把参数透传给 install.ps1（%*）", bat_fwd,
          "在调用 install.ps1 的那行末尾加上 %*")

    # 10) 默认仓库地址：三处都要写死，而且三处要一样，不能是占位符。
    #     这条真出过事 —— 包发出去之后用户双击安装，脚本跑去 clone
    #     github.com/your-name/xuxingjiu，甩一句「Repository not found」。
    #     install.sh / install.ps1 / make_dist.py 的 SOURCE.txt 各有一份默认值，
    #     只改其中一份就会指错地方，所以让机器盯着三份。
    sh_url = re.search(r'REPO_URL="\$\{REPO_URL:-([^}"]+)\}"', sh.read_text(encoding="utf-8"))
    ps_url = re.search(r'\$RepoUrl\s*=\s*"([^"]+)"', ps1.read_text(encoding="utf-8-sig"))
    dist_py = ROOT / "tools" / "make_dist.py"
    md_url = re.search(r'repo_url\s*=\s*"([^"]+)"',
                       dist_py.read_text(encoding="utf-8")) if dist_py.exists() else None
    urls = {
        "install.sh": sh_url.group(1) if sh_url else "",
        "install.ps1": ps_url.group(1) if ps_url else "",
        "make_dist.py": md_url.group(1) if md_url else "",
    }
    missing_url = [k for k, v in urls.items() if not v]
    check("默认仓库地址三处都写死了", not missing_url, f"没解析到：{missing_url}")
    if not missing_url:
        check("三处默认仓库地址一致", len(set(urls.values())) == 1,
              "不一致：" + " / ".join(f"{k}={v}" for k, v in urls.items()))
        placeholder = sorted({v for v in urls.values()
                              if re.search(r"your-name|your_name|example\.com|<|TODO", v)})
        check("默认仓库地址不是占位符", not placeholder, f"还是占位符：{placeholder}")

    # 11) clone 失败必须当场停。原生命令失败在 PowerShell 里不抛异常，
    #     不看 $LASTEXITCODE 的话，clone 挂了照样报 [ok]，
    #     然后一路走到「缺 requirements.txt」才露馅 —— 报错点离原因隔了七步。
    ps_text = ps1.read_text(encoding="utf-8-sig")
    check("install.ps1 在 clone 后检查了 $LASTEXITCODE", "$LASTEXITCODE -ne 0" in ps_text,
          "clone 之后加：if (-not $ReadOnlyMode -and $LASTEXITCODE -ne 0) { Die ... }")

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
