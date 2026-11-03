#!/usr/bin/env bash
# University Timetable Agent launcher (macOS / Linux)
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e ".[gemini,claude,dev]"
fi
[ -f data/processed/dataset.json ] || .venv/bin/ttagent ingest
exec .venv/bin/ttagent serve "$@"
