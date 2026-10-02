"""Tests for openeo2mintpy.align."""

import json

import numpy as np
import pytest

gdal = pytest.importorskip("osgeo.gdal")
osr = pytest.importorskip("osgeo.osr")
osr.UseExceptions()  # as the package does for gdal; silences the GDAL 4.0 FutureWarning

from openeo2mintpy.align import align_rasters, on_common_lattice, prepare_dem  # noqa: E402

RES = 0.0001796630568239043


def _write(path, data, x0, y0, res=RES):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), data.shape[1], data.shape[0], 1,
                                              gdal.GDT_Float32)
    ds.SetGeoTransform((x0, res, 0.0, y0, 0.0, -res))
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(data)
    ds = None


def _read(path):
    ds = gdal.Open(str(path))
    out = ds.GetRasterBand(1).ReadAsArray(), ds.GetGeoTransform()
    ds = None
    return out


@pytest.fixture
def lattice_stack(tmp_path):
    """Three rasters cut from one lattice with different extents."""
    rng = np.random.default_rng(1)
    full = rng.normal(size=(40, 60)).astype(np.float32)
    x0, y0 = 35.0, 41.0
    cuts = {"20240101_20240113": (0, 0, 40, 55), "20240101_20240125": (2, 3, 35, 60),
            "20240113_20240125": (1, 5, 38, 58)}
    for name, (r0, c0, r1, c1) in cuts.items():
        _write(tmp_path / f"{name}.unw.tif", full[r0:r1, c0:c1] + 0.0,
               x0 + c0 * RES, y0 - r0 * RES)
    return tmp_path, full, x0, y0


class TestLattice:
    def test_detects_common_lattice(self):
        g = {"xmin": 35.0, "xres": RES, "ymax": 41.0, "yres": -RES}
        h = dict(g, xmin=35.0 + 7 * RES, ymax=41.0 - 3 * RES)
        assert on_common_lattice([g, h])
        assert not on_common_lattice([g, dict(h, xmin=35.0 + 7.5 * RES)])
        assert not on_common_lattice([g, dict(h, xres=RES * 1.001)])


class TestAlign:
    def test_crop_is_value_preserving(self, lattice_stack):
        d, full, x0, y0 = lattice_stack
        result = align_rasters(d)
        assert result["method"] == "crop"
        assert result["common_lattice"] is True
        assert result["target"]["width"] == 50 and result["target"]["height"] == 33
        for f in sorted(d.glob("*.unw.tif")):
            data, gt = _read(f)
            assert data.shape == (33, 50)
            assert gt[0] == pytest.approx(x0 + 5 * RES, abs=1e-12)
            assert gt[3] == pytest.approx(y0 - 2 * RES, abs=1e-12)
            assert np.array_equal(data, full[2:35, 5:55])
        report = json.loads((d / "alignment_report.json").read_text())
        assert report["method"] == "crop"

    def test_half_pixel_offset_falls_back_to_warp(self, tmp_path):
        a = np.ones((20, 20), dtype=np.float32)
        _write(tmp_path / "20240101_20240113.unw.tif", a, 35.0, 41.0)
        _write(tmp_path / "20240101_20240125.unw.tif", a, 35.0 + 0.5 * RES, 41.0)
        result = align_rasters(tmp_path)
        assert result["method"] == "warp"
        assert result["target"]["resample_alg"] == "bilinear"

    def test_crop_refuses_misaligned(self, tmp_path):
        a = np.ones((20, 20), dtype=np.float32)
        _write(tmp_path / "20240101_20240113.unw.tif", a, 35.0, 41.0)
        _write(tmp_path / "20240101_20240125.unw.tif", a, 35.0 + 0.5 * RES, 41.0)
        with pytest.raises(RuntimeError, match="common pixel lattice"):
            align_rasters(tmp_path, method="crop")

    def test_invalid_method(self, tmp_path):
        with pytest.raises(ValueError):
            align_rasters(tmp_path, method="bogus")


def test_prepare_dem_accepts_file_or_directory(tmp_path):
    unw = tmp_path / "unw"
    unw.mkdir()
    _write(unw / "20240101_20240113.unw.tif", np.zeros((20, 30), np.float32), 35.0, 41.0)
    src = tmp_path / "src"
    src.mkdir()
    yy, xx = np.mgrid[0:60, 0:80]
    _write(src / "dem.tif", (100.0 + yy + 0.5 * xx).astype(np.float32),
           35.0 - 10 * RES, 41.0 + 10 * RES)

    from_file = prepare_dem(unw, src / "dem.tif", tmp_path / "a" / "dem.tif")
    from_dir = prepare_dem(unw, src, tmp_path / "b" / "dem.tif")
    a, gt_a = _read(from_file)
    b, gt_b = _read(from_dir)
    assert a.shape == (20, 30)
    assert gt_a == gt_b
    np.testing.assert_array_equal(a, b)
