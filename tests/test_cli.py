"""Tests for openeo2mintpy.cli module."""

import json
from unittest.mock import patch

import pytest

from openeo2mintpy import __version__
from openeo2mintpy.cli import main


def _burst(date, burst_id=28163, track=14, swath="IW2", platform="A"):
    return {
        "BeginningDateTime": f"{date}T15:35:21.0Z",
        "RelativeOrbitNumber": track,
        "BurstId": burst_id,
        "SwathIdentifier": swath,
        "PlatformSerialIdentifier": platform,
        "OrbitDirection": "ASCENDING",
        "GeoFootprint": None,
    }


class TestCli:
    """Tests for CLI argument parsing and dispatch."""

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert __version__ in captured.out

    def test_no_args_launches_gui(self):
        with patch("openeo2mintpy.cli._cmd_gui") as mock_gui:
            main([])
            mock_gui.assert_called_once()

    def test_gui_subcommand(self):
        with patch("openeo2mintpy.cli._cmd_gui") as mock_gui:
            main(["gui"])
            mock_gui.assert_called_once()

    def test_prepare_requires_metadata(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(["prepare", "--unw-dir", str(tmp_path)])
        assert exc_info.value.code != 0

    def test_generate_config_requires_args(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["generate-config"])
        assert exc_info.value.code != 0

    def test_info_requires_unw_dir(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["info"])
        assert exc_info.value.code != 0

    def test_prepare_rejects_invalid_geometry_mode(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(["prepare", "--unw-dir", str(tmp_path), "--metadata", "m.json",
                  "--geometry-mode", "bogus"])
        assert exc_info.value.code != 0

    def test_prepare_passes_metadata(self, tmp_path):
        with patch("openeo2mintpy.prepare.prepare_stack") as mock_prep:
            mock_prep.return_value = {
                "rsc_written": 0, "errors": [], "skipped": 0,
                "details": [], "geometry_mode": "geo",
            }
            main(["prepare", "--unw-dir", str(tmp_path), "--metadata", "meta.json",
                  "--geometry-mode", "geo"])
            kwargs = mock_prep.call_args.kwargs
            assert kwargs["geometry_mode"] == "geo"
            assert kwargs["stack_metadata"] == "meta.json"

    def test_align_rejects_invalid_method(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["align", "--unw-dir", str(tmp_path), "--method", "bogus"])


class TestHeadless:
    """Tests for the openEO request commands."""

    def test_search_lists_bursts(self, capsys):
        records = [_burst("2025-11-25"), _burst("2025-12-01", platform="C"),
                   _burst("2025-11-25", burst_id=28164)]
        with patch("openeo2mintpy.openeo_client.query_burst_acquisitions",
                   return_value=records):
            main(["search", "--start", "2025-11-20", "--end", "2025-12-05",
                  "--bbox", "35.3", "40.8", "35.6", "41.0"])
        out = capsys.readouterr().out
        assert "28163" in out and "28164" in out

    def test_submit_dry_run_writes_pairs(self, tmp_path):
        records = [_burst(d) for d in ("2025-11-25", "2025-12-07", "2025-12-19", "2026-01-24")]
        manifest = tmp_path / "jobs.json"
        with patch("openeo2mintpy.openeo_client.query_burst_acquisitions",
                   return_value=records), \
             patch("openeo2mintpy.openeo_client.connect_and_auth") as auth:
            main(["submit", "--start", "2025-11-20", "--end", "2026-02-01",
                  "--bbox", "35.3", "40.8", "35.6", "41.0", "--track", "14",
                  "--burst-id", "28163", "--sub-swath", "IW2",
                  "--max-temporal-baseline", "24", "--manifest", str(manifest), "--dry-run"])
            auth.assert_not_called()
        doc = json.loads(manifest.read_text())
        assert doc["jobs"] == []
        assert doc["request"]["direction"] == "ascending"
        assert ["2025-11-25", "2025-12-07"] in doc["request"]["pairs"]
        assert ["2025-12-19", "2026-01-24"] not in doc["request"]["pairs"]

    def test_submit_bbox_must_be_ordered(self, tmp_path):
        with pytest.raises(ValueError):
            main(["submit", "--start", "2025-11-20", "--end", "2026-02-01",
                  "--bbox", "35.6", "40.8", "35.3", "41.0", "--track", "14",
                  "--burst-id", "28163", "--sub-swath", "IW2",
                  "--manifest", str(tmp_path / "m.json"), "--dry-run"])
