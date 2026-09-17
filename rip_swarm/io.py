from __future__ import annotations

import json
import os
from pathlib import Path


class ExclExistsError(FileExistsError):
    pass


def _dump(obj: dict) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def excl_create_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, 0o644)
    except FileExistsError as e:
        raise ExclExistsError(str(path)) from e
    try:
        os.write(fd, _dump(obj))
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(tmp), flags, 0o644)
    try:
        os.write(fd, _dump(obj))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data
