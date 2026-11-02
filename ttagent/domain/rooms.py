"""Room-suitability policy (shared by solver, validator and diagnosis as *policy*, not algorithm)."""
from __future__ import annotations

from ttagent.domain.models import Dataset, Requirement, Room

# A session whose requested venue is of category X may move to these categories.
COMPATIBLE = {
    "classroom": ("classroom", "lecture_theatre"),
    "lecture_theatre": ("lecture_theatre", "digital_lc"),
    "digital_lc": ("digital_lc", "lecture_theatre"),
    "computer_lab": ("computer_lab",),
    "special_lab": ("special_lab",),
    "other": ("other",),
}


def room_suitable(ds: Dataset, req: Requirement, room: Room) -> bool:
    home = ds.rooms.get(req.venue)
    if home is None:
        return room.room_id == req.venue
    if room.room_id == home.room_id:
        return True
    if room.category not in COMPATIBLE.get(home.category, (home.category,)):
        return False
    if home.lab_type and room.lab_type != home.lab_type:
        return False
    return room.observed_batch_capacity >= len(req.batches)


def room_options(ds: Dataset, req: Requirement, mode: str = "flex", max_alt: int = 5) -> list[str]:
    """Requested venue first, then the best-fitting suitable alternatives (smallest adequate capacity)."""
    if mode == "home" or req.venue not in ds.rooms:
        return [req.venue]
    alts = [r for r in ds.rooms.values() if r.room_id != req.venue and room_suitable(ds, req, r)]
    home_cat = ds.rooms[req.venue].category
    alts.sort(key=lambda r: (r.category != home_cat, r.observed_batch_capacity, r.room_id))
    return [req.venue] + [r.room_id for r in alts[:max_alt]]
