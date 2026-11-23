import os
import sys
import tempfile
from pathlib import Path

import pytest

# isolate all state (SQLite store, checkpoints) from the developer's data
_TMP = tempfile.mkdtemp(prefix="ttagent_test_")
os.environ["TTAGENT_STATE_DIR"] = _TMP
os.environ["MODEL_PROVIDER"] = "none"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _have_workbook() -> bool:
    from ttagent.config import workbook_path
    return workbook_path().exists()


def pytest_configure(config):
    config.addinivalue_line("markers", "workbook: needs the institutional timetable workbook (not distributed)")


def pytest_collection_modifyitems(config, items):
    """The institution's workbook is not part of the public repository. Without it, tests that need the real
    data are skipped; the ITC-2007 and engine tests still run."""
    if _have_workbook():
        return
    skip = pytest.mark.skip(reason="institutional workbook not available (see data/raw/README.md)")
    for item in items:
        if {"ds", "svc"} & set(getattr(item, "fixturenames", ())) or item.get_closest_marker("workbook"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def ds():
    from ttagent.ingestion.excel_loader import load_workbook_dataset
    return load_workbook_dataset()


@pytest.fixture(scope="session")
def svc():
    from ttagent.engine.service import TimetableService
    s = TimetableService()
    s.ingest()
    return s
