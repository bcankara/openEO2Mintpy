"""
openEO band-splitting utility for openEO2Mintpy.

Separates 3-band openEO GeoTIFFs into standalone Unwrapped Phase (Band 2)
and Coherence (Band 3) single-band GeoTIFFs, named to fit the YYYYMMDD_YYYYMMDD
pattern expected by MintPy.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Pattern to extract date pairs like 20251124T035010_20251130T034907 or 20251124_20251130
DATE_PAIR_PATTERN = re.compile(r"(\d{8})(?:T\d{6})?_(\d{8})")


def extract_dates_from_openeo_filename(filename: str) -> tuple[str, str] | None:
    """Extract start and end date (YYYYMMDD) from openEO GeoTIFF filename.

    Parameters
    ----------
    filename : str
        The basename of the file.

    Returns
    -------
    tuple of (str, str) or None
        (date1, date2) in YYYYMMDD format if found, otherwise None.
    """
    match = DATE_PAIR_PATTERN.search(filename)
    if match:
        return match.group(1), match.group(2)
    return None


def split_openeo_bands(
    input_dir: str | Path,
    unw_dir: str | Path,
    cor_dir: str | Path,
    progress_callback=None,
    log_callback=None,
) -> dict:
    """Extract Band 2 (unwrapped phase) and Band 3 (coherence) from openEO GeoTIFFs.

    Parameters
    ----------
    input_dir : str or Path
        Directory containing openEO 3-band GeoTIFFs.
    unw_dir : str or Path
        Output directory for unwrapped phase single-band GeoTIFFs.
    cor_dir : str or Path
        Output directory for coherence single-band GeoTIFFs.
    progress_callback : callable, optional
        Callback function invoked as progress_callback(current, total).
    log_callback : callable, optional
        Callback function invoked as log_callback(message) for per-file logging.

    Returns
    -------
    dict
        Summary dictionary with keys: 'processed', 'errors', 'details'.
    """
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise ImportError(
            "GDAL is required but not installed in the python environment. "
            "Please install it using: conda install -c conda-forge gdal"
        ) from exc

    gdal.UseExceptions()

    input_dir = Path(input_dir)
    unw_dir = Path(unw_dir)
    cor_dir = Path(cor_dir)

    unw_dir.mkdir(parents=True, exist_ok=True)
    cor_dir.mkdir(parents=True, exist_ok=True)

    # Search for TIFF files in input directory
    tiff_files = sorted(list(input_dir.glob("*.tif")) + list(input_dir.glob("*.tiff")))
    
    # Filter files that contain a valid date pair
    jobs = []
    for f in tiff_files:
        dates = extract_dates_from_openeo_filename(f.name)
        if dates:
            jobs.append((f, dates))

    result = {
        "processed": 0,
        "errors": [],
        "details": [],
    }

    total = len(jobs)
    if total == 0:
        msg = f"No openEO GeoTIFF files found in: {input_dir}"
        logger.warning(msg)
        if log_callback:
            log_callback(msg)
        return result

    msg = f"Found {total} openEO files to split..."
    logger.info(msg)
    if log_callback:
        log_callback(msg)

    for i, (fpath, (date1, date2)) in enumerate(jobs):
        try:
            msg = f"[{i + 1}/{total}] Splitting {fpath.name} ({date1} -> {date2})"
            logger.info(msg)
            if log_callback:
                log_callback(msg)

            ds = gdal.Open(str(fpath))
            if ds is None:
                raise RuntimeError(f"GDAL failed to open file: {fpath}")

            if ds.RasterCount < 3:
                raise ValueError(
                    f"File must have at least 3 bands (has {ds.RasterCount} bands)"
                )

            # Metadata to preserve
            geotransform = ds.GetGeoTransform()
            projection = ds.GetProjection()
            width = ds.RasterXSize
            length = ds.RasterYSize
            driver = gdal.GetDriverByName("GTiff")

            # Extract Band 2 (Unwrapped phase)
            unw_band = ds.GetRasterBand(2)
            unw_data = unw_band.ReadAsArray()
            unw_out_path = unw_dir / f"{date1}_{date2}.unw.tif"
            
            unw_ds = driver.Create(
                str(unw_out_path), width, length, 1, unw_band.DataType
            )
            unw_ds.SetGeoTransform(geotransform)
            unw_ds.SetProjection(projection)
            
            unw_out_band = unw_ds.GetRasterBand(1)
            unw_out_band.WriteArray(unw_data)
            
            nodata_unw = unw_band.GetNoDataValue()
            if nodata_unw is not None:
                unw_out_band.SetNoDataValue(nodata_unw)
            
            unw_ds = None  # Close and save file

            # Extract Band 3 (Coherence)
            cor_band = ds.GetRasterBand(3)
            cor_data = cor_band.ReadAsArray()
            cor_out_path = cor_dir / f"{date1}_{date2}.cor.tif"
            
            cor_ds = driver.Create(
                str(cor_out_path), width, length, 1, cor_band.DataType
            )
            cor_ds.SetGeoTransform(geotransform)
            cor_ds.SetProjection(projection)
            
            cor_out_band = cor_ds.GetRasterBand(1)
            cor_out_band.WriteArray(cor_data)
            
            nodata_cor = cor_band.GetNoDataValue()
            if nodata_cor is not None:
                cor_out_band.SetNoDataValue(nodata_cor)
            
            cor_ds = None  # Close and save file

            ds = None  # Close input dataset

            result["processed"] += 1
            result["details"].append(
                {
                    "original": fpath.name,
                    "unw": unw_out_path.name,
                    "cor": cor_out_path.name,
                }
            )
            done_msg = f"  ✓ {unw_out_path.name} + {cor_out_path.name}"
            logger.info(done_msg)
            if log_callback:
                log_callback(done_msg)

        except Exception as e:
            err_msg = f"  ✗ Failed to split {fpath.name}: {e}"
            logger.error(err_msg)
            if log_callback:
                log_callback(err_msg)
            result["errors"].append({"file": fpath.name, "error": str(e)})

        if progress_callback:
            progress_callback(i + 1, total)

    return result
