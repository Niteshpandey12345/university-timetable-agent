"""Every hard rule of the independent validator, on synthetic bookings over the real dataset."""
from ttagent.domain.models import Booking, Kind
from ttagent.engine.validator import validate


def B(day, start, batches, fac, room, course="X", kind="L", dur=1, **kw):
    return Booking(day=day, start=start, duration=dur, batches=batches, faculty=fac, room=room, course=course,
                   kind=Kind(kind), origin="solver", **kw)


def rules(ds, bookings):
    return validate(ds, bookings, check_allocation=False)["counts"]


def test_clean(ds):
    assert rules(ds, [B("Mon", 1, ["1B1"], ["RAVI"], "LT1"), B("Mon", 1, ["1B2"], ["DJ"], "LT2")]) == {}


def test_batch_faculty_room_clash(ds):
    c = rules(ds, [B("Mon", 1, ["1B1"], ["RAVI"], "LT1"), B("Mon", 1, ["1B1", "1B2"], ["RAVI"], "LT1", course="Y")])
    assert c["H1_batch_clash"] == 1 and c["H2_faculty_clash"] == 1 and c["H3_room_clash"] == 1


def test_parallel_electives_are_exempt(ds):
    a = B("Tue", 2, ["7B1", "7B2"], ["DJ"], "LT2", course="OR", basket_id="S7-E1")
    b = B("Tue", 2, ["7B1", "7B2"], ["YKM"], "LT9", course="3DP", basket_id="S7-E1")
    assert "H1_batch_clash" not in rules(ds, [a, b])
    c = b.model_copy(update={"basket_id": "S7-E2"})
    assert rules(ds, [a, c])["H1_batch_clash"] == 1


def test_placeholder_faculty_exempt(ds):
    assert "H2_faculty_clash" not in rules(ds, [B("Mon", 3, ["2BBA"], ["HSS2"], "CR5"), B("Mon", 3, ["4BBA"], ["HSS2"], "LT14")])


def test_lunch_hours_saturday(ds):
    c = rules(ds, [B("Mon", 5, ["1B1"], ["RAVI"], "LT1"), B("Tue", 8, ["1B1"], ["RAVI"], "LT1"), B("Sat", 6, ["1B1"], ["RAVI"], "LT1")])
    assert c["H4_lunch"] == 1 and c["H5_outside_hours"] == 1 and c["H5_saturday_cutoff"] == 1


def test_block_integrity_same_day_and_room_suitability(ds):
    lab = next(r for r in ds.requirements if r.kind.value == "P" and r.per_week >= 2)
    b1 = B("Mon", 1, lab.batches, lab.faculty, lab.venue, course=lab.course, kind="P", dur=1, token=lab.token)
    b2 = B("Mon", 3, lab.batches, lab.faculty, "CR2", course=lab.course, kind="P", dur=2, token=lab.token)
    c = rules(ds, [b1, b2])
    assert c["H6_block_integrity"] == 1 and c["H7_same_day_repeat"] == 1 and c["H8_room_unsuitable"] == 1


def test_allocation(ds):
    r = ds.requirements[0]
    v = validate(ds, [], scope_tokens={r.token})
    assert not v["valid"] and v["allocation"]["under_allocated"] == 1
