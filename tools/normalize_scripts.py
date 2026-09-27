"""把仓库里的脚本文件编码/换行符规范化到「跨平台不会翻车」的那一套。

为什么需要这个工具（都是实测踩过的坑）：

  1. `.bat` / `.cmd` **必须纯 ASCII**。
     cmd.exe 用控制台代码页（中文 Windows = 936/GBK）逐行解码批处理。
     UTF-8 中文字符是 3 字节，被 GBK 按 2 字节错配后，
     多出来的那半个字节会把**后面紧挨着的 ASCII 字符一起吞掉** ——
     `echo T2: 中文` 会变成 `'中文' 不是内部或外部命令`，
     `install.ps1` 会变成 `'nstall.ps1'`。
     实测：中文只要出现在 `chcp 65001` **之前**就一定炸；
     而且就算把 `chcp` 放到第 2 行，**被别的 .bat `call` 调用时照样炸**。
     所以唯一可靠的做法是「一个非 ASCII 字节都不要有」。

  2. `.ps1` 含中文时 **必须带 UTF-8 BOM**。
     Windows PowerShell 5.1 对没有 BOM 的脚本按系统 ANSI 代码页解码，
     中文全变乱码（不是显示问题，是解码错误）。
     PS 7 两种都认，所以加 BOM 对两边都安全。

  3. `.sh` **必须 LF + 无 BOM**。CRLF 会让 Linux 上的 shebang 失效
     （`bad interpreter: /bin/bash^M`），BOM 会让 `#!/bin/sh` 不再是第一行。

  4. `.py` / `.pyw` 必须是 UTF-8（有 BOM 也行，Python 3 认）。

用法：
    python tools/normalize_scripts.py            # 检查，只报告不改（默认）
    python tools/normalize_scripts.py --fix      # 顺手修好
    python tools/normalize_scripts.py --fix --quiet

退出码：0 = 全部合规（或已修好）；1 = 有不合规且没加 --fix。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

# 输出统一成 UTF-8。CI 的 Windows runner 是英文系统，stdout 走 cp1252，
# 下面那些中文（连 --quiet 也要打一行汇总）直接 print 会抛 UnicodeEncodeError ——
# 这是个守卫脚本，自己先崩掉比没有守卫还糟。三段式和 qqbot/bot.py 一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", "test_cache", "_backup", ".pytest_cache",
}

BAT_SUFFIXES = {".bat", ".cmd"}
PS_SUFFIXES = {".ps1", ".psm1"}
SH_SUFFIXES = {".sh", ".bash"}
PY_SUFFIXES = {".py", ".pyw"}

BOM = b"\xef\xbb\xbf"


def targets() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        if SKIP_DIRS & set(p.parts):
            continue
        if p.suffix.lower() in (BAT_SUFFIXES | PS_SUFFIXES | SH_SUFFIXES | PY_SUFFIXES):
            out.append(p)
    return out


def check(path: pathlib.Path) -> list[tuple[str, str]]:
    """返回 [(问题代码, 人话说明)]；空列表 = 合规。"""
    raw = path.read_bytes()
    suf = path.suffix.lower()
    problems: list[tuple[str, str]] = []

    if suf in BAT_SUFFIXES:
        bad = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
        if bad:
            # 给第一个越界字节定位到行号，方便直接跳过去改
            line = raw[: bad[0][0]].count(b"\n") + 1
            problems.append((
                "bat-cjk",
                f"出现 {len(bad)} 个非 ASCII 字节（第一个在第 {line} 行，"
                f"0x{bad[0][1]:02x}）。.bat 必须是纯 ASCII —— "
                f"中文请放进配套的 .ps1，或改成英文",
            ))
        # 注意用 b"\r\n" 而不是 rb"\r\n" —— 后者是字面反斜杠加 n，永远判不出 CRLF
        if b"\r\n" not in raw and b"\n" in raw:
            problems.append(("bat-lf", "换行是 LF；批处理建议 CRLF（.gitattributes 已声明）"))

    elif suf in PS_SUFFIXES:
        has_bom = raw.startswith(BOM)
        try:
            raw.decode("ascii")
            has_cjk = False
        except UnicodeDecodeError:
            has_cjk = True
        if has_cjk and not has_bom:
            problems.append((
                "ps1-nobom",
                "含中文却没有 UTF-8 BOM —— Windows PowerShell 5.1 会按 GBK "
                "解码，中文全乱。请另存为 'UTF-8 with BOM'",
            ))
        if has_bom:
            try:
                raw[len(BOM):].decode("utf-8")
            except UnicodeDecodeError as exc:
                problems.append(("ps1-badutf8", f"BOM 之后不是合法 UTF-8：{exc}"))

    elif suf in SH_SUFFIXES:
        if raw.startswith(BOM):
            problems.append((
                "sh-bom", "有 BOM —— Linux 上 `#!/bin/sh` 不再是第一行，"
                          "会报 'No such file or directory'",
            ))
        crlf = raw.count(b"\r\n")
        if crlf:
            problems.append((
                "sh-crlf",
                f"有 {crlf} 处 CRLF —— Linux 上会变成 "
                f"'bad interpreter: /bin/bash^M'",
            ))

    elif suf in PY_SUFFIXES:
        body = raw[len(BOM):] if raw.startswith(BOM) else raw
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append(("py-notutf8", f"不是合法 UTF-8：{exc}"))

    return problems


def fix(path: pathlib.Path, problems: list[tuple[str, str]]) -> bool:
    """尝试自动修。返回是否真的改了文件。"""
    codes = {c for c, _ in problems}
    raw = path.read_bytes()
    suf = path.suffix.lower()

    # .bat 里的中文没法"自动翻译"，只能报错让人处理
    if "bat-cjk" in codes:
        return False

    if "ps1-nobom" in codes:
        path.write_bytes(BOM + raw)
        return True

    if "sh-bom" in codes:
        path.write_bytes(raw[len(BOM):].replace(b"\r\n", b"\n"))
        return True

    if "sh-crlf" in codes:
        path.write_bytes(raw.replace(b"\r\n", b"\n"))
        return True

    if "bat-lf" in codes and suf in BAT_SUFFIXES:
        path.write_bytes(raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        return True

    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", action="store_true", help="能自动修的顺手修掉")
    ap.add_argument("--quiet", action="store_true", help="只打印有问题的")
    args = ap.parse_args()

    files = targets()
    bad_total = 0
    fixed_total = 0
    unautofixable: list[tuple[pathlib.Path, str]] = []

    for p in files:
        problems = check(p)
        if not problems:
            if not args.quiet:
                print(f"  ok   {p.relative_to(ROOT)}")
            continue

        bad_total += len(problems)
        rel = p.relative_to(ROOT)
        print(f"  !!   {rel}")
        for code, msg in problems:
            print(f"         [{code}] {msg}")

        if args.fix:
            if fix(p, problems):
                fixed_total += len(problems)
                print("         -> 已自动修好")
                # 修完复查，避免"假装修好了"
                left = check(p)
                if left:
                    unautofixable.extend((rel, m) for _, m in left)
            else:
                unautofixable.extend((rel, m) for _, m in problems)

    print()
    print(f"扫描 {len(files)} 个脚本文件，{bad_total} 处不合规"
          + (f"，已自动修 {fixed_total} 处" if args.fix else ""))
    if unautofixable:
        print("需要人工处理的：")
        for rel, msg in unautofixable:
            print(f"  - {rel}: {msg}")
        return 1
    if bad_total and not args.fix:
        print("提示：加 --fix 可自动修掉能修的。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
