"""Shared secure evidence writer — stdlib only.

All evidence-writing authorities use this module. The uploader is
read-only transport and never calls these primitives.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

MAX_READ_BYTES = 100 * 1024 * 1024


class EvidenceWriteError(Exception):
    pass


class TrustedRoot:
    """A resolved, validated evidence directory."""

    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        if not p.is_absolute():
            raise EvidenceWriteError("evidence root must be absolute")
        try:
            resolved = p.resolve(strict=True)
        except OSError as exc:
            raise EvidenceWriteError(f"cannot resolve evidence root: {exc}") from exc
        if p.is_symlink() or not resolved.is_dir():
            raise EvidenceWriteError("evidence root must be a real directory")
        self._root = resolved
        self._root_fd = os.open(str(resolved), os.O_RDONLY | os.O_DIRECTORY)

    @property
    def path(self) -> Path:
        return self._root

    def resolve(self, relpath: str) -> Path:
        raw = self._root / relpath
        resolved = raw.resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise EvidenceWriteError(f"path traversal: {relpath!r} escapes evidence root") from exc
        return raw

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> TrustedRoot:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _check_not_symlink(path: Path, label: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise EvidenceWriteError(f"{label} is a symlink: {path}")


def _fsync_dir(dirpath: Path) -> None:
    fd = os.open(str(dirpath), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise EvidenceWriteError("short write: os.write returned 0")
        view = view[written:]


def _serialize(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def exclusive_create_json(root: TrustedRoot, relpath: str, data: dict[str, Any]) -> Path:
    target = root.resolve(relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    _check_not_symlink(target, "target")
    content = _serialize(data)
    fd = -1
    try:
        fd = os.open(
            str(target),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        _write_all(fd, content)
        os.fsync(fd)
    except FileExistsError as exc:
        raise EvidenceWriteError(f"exclusive create failed, already exists: {target}") from exc
    except OSError as exc:
        raise EvidenceWriteError(f"exclusive create failed: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    _fsync_dir(target.parent)
    return target


def exclusive_create_empty(root: TrustedRoot, relpath: str) -> Path:
    target = root.resolve(relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    _check_not_symlink(target, "target")
    fd = -1
    try:
        fd = os.open(
            str(target),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fsync(fd)
    except FileExistsError as exc:
        raise EvidenceWriteError(f"exclusive create failed, already exists: {target}") from exc
    except OSError as exc:
        raise EvidenceWriteError(f"exclusive create failed: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    _fsync_dir(target.parent)
    return target


def atomic_replace_json(root: TrustedRoot, relpath: str, data: dict[str, Any]) -> Path:
    target = root.resolve(relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    _check_not_symlink(target, "target")
    content = _serialize(data)
    fd = -1
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        os.fchmod(fd, 0o600)
        _write_all(fd, content)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        _check_not_symlink(target, "target before replace")
        os.replace(tmp_path, str(target))
        tmp_path = None
        _fsync_dir(target.parent)
    except EvidenceWriteError:
        raise
    except OSError as exc:
        raise EvidenceWriteError(f"atomic replace failed: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
    return target


def append_jsonl(root: TrustedRoot, relpath: str, record: dict[str, Any]) -> None:
    target = root.resolve(relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    _check_not_symlink(target, "target")
    line = json.dumps(record, sort_keys=True) + "\n"
    data = line.encode("utf-8")
    fd = os.open(
        str(target),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def safe_read(root: TrustedRoot, relpath: str) -> bytes:
    target = root.resolve(relpath)
    _check_not_symlink(target, "read target")
    fd = -1
    try:
        fd = os.open(
            str(target),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise EvidenceWriteError(f"not a regular file: {target}")
        if st.st_size < 0 or st.st_size > MAX_READ_BYTES:
            raise EvidenceWriteError(f"invalid file size ({st.st_size}): {target}")
        if st.st_size == 0:
            return b""
        data = b""
        while len(data) < st.st_size:
            chunk = os.read(fd, min(65536, st.st_size - len(data)))
            if not chunk:
                break
            data += chunk
        return data
    except FileNotFoundError as exc:
        raise EvidenceWriteError(f"file not found: {target}") from exc
    except EvidenceWriteError:
        raise
    except OSError as exc:
        raise EvidenceWriteError(f"read failed: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
