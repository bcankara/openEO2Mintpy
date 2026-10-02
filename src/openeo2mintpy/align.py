"""
Raster alignment utility for openeo2mintpy.

Brings all split GeoTIFFs (unwrapped phase and coherence) onto one grid:
the intersection of their extents. No raster is used as a template. When
every raster already lies on a common pixel lattice (same pixel size,
origins an integer number of pixels apart), as openEO burst products of
one stack do, the intersection is cut out by array slicing and no value
is resampled. Otherwise the rasters are warped with GDAL onto the
intersection at the mean pixel size with a user-selected kernel.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

ALIGN_METHODS = ("auto", "crop", "warp")


def _raster_grid(ds) -> dict:
    gt = ds.GetGeoTransform()
    w, h = ds.RasterXSize, ds.RasterYSize
    return {
        "xmin": gt[0], "xres": gt[1], "ymax": gt[3], "yres": gt[5],
        "xmax": gt[0] + gt[1] * w, "ymin": gt[3] + gt[5] * h,
        "width": w, "height": h,
    }


def on_common_lattice(grids: list[dict], tol_px: float = 1e-6) -> bool:
    """Return True when all grids share pixel size and integer pixel offsets."""
    ref = grids[0]
    for g in grids[1:]:
        if abs(g["xres"] - ref["xres"]) > abs(ref["xres"]) * 1e-9:
            return False
        if abs(g["yres"] - ref["yres"]) > abs(ref["yres"]) * 1e-9:
            return False
        dx = (g["xmin"] - ref["xmin"]) / ref["xres"]
        dy = (g["ymax"] - ref["ymax"]) / ref["yres"]
        if abs(dx - round(dx)) > tol_px or abs(dy - round(dy)) > tol_px:
            return False
    return True


def _crop_to_window(gdal, src: Path, dst: Path, col: int, row: int, width: int,
                    height: int, geotransform: tuple) -> None:
    ds = gdal.Open(str(src))
    band = ds.GetRasterBand(1)
    data = band.ReadAsArray(col, row, width, height)
    out = gdal.GetDriverByName("GTiff").Create(str(dst), width, height, 1, band.DataType)
    out.SetGeoTransform(geotransform)
    out.SetProjection(ds.GetProjection())
    out_band = out.GetRasterBand(1)
    out_band.WriteArray(data)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        out_band.SetNoDataValue(nodata)
    out = None
    ds = None


def align_rasters(
    unw_dir: str | Path,
    cor_dir: str | Path | None = None,
    resample_alg: str = "bilinear",
    method: str = "auto",
    report_path: str | Path | None = None,
    log_callback=None,
    progress_callback=None,
) -> dict:
    """Align all unwrapped phase and coherence GeoTIFFs to a common grid.

    The target grid is the spatial intersection of all rasters. With
    ``method="auto"`` the rasters are cropped without resampling when they
    lie on a common lattice and warped otherwise; ``"crop"`` refuses to
    resample and ``"warp"`` always resamples. Files are replaced in place.

    Parameters
    ----------
    unw_dir : str or Path
        Directory containing unwrapped phase GeoTIFFs (*.unw.tif).
    cor_dir : str or Path, optional
        Directory containing coherence GeoTIFFs (*.cor.tif).
    resample_alg : str, default "bilinear"
        GDAL resampling kernel used by the warp path
        ('near', 'bilinear', 'cubic', 'cubicspline', 'lanczos').
    method : {"auto", "crop", "warp"}
        Alignment strategy.
    report_path : str or Path, optional
        Where to write the JSON alignment report. Defaults to
        ``<unw_dir>/alignment_report.json``.
    log_callback : callable, optional
        Callback function for log messages.

    Returns
    -------
    dict
        Summary with keys: 'aligned', 'errors', 'details', 'method',
        'common_lattice' and 'target'.
    """
    if method not in ALIGN_METHODS:
        raise ValueError(f"method must be one of {ALIGN_METHODS}, got {method!r}")
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise ImportError(
            "GDAL is required but not installed in the python environment. "
            "Please install it using: conda install -c conda-forge gdal"
        ) from exc

    gdal.UseExceptions()

    # Map resample algorithm string to GDAL constant
    resample_map = {
        "near": gdal.GRA_NearestNeighbour,
        "bilinear": gdal.GRA_Bilinear,
        "cubic": gdal.GRA_Cubic,
        "cubicspline": gdal.GRA_CubicSpline,
        "lanczos": gdal.GRA_Lanczos,
    }
    alg = resample_map.get(resample_alg.lower(), gdal.GRA_Bilinear)

    unw_dir = Path(unw_dir)
    cor_dir = Path(cor_dir) if cor_dir else unw_dir

    # Find files
    unw_files = sorted(list(unw_dir.glob("*.unw.tif")))
    cor_files = sorted(list(cor_dir.glob("*.cor.tif")))
    all_files = unw_files + cor_files

    result = {
        "aligned": 0,
        "errors": [],
        "details": [],
        "method": None,
        "common_lattice": None,
        "target": None,
    }

    if not all_files:
        msg = f"No .unw.tif or .cor.tif files found in: {unw_dir} / {cor_dir}"
        logger.warning(msg)
        if log_callback:
            log_callback(msg)
        return result

    msg = f"Found {len(all_files)} files to align. Calculating bounding boxes..."
    logger.info(msg)
    if log_callback:
        log_callback(msg)

    grids = {}
    for f in all_files:
        try:
            ds = gdal.Open(str(f))
            if not ds:
                raise RuntimeError(f"Failed to open raster: {f.name}")
            grids[f] = _raster_grid(ds)
            ds = None
        except Exception as e:
            err_msg = f"Error reading bounds of {f.name}: {e}"
            logger.error(err_msg)
            if log_callback:
                log_callback(err_msg)
            result["errors"].append({"file": f.name, "error": str(e)})

    if not grids:
        raise RuntimeError("No valid raster files could be read for bounds check.")
    all_files = list(grids)
    glist = list(grids.values())

    inter_xmin = max(g["xmin"] for g in glist)
    inter_xmax = min(g["xmax"] for g in glist)
    inter_ymin = max(g["ymin"] for g in glist)
    inter_ymax = min(g["ymax"] for g in glist)

    if inter_xmin >= inter_xmax or inter_ymin >= inter_ymax:
        raise RuntimeError(
            f"No valid spatial intersection found! Extents do not overlap.\n"
            f"X range: [{inter_xmin}, {inter_xmax}]\n"
            f"Y range: [{inter_ymin}, {inter_ymax}]"
        )

    common = on_common_lattice(glist)
    if method == "crop" and not common:
        raise RuntimeError(
            "Rasters do not share a common pixel lattice; cropping without "
            "resampling is impossible. Use method='warp' or 'auto'."
        )
    use_crop = common and method in ("auto", "crop")
    result["method"] = "crop" if use_crop else "warp"
    result["common_lattice"] = common

    if use_crop:
        xres, yres = glist[0]["xres"], glist[0]["yres"]
    else:
        xres = sum(g["xres"] for g in glist) / len(glist)
        yres = sum(g["yres"] for g in glist) / len(glist)
    target_w = int(round((inter_xmax - inter_xmin) / xres))
    target_h = int(round((inter_ymax - inter_ymin) / abs(yres)))
    target_gt = (inter_xmin, xres, 0.0, inter_ymax, 0.0, yres)
    result["target"] = {
        "geotransform": list(target_gt),
        "width": target_w,
        "height": target_h,
        "resample_alg": None if use_crop else resample_alg,
    }

    msg = (
        f"Intersection bounding box calculated:\n"
        f"  XMin: {inter_xmin:.8f}, XMax: {inter_xmax:.8f}\n"
        f"  YMin: {inter_ymin:.8f}, YMax: {inter_ymax:.8f}\n"
        f"  Pixel size: {xres:.12f}, {yres:.12f}\n"
        f"  Target size: ({target_h}, {target_w}) pixels\n"
        f"  Common lattice: {common} -> method: {result['method']}"
    )
    logger.info(msg)
    if log_callback:
        log_callback(msg)

    warp_options = None
    if not use_crop:
        warp_options = gdal.WarpOptions(
            outputBounds=[inter_xmin, inter_ymin, inter_xmax, inter_ymax],
            xRes=xres,
            yRes=abs(yres),
            resampleAlg=alg,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        for idx, fpath in enumerate(all_files):
            try:
                msg = f"[{idx + 1}/{len(all_files)}] Aligning {fpath.name}..."
                logger.info(msg)
                if log_callback:
                    log_callback(msg)

                temp_out = tmp_path / fpath.name
                g = grids[fpath]
                col = int(round((inter_xmin - g["xmin"]) / g["xres"]))
                row = int(round((g["ymax"] - inter_ymax) / abs(g["yres"])))

                if use_crop:
                    _crop_to_window(gdal, fpath, temp_out, col, row, target_w, target_h,
                                    target_gt)
                else:
                    gdal.Warp(str(temp_out), str(fpath), options=warp_options)

                # Replace the original file with the warped one.
                # Use shutil.copy2 + unlink instead of os.replace because
                # the temp dir may be on a different filesystem (e.g. /tmp
                # vs /mnt/w on WSL), and os.replace fails across mount points.
                if fpath.exists():
                    fpath.unlink()
                shutil.copy2(str(temp_out), str(fpath))

                result["aligned"] += 1
                result["details"].append(
                    {
                        "file": fpath.name,
                        "status": "aligned",
                        "source_width": g["width"],
                        "source_height": g["height"],
                        "col_offset": col,
                        "row_offset": row,
                    }
                )

                # If there's an existing .rsc file, we must delete it because the
                # image dimensions and geotransform boundaries have changed!
                rsc_file = Path(str(fpath) + ".rsc")
                if rsc_file.exists():
                    rsc_file.unlink()
                    logger.debug(f"Removed outdated .rsc sidecar: {rsc_file.name}")

            except Exception as e:
                err_msg = f"✗ Failed to align {fpath.name}: {e}"
                logger.error(err_msg)
                if log_callback:
                    log_callback(err_msg)
                result["errors"].append({"file": fpath.name, "error": str(e)})

            if progress_callback:
                progress_callback(idx + 1, len(all_files))

    report_path = Path(report_path) if report_path else unw_dir / "alignment_report.json"
    with open(report_path, "w") as f:
        json.dump(result, f, indent=2)

    msg = (
        f"Alignment completed ({result['method']}): {result['aligned']} files aligned, "
        f"{len(result['errors'])} errors. Report: {report_path}"
    )
    logger.info(msg)
    if log_callback:
        log_callback(msg)

    return result


def prepare_dem(
    unw_dir: str | Path,
    zip_dir: str | Path,
    output_file: str | Path,
    log_callback=None,
) -> Path:
    """Extract, merge, and align NASADEM HGT files to match the stack grid.

    Parameters
    ----------
    unw_dir : str or Path
        Directory containing aligned unwrapped GeoTIFFs (to extract reference extent/resolution).
    zip_dir : str or Path
        Directory containing downloaded NASADEM zip files or extracted HGT/DEM/TIF
        files, or a single DEM file.
    output_file : str or Path
        Path to output aligned DEM GeoTIFF.
    log_callback : callable, optional
        Callback function for log messages.

    Returns
    -------
    Path
        Path to the generated aligned DEM GeoTIFF.
    """
    import zipfile
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise ImportError(
            "GDAL is required but not installed in the python environment. "
            "Please install it using: conda install -c conda-forge gdal"
        ) from exc

    gdal.UseExceptions()

    unw_dir = Path(unw_dir)
    zip_dir = Path(zip_dir)
    output_file = Path(output_file)

    msg = f"Preparing DEM. Reading aligned stack reference from {unw_dir}..."
    logger.info(msg)
    if log_callback:
        log_callback(msg)

    # 1. Find aligned reference file to copy extent, grid, and projection
    unw_files = sorted(list(unw_dir.glob("*.unw.tif")))
    if not unw_files:
        raise RuntimeError(
            f"No unwrapped phase files (*.unw.tif) found in {unw_dir}.\n"
            f"You must run alignment ('openeo2mintpy align') first to create aligned files."
        )

    ref_file = unw_files[0]
    ds_ref = gdal.Open(str(ref_file))
    if not ds_ref:
        raise RuntimeError(f"Failed to open reference file: {ref_file}")

    gt = ds_ref.GetGeoTransform()
    w = ds_ref.RasterXSize
    h = ds_ref.RasterYSize
    ref_proj = ds_ref.GetProjection()

    ref_xmin = gt[0]
    ref_xres = gt[1]
    ref_xmax = ref_xmin + ref_xres * w
    ref_ymax = gt[3]
    ref_yres = gt[5]
    ref_ymin = ref_ymax + ref_yres * h
    ds_ref = None

    msg = (
        f"Reference stack grid:\n"
        f"  XMin: {ref_xmin:.8f}, XMax: {ref_xmax:.8f}\n"
        f"  YMin: {ref_ymin:.8f}, YMax: {ref_ymax:.8f}\n"
        f"  Size: {w} x {h} pixels"
    )
    logger.info(msg)
    if log_callback:
        log_callback(msg)

    # 2. Gather input tiles (HGT, DEM, or TIF files)
    hgt_files = []

    # A single DEM file may be given instead of a directory
    if zip_dir.is_file():
        hgt_files.append(zip_dir)

    # Check for direct files in the zip directory
    hgt_files.extend(list(zip_dir.glob("*.hgt")) + list(zip_dir.glob("*.dem")))
    hgt_files.extend(list(zip_dir.glob("*.HGT")) + list(zip_dir.glob("*.DEM")))
    hgt_files.extend(list(zip_dir.glob("*.tif")) + list(zip_dir.glob("*.TIF")))

    # Extract from zip files if present
    zip_files = list(zip_dir.glob("*.zip"))
    extracted_paths = []
    tmp_dir_obj = None

    if zip_files:
        msg = f"Found {len(zip_files)} zip files to inspect/extract..."
        logger.info(msg)
        if log_callback:
            log_callback(msg)

        tmp_dir_obj = tempfile.TemporaryDirectory()
        tmp_dir = Path(tmp_dir_obj.name)

        for zpath in zip_files:
            try:
                with zipfile.ZipFile(zpath, "r") as zref:
                    for member in zref.namelist():
                        if member.lower().endswith((".hgt", ".dem")):
                            # Extract to temp dir
                            zref.extract(member, tmp_dir)
                            extracted_paths.append(tmp_dir / member)
                            logger.debug(f"Extracted {member} from {zpath.name}")
            except Exception as e:
                logger.warning(f"Failed to read zip {zpath.name}: {e}")

    hgt_files.extend(extracted_paths)

    # Dedup paths
    hgt_files = sorted(list(set(hgt_files)))

    if not hgt_files:
        raise RuntimeError(
            f"No .hgt, .dem, .tif, or .zip files found in {zip_dir}. "
            f"Please place DEM files in this folder."
        )

    msg = f"Found {len(hgt_files)} DEM tiles to merge and align."
    logger.info(msg)
    if log_callback:
        log_callback(msg)

    # 3. Merge and Warp DEM tiles using GDAL Warp
    output_file.parent.mkdir(parents=True, exist_ok=True)
    src_datasets = [str(p) for p in hgt_files]

    warp_options = gdal.WarpOptions(
        dstSRS=ref_proj,
        outputBounds=[ref_xmin, ref_ymin, ref_xmax, ref_ymax],
        xRes=ref_xres,
        yRes=abs(ref_yres),
        resampleAlg=gdal.GRA_Bilinear,
    )

    msg = f"Warping and merging DEM to {output_file.name}..."
    logger.info(msg)
    if log_callback:
        log_callback(msg)

    gdal.Warp(str(output_file), src_datasets, options=warp_options)

    # Clean up extracted temp directory if we created one
    if tmp_dir_obj:
        try:
            tmp_dir_obj.cleanup()
        except Exception:
            pass

    # 4. Generate the corresponding .rsc sidecar metadata file
    from openeo2mintpy.prepare import prepare_rsc
    rsc_path = prepare_rsc(
        tif_path=output_file,
        file_type=".dem",
        is_interferogram=False,
    )

    msg = f"DEM prepared successfully: {output_file.name} + {rsc_path.name}"
    logger.info(msg)
    if log_callback:
        log_callback(msg)

    return output_file

