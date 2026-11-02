"""The weekly time grid and the slot rules encoded in the original VBA macro."""
from __future__ import annotations

from dataclasses import dataclass, field

from ttagent.config import settings


@dataclass(frozen=True)
class TimeGrid:
    days: tuple[str, ...]
    slot_times: dict[int, str]
    lunch_slot: int
    teaching_slots: tuple[int, ...]
    extended_slots: tuple[int, ...] = ()
    saturday_last_slot: int = 4
    use_extended: bool = False
    unavailable_days: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_settings(cls, use_extended: bool = False) -> "TimeGrid":
        tg = settings()["time_grid"]
        return cls(
            days=tuple(tg["days"]),
            slot_times=dict(tg["slots"]),
            lunch_slot=int(tg["lunch_slot"]),
            teaching_slots=tuple(tg["teaching_slots"]),
            extended_slots=tuple(tg.get("extended_slots", [])),
            saturday_last_slot=int(tg.get("saturday_last_slot", 4)),
            use_extended=use_extended,
        )

    def with_extended(self, flag: bool) -> "TimeGrid":
        return TimeGrid(self.days, self.slot_times, self.lunch_slot, self.teaching_slots,
                        self.extended_slots, self.saturday_last_slot, flag, self.unavailable_days)

    # ------------------------------------------------------------------
    def slots_for(self, day: str) -> list[int]:
        base = list(self.teaching_slots) + (list(self.extended_slots) if self.use_extended else [])
        if day == "Sat":
            base = [s for s in base if s <= self.saturday_last_slot]
        return sorted(s for s in base if s != self.lunch_slot)

    def all_slots(self) -> list[tuple[str, int]]:
        return [(d, s) for d in self.days for s in self.slots_for(d)]

    def starts_for(self, day: str, duration: int) -> list[int]:
        """Start slots where `duration` consecutive teaching slots fit without crossing lunch."""
        ok = set(self.slots_for(day))
        return [s for s in sorted(ok) if all((s + i) in ok for i in range(duration))]

    def capacity_hours(self, day: str | None = None) -> int:
        if day:
            return len(self.slots_for(day))
        return sum(len(self.slots_for(d)) for d in self.days)

    def is_extended(self, slot: int) -> bool:
        return slot in self.extended_slots

    def label(self, day: str, start: int, duration: int = 1) -> str:
        a = self.slot_times[start].split("-")[0]
        b = self.slot_times[start + duration - 1].split("-")[1]
        return f"{day} {a}-{b}"

    def day_index(self, day: str) -> int:
        return self.days.index(day)
