"""Tests for openeo2mintpy.acquisitions."""

import datetime as dt
import json
from unittest.mock import patch

import pytest

from openeo2mintpy.acquisitions import (
    build_acquisition_table,
    match_acquisitions,
    parse_acquisition_times,
    read_job_context,
)


def _record(start, platform="A", direction="ASCENDING"):
    return {
        "BeginningDateTime": start,
        "AzimuthTime": start.replace(":21.", ":20."),
        "PlatformSerialIdentifier": platform,
        "OrbitDirection": direction,
        "RelativeOrbitNumber": 14,
        "BurstId": 28163,
        "SwathIdentifier": "IW2",
        "ParentProductName": f"S1{platform}_IW_SLC__1SDV_X.SAFE",
    }


@pytest.fixture
def raw_dir(tmp_path):
    for name in ("phase_coh_20251125T153521_20251201T153418.tif",
                 "phase_coh_20251125T153521_20251207T153520.tif"):
        (tmp_path / name).write_bytes(b"x")
    job = {"providers": [{"processing:expression": {"expression": {
        "runcwltostac1": {"process_id": "run_cwl_to_stac",
                          "arguments": {"context": {"burst_id": 28163, "sub_swath": "IW2",
                                                    "polarization": "vv", "n_rg_looks": 4}}},
        "export": {"process_id": "export_workspace", "arguments": {}}}}}]}
    (tmp_path / "job-results.json").write_text(json.dumps(job))
    return tmp_path


class TestParsing:
    def test_times_from_file_names(self, raw_dir):
        times = parse_acquisition_times(raw_dir)
        assert times == {
            "20251125": dt.datetime(2025, 11, 25, 15, 35, 21),
            "20251201": dt.datetime(2025, 12, 1, 15, 34, 18),
            "20251207": dt.datetime(2025, 12, 7, 15, 35, 20),
        }

    def test_conflicting_times_raise(self, raw_dir):
        (raw_dir / "phase_coh_20251125T153530_20251213T153418.tif").write_bytes(b"x")
        with pytest.raises(ValueError, match="two start times"):
            parse_acquisition_times(raw_dir)

    def test_job_context(self, raw_dir):
        ctx = read_job_context(raw_dir)
        assert ctx["burst_id"] == 28163
        assert ctx["sub_swath"] == "IW2"

    def test_job_context_missing(self, tmp_path):
        assert read_job_context(tmp_path) == {}

    def test_job_context_published_process(self, tmp_path):
        # Layout of job-results.json for a job that calls sentinel1_sar_interferogram.
        job = {"providers": [{"processing:expression": {"format": "openeo", "expression": {
            "saveresult1": {"process_id": "save_result", "result": True,
                            "arguments": {"data": {"from_node": "sentinel1sarinterferogram1"},
                                          "format": "GTiff", "options": {}}},
            "sentinel1sarinterferogram1": {
                "process_id": "sentinel1_sar_interferogram",
                "namespace": "https://example.org/sentinel1_sar_interferogram.json",
                "arguments": {"InSAR_pairs": [["2025-11-25", "2025-12-01"]], "burst_id": 28163,
                              "coherence_window_az": 2, "coherence_window_rg": 10,
                              "n_az_looks": 1, "n_rg_looks": 4, "polarization": "VV",
                              "sub_swath": "IW2"}}}}}]}
        (tmp_path / "job-metadata.json").write_text(json.dumps([1, 2]))
        (tmp_path / "job-results.json").write_text(json.dumps(job))
        ctx = read_job_context(tmp_path)
        assert ctx["burst_id"] == 28163
        assert ctx["sub_swath"] == "IW2"
        assert ctx["polarization"] == "VV"
        assert ctx["n_rg_looks"] == 4


class TestMatching:
    def test_match(self):
        times = {"20251125": dt.datetime(2025, 11, 25, 15, 35, 21)}
        records = [_record("2025-11-25T15:35:21.204735Z"), _record("2025-12-07T15:35:20.1Z")]
        table = match_acquisitions(times, records)
        assert table[0]["platform"] == "S1A"
        assert table[0]["orbit_direction"] == "ASCENDING"
        assert table[0]["azimuth_time"].startswith("2025-11-25T15:35:20")

    def test_missing_record_raises(self):
        times = {"20251125": dt.datetime(2025, 11, 25, 15, 35, 21)}
        with pytest.raises(LookupError):
            match_acquisitions(times, [_record("2025-11-25T15:40:00.0Z")])


class TestBuildTable:
    def test_uses_job_context(self, raw_dir):
        records = [
            _record("2025-11-25T15:35:21.2Z"),
            _record("2025-12-01T15:34:18.6Z", platform="C"),
            _record("2025-12-07T15:35:20.1Z"),
        ]
        with patch("openeo2mintpy.acquisitions.query_burst_records",
                   return_value=records) as q:
            table = build_acquisition_table(raw_dir)
        assert q.call_args.args[:2] == (28163, "IW2")
        assert [r["platform"] for r in table] == ["S1A", "S1C", "S1A"]

    def test_mixed_directions_raise(self, raw_dir):
        records = [
            _record("2025-11-25T15:35:21.2Z"),
            _record("2025-12-01T15:34:18.6Z", direction="DESCENDING"),
            _record("2025-12-07T15:35:20.1Z"),
        ]
        with patch("openeo2mintpy.acquisitions.query_burst_records", return_value=records):
            with pytest.raises(ValueError, match="mixes orbit directions"):
                build_acquisition_table(raw_dir)

    def test_unknown_burst_raises(self, tmp_path):
        (tmp_path / "phase_coh_20251125T153521_20251201T153418.tif").write_bytes(b"x")
        with pytest.raises(ValueError, match="Burst id"):
            build_acquisition_table(tmp_path)
