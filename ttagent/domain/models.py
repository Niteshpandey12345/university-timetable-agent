"""
Typed domain model for the university timetabling problem.

Vocabulary (mirrors the workbook so faculty/admins recognise it):

* Requirement  -- one row of Main!K:V, e.g. ``1B1,1B2:P-CPLAB@CL3_REENU,RP``:
                  batches 1B1+1B2 need a 2-hour CPLAB practical in CL3 taught
                  by REENU and RP, ``per_week`` times a week.
* Session      -- one weekly occurrence of a requirement (requirement x k).
* Assignment   -- a session placed at (day, start slot, room).
* Booking      -- anything that occupies time: an assignment, a fixed external
                  booking, or a cell of the published grid. The validator works
                  on bookings so it can judge *any* timetable, including the
                  hand-edited one in the workbook.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Kind(str, Enum):
    LECTURE = "L"
    TUTORIAL = "T"
    PRACTICAL = "P"


KIND_NAMES = {"L": "Lecture", "T": "Tutorial", "P": "Practical/Lab"}


class Requirement(BaseModel):
    token: int
    batches: list[str]
    kind: Kind
    course: str
    duration: int = Field(ge=1, le=4, description="contiguous hours per session")
    venue: str
    faculty: list[str]
    per_week: int = Field(ge=1, le=7)
    semester: int
    program: str = "B"
    source_row: int | None = None
    macro_scheduled: int | None = Field(None, description="VBA 'Freq. Scheduled' bookkeeping")
    basket_id: str | None = None

    @property
    def batch_str(self) -> str:
        return ",".join(self.batches)

    @property
    def code(self) -> str:
        return f"{self.batch_str}:{self.kind.value}-{self.course}@{self.venue}_{','.join(self.faculty)}"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.batch_str, self.kind.value, self.course)

    @property
    def weekly_hours(self) -> int:
        return self.duration * self.per_week


class Room(BaseModel):
    room_id: str
    category: str  # lecture_theatre | classroom | computer_lab | special_lab | digital_lc
    lab_type: str | None = None
    observed_batch_capacity: int = 1
    used_by_requirements: int = 0
    seats: int | None = None          # real seat count from config/rooms.yaml (if provided)


class Faculty(BaseModel):
    code: str
    is_placeholder: bool = False
    aliases: list[str] = Field(default_factory=list)
    unavailable: list[tuple[str, int | None]] = Field(default_factory=list)
    avoid: list[tuple[str, int | None]] = Field(default_factory=list)   # soft time preferences


class Batch(BaseModel):
    code: str
    semester: int | None
    program: str
    in_requirements: bool = True
    size: int | None = None           # students (config/batches.yaml or default)


class ElectiveBasket(BaseModel):
    basket_id: str
    semester: int
    courses: list[str]
    evidence: int = 0
    confidence: str = "medium"        # high | medium | low (from co-scheduling evidence)
    note: str = ""


class Booking(BaseModel):
    """Anything that occupies (day, slots)."""
    day: str
    start: int
    duration: int = 1
    batches: list[str]
    faculty: list[str]
    room: str
    course: str
    kind: Kind
    origin: str = "solver"   # solver | fixed | published | pinned
    session_id: str | None = None
    token: int | None = None
    basket_id: str | None = None
    label: str | None = None

    @property
    def slots(self) -> list[int]:
        return list(range(self.start, self.start + self.duration))

    @property
    def code(self) -> str:
        return f"{','.join(self.batches)}:{self.kind.value}-{self.course}@{self.room}_{','.join(self.faculty)}"


class Assignment(BaseModel):
    session_id: str
    token: int
    day: str
    start: int
    room: str


class ReferenceSession(BaseModel):
    token: int
    day: str
    start: int
    duration: int
    room: str
    faculty: list[str]


class Dataset(BaseModel):
    source: str
    requirements: list[Requirement]
    rooms: dict[str, Room]
    faculty: dict[str, Faculty]
    batches: dict[str, Batch]
    baskets: list[ElectiveBasket]
    fixed_bookings: list[Booking]
    published: list[Booking] = Field(default_factory=list, description="every parsed cell of the workbook grid")
    reference: list[ReferenceSession] = Field(default_factory=list, description="published grid mapped back to requirement tokens")
    quality: dict[str, Any] = Field(default_factory=dict)

    # ---- convenience -------------------------------------------------
    def req(self, token: int) -> Requirement:
        return self._by_token()[token]

    def _by_token(self) -> dict[int, Requirement]:
        cache = getattr(self, "_tok_cache", None)
        if cache is None:
            cache = {r.token: r for r in self.requirements}
            object.__setattr__(self, "_tok_cache", cache)
        return cache

    def semesters(self) -> list[int]:
        return sorted({r.semester for r in self.requirements})

    def is_placeholder(self, fac: str) -> bool:
        f = self.faculty.get(fac)
        return bool(f and f.is_placeholder)

    def basket_of(self, req: Requirement) -> str | None:
        return req.basket_id


def session_ids(req: Requirement) -> list[str]:
    return [f"R{req.token}#{k}" for k in range(req.per_week)]


def parse_session_id(sid: str) -> tuple[int, int]:
    tok, k = sid[1:].split("#")
    return int(tok), int(k)
