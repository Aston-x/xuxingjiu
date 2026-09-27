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


def _text_with_bom(data: bytes) -> bytes:
    """给 zip 里的 .txt 补 UTF-8 BOM。

    用户拿到包，第一眼打开的就是「安装说明.txt」。Windows 记事本虽然新版
    能猜 UTF-8，但老版本、以及不少压缩软件的内置预览，都会把无 BOM 的
    UTF-8 按 GBK 显示 —— 整篇中文乱码，用户连第一步都读不懂。
    加 BOM 是这类"给人看的纯文本"最省事的保险。
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data
    return b"\xef\xbb\xbf" + data


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
            # 统一摊平到 zip 根，别多套一层目录，用户解压完一眼能看全
            name = pathlib.Path(rel).name
            data = (ROOT / rel).read_bytes()
            if pathlib.Path(rel).suffix.lower() == ".txt":
                data = _text_with_bom(data)
            z.writestr(f"{PREFIX}/{name}", data)
        # 附一份源码包地址的占位说明，避免用户拿到 zip 不知道去哪 clone
        src_txt = ("源码地址：请填入你的仓库 URL\n"
                   "（install.sh / install.ps1 的 REPO_URL 默认值就是它）\n")
        z.writestr(f"{PREFIX}/SOURCE.txt",
                   _text_with_bom(src_txt.encode("utf-8")))

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"已生成：{out}")
    print(f"大小：{out.stat().st_size / 1024:.1f} KB")
    print(f"sha256：{digest}")
    print("\n把这一行写进 Release Notes：")
    print(f"  sha256  许杏玖-安装包-{args.version}.zip = {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
