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
