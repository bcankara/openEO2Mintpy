"""Tests for openeo2mintpy.metadata module."""


import pytest

from openeo2mintpy.metadata import (
    _detect_geocoded,
    _is_default_geotransform,
    auto_detect_ref_date,
    compute_bperp_pair,
    extract_dates_from_filename,
)


class TestComputeBperpPair:
    """Tests for Bperp pair computation."""

    def test_compute_valid_pair(self):
        baselines = {"20240101": 0.0, "20240201": 50.0, "20240301": -30.0}
        # Bperp(d1, d2) = Bperp(ref, d2) - Bperp(ref, d1)
        assert compute_bperp_pair(baselines, "20240201", "20240301") == pytest.approx(-80.0)

    def test_missing_date_returns_none(self):
        baselines = {"20240101": 0.0}
        assert compute_bperp_pair(baselines, "20240101", "20240201") is None

    def test_top_bottom_pairs(self):
        baselines = {"20240101": (0.0, 0.0), "20240201": (50.0, 52.0), "20240301": (-30.0, -29.0)}
        top, bottom = compute_bperp_pair(baselines, "20240201", "20240301")
        assert top == pytest.approx(-80.0)
        assert bottom == pytest.approx(-81.0)


class TestExtractDates:
    """Tests for date extraction from filenames."""

    def test_standard_dolphin_filename(self):
        result = extract_dates_from_filename("20240907_20241001.unw.tif")
        assert result == ("20240907", "20241001")

    def test_with_prefix(self):
        result = extract_dates_from_filename("phase_20240907_20241001.unw.tif")
        assert result == ("20240907", "20241001")

    def test_no_dates(self):
        result = extract_dates_from_filename("dem.tif")
        assert result is None

    def test_single_date(self):
        result = extract_dates_from_filename("20240907.tif")
        assert result is None


class TestAutoDetectRefDate:
    """Tests for automatic reference date detection."""

    def test_detects_most_frequent(self, tmp_path):
        for d in ("20240907", "20241001", "20241013"):
            (tmp_path / f"20240919_{d}.unw.tif").write_bytes(b"x")
        (tmp_path / "20240907_20241001.unw.tif").write_bytes(b"x")
        assert auto_detect_ref_date(tmp_path) == "20240919"

    def test_empty_dir_returns_none(self, tmp_path):
        empty_dir = tmp_path / "empty_baselines"
        empty_dir.mkdir()
        assert auto_detect_ref_date(empty_dir) is None

    def test_nonexistent_dir_returns_none(self):
        assert auto_detect_ref_date("/nonexistent/path") is None


class TestDetectGeocoded:
    """Tests for the projection + geotransform geocoded detector."""

    def test_identity_geotransform_is_default(self):
        assert _is_default_geotransform((0.0, 1.0, 0.0, 0.0, 0.0, 1.0)) is True

    def test_non_identity_geotransform(self):
        assert _is_default_geotransform((36.0, 0.001, 0, 41.0, 0, -0.001)) is False

    def test_none_geotransform_treated_as_default(self):
        assert _is_default_geotransform(None) is True

    def test_real_geocoded(self):
        wkt = 'GEOGCS["WGS 84",...]'
        gt = (36.0, 0.001, 0.0, 41.0, 0.0, -0.001)
        is_geo, reason = _detect_geocoded(wkt, gt)
        assert is_geo is True
        assert "projection" in reason

    def test_dolphin_radar_geometry(self):
        is_geo, reason = _detect_geocoded("", (0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
        assert is_geo is False
        assert "identity" in reason

    def test_projection_without_real_geotransform(self):
        is_geo, _ = _detect_geocoded(
            'GEOGCS["WGS 84",...]', (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        )
        assert is_geo is False

    def test_geotransform_without_projection(self):
        is_geo, _ = _detect_geocoded("", (36.0, 0.001, 0.0, 41.0, 0.0, -0.001))
        assert is_geo is False
