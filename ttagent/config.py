"""Central configuration: loads config/settings.yaml once and resolves paths."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("TTAGENT_CONFIG", ROOT / "config" / "settings.yaml"))
PROCESSED_DIR = Path(os.getenv("TTAGENT_PROCESSED_DIR", ROOT / "data" / "processed"))
STATE_DIR = Path(os.getenv("TTAGENT_STATE_DIR", ROOT / "data" / "state"))
DB_PATH = Path(os.getenv("TTAGENT_DB", STATE_DIR / "timetable.db"))
CHECKPOINT_DB = Path(os.getenv("TTAGENT_CHECKPOINT_DB", STATE_DIR / "agent_memory.db"))
LOG_DIR = STATE_DIR / "logs"


OVERRIDES_PATH = ROOT / "config" / "overrides.yaml"


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    """config/settings.yaml, with user changes from config/overrides.yaml (written by the Settings page) on top."""
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if OVERRIDES_PATH.exists():
        cfg = _merge(cfg, yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8")) or {})
    cfg["time_grid"]["slots"] = {int(k): v for k, v in cfg["time_grid"]["slots"].items()}
    return cfg


def save_overrides(changes: dict[str, Any]) -> dict[str, Any]:
    current = yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8")) if OVERRIDES_PATH.exists() else {}
    merged = _merge(current or {}, changes)
    OVERRIDES_PATH.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    settings.cache_clear()
    return merged


def workbook_path() -> Path:
    p = Path(os.getenv("TTAGENT_WORKBOOK", settings()["workbook"]["path"]))
    return p if p.is_absolute() else ROOT / p


def ensure_dirs() -> None:
    for d in (PROCESSED_DIR, STATE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
