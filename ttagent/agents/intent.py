"""Intent schema + a deterministic rule-based parser (used offline and as LLM fallback)."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

Action = Literal["generate", "validate", "explain_unplaced", "view", "what_if_unavailable", "move", "repair",
                 "compare", "quality", "capacity", "export", "publish", "chat"]

DAYS = {"mon": "Mon", "monday": "Mon", "tue": "Tue", "tues": "Tue", "tuesday": "Tue", "wed": "Wed", "wednesday": "Wed",
        "thu": "Thu", "thur": "Thu", "thurs": "Thu", "thursday": "Thu", "fri": "Fri", "friday": "Fri",
        "sat": "Sat", "saturday": "Sat"}
YEAR_TO_SEM = {1: 1, 2: 3, 3: 5, 4: 7}


class Intent(BaseModel):
    action: Action = Field(description="what the user wants")
    semesters: list[int] | None = Field(None, description="semesters in scope, e.g. [3]; null = all")
    kind: Literal["batch", "faculty", "room", "semester"] | None = None
    code: str | None = Field(None, description="batch like 1B1, faculty code like RAVI, room like LT1, or semester number")
    token: int | None = Field(None, description="requirement token number")
    session_id: str | None = Field(None, description="session id like R104#0")
    day: str | None = Field(None, description="Mon..Sat")
    slot: int | None = None
    start: int | None = None
    room: str | None = None
    stability: bool = Field(False, description="keep the published timetable as much as possible")
    fmt: str | None = None
    entity: str | None = None


def parse_rules(text: str, known_faculty: set[str] | None = None, known_rooms: set[str] | None = None) -> Intent:
    t = text.strip()
    low = t.lower()
    up = t.upper()
    sems = _semesters(low)
    day = next((v for k, v in DAYS.items() if re.search(rf"\b{k}\b", low)), None)
    slot = _int(re.search(r"\bslot\s*(\d)", low))
    batch = re.search(r"\b(\d[A-Z]{1,3}\d{0,2})\b", up)
    sid = re.search(r"\b(R\d+#\d)\b", up)
    token = _int(re.search(r"\btoken\s*#?(\d+)", low))
    fac = next((f for f in (known_faculty or set()) if re.search(rf"\b{re.escape(f)}\b", up)), None)
    room = next((r for r in sorted(known_rooms or set(), key=len, reverse=True) if re.search(rf"\b{re.escape(r)}\b", up)), None)
    stable = any(w in low for w in ("minimal", "minimum change", "keep", "stable", "disrupt"))

    def I(action, **kw):  # noqa: N802
        return Intent(action=action, semesters=sems, stability=stable, **kw)

    if sid and any(w in low for w in ("move", "shift", "reschedule")):
        return I("move", session_id=sid.group(1), day=day, start=slot)
    if any(w in low for w in ("unavailable", "not available", "on leave", "absent", "can't come", "cannot come", "out of office")):
        if fac:
            return I("what_if_unavailable", kind="faculty", code=fac, day=day, slot=slot)
        if room:
            return I("what_if_unavailable", kind="room", code=room, day=day, slot=slot)
        if batch:
            return I("what_if_unavailable", kind="batch", code=batch.group(1), day=day, slot=slot)
    if "data" in low and any(w in low for w in ("quality", "good", "issue", "clean", "dirty", "problem", "error")):
        return I("quality")
    if any(w in low for w in ("export", "download", "excel", "xlsx", ".ics", "calendar", "ical")):
        fmt = "ics" if any(w in low for w in ("ics", "calendar", "ical")) else ("json" if "json" in low else "xlsx")
        ent = f"batch:{batch.group(1)}" if batch else (f"faculty:{fac}" if fac else None)
        return I("export", fmt=fmt, entity=ent)
    if any(w in low for w in ("compare", " vs ", "versus", "baseline", "better than", "macro")):
        return I("compare")
    if re.search(r"\bpublish\b", low):
        return I("publish")
    if any(w in low for w in ("data quality", "quality of the data", "issues in the excel", "data issues", "dirty", "clean the data")):
        return I("quality")
    if any(w in low for w in ("capacity", "feasib", "possible to schedule", "enough rooms", "pigeonhole")):
        return I("capacity")
    if any(w in low for w in ("why", "unplaced", "could not", "couldn't", "cannot schedule", "can't schedule", "not scheduled")):
        return I("explain_unplaced", token=token)
    if "repair" in low or "fix" in low:
        return I("repair")
    if any(w in low for w in ("conflict", "clash", "validate", "check", "violation")):
        return I("validate")
    if any(w in low for w in ("generate", "create", "build", "make", "schedule", "solve", "prepare", "allocate")):
        return I("generate")
    if any(w in low for w in ("show", "view", "display", "timetable of", "timetable for", "what does", "when does", "list")):
        if batch:
            return I("view", kind="batch", code=batch.group(1))
        if fac:
            return I("view", kind="faculty", code=fac)
        if room:
            return I("view", kind="room", code=room)
        if sems:
            return I("view", kind="semester", code=str(sems[0]))
    return I("chat")


def _semesters(low: str) -> list[int] | None:
    out = set()
    for m in re.finditer(r"\bsem(?:ester)?\s*(\d)", low):
        out.add(int(m.group(1)))
    for m in re.finditer(r"\b(?:year|yr)\s*(\d)", low):
        out.add(YEAR_TO_SEM.get(int(m.group(1)), int(m.group(1))))
    for m in re.finditer(r"\b(\d)(?:st|nd|rd|th)\s+year", low):
        out.add(YEAR_TO_SEM.get(int(m.group(1)), int(m.group(1))))
    if any(w in low for w in ("whole university", "all semesters", "entire", "everything", "full timetable")):
        return None
    return sorted(out) or None


def _int(m) -> int | None:
    return int(m.group(1)) if m else None
