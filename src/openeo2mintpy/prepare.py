"""
Core RSC sidecar generation for openEO Sentinel-1 InSAR GeoTIFF outputs.

Transforms openEO-produced unwrapped phase / coherence GeoTIFFs into
MintPy-compatible datasets by generating ROI_PAC-style .rsc metadata
sidecar files. The acquisition geometry and the baselines come from the
``stack_metadata.json`` written by ``openeo2mintpy metadata``; nothing is
filled in with nominal defaults.
"""

import logging
from pathlib import Path

from openeo2mintpy.constants import (
    DEFAULT_ANTENNA_SIDE,
    DEFAULT_GEOMETRY_MODE,
    GEOMETRY_MODES,
    MINTPY_PROCESSOR,
    RADAR_KEYS,
    RSC_GEO_BLOCK,
    RSC_IFG_EXTRA,
    RSC_RADAR_BLOCK,
    RSC_RASTER_BLOCK,
)
from openeo2mintpy.metadata import (
    compute_bperp_pair,
    extract_dates_from_filename,
    parse_gdal_metadata,
)

logger = logging.getLogger(__name__)


def _resolve_geocoded(gdal_meta, geometry_mode):
    """Decide whether to emit geocoded metadata given the requested mode.

    Parameters
    ----------
    gdal_meta : dict
        Metadata dict returned by ``parse_gdal_metadata``.
    geometry_mode : str
        One of ``"auto"``, ``"radar"`` or ``"geo"``.

    Returns
    -------
    (bool, str)
        ``(is_geocoded, reason)`` — the effective decision plus the
        reason string to be logged.
    """
    if geometry_mode not in GEOMETRY_MODES:
        raise ValueError(
            f"geometry_mode must be one of {GEOMETRY_MODES}, got {geometry_mode!r}"
        )

    detected = bool(gdal_meta.get("IS_GEOCODED", False))
    detected_reason = gdal_meta.get("GEOCODED_REASON", "no detection info")

    if geometry_mode == "auto":
        return detected, f"auto ({detected_reason})"
    if geometry_mode == "radar":
        return False, "radar (user override)"
    # "geo"
    return True, "geo (user override)"


def prepare_rsc(
    tif_path,
    date1=None,
    date2=None,
    bperp=None,
    radar_params=None,
    file_type=".unw",
    is_interferogram=True,
    geometry_mode=DEFAULT_GEOMETRY_MODE,
):
    """Generate a .rsc sidecar file for a single GeoTIFF.

    Parameters
    ----------
    tif_path : str or Path
        Path to the GeoTIFF file.
    date1 : str, optional
        First acquisition date (YYYYMMDD). Required for interferograms.
    date2 : str, optional
        Second acquisition date (YYYYMMDD). Required for interferograms.
    bperp : float or (float, float), optional
        Perpendicular baseline of the pair in metres, either one value or
        the ``(top, bottom)`` values of the scene. Required for interferograms.
    radar_params : dict, optional
        MintPy attributes from ``stack_metadata.json`` (``radar`` block).
        Required for interferograms; without it only the raster
        description is written (e.g. for a DEM).
    file_type : str
        File type label for the .rsc (e.g., '.unw', '.cor', '.conncomp').
    is_interferogram : bool
        If True, includes DATE12 and baseline fields.
    geometry_mode : {"auto", "radar", "geo"}
        Controls how the geotransform block is written:
          * ``auto``  — infer from the GeoTIFF (projection + geotransform)
          * ``radar`` — force radar geometry (no X_FIRST / Y_FIRST lines)
          * ``geo``   — force geocoded output even if detection says radar

    Returns
    -------
    Path
        Path to the generated .rsc file.

    Raises
    ------
    FileNotFoundError
        If the GeoTIFF does not exist.
    ValueError
        If ``geometry_mode`` is invalid, or an interferogram lacks radar
        parameters or a baseline.
    """
    tif_path = Path(tif_path)
    if not tif_path.exists():
        raise FileNotFoundError(f"GeoTIFF not found: {tif_path}")

    has_dates = bool(is_interferogram and date1 and date2)
    if is_interferogram and radar_params is None:
        raise ValueError(
            f"{tif_path.name}: interferograms need orbit-derived radar parameters; "
            "run 'openeo2mintpy metadata' first."
        )
    if has_dates and bperp is None:
        raise ValueError(f"{tif_path.name}: no perpendicular baseline for {date1}_{date2}.")

    gdal_meta = parse_gdal_metadata(tif_path)
    width = int(gdal_meta["WIDTH"])
    length = int(gdal_meta["LENGTH"])

    is_geocoded, reason = _resolve_geocoded(gdal_meta, geometry_mode)
    logger.debug(
        "Geometry decision for %s: geocoded=%s (%s)",
        tif_path.name, is_geocoded, reason,
    )

    rsc_content = RSC_RASTER_BLOCK.format(
        width=width,
        length=length,
        xmax=width - 1,
        ymax=length - 1,
        processor=MINTPY_PROCESSOR,
        number_bands=gdal_meta.get("NUMBER_BANDS", "1"),
        file_type=file_type,
        data_type=gdal_meta.get("DATA_TYPE", "float32"),
    )

    if radar_params is not None:
        missing = [k for k in RADAR_KEYS if k not in radar_params]
        if missing:
            raise ValueError(f"Radar parameters are missing: {', '.join(missing)}")
        values = {k: radar_params[k] for k in RADAR_KEYS}
        values["ANTENNA_SIDE"] = radar_params.get("ANTENNA_SIDE", DEFAULT_ANTENNA_SIDE)
        rsc_content += RSC_RADAR_BLOCK.format(**values)

    if is_geocoded:
        x_step = float(gdal_meta.get("X_STEP", 1.0))
        y_step = float(gdal_meta.get("Y_STEP", -1.0))
        # Heuristic: |step| < 1 degree → lon/lat grid; otherwise metres (UTM)
        x_unit = "degree" if abs(x_step) < 1.0 else "meter"
        y_unit = "degree" if abs(y_step) < 1.0 else "meter"
        rsc_content += RSC_GEO_BLOCK.format(
            x_first=gdal_meta.get("X_FIRST", "0.0"),
            y_first=gdal_meta.get("Y_FIRST", "0.0"),
            x_step=gdal_meta.get("X_STEP", "1.0"),
            y_step=gdal_meta.get("Y_STEP", "-1.0"),
            x_unit=x_unit,
            y_unit=y_unit,
        )

    if has_dates:
        top, bottom = bperp if isinstance(bperp, (tuple, list)) else (bperp, bperp)
        rsc_content += RSC_IFG_EXTRA.format(
            date12=f"{date1[2:]}-{date2[2:]}",
            bperp_top=f"{top:.4f}",
            bperp_bottom=f"{bottom:.4f}",
        )

    rsc_path = Path(str(tif_path) + ".rsc")
    with open(rsc_path, "w") as f:
        f.write(rsc_content)

    logger.debug("Generated .rsc: %s (mode=%s)", rsc_path.name, geometry_mode)
    return rsc_path


def prepare_stack(
    unw_dir,
    stack_metadata,
    cor_dir=None,
    conncomp_dir=None,
    geometry_dir=None,
    progress_callback=None,
    geometry_mode=DEFAULT_GEOMETRY_MODE,
):
    """Generate .rsc sidecar files for an entire interferogram stack.

    Parameters
    ----------
    unw_dir : str or Path
        Directory containing unwrapped phase GeoTIFFs (*.unw.tif).
    stack_metadata : str, Path or dict
        ``stack_metadata.json`` written by ``openeo2mintpy metadata`` (or the
        dict returned by :func:`openeo2mintpy.geometry.load_stack_metadata`).
    cor_dir : str or Path, optional
        Directory containing coherence GeoTIFFs (*.cor.tif or *.int.cor.tif).
        Defaults to unw_dir.
    conncomp_dir : str or Path, optional
        Directory containing connected component GeoTIFFs (*.conncomp.tif).
        Defaults to unw_dir.
    geometry_dir : str or Path, optional
        Directory containing geometry GeoTIFFs (DEM, incidence, azimuth).
        Defaults to the directory of the angle rasters in the metadata.
    progress_callback : callable, optional
        Function called with (current, total) for progress reporting.
    geometry_mode : {"auto", "radar", "geo"}
        Controls whether .rsc files are written as radar or geocoded.
        ``auto`` inspects the GeoTIFF CRS and geotransform; ``radar`` and
        ``geo`` force the corresponding layout regardless of detection.

    Returns
    -------
    dict
        Summary with keys: ``rsc_written``, ``skipped``, ``errors``,
        ``details`` and ``geometry_mode`` (the effective mode for the run).
    """
    from openeo2mintpy.geometry import load_stack_metadata

    if geometry_mode not in GEOMETRY_MODES:
        raise ValueError(
            f"geometry_mode must be one of {GEOMETRY_MODES}, got {geometry_mode!r}"
        )

    meta = stack_metadata
    if not isinstance(meta, dict):
        meta = load_stack_metadata(meta)
    radar_params = meta["radar"]
    baselines = meta["baselines"]

    unw_dir = Path(unw_dir)
    cor_dir = Path(cor_dir) if cor_dir else unw_dir
    conncomp_dir = Path(conncomp_dir) if conncomp_dir else unw_dir
    if geometry_dir is None and meta.get("geometry_files"):
        geometry_dir = Path(next(iter(meta["geometry_files"].values()))).parent

    # Collect all files to process
    file_groups = []

    # Unwrapped phase files
    unw_files = _find_tif_files(unw_dir, ["*.unw.tif"])
    for f in unw_files:
        file_groups.append((f, ".unw", True))

    # Coherence files
    cor_patterns = ["*.int.cor.tif", "*.cor.tif"]
    cor_files = _find_tif_files(cor_dir, cor_patterns)
    for f in cor_files:
        file_groups.append((f, ".cor", True))

    # Connected component files
    conn_files = _find_tif_files(conncomp_dir, ["*.unw.conncomp.tif", "*.conncomp.tif"])
    for f in conn_files:
        file_groups.append((f, ".conncomp", True))

    # Geometry files
    if geometry_dir:
        geom_dir = Path(geometry_dir)
        if geom_dir.exists():
            geom_patterns = [
                ("*.dem.tif", ".dem"),
                ("*dem*.tif", ".dem"),
                ("*height*.tif", ".dem"),
                ("*incidence*.tif", ".inc"),
                ("*inc*.tif", ".inc"),
                ("*azimuth*.tif", ".az"),
                ("*az*.tif", ".az"),
                ("*shadow*.tif", ".shadowMask"),
                ("*water*.tif", ".waterMask"),
            ]
            seen_geom_files = set()
            for pattern, ftype in geom_patterns:
                for f in sorted(geom_dir.glob(pattern)):
                    if not _is_data_raster(f) or f in seen_geom_files:
                        continue
                    seen_geom_files.add(f)
                    file_groups.append((f, ftype, False))

    total = len(file_groups)
    result = {
        "rsc_written": 0,
        "skipped": 0,
        "errors": [],
        "details": [],
        "geometry_mode": geometry_mode,
    }

    if total == 0:
        logger.warning("No GeoTIFF files found to process.")
        return result

    _log_geometry_decision(file_groups[0][0], geometry_mode)
    _validate_geometry_consistency(file_groups[0][0], geometry_dir, geometry_mode)

    logger.info("Processing %d files (geometry_mode=%s)...", total, geometry_mode)

    for i, (fpath, file_type, is_ifg) in enumerate(file_groups):
        try:
            d1, d2, bperp = None, None, None
            dates = extract_dates_from_filename(fpath.name) if is_ifg else None
            if dates:
                d1, d2 = dates
                bperp = compute_bperp_pair(baselines, d1, d2)

            rsc_path = prepare_rsc(
                tif_path=fpath,
                date1=d1,
                date2=d2,
                bperp=bperp,
                radar_params=radar_params,
                file_type=file_type,
                is_interferogram=is_ifg,
                geometry_mode=geometry_mode,
            )

            result["rsc_written"] += 1
            result["details"].append({"file": str(fpath.name), "rsc": str(rsc_path.name)})

        except Exception as e:
            result["errors"].append({"file": str(fpath.name), "error": str(e)})
            logger.error("Error processing %s: %s", fpath.name, e)

        if progress_callback:
            progress_callback(i + 1, total)

    logger.info(
        "Complete: %d .rsc written, %d errors.",
        result["rsc_written"],
        len(result["errors"]),
    )
    return result


def _log_geometry_decision(sample_tif, geometry_mode):
    """Log the effective geometry decision using the first file as reference.

    Helps the user understand why the pipeline produced radar- or
    geocoded-flavoured .rsc sidecars — the root cause of a common
    MintPy ``geometryGeo.h5 not found`` failure.
    """
    try:
        meta = parse_gdal_metadata(sample_tif)
    except Exception as e:
        logger.warning("Could not probe %s for geometry detection: %s", sample_tif, e)
        return

    effective, reason = _resolve_geocoded(meta, geometry_mode)
    override_hint = ""
    if geometry_mode == "auto":
        override_hint = " (override with geometry_mode='radar' or 'geo' if incorrect)"

    logger.info(
        "Detected geometry: %s — %s%s",
        "GEOCODED" if effective else "RADAR",
        reason,
        override_hint,
    )


def _validate_geometry_consistency(sample_tif, geometry_dir, geometry_mode):
    """Warn when stack geometry and geometry_dir contents look mismatched.

    The goal is to surface the "ifgramStack is radar but geometry files
    are geocoded" (or vice-versa) mismatch before MintPy's
    ``check_loaded_dataset`` aborts the run with an obscure
    ``FileNotFoundError``.
    """
    if not geometry_dir:
        return

    geom_dir = Path(geometry_dir)
    if not geom_dir.exists():
        return

    try:
        stack_meta = parse_gdal_metadata(sample_tif)
    except Exception:
        return

    stack_effective, _ = _resolve_geocoded(stack_meta, geometry_mode)

    geom_candidates = [
        candidate
        for pattern in ("*.tif", "*.vrt")
        for candidate in geom_dir.glob(pattern)
        if _is_data_raster(candidate)
    ]
    if not geom_candidates:
        return

    try:
        geom_meta = parse_gdal_metadata(geom_candidates[0])
    except Exception:
        return

    geom_is_geocoded = bool(geom_meta.get("IS_GEOCODED", False))

    if stack_effective != geom_is_geocoded:
        logger.warning(
            "Geometry mismatch: stack is %s but %s is %s. "
            "MintPy check_loaded_dataset will likely fail — re-run with a "
            "matching geometry_mode or resample the geometry files first.",
            "GEOCODED" if stack_effective else "RADAR",
            geom_candidates[0].name,
            "GEOCODED" if geom_is_geocoded else "RADAR",
        )

    if (stack_meta["WIDTH"], stack_meta["LENGTH"]) != (geom_meta["WIDTH"], geom_meta["LENGTH"]):
        logger.warning(
            "Geometry size mismatch: stack is %sx%s but %s is %sx%s.",
            stack_meta["WIDTH"], stack_meta["LENGTH"], geom_candidates[0].name,
            geom_meta["WIDTH"], geom_meta["LENGTH"],
        )


def _is_data_raster(path):
    """Return True for GDAL-readable rasters and False for metadata sidecars."""
    if not path.is_file():
        return False
    name = path.name.lower()
    return not (name.endswith(".rsc") or name.endswith(".xml") or name.endswith(".json"))


def _find_tif_files(directory, patterns):
    """Find GeoTIFF files matching any of the given glob patterns.

    Uses a set to avoid duplicates when patterns overlap.

    Parameters
    ----------
    directory : Path
        Directory to search.
    patterns : list of str
        Glob patterns to match.

    Returns
    -------
    list of Path
        Sorted list of unique matching file paths.
    """
    if not directory.exists():
        return []

    found = set()
    for pattern in patterns:
        for f in directory.glob(pattern):
            found.add(f)

    return sorted(found)
