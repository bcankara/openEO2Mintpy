"""Tests for openeo2mintpy.split module."""

import pytest
from pathlib import Path
from openeo2mintpy.split import (
    extract_dates_from_openeo_filename,
    split_openeo_bands,
)

# Helper to check if GDAL is available
try:
    from osgeo import gdal
    GDAL_AVAILABLE = True
except ImportError:
    GDAL_AVAILABLE = False


def test_extract_dates_from_openeo_filename():
    # Test valid filename patterns
    assert extract_dates_from_openeo_filename("phase_coh_20251124T035010_20251130T034907.tif") == ("20251124", "20251130")
    assert extract_dates_from_openeo_filename("20251124_20251130.tif") == ("20251124", "20251130")
    assert extract_dates_from_openeo_filename("prefix_20251124_20251130_suffix.tif") == ("20251124", "20251130")
    
    # Test invalid patterns
    assert extract_dates_from_openeo_filename("dem.tif") is None
    assert extract_dates_from_openeo_filename("20251124.tif") is None
    assert extract_dates_from_openeo_filename("2025112_20251130.tif") is None


@pytest.mark.skipif(not GDAL_AVAILABLE, reason="GDAL is not available in the current environment")
def test_split_openeo_bands_valid(tmp_path):
    input_dir = tmp_path / "inputs"
    unw_dir = tmp_path / "unw"
    cor_dir = tmp_path / "cor"
    
    input_dir.mkdir()
    
    # Create a dummy 3-band GeoTIFF
    tif_name = "phase_coh_20251124T035010_20251130T034907.tif"
    tif_path = input_dir / tif_name
    
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(str(tif_path), 10, 10, 3, gdal.GDT_Float32)
    
    # Set mock projection and geotransform
    geotransform = [36.0, 0.001, 0, 41.0, 0, -0.001]
    ds.SetGeoTransform(geotransform)
    
    # Set dummy band data
    import numpy as np
    b2_data = np.ones((10, 10), dtype=np.float32) * 2.5
    b3_data = np.ones((10, 10), dtype=np.float32) * 0.8
    
    ds.GetRasterBand(2).WriteArray(b2_data)
    ds.GetRasterBand(2).SetNoDataValue(-9999.0)
    
    ds.GetRasterBand(3).WriteArray(b3_data)
    ds.GetRasterBand(3).SetNoDataValue(-9999.0)
    
    ds.FlushCache()
    ds = None
    
    # Run split
    result = split_openeo_bands(input_dir, unw_dir, cor_dir)
    
    assert result["processed"] == 1
    assert len(result["errors"]) == 0
    assert len(result["details"]) == 1
    
    # Verify outputs
    unw_file = unw_dir / "20251124_20251130.unw.tif"
    cor_file = cor_dir / "20251124_20251130.cor.tif"
    
    assert unw_file.exists()
    assert cor_file.exists()
    
    # Read outputs and verify band values and metadata
    unw_ds = gdal.Open(str(unw_file))
    assert unw_ds.RasterCount == 1
    assert unw_ds.GetGeoTransform() == pytest.approx(geotransform)
    assert unw_ds.GetRasterBand(1).GetNoDataValue() == pytest.approx(-9999.0)
    unw_arr = unw_ds.GetRasterBand(1).ReadAsArray()
    assert np.allclose(unw_arr, 2.5)
    unw_ds = None
    
    cor_ds = gdal.Open(str(cor_file))
    assert cor_ds.RasterCount == 1
    assert cor_ds.GetGeoTransform() == pytest.approx(geotransform)
    assert cor_ds.GetRasterBand(1).GetNoDataValue() == pytest.approx(-9999.0)
    cor_arr = cor_ds.GetRasterBand(1).ReadAsArray()
    assert np.allclose(cor_arr, 0.8)
    cor_ds = None
