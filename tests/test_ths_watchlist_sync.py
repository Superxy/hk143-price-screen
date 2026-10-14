"""Offline regression tests for the verified 同花顺远航版 XML exporter."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ths_watchlist_sync import ExportError, normalize_code, read_daily_candidates, render_group_xml, run

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config" / "ths_blockstock_template.xml"


def make_report(codes):
    return {
        "meta": {"target_session_hkt": "2026-10-14", "candidate_count": len(codes), "status": "complete_date_coverage"},
        "ranked_candidates": [
            {"rank": i, "code": code, "latest_date": "2026-10-14", "data_status": "fresh"}
            for i, code in enumerate(codes, start=1)
        ],
    }


def test_hk_codes_with_leading_zeros():
    assert [normalize_code(c) for c in ("01833", "00753", "00020", "01810", "09866")] == [
        "HK1833", "HK0753", "HK0020", "HK1810", "HK9866"
    ]
    for invalid in ("", "HK1810", "00000", "123456", "18.10"):
        with pytest.raises(ExportError):
            normalize_code(invalid)


def test_verified_template_preserves_other_groups():
    xml = render_group_xml(TEMPLATE, ["HK0753", "HK0020", "HK1810"])
    root = ET.fromstring(xml)
    assert root.attrib == {"version": "36", "sort_list": "23,24,"}
    group = next(b for b in root.findall("Block") if b.get("id") == "35")
    assert group.get("name") == "港股低价自选"
    assert [(s.get("market"), s.get("code")) for s in group.findall("security")] == [
        ("UHKM", "HK0753"), ("UHKM", "HK0020"), ("UHKM", "HK1810")
    ]
    other = next(b for b in root.findall("Block") if b.get("id") == "36")
    assert other.attrib == {"name": "A股低价自选", "id": "36"}
    assert list(other) == []


def test_exports_latest_and_history(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ths_blockstock_template.xml").write_bytes(TEMPLATE.read_bytes())
    report = make_report(["01833", "00753", "00020", "03896"])
    (tmp_path / "data" / "latest.json").write_text(json.dumps(report), encoding="utf-8")
    session, count, out = run(tmp_path)
    assert (session, count) == ("2026-10-14", 4)
    archive = out.parent / "history" / "2026-10-14_blockstockV3.xml"
    assert out.read_bytes() == archive.read_bytes()
    codes = [x.attrib["code"] for x in ET.parse(out).findall("./Block[@id='35']/security")]
    assert codes == ["HK1833", "HK0753", "HK0020", "HK3896"]


def test_refuse_stale_or_empty_or_duplicate_without_overwriting(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ths_blockstock_template.xml").write_bytes(TEMPLATE.read_bytes())
    latest = tmp_path / "data" / "latest.json"
    output = tmp_path / "data" / "ths" / "blockstockV3.xml"
    output.parent.mkdir()
    output.write_text("keep-me", encoding="utf-8")
    for report in (make_report([]), make_report(["01810", "01810"]),
                   {**make_report(["01810"]), "meta": {**make_report(["01810"])["meta"], "status": "partial"}}):
        latest.write_text(json.dumps(report), encoding="utf-8")
        with pytest.raises(ExportError):
            run(tmp_path)
        assert output.read_text(encoding="utf-8") == "keep-me"


def test_detect_stale_rows_or_unexpected_template(tmp_path):
    latest = tmp_path / "latest.json"
    report = make_report(["01810"])
    report["ranked_candidates"][0]["latest_date"] = "2026-10-13"
    latest.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ExportError):
        read_daily_candidates(latest)
    wrong = tmp_path / "wrong.xml"
    wrong.write_text('<hevo><Block name="foo" id="35"/></hevo>', encoding="utf-8")
    with pytest.raises(ExportError):
        render_group_xml(wrong, ["HK1810"])
