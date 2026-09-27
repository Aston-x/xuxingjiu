"""跨平台文件锁（单实例保护）。

原来只有 `msvcrt`（Windows 专有），非 Windows 上直接 `return True` —— 也就是说
**Linux/macOS 上单实例保护是静默失效的**，两个 bot 一起写 memory.json / mood.json /
tokens.json 会把数据写坏。这不是"体验问题"，是数据损坏。

这里换成一个薄封装：Windows 用 `msvcrt.locking`，POSIX 用 `fcntl.flock`。
两者语义一致的关键点是 **进程退出时由操作系统自动释放** ——
所以"上次被 kill -9 留下的陈旧锁"能自愈，不需要手工删文件。

⚠️ `flock` 在 NFS / 某些容器 overlay 上会返回 `ENOTSUP`。这种情况下**放行**
（返回"拿到了锁"）：宁可少一层保护，也不能让 bot 在容器里根本起不来。
"""

from __future__ import annotations

import errno
import os
from pathlib import Path


def try_lock(fh) -> bool:
    """尝试对已打开的文件句柄加排他锁。已占用返回 False。"""
    if os.name == "nt":
        import msvcrt
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOLCK):
            # 文件系统不支持锁（NFS / overlay / 某些容器卷）：放行，别把启动卡死
            return True
        return False


def unlock(fh) -> None:
    """释放锁。失败也就算了 —— 进程退出时操作系统会兜底。"""
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001
        pass


def acquire(path: Path, *, pid_text: bool = True):
    """打开并锁住 `path`。成功返回 (文件句柄, PID)；被占用返回 (None, 持有者 PID)。

    返回的句柄必须一直持有（挂到模块级变量上），被 GC 掉就等于放锁。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    if not try_lock(fh):
        # 读不到持有者 PID 是**正常的**：Windows 的 LockFile 语义会把锁住的字节
        # 连同读取一起拒掉（PermissionError），POSIX 倒是能读。
        # 所以这里只做尽力而为 —— 拿不到就报 0，让上层显示"PID ?"就行，
        # 绝不能因为读个提示信息而抛异常。
        holder = 0
        try:
            fh.seek(0)
            lines = (fh.read() or "").strip().splitlines()
            if lines:
                holder = int(lines[0])
        except Exception:  # noqa: BLE001
            holder = 0
        fh.close()
        return None, holder
    if pid_text:
        try:
            fh.seek(0)
            fh.write(f"{os.getpid()}\n")
            fh.truncate()
            fh.flush()
        except OSError:
            pass
    return fh, os.getpid()


def release(fh) -> None:
    if fh is None:
        return
    unlock(fh)
    try:
        fh.close()
    except OSError:
        pass
