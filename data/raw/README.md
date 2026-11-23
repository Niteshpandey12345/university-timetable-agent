# Input workbook

The timetable workbook of the institution is not part of this repository. To run the system on real data, put
a macro-enabled workbook in the same format here:

    data/raw/Master_TimeTable_July-Dec-2026.xlsm

or set the path in the environment:

    TTAGENT_WORKBOOK=path/to/workbook.xlsm

Then run `ttagent ingest`. It writes the parsed data to `data/processed/` (also not tracked).

The format: a requirement sheet with one row per course, type (L, T or P), batches, teachers, hours per
session and sessions per week, and the published weekly grid. See `ttagent/ingestion/excel_loader.py` for the
exact columns that are read.

Without a workbook, the ITC-2007 benchmark (`data/benchmarks/itc2007`) and the engine tests run as usual; the
tests that need the real data are skipped.
