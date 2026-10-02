"""Tests for openeo2mintpy.geometry."""

import datetime as dt
import json

import numpy as np
import pytest

from openeo2mintpy.geometry import (
    WGS84_A,
    WGS84_E2,
    heading_at,
    llh_to_ecef,
    los_angles,
    perpendicular_baseline,
)


def mintpy_azimuth_from_heading(heading):
    """MintPy's heading2azimuth_angle for right-looking sensors."""
    az = (heading - 90.0) * -1.0
    return az - np.round(az / 360.0) * 360.0


class TestFrames:
    def test_equator_and_pole(self):
        assert np.allclose(llh_to_ecef(np.array(0.0), np.array(0.0), np.array(0.0)),
                           [WGS84_A, 0.0, 0.0])
        b = WGS84_A * np.sqrt(1 - WGS84_E2)
        assert np.allclose(llh_to_ecef(np.array(90.0), np.array(0.0), np.array(0.0)),
                           [0.0, 0.0, b], atol=1e-6)

    def test_heading_north(self):
        pos = np.array([7.0e6, 0.0, 0.0])
        assert heading_at(pos, np.array([0.0, 0.0, 7500.0])) == pytest.approx(0.0, abs=1e-9)
        assert heading_at(pos, np.array([0.0, 0.0, -7500.0])) == pytest.approx(180.0)


class TestLosAngles:
    def _target(self):
        return llh_to_ecef(np.array(0.0), np.array(0.0), np.array(0.0))

    def test_ascending_right_looking(self):
        target = self._target()
        east, up = np.array([0.0, 1.0, 0.0]), np.array([1.0, 0.0, 0.0])
        sat = target + 700e3 * up - 500e3 * east
        inc, az = los_angles(target, sat, np.array(0.0), np.array(0.0))
        assert inc == pytest.approx(np.degrees(np.arctan2(500.0, 700.0)))
        assert az == pytest.approx(90.0)
        assert az == pytest.approx(mintpy_azimuth_from_heading(0.0))

    def test_descending_right_looking(self):
        target = self._target()
        east, up = np.array([0.0, 1.0, 0.0]), np.array([1.0, 0.0, 0.0])
        sat = target + 700e3 * up + 500e3 * east
        _, az = los_angles(target, sat, np.array(0.0), np.array(0.0))
        assert az == pytest.approx(-90.0)
        assert az == pytest.approx(mintpy_azimuth_from_heading(180.0))


class TestPerpendicularBaseline:
    def test_isce2_sign_convention(self):
        target = np.array([0.0, 0.0, 0.0])
        ref = np.array([0.0, 0.0, 1000.0])
        sec = ref + np.array([10.0, 0.0, 5.0])
        bperp, bpar = perpendicular_baseline(target, ref, np.array([0.0, 1.0, 0.0]), sec)
        assert bpar == pytest.approx(-5.0)
        assert bperp == pytest.approx(-10.0)
        bperp_rev, _ = perpendicular_baseline(target, ref, np.array([0.0, -1.0, 0.0]), sec)
        assert bperp_rev == pytest.approx(10.0)

    def test_zero_for_identical_positions(self):
        target = np.array([0.0, 0.0, 0.0])
        ref = np.array([0.0, 0.0, 1000.0])
        bperp, bpar = perpendicular_baseline(target, ref, np.array([0.0, 1.0, 0.0]), ref)
        assert bperp == 0.0
        assert bpar == 0.0


@pytest.fixture
def synthetic_stack(tmp_path, circular_orbit):
    gdal = pytest.importorskip("osgeo.gdal")
    osr = pytest.importorskip("osgeo.osr")
    osr.UseExceptions()  # as the package does for gdal; silences the GDAL 4.0 FutureWarning
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    gt = (0.0, 0.0005, 0.0, 0.01, 0.0, -0.0005)

    def write(path, data):
        ds = gdal.GetDriverByName("GTiff").Create(str(path), data.shape[1], data.shape[0], 1,
                                                  gdal.GDT_Float32)
        ds.SetGeoTransform(gt)
        ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(data)
        ds = None

    grid_tif = tmp_path / "20251125_20251201.unw.tif"
    dem_tif = tmp_path / "dem.tif"
    write(grid_tif, np.zeros((20, 40), dtype=np.float32))
    write(dem_tif, np.full((20, 40), 150.0, dtype=np.float32))

    ref_orbit, _, epoch, _ = circular_orbit(radius=7_070_000.0)
    sec_orbit, *_ = circular_orbit(radius=7_070_100.0)
    common = {"orbit_direction": "ASCENDING", "platform": "S1A", "burst_id": 1,
              "sub_swath": "IW2", "relative_orbit": 14, "start_time": epoch.isoformat(),
              "azimuth_time": epoch.isoformat(), "parent_product": ""}
    table = [dict(common, date="20251125"), dict(common, date="20251201")]
    orbits = {"20251125": (ref_orbit, "ref.EOF"), "20251201": (sec_orbit, "sec.EOF")}

    input_dir = tmp_path / "raw"
    input_dir.mkdir()
    job = {"providers": [{"processing:expression": {"expression": {"n": {
        "process_id": "run_cwl_to_stac",
        "arguments": {"context": {"n_rg_looks": 4, "n_az_looks": 1}}}}}}]}
    (input_dir / "job-results.json").write_text(json.dumps(job))
    return grid_tif, dem_tif, table, orbits, input_dir, epoch


class TestStackGeometry:
    def test_compute_and_write(self, tmp_path, synthetic_stack):
        from openeo2mintpy.geometry import (
            compute_stack_geometry,
            load_stack_metadata,
            write_stack_metadata,
        )

        grid_tif, dem_tif, table, orbits, input_dir, epoch = synthetic_stack
        result = compute_stack_geometry(grid_tif, dem_tif, table, orbits, row_chunk=7,
                                        baseline_step=10)
        assert result["incidence"].shape == (20, 40)
        assert np.all((result["incidence"] > 20) & (result["incidence"] < 60))
        assert np.all(np.abs(result["azimuth"] - 90.0) < 5.0)
        assert np.all(np.diff(result["incidence"], axis=1) > 0)

        scene = result["scene"]
        assert scene["ORBIT_DIRECTION"] == "ASCENDING"
        assert scene["HEADING"] == pytest.approx(0.0, abs=0.5)
        midnight = dt.datetime.combine(epoch.date(), dt.time.min)
        assert scene["CENTER_LINE_UTC"] == pytest.approx((epoch - midnight).total_seconds(),
                                                         abs=5.0)
        assert scene["HEIGHT"] == pytest.approx(7_070_000.0 - scene["EARTH_RADIUS"], abs=1.0)

        acq = {a["date"]: a for a in result["acquisitions"]}
        assert acq["20251125"]["bperp_mean"] == 0.0
        assert 10.0 < abs(acq["20251201"]["bperp_mean"]) < 100.0
        assert acq["20251201"]["bpar_mean"] != 0.0

        path = write_stack_metadata(result, tmp_path / "geom", input_dir, dem_tif)
        meta = load_stack_metadata(path)
        assert meta["radar"]["RLOOKS"] == 4
        assert meta["radar"]["RANGE_PIXEL_SIZE"] == pytest.approx(4 * 2.329562)
        assert set(meta["baselines"]) == {"20251125", "20251201"}
        assert meta["snap_crosscheck"]["products_with_snap_header"] == 0
        assert meta["snap_crosscheck"]["pairs_compared"] == 0
        assert (tmp_path / "geom" / "incidenceAngle.tif").exists()
        assert (tmp_path / "geom" / "azimuthAngle.tif").exists()

    def test_dem_grid_mismatch_raises(self, tmp_path, synthetic_stack):
        from osgeo import gdal

        from openeo2mintpy.geometry import compute_stack_geometry

        grid_tif, _, table, orbits, *_ = synthetic_stack
        bad = tmp_path / "bad_dem.tif"
        ds = gdal.GetDriverByName("GTiff").Create(str(bad), 10, 10, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((0.0, 0.0005, 0.0, 0.01, 0.0, -0.0005))
        ds = None
        with pytest.raises(ValueError, match="differs from the stack grid"):
            compute_stack_geometry(grid_tif, bad, table, orbits)
