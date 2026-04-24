from __future__ import annotations

import os
from pathlib import Path
from typing import Union

DATA_DIR_ENV = "RATBOT_DATA_DIR"


def get_data_dir() -> Path:
    configured = (os.getenv(DATA_DIR_ENV) or "").strip()
    base = Path(configured) if configured else Path(".")
    return base.resolve()


def ensure_data_dir() -> Path:
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def data_path(*parts: Union[str, os.PathLike[str]]) -> Path:
    return ensure_data_dir().joinpath(*parts)


def resolve_storage_path(path_value: Union[str, os.PathLike[str]]) -> Path:
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    return data_path(candidate)
