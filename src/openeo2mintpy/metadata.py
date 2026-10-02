"""
Raster and file-name metadata helpers.

Supports:
  - GDAL GeoTIFF metadata (raster dimensions + geotransform)
  - Date extraction from openEO/MintPy-style filenames (YYYYMMDD_YYYYMMDD)
  - Pair baselines from per-date baselines
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DATE_PAIR_PATTERN = re.compile(r"(\d{8})_(\d{8})")


# Geotransform returned by GDAL for a raster that has *no* georeference.
# We use this to distinguish a real geocoded product from a radar
# geometry GeoTIFF whose geotransform GDAL fills with identity values.
_DEFAULT_GT = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


def _is_default_geotransform(gt, tol=1e-9):
    """Return True when the geotransform matches GDAL's identity default."""
    if gt is None or len(gt) < 6:
        return True
    return all(abs(float(gt[i]) - _DEFAULT_GT[i]) < tol for i in range(6))


def _detect_geocoded(projection_wkt, geotransform):
    """Decide whether a raster is truly geocoded.

    A product is considered geocoded only when:
      * it has a non-empty projection string (``GetProjection``), **and**
      * its geotransform is not the GDAL identity default (0, 1, 0, 0, 0, 1).

    Either condition alone can be misleading (radar-geometry GeoTIFFs
    carry the default geotransform but no projection; some tools strip
    the CRS while keeping a valid geotransform), so we require both.

    Parameters
    ----------
    projection_wkt : str
        WKT projection string from ``GDALDataset.GetProjection``.
    geotransform : sequence of float
        Six-element geotransform from ``GDALDataset.GetGeoTransform``.

    Returns
    -------
    (bool, str)
        Tuple of ``(is_geocoded, reason)``. The reason string is suitable
        for logging and debugging.
    """
    has_proj = bool(projection_wkt and projection_wkt.strip())
    has_gt = not _is_default_geotransform(geotransform)

    if has_proj and has_gt:
        return True, "projection present and non-identity geotransform"
    if has_proj and not has_gt:
        return False, "projection present but identity geotransform (treated as radar)"
    if not has_proj and has_gt:
        return False, "non-identity geotransform but no projection (treated as radar)"
    return False, "no projection and identity geotransform"


def parse_gdal_metadata(tif_path):
    """Read raster dimensions, geotransform and geocoded flag from GDAL.

    Parameters
    ----------
    tif_path : str or Path
        Path to the GeoTIFF file.

    Returns
    -------
    dict
        Dictionary with keys: WIDTH, LENGTH, NUMBER_BANDS,
        X_FIRST, Y_FIRST, X_STEP, Y_STEP, DATA_TYPE,
        IS_GEOCODED, GEOCODED_REASON, PROJECTION_WKT.

        ``IS_GEOCODED`` is a Python bool and ``GEOCODED_REASON`` is a
        short human-readable explanation of the decision, useful for
        log output and debugging.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    RuntimeError
        If GDAL cannot open the file.
    """
    try:
        from osgeo import gdal
    except ImportError:
        raise ImportError(
            "GDAL is required but not installed. "
            "Install it with: conda install -c conda-forge gdal"
        )

    tif_path = Path(tif_path)
    if not tif_path.exists():
        raise FileNotFoundError(f"GeoTIFF not found: {tif_path}")

    gdal.UseExceptions()
    ds = gdal.Open(str(tif_path))
    if ds is None:
        raise RuntimeError(f"GDAL failed to open: {tif_path}")

    width = ds.RasterXSize
    length = ds.RasterYSize
    num_bands = ds.RasterCount

    gt = ds.GetGeoTransform()  # (x_origin, x_step, 0, y_origin, 0, y_step)
    projection = ds.GetProjection() or ""

    is_geocoded, reason = _detect_geocoded(projection, gt)

    x_first = gt[0] if gt else 0.0
    y_first = gt[3] if gt else 0.0
    x_step = gt[1] if gt else 1.0
    y_step = gt[5] if gt else 1.0

    band = ds.GetRasterBand(1)
    gdal_dtype = gdal.GetDataTypeName(band.DataType).lower()
    dtype_map = {
        "float32": "float32",
        "float64": "float64",
        "int16": "int16",
        "int32": "int32",
        "uint8": "uint8",
        "byte": "uint8",
    }
    data_type = dtype_map.get(gdal_dtype, "float32")

    ds = None  # close dataset

    meta = {
        "WIDTH": str(width),
        "LENGTH": str(length),
        "NUMBER_BANDS": str(num_bands),
        "X_FIRST": str(x_first),
        "Y_FIRST": str(y_first),
        "X_STEP": str(x_step),
        "Y_STEP": str(y_step),
        "DATA_TYPE": data_type,
        "IS_GEOCODED": is_geocoded,
        "GEOCODED_REASON": reason,
        "PROJECTION_WKT": projection,
    }

    logger.debug(
        "GDAL metadata for %s: %dx%d, %s bands, geocoded=%s (%s)",
        tif_path.name, width, length, num_bands, is_geocoded, reason,
    )
    return meta


def compute_bperp_pair(baselines, date1, date2):
    """Compute the perpendicular baseline of a pair from per-date baselines.

    Bperp(d1, d2) = Bperp(ref, d2) - Bperp(ref, d1)

    Parameters
    ----------
    baselines : dict
        Mapping from date string to the baseline relative to the
        reference, either a float or a ``(top, bottom)`` tuple.
    date1 : str
        First date (YYYYMMDD).
    date2 : str
        Second date (YYYYMMDD).

    Returns
    -------
    float, tuple or None
        Perpendicular baseline in meters (same shape as the inputs), or
        None if a date is missing.
    """
    b1 = baselines.get(date1)
    b2 = baselines.get(date2)
    if b1 is None or b2 is None:
        return None
    if isinstance(b1, (tuple, list)):
        return tuple(v2 - v1 for v1, v2 in zip(b1, b2))
    return b2 - b1


def extract_dates_from_filename(filename):
    """Extract YYYYMMDD date pair from a filename.

    Looks for the pattern YYYYMMDD_YYYYMMDD anywhere in the filename.

    Parameters
    ----------
    filename : str
        Filename (basename, not full path).

    Returns
    -------
    tuple of (str, str) or None
        (date1, date2) if found, None otherwise.
    """
    m = DATE_PAIR_PATTERN.search(filename)
    if m:
        return m.group(1), m.group(2)
    return None


def auto_detect_ref_date(directory):
    """Auto-detect the reference (super-master) date from a directory.

    Scans the given directory for sub-folders or files containing the
    YYYYMMDD_YYYYMMDD date pair pattern. The reference date is the one
    that appears most frequently as the first date in those pairs.

    Parameters
    ----------
    directory : str or Path
        Path to the directory to scan (e.g. unwrapped dir).

    Returns
    -------
    str or None
        The detected reference date, or None if detection fails.
    """
    directory = Path(directory)
    if not directory.exists():
        return None

    date_counts = {}
    for path in directory.iterdir():
        name = path.name
        # Match YYYYMMDD_YYYYMMDD pattern in folder or file name
        m = DATE_PAIR_PATTERN.search(name)
        if m:
            d1 = m.group(1)
            date_counts[d1] = date_counts.get(d1, 0) + 1

    if not date_counts:
        return None

    # The reference date appears in most pairs
    ref_date = max(date_counts, key=date_counts.get)
    logger.info(
        "Auto-detected reference date: %s (%d occurrences)",
        ref_date,
        date_counts[ref_date],
    )
    return ref_date


def count_files(directory, pattern):
    """Count files matching a glob pattern in a directory.

    Parameters
    ----------
    directory : str or Path
        Directory to search.
    pattern : str
        Glob pattern (e.g., '*.unw.tif').

    Returns
    -------
    int
        Number of matching files.
    """
    directory = Path(directory)
    if not directory.exists():
        return 0
    return len(list(directory.glob(pattern)))
