"""打一个「一键安装包」zip。

内容**只有安装件**，不含源码 —— zip 是引导器，脚本自己去 clone / 下载。
这样包小（几 KB），也不会跟仓库版本脱节。

用法：
    python tools/make_dist.py                 # 产物在 dist/许杏玖-安装包-<版本>.zip
    python tools/make_dist.py --version 1.0.0

装好后建议：
    1) 记下 zip 的 sha256，写进 Release Notes；
    2) 把 zip 挂到 GitHub Release 的 assets 上（别只放仓库里，Release 才好分发）。
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

# 只放这些（路径相对仓库根）
INCLUDE = [
    "install.bat",
    "install.ps1",
    "install.sh",
    "dist/安装说明.txt",
    "qqbot/tools/doctor.py",
]
# zip 内部的顶层目录名
PREFIX = "xuxingjiu-installer"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="1.0.0")
    args = ap.parse_args()

    missing = [p for p in INCLUDE if not (ROOT / p).exists()]
    if missing:
        print("缺文件，先补齐：" + "、".join(missing))
        return 1

    out = DIST / f"许杏玖-安装包-{args.version}.zip"
    DIST.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in INCLUDE:
            # doctor.py 在 zip 里放到根，方便安装脚本引用（脚本里也兼容两种位置）
            name = pathlib.Path(rel).name if rel.endswith("doctor.py") else pathlib.Path(rel).name
            z.write(ROOT / rel, f"{PREFIX}/{name}")
        # 附一份源码包地址的占位说明，避免用户拿到 zip 不知道去哪 clone
        z.writestr(f"{PREFIX}/SOURCE.txt",
                   "源码地址：请填入你的仓库 URL\n"
                   "（install.sh / install.ps1 的 REPO_URL 默认值就是它）\n")

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"已生成：{out}")
    print(f"大小：{out.stat().st_size / 1024:.1f} KB")
    print(f"sha256：{digest}")
    print("\n把这一行写进 Release Notes：")
    print(f"  sha256  许杏玖-安装包-{args.version}.zip = {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
