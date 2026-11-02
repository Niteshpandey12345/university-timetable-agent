"""iCalendar export: one weekly-recurring event per session for a batch or a faculty member."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from ttagent.config import settings
from ttagent.domain.models import KIND_NAMES, Booking, Dataset
from ttagent.domain.timegrid import TimeGrid


def export_ics(ds: Dataset, bookings: list[Booking], entity: str, path: Path) -> Path:
    kind, _, code = entity.partition(":")
    cal = settings().get("calendar", {})
    start = dt.date.fromisoformat(str(cal.get("semester_start", "2026-07-20")))
    end = dt.date.fromisoformat(str(cal.get("semester_end", "2026-12-05")))
    tz = cal.get("timezone", "Asia/Kolkata")
    grid = TimeGrid.from_settings(use_extended=True)
    sel = [b for b in bookings if (kind == "batch" and code in b.batches) or (kind == "faculty" and code.upper() in b.faculty)
           or (kind == "room" and b.room == code.upper())]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ttagent//University Timetable Agent//EN",
             f"X-WR-CALNAME:Timetable {entity}", f"X-WR-TIMEZONE:{tz}"]
    for i, b in enumerate(sorted(sel, key=lambda b: (grid.day_index(b.day), b.start))):
        day0 = start + dt.timedelta(days=grid.day_index(b.day))
        t_a = grid.slot_times[b.start].split("-")[0].replace(":", "")
        t_b = grid.slot_times[b.start + b.duration - 1].split("-")[1].replace(":", "")
        lines += ["BEGIN:VEVENT",
                  f"UID:{entity}-{b.session_id or i}-{b.day}{b.start}@ttagent",
                  f"DTSTAMP:{stamp}",
                  f"DTSTART;TZID={tz}:{day0:%Y%m%d}T{t_a}00",
                  f"DTEND;TZID={tz}:{day0:%Y%m%d}T{t_b}00",
                  f"RRULE:FREQ=WEEKLY;UNTIL={end:%Y%m%d}T235959Z",
                  f"SUMMARY:{b.course} ({KIND_NAMES[b.kind.value]})",
                  f"LOCATION:{b.room}",
                  f"DESCRIPTION:Batches {','.join(b.batches)} | Faculty {','.join(b.faculty)}",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return path
