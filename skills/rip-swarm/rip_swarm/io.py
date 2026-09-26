from __future__ import annotations

import json
import os
from pathlib import Path


class ExclExistsError(FileExistsError):
    pass


def _dump(obj: dict) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _fsync_dir(path: Path) -> None:
    """Persist a directory entry (create/rename) so it survives a crash."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _tmp_name(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")


def excl_create_json(path: Path, obj: dict) -> None:
    payload = _dump(obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, 0o644)
    except FileExistsError as e:
        raise ExclExistsError(str(path)) from e
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: dict) -> None:
    payload = _dump(obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_name(path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(tmp), flags, 0o644)
    try:
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def write_json_to_new_path(path: Path, obj: dict) -> bool:
    """Atomically place `obj` at `path` only if nothing holds that name yet.

    Returns False (writing nothing) when `path` is already taken, so callers
    can walk a sequence of candidate names without ever clobbering a file.
    """
    payload = _dump(obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_name(path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(tmp), flags, 0o644)
    try:
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        # Reserve the name with a hard link: fails loudly if it is taken.
        try:
            os.link(str(tmp), str(path))
        except FileExistsError:
            return False
        except OSError:
            if path.exists():
                return False
            os.replace(str(tmp), str(path))
            _fsync_dir(path.parent)
            return True
    finally:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
    _fsync_dir(path.parent)
    return True


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data
