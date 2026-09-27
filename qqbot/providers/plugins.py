"""Python 插件发现与加载。

⚠️ **安全边界说清楚**：插件是在**主进程里 import 的 Python 文件**，拿到了就等于
能执行任意代码 —— 这里没有任何真沙箱。所以默认策略是：

  · `allow_dir_scan=False`（默认）：只加载 `enabled` 白名单里点名的文件，
    往目录里丢一个 .py 是不会被自动执行的；
  · `allow_dir_scan=True`：才扫描目录下所有 *.py，跳过 `_` 前缀与 `__pycache__`；
  · 每个文件独立 try/except（连 BaseException 都接），失败只记错误，**绝不让插件异常冒泡**；
  · 用 `importlib.util.spec_from_file_location` 配唯一模块名，不污染 sys.modules；
  · 启动日志打印每个已加载插件的 SHA256 前 12 位，用户可以自己核对；
  · 名字撞车默认跳过（见 Registry.register），不静默覆盖内置协议。
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import sys
from pathlib import Path

from .base import Provider, registered_hooks
from .registry import LoadReport

logger = logging.getLogger("providers.plugins")


def _sha256_head(path: Path, n: int = 12) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:n]
    except OSError:
        return "?"


def candidates(dir_: Path, *, enabled: list[str], allow_dir_scan: bool) -> list[Path]:
    """要加载哪些文件。白名单优先；白名单为空且允许目录扫描时才扫目录。"""
    if enabled:
        out = []
        for name in enabled:
            p = Path(name)
            if not p.is_absolute():
                p = dir_ / p
            out.append(p)
        return out
    if not allow_dir_scan:
        return []
    out = []
    for p in sorted(dir_.glob("*.py")):
        if p.name.startswith("_") or p.name == "__init__.py":
            continue
        out.append(p)
    return out


def discover(dir_: Path, *, enabled: list[str] | None = None,
             allow_dir_scan: bool = False, registry=None) -> LoadReport:
    """扫描并加载插件。registry 给定时顺带把新注册的 Provider 登记进去。"""
    enabled = list(enabled or [])
    rep = LoadReport(source="Python 插件")
    if not dir_.is_dir():
        if enabled:
            rep.errors.append((str(dir_), "插件目录不存在"))
        return rep

    files = candidates(dir_, enabled=enabled, allow_dir_scan=allow_dir_scan)
    if not files:
        rep.skipped.append((str(dir_),
                            "未启用目录扫描（allow_dir_scan=false），也不在白名单里，共 0 个插件"))
        return rep

    before = {id(h) for h in registered_hooks()}
    for path in files:
        if not path.is_file():
            rep.errors.append((path.name, "文件不存在"))
            continue
        name = f"qqbot_plugin_{path.stem}"
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
        new_hooks = [h for h in registered_hooks() if id(h) not in before]
        before = {id(h) for h in registered_hooks()}
        if not new_hooks:
            rep.skipped.append((path.name, f"加载成功但没有注册任何 Provider（sha256 {digest}）"))
            continue
        for hook_name, cls, prio in new_hooks:
            inst = _instantiate(cls)
            if inst is None:
                rep.skipped.append((path.name, f"{hook_name} 需要构造参数，插件不支持"))
                continue
            inst.name = inst.name or hook_name
            if registry is not None:
                if registry.register(hook_name, inst):
                    rep.loaded.append(f"{hook_name}（{path.name} sha256:{digest}）")
                else:
                    rep.skipped.append((hook_name, "名字已被内置或其它插件占用"))
            else:
                rep.loaded.append(f"{hook_name}（{path.name} sha256:{digest}）")
    return rep


def _instantiate(cls: type[Provider]) -> Provider | None:
    import inspect  # noqa: PLC0415
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return None
    required = [p for p in sig.parameters.values()
                if p.name != "self" and p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if required:
        return None
    try:
        return cls()
    except Exception as exc:  # noqa: BLE001
        logger.warning("插件 %s 实例化失败：%s", cls, exc)
        return None
