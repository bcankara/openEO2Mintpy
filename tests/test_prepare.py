"""Tests for openeo2mintpy.prepare module."""

from unittest.mock import patch

import pytest

from openeo2mintpy.prepare import prepare_rsc, prepare_stack

# Mock GDAL metadata for a truly geocoded raster (lat/lon grid).
MOCK_GDAL_META = {
    "WIDTH": "100",
    "LENGTH": "50",
    "NUMBER_BANDS": "1",
    "X_FIRST": "36.0",
    "Y_FIRST": "41.0",
    "X_STEP": "0.001",
    "Y_STEP": "-0.001",
    "DATA_TYPE": "float32",
    "IS_GEOCODED": True,
    "GEOCODED_REASON": "projection present and non-identity geotransform",
    "PROJECTION_WKT": 'GEOGCS["WGS 84", ...]',
}

# Mock metadata for a radar-geometry GeoTIFF (no CRS, identity GT).
MOCK_RADAR_META = {
    "WIDTH": "100",
    "LENGTH": "50",
    "NUMBER_BANDS": "1",
    "X_FIRST": "0.0",
    "Y_FIRST": "0.0",
    "X_STEP": "1.0",
    "Y_STEP": "1.0",
    "DATA_TYPE": "float32",
    "IS_GEOCODED": False,
    "GEOCODED_REASON": "no projection and identity geotransform",
    "PROJECTION_WKT": "",
}


def _read_rsc(path):
    out = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition(" ")
        out[key] = value.strip()
    return out


@patch("openeo2mintpy.prepare.parse_gdal_metadata", return_value=MOCK_GDAL_META)
class TestPrepareRsc:
    """Tests for single-file .rsc generation."""

    def test_interferogram_rsc(self, mock_gdal, tmp_path, radar_params):
        tif = tmp_path / "20240907_20240919.unw.tif"
        tif.write_bytes(b"x")
        rsc = prepare_rsc(tif, "20240907", "20240919", bperp=(-45.0, -47.0),
                          radar_params=radar_params)
        attrs = _read_rsc(rsc)
        assert attrs["ORBIT_DIRECTION"] == "DESCENDING"
        assert float(attrs["HEADING"]) == pytest.approx(-167.9)
        assert float(attrs["CENTER_LINE_UTC"]) == pytest.approx(12933.5)
        assert attrs["DATE12"] == "240907-240919"
        assert float(attrs["P_BASELINE_TOP_HDR"]) == pytest.approx(-45.0)
        assert float(attrs["P_BASELINE_BOTTOM_HDR"]) == pytest.approx(-47.0)
        assert attrs["PROCESSOR"] == "hyp3"
        assert "X_FIRST" in attrs

    def test_scalar_bperp(self, mock_gdal, tmp_path, radar_params):
        tif = tmp_path / "20240907_20240919.unw.tif"
        tif.write_bytes(b"x")
        attrs = _read_rsc(prepare_rsc(tif, "20240907", "20240919", bperp=12.5,
                                      radar_params=radar_params))
        assert float(attrs["P_BASELINE_TOP_HDR"]) == pytest.approx(12.5)
        assert float(attrs["P_BASELINE_BOTTOM_HDR"]) == pytest.approx(12.5)

    def test_interferogram_without_radar_params_raises(self, mock_gdal, tmp_path):
        tif = tmp_path / "20240907_20240919.unw.tif"
        tif.write_bytes(b"x")
        with pytest.raises(ValueError, match="radar parameters"):
            prepare_rsc(tif, "20240907", "20240919", bperp=1.0)

    def test_interferogram_without_baseline_raises(self, mock_gdal, tmp_path, radar_params):
        tif = tmp_path / "20240907_20240919.unw.tif"
        tif.write_bytes(b"x")
        with pytest.raises(ValueError, match="baseline"):
            prepare_rsc(tif, "20240907", "20240919", radar_params=radar_params)

    def test_incomplete_radar_params_raise(self, mock_gdal, tmp_path, radar_params):
        tif = tmp_path / "20240907_20240919.unw.tif"
        tif.write_bytes(b"x")
        del radar_params["HEADING"]
        with pytest.raises(ValueError, match="HEADING"):
            prepare_rsc(tif, "20240907", "20240919", bperp=1.0, radar_params=radar_params)

    def test_dem_without_radar_params(self, mock_gdal, tmp_path):
        tif = tmp_path / "dem.tif"
        tif.write_bytes(b"x")
        attrs = _read_rsc(prepare_rsc(tif, file_type=".dem", is_interferogram=False))
        assert attrs["FILE_TYPE"] == ".dem"
        assert "ORBIT_DIRECTION" not in attrs
        assert "P_BASELINE_TOP_HDR" not in attrs

    def test_missing_file_raises(self, mock_gdal, tmp_path, radar_params):
        with pytest.raises(FileNotFoundError):
            prepare_rsc(tmp_path / "missing.tif", radar_params=radar_params)


@patch("openeo2mintpy.prepare.parse_gdal_metadata", return_value=MOCK_GDAL_META)
class TestPrepareStack:
    """Tests for batch .rsc generation."""

    def _make_files(self, unw_dir, names):
        for n in names:
            (unw_dir / n).write_bytes(b"x")

    def test_empty_directory(self, mock_gdal, tmp_workspace, stack_metadata):
        result = prepare_stack(tmp_workspace["unw_dir"], stack_metadata)
        assert result["rsc_written"] == 0

    def test_pair_baseline_from_metadata(self, mock_gdal, tmp_workspace, stack_metadata):
        unw = tmp_workspace["unw_dir"]
        self._make_files(unw, ["20240919_20241001.unw.tif", "20240919_20241001.cor.tif"])
        result = prepare_stack(unw, stack_metadata)
        assert result["rsc_written"] == 2
        attrs = _read_rsc(unw / "20240919_20241001.unw.tif.rsc")
        assert float(attrs["P_BASELINE_TOP_HDR"]) == pytest.approx(77.0)
        assert float(attrs["P_BASELINE_BOTTOM_HDR"]) == pytest.approx(81.0)
        assert attrs["ORBIT_DIRECTION"] == "DESCENDING"

    def test_date_missing_from_metadata_is_an_error(self, mock_gdal, tmp_workspace,
                                                    stack_metadata):
        unw = tmp_workspace["unw_dir"]
        self._make_files(unw, ["20240919_20250101.unw.tif"])
        result = prepare_stack(unw, stack_metadata)
        assert result["rsc_written"] == 0
        assert "baseline" in result["errors"][0]["error"]

    def test_geometry_files(self, mock_gdal, tmp_workspace, stack_metadata):
        unw = tmp_workspace["unw_dir"]
        geom = tmp_workspace["root"] / "geometry"
        geom.mkdir()
        self._make_files(unw, ["20240907_20240919.unw.tif"])
        for n in ("dem.tif", "incidenceAngle.tif", "azimuthAngle.tif"):
            (geom / n).write_bytes(b"x")
        result = prepare_stack(unw, stack_metadata, geometry_dir=geom)
        assert result["rsc_written"] == 4
        assert _read_rsc(geom / "incidenceAngle.tif.rsc")["FILE_TYPE"] == ".inc"
        assert _read_rsc(geom / "azimuthAngle.tif.rsc")["FILE_TYPE"] == ".az"
        assert _read_rsc(geom / "dem.tif.rsc")["FILE_TYPE"] == ".dem"

    def test_progress_callback(self, mock_gdal, tmp_workspace, stack_metadata):
        unw = tmp_workspace["unw_dir"]
        self._make_files(unw, ["20240907_20240919.unw.tif", "20240907_20241001.unw.tif"])
        calls = []
        prepare_stack(unw, stack_metadata, progress_callback=lambda c, t: calls.append((c, t)))
        assert calls == [(1, 2), (2, 2)]


class TestGeometryMode:
    """Tests for the geometry_mode switch."""

    def test_auto_detects_radar_omits_geotransform(self, tmp_path, radar_params):
        tif = tmp_path / "20240907_20240919.unw.tif"
        tif.write_bytes(b"x")
        with patch("openeo2mintpy.prepare.parse_gdal_metadata", return_value=MOCK_RADAR_META):
            attrs = _read_rsc(prepare_rsc(tif, "20240907", "20240919", bperp=1.0,
                                          radar_params=radar_params))
        assert "X_FIRST" not in attrs

    def test_force_radar_overrides_detection(self, tmp_path, radar_params):
        tif = tmp_path / "20240907_20240919.unw.tif"
        tif.write_bytes(b"x")
        with patch("openeo2mintpy.prepare.parse_gdal_metadata", return_value=MOCK_GDAL_META):
            attrs = _read_rsc(prepare_rsc(tif, "20240907", "20240919", bperp=1.0,
                                          radar_params=radar_params, geometry_mode="radar"))
        assert "X_FIRST" not in attrs

    def test_force_geo_overrides_detection(self, tmp_path, radar_params):
        tif = tmp_path / "20240907_20240919.unw.tif"
        tif.write_bytes(b"x")
        with patch("openeo2mintpy.prepare.parse_gdal_metadata", return_value=MOCK_RADAR_META):
            attrs = _read_rsc(prepare_rsc(tif, "20240907", "20240919", bperp=1.0,
                                          radar_params=radar_params, geometry_mode="geo"))
        assert "X_FIRST" in attrs

    def test_invalid_mode_raises(self, tmp_path, radar_params):
        tif = tmp_path / "20240907_20240919.unw.tif"
        tif.write_bytes(b"x")
        with patch("openeo2mintpy.prepare.parse_gdal_metadata", return_value=MOCK_GDAL_META):
            with pytest.raises(ValueError):
                prepare_rsc(tif, "20240907", "20240919", bperp=1.0,
                            radar_params=radar_params, geometry_mode="bogus")

    def test_stack_rejects_invalid_mode(self, tmp_workspace, stack_metadata):
        with pytest.raises(ValueError):
            prepare_stack(tmp_workspace["unw_dir"], stack_metadata, geometry_mode="bogus")
