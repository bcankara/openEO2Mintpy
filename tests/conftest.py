"""Shared test fixtures for openeo2mintpy tests."""

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pytest

GM = 3.986004418e14


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace with realistic directory structure."""
    unw_dir = tmp_path / "unwrapped"
    cor_dir = tmp_path / "interferograms"
    work_dir = tmp_path / "mintpy"

    for d in [unw_dir, cor_dir, work_dir]:
        d.mkdir()

    return {
        "root": tmp_path,
        "unw_dir": unw_dir,
        "cor_dir": cor_dir,
        "work_dir": work_dir,
    }


@pytest.fixture
def radar_params():
    """Orbit-derived MintPy attributes as written into stack_metadata.json."""
    return {
        "ORBIT_DIRECTION": "DESCENDING",
        "HEADING": -167.9,
        "CENTER_LINE_UTC": 12933.5,
        "STARTING_RANGE": 800123.4,
        "SLANT_RANGE_DISTANCE": 830456.7,
        "INCIDENCE_ANGLE": 33.9,
        "EARTH_RADIUS": 6368765.0,
        "HEIGHT": 700654.0,
        "WAVELENGTH": 0.05546576,
        "RANGE_PIXEL_SIZE": 9.318248,
        "AZIMUTH_PIXEL_SIZE": 13.932898,
        "PRF": 486.486,
        "RLOOKS": 4,
        "ALOOKS": 1,
    }


@pytest.fixture
def stack_metadata(tmp_path, radar_params):
    """A minimal stack_metadata.json with three dates."""
    acquisitions = [
        {"date": "20240907", "bperp_top": 0.0, "bperp_bottom": 0.0, "bperp_mean": 0.0},
        {"date": "20240919", "bperp_top": -45.0, "bperp_bottom": -47.0, "bperp_mean": -46.0},
        {"date": "20241001", "bperp_top": 32.0, "bperp_bottom": 34.0, "bperp_mean": 33.0},
    ]
    doc = {
        "reference_date": "20240907",
        "radar": radar_params,
        "geometry_files": {},
        "acquisitions": acquisitions,
    }
    path = tmp_path / "stack_metadata.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def circular_orbit():
    """Factory for an analytic circular orbit sampled like a POEORB file.

    The orbit lies in a plane containing the z axis, rotated about z by
    ``-beta_deg`` so that at t=0 the satellite is over longitude
    ``-beta_deg`` on the equator and flies north (an ascending,
    right-looking pass for targets east of the ground track).
    """

    def make(radius=7_070_000.0, beta_deg=5.0, span_s=600.0, step_s=10.0,
             epoch=dt.datetime(2025, 11, 25, 15, 30, 0)):
        from openeo2mintpy.orbit import Orbit

        w = np.sqrt(GM / radius**3)
        b = np.radians(beta_deg)
        t = np.arange(-span_s, span_s + step_s / 2, step_s)
        th = w * t
        pos = radius * np.stack([np.cos(th) * np.cos(b), -np.cos(th) * np.sin(b), np.sin(th)], 1)
        vel = radius * w * np.stack(
            [-np.sin(th) * np.cos(b), np.sin(th) * np.sin(b), np.cos(th)], 1
        )
        times = [epoch + dt.timedelta(seconds=float(s)) for s in t]

        def truth(ts):
            ts = np.atleast_1d(ts)
            thq = w * ts
            p = radius * np.stack(
                [np.cos(thq) * np.cos(b), -np.cos(thq) * np.sin(b), np.sin(thq)], 1
            )
            return p

        return Orbit(times, pos, vel), truth, epoch, w

    return make


def create_mock_geotiff(filepath, width=100, height=50, bands=1):
    """Create a minimal valid GeoTIFF file for testing.

    This creates a bare-minimum TIFF file that GDAL can open.
    For tests that don't need GDAL, use create_dummy_tif instead.
    """
    try:
        from osgeo import gdal, osr

        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(str(filepath), width, height, bands, gdal.GDT_Float32)

        # Set geotransform
        ds.SetGeoTransform([36.0, 0.001, 0, 41.0, 0, -0.001])

        # Set projection (WGS84)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt())

        ds.FlushCache()
        ds = None
        return True
    except ImportError:
        # GDAL not available, create a dummy file
        create_dummy_tif(filepath)
        return False


def create_dummy_tif(filepath):
    """Create a minimal placeholder .tif file (not GDAL-readable)."""
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_bytes(b"dummy_tif_content")
