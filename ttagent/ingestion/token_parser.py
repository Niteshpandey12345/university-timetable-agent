"""
Parser for the workbook's session token grammar.

    BATCHES ":" KIND "-" COURSE "@" VENUE "_" FACULTY ("," FACULTY)* [ANNOTATION]

    1B1,1B2:P-CPLAB@CL3_REENU,RP
    1B7,1B8,1B9:L-CP@LT8_REENU_Extra      <- "_Extra" annotation
    1B13,1B14:L-CP@LT8_RAVI-EXTRA         <- "-EXTRA" annotation
    4BBA:L-HHS1@LT14_HSS2_CR17            <- trailing venue note
    7B1,...,7B6-AW@LT11_KLD               <- malformed (missing ":L"), recovered

The parser is lenient but *honest*: every normalisation or recovery is
returned as a note so the data-quality report can surface it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TOKEN_RE = re.compile(r"^(?P<batches>[^:@]+):(?P<kind>[LTP])-(?P<course>[^@]+)@(?P<venue>[^_]+)_(?P<faculty>.+)$")
RECOVER_RE = re.compile(r"^(?P<batches>(?:\d?[A-Z]+\d*,?)+)-(?P<course>[^@]+)@(?P<venue>[^_]+)_(?P<faculty>.+)$")
BATCH_RE = re.compile(r"^(?P<sem>\d)?(?P<prog>[A-Z]+?)(?P<section>\d*)$")
ANNOT_RE = re.compile(r"[_-](EXTRA|Extra|extra)$")


@dataclass
class ParsedToken:
    raw: str
    batches: list[str]
    kind: str
    course: str
    venue: str
    faculty: list[str]
    notes: list[str] = field(default_factory=list)
    recovered: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (",".join(self.batches), self.kind, self.course)


def normalise_faculty(raw: str) -> tuple[list[str], list[str]]:
    """Return (faculty codes, notes). Strips Extra/venue annotations, upper-cases."""
    notes: list[str] = []
    s = raw.strip()
    m = ANNOT_RE.search(s)
    if m:
        notes.append(f"annotation '{m.group(0)}' stripped (extra/additional class marker)")
        s = s[: m.start()]
    if "_" in s:  # e.g. HSS2_CR17 -> venue note after faculty
        s, extra = s.split("_", 1)
        notes.append(f"trailing note '_{extra}' stripped")
    out = []
    for f in s.split(","):
        f = f.strip()
        if not f:
            continue
        if f != f.upper():
            notes.append(f"faculty code '{f}' normalised to '{f.upper()}'")
        f2 = ANNOT_RE.sub("", f.upper())
        out.append(f2)
    return out, notes


def parse_token(raw: object) -> ParsedToken | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or len(s) < 5:
        return None
    m = TOKEN_RE.match(s)
    recovered = False
    notes: list[str] = []
    if not m:
        m2 = RECOVER_RE.match(s)
        if not m2:
            return None
        recovered = True
        notes.append("malformed token: missing ':KIND' -- recovered assuming Lecture (L)")
        d = m2.groupdict() | {"kind": "L"}
    else:
        d = m.groupdict()
    batches = [b.strip() for b in d["batches"].split(",") if b.strip()]
    fac, fnotes = normalise_faculty(d["faculty"])
    return ParsedToken(raw=s, batches=batches, kind=d["kind"], course=d["course"].strip(),
                       venue=d["venue"].strip().upper(), faculty=fac, notes=notes + fnotes,
                       recovered=recovered)


def batch_parts(code: str) -> tuple[int | None, str, str]:
    m = BATCH_RE.match(code)
    if not m:
        return None, code, ""
    sem = int(m.group("sem")) if m.group("sem") else None
    return sem, m.group("prog"), m.group("section")


def room_category(room: str) -> tuple[str, str | None]:
    r = room.upper()
    if r.startswith("LT"):
        return "lecture_theatre", None
    if r == "DLC":
        return "digital_lc", None
    if r.startswith("CR"):
        return "classroom", None
    if r.startswith("CL"):
        return "computer_lab", "computer"
    if "LAB" in r:
        return "special_lab", re.sub(r"\d+$", "", r)
    return "other", None
