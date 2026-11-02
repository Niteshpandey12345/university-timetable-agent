"""Parsing and inference on the REAL workbook."""
from ttagent.domain.timegrid import TimeGrid
from ttagent.ingestion.token_parser import batch_parts, parse_token, room_category


def test_token_basic():
    t = parse_token("1B1,1B2:P-CPLAB@CL3_REENU,RP")
    assert t.batches == ["1B1", "1B2"] and t.kind == "P" and t.course == "CPLAB"
    assert t.venue == "CL3" and t.faculty == ["REENU", "RP"] and not t.recovered


def test_token_annotations_are_stripped_and_reported():
    for raw, fac in [("1B7,1B8,1B9:L-CP@LT8_REENU_Extra", ["REENU"]), ("1B13,1B14:L-CP@LT8_RAVI-EXTRA", ["RAVI"]),
                     ("4BBA:L-HHS1@LT14_HSS2_CR17", ["HSS2"]), ("1B4:T-CP@CR4_DuMKA", ["DUMKA"])]:
        t = parse_token(raw)
        assert t.faculty == fac and t.notes, raw


def test_malformed_token_is_recovered_with_warning():
    t = parse_token("7B1,7B2,7B3-AW@LT11_KLD")
    assert t.recovered and t.kind == "L" and t.batches == ["7B1", "7B2", "7B3"]


def test_junk_is_rejected():
    assert parse_token(".") is None and parse_token(None) is None


def test_batch_and_room_helpers():
    assert batch_parts("3B21") == (3, "B", "21") and batch_parts("2BBA")[1] == "BBA" and batch_parts("BBA")[0] is None
    assert room_category("LT4")[0] == "lecture_theatre" and room_category("PHYLAB2") == ("special_lab", "PHYLAB")


def test_requirement_table(ds):
    assert len(ds.requirements) == 348
    assert sum(r.weekly_hours for r in ds.requirements) == 674
    assert sum(r.per_week for r in ds.requirements) == 567
    assert ds.semesters() == [1, 3, 5, 7]


def test_catalogues(ds):
    assert len(ds.rooms) == 38
    assert ds.faculty["HSS2"].is_placeholder and not ds.faculty["RAVI"].is_placeholder
    assert {b.code for b in ds.batches.values() if not b.in_requirements} >= {"2BBA", "4BBA", "2M1"}
    assert len(ds.fixed_bookings) > 50


def test_elective_baskets_inferred(ds):
    baskets = {tuple(b.courses) for b in ds.baskets}
    assert ("3DP", "ITS", "KM", "OR") in baskets and ("AI", "PEGAI") in baskets and ("IQC", "MSA") in baskets
    assert len(ds.baskets) == 7


def test_macro_bookkeeping_and_published_clashes(ds):
    q = ds.quality
    assert q["vba_macro_bookkeeping"]["requirements_not_fully_scheduled"] == 40
    msgs = " ".join(v["message"] for v in q["published_grid_validation"]["violations"])
    for who in ("KBH", "PP", "SMB"):
        assert f"faculty {who} double-booked" in msgs


def test_capacity_proof(ds):
    from ttagent.engine.diagnosis import capacity_analysis
    strict = capacity_analysis(ds, basket_aware=False)
    aware = capacity_analysis(ds, basket_aware=True)
    assert strict["provably_infeasible"] and any(x.get("batch", "").startswith("7B") for x in strict["infeasible_resources"])
    assert not aware["provably_infeasible"]


def test_timegrid_rules():
    g = TimeGrid.from_settings()
    assert g.slots_for("Mon") == [1, 2, 3, 4, 6, 7] and g.slots_for("Sat") == [1, 2, 3, 4]
    assert g.starts_for("Mon", 2) == [1, 2, 3, 6]          # labs never straddle lunch
    assert g.starts_for("Sat", 2) == [1, 2, 3]
    assert g.capacity_hours() == 34
    assert 7 in g.with_extended(True).starts_for("Mon", 2)
