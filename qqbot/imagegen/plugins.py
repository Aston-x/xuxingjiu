"""生图插件发现与加载。

策略与 `providers/plugins.py` 完全一致（那是本体，这里只是换了张钩子表）：

  · 默认**不扫目录**（`allow_dir_scan=False`），只加载 `enabled` 白名单里点名的文件；
  · 每个文件独立 `try/except BaseException`，失败只记错误、绝不让异常冒泡；
  · 用 `spec_from_file_location` + 唯一模块名，不污染 `sys.modules`；
  · 日志打印每个已加载插件的 SHA256 前 12 位，用户可以自己核对；
  · 与内置适配器**同名**的插件会被跳过（不静默覆盖内置实现）。

⚠️ 插件是**主进程内执行**的 Python，没有真沙箱 —— 白名单是唯一的信任边界。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from providers.plugins import _sha256_head, candidates
from providers.registry import LoadReport
from .base import ImageProvider, registered_image_hooks

logger = logging.getLogger("imagegen.plugins")


def discover(dir_: Path, *, enabled: list[str] | None = None,
             allow_dir_scan: bool = False, registry=None) -> LoadReport:
    """扫描并加载生图插件。registry 给定时顺带把新注册的适配器登记进去。"""
    enabled = list(enabled or [])
    rep = LoadReport(source="生图插件")
    if not dir_.is_dir():
        if enabled:
            rep.errors.append((str(dir_), "插件目录不存在"))
        return rep

    files = candidates(dir_, enabled=enabled, allow_dir_scan=allow_dir_scan)
    if not files:
        rep.skipped.append((str(dir_),
                            "未启用目录扫描（allow_dir_scan=false），也不在白名单里，共 0 个插件"))
        return rep

    before = {id(h) for h in registered_image_hooks()}
    for path in files:
        if not path.is_file():
            rep.errors.append((path.name, "文件不存在"))
            continue
        name = f"qqbot_imageplugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                rep.errors.append((path.name, "无法构造 import spec"))
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        except BaseException as exc:  # noqa: BLE001  插件什么都能抛，全接住
            sys.modules.pop(name, None)
            rep.errors.append((path.name, f"{type(exc).__name__}: {exc}"))
            continue
        digest = _sha256_head(path)
        fresh = [h for h in registered_image_hooks() if id(h) not in before]
        before = {id(h) for h in registered_image_hooks()}
        if not fresh:
            rep.skipped.append((path.name,
                                f"加载成功但没有注册任何生图适配器（sha256 {digest}）"))
            continue
        for hook_name, cls, _prio in fresh:
            try:
                inst = cls()
            except Exception as exc:  # noqa: BLE001
                rep.errors.append((hook_name, f"实例化失败：{exc}"))
                continue
            inst.name = inst.name or hook_name
            if registry is None:
                rep.loaded.append(f"{hook_name}（{path.name} sha256:{digest}）")
            elif registry.register(hook_name, inst):
                rep.loaded.append(f"{hook_name}（{path.name} sha256:{digest}）")
            else:
                rep.skipped.append((hook_name, "名字已被内置或其它插件占用"))
    return rep


__all__ = ["discover", "ImageProvider"]
