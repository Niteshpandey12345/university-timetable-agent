"""Ingest the workbook and print the data-quality headline.   python -m scripts.ingest [path.xlsm]"""
import json
import sys

from ttagent.config import PROCESSED_DIR
from ttagent.engine.service import TimetableService

if __name__ == "__main__":
    svc = TimetableService()
    out = svc.ingest(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(out, indent=1, default=str))
    print(f"\nWrote {PROCESSED_DIR / 'dataset.json'}, quality_report.json, electives.yaml")
