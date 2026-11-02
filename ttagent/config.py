"""Central configuration: loads config/settings.yaml once and resolves paths."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("TTAGENT_CONFIG", ROOT / "config" / "settings.yaml"))
PROCESSED_DIR = ROOT / "data" / "processed"
STATE_DIR = Path(os.getenv("TTAGENT_STATE_DIR", ROOT / "data" / "state"))
DB_PATH = Path(os.getenv("TTAGENT_DB", STATE_DIR / "timetable.db"))
CHECKPOINT_DB = Path(os.getenv("TTAGENT_CHECKPOINT_DB", STATE_DIR / "agent_memory.db"))
LOG_DIR = STATE_DIR / "logs"


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["time_grid"]["slots"] = {int(k): v for k, v in cfg["time_grid"]["slots"].items()}
    return cfg


def workbook_path() -> Path:
    p = Path(os.getenv("TTAGENT_WORKBOOK", settings()["workbook"]["path"]))
    return p if p.is_absolute() else ROOT / p


def ensure_dirs() -> None:
    for d in (PROCESSED_DIR, STATE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
