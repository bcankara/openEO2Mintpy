"""
Imaging geometry and perpendicular baselines of an openEO burst stack.

Given the aligned stack grid, the DEM resampled onto it, the acquisition
table and the precise orbits, this module computes what the openEO
products do not carry: the incidence and azimuth angles and slant range
of every pixel, the scene heading and centre-line time, and the
perpendicular baseline of every acquisition relative to the reference.
The baseline follows the ISCE2 topsStack convention (magnitude of the
secondary-minus-reference position orthogonal to the reference line of
sight, signed by the cross product with the reference velocity).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import numpy as np

from openeo2mintpy.acquisitions import read_job_context
from openeo2mintpy.constants import (
    S1_AZIMUTH_PIXEL_SIZE,
    S1_RANGE_PIXEL_SIZE,
    S1_WAVELENGTH,
)
from openeo2mintpy.orbit import Orbit, download_orbit_file, find_orbit_file
from openeo2mintpy.snap_header import crosscheck_baselines

logger = logging.getLogger(__name__)

WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3
S1_PRF = 486.486
ORBIT_WINDOW = dt.timedelta(minutes=10)


def llh_to_ecef(lat: np.ndarray, lon: np.ndarray, h: np.ndarray) -> np.ndarray:
    """Convert geodetic WGS84 coordinates (degrees, metres) to ECEF metres."""
    lat_r, lon_r = np.radians(lat), np.radians(lon)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * np.sin(lat_r) ** 2)
    return np.stack(
        [
            (n + h) * np.cos(lat_r) * np.cos(lon_r),
            (n + h) * np.cos(lat_r) * np.sin(lon_r),
            (n * (1.0 - WGS84_E2) + h) * np.sin(lat_r),
        ],
        axis=-1,
    )


def enu_basis(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return the local east, north and up unit vectors in ECEF."""
    lat_r, lon_r = np.radians(lat), np.radians(lon)
    east = np.stack([-np.sin(lon_r), np.cos(lon_r), np.zeros_like(lon_r)], axis=-1)
    north = np.stack(
        [-np.sin(lat_r) * np.cos(lon_r), -np.sin(lat_r) * np.sin(lon_r), np.cos(lat_r)], axis=-1
    )
    up = np.stack(
        [np.cos(lat_r) * np.cos(lon_r), np.cos(lat_r) * np.sin(lon_r), np.sin(lat_r)], axis=-1
    )
    return east, north, up


def los_angles(target: np.ndarray, sat: np.ndarray, lat: np.ndarray,
               lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Incidence and azimuth angles of the ground-to-satellite line of sight.

    The incidence angle is measured from the ellipsoid normal; the azimuth
    angle follows the MintPy/ISCE2 convention (from north, anti-clockwise
    positive), both in degrees.
    """
    los = sat - target
    los /= np.linalg.norm(los, axis=-1, keepdims=True)
    east, north, up = enu_basis(lat, lon)
    inc = np.degrees(np.arccos(np.clip(np.einsum("...i,...i", los, up), -1.0, 1.0)))
    az = np.degrees(np.arctan2(-np.einsum("...i,...i", los, east),
                               np.einsum("...i,...i", los, north)))
    return inc, az


def perpendicular_baseline(target: np.ndarray, ref_pos: np.ndarray, ref_vel: np.ndarray,
                           sec_pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Perpendicular and parallel baselines of a secondary acquisition.

    Uses the ISCE2 topsStack definitions: the parallel component is the
    projection of ``sec_pos - ref_pos`` onto the reference look vector and
    the perpendicular component carries the sign of
    ``((target - ref_pos) x (sec_pos - ref_pos)) . ref_vel``.
    """
    look = target - ref_pos
    unit = look / np.linalg.norm(look, axis=-1, keepdims=True)
    base = sec_pos - ref_pos
    bpar = np.einsum("...i,...i", base, unit)
    bperp = np.sqrt(np.maximum(np.einsum("...i,...i", base, base) - bpar**2, 0.0))
    sign = np.sign(np.einsum("...i,...i", np.cross(look, base), ref_vel))
    return sign * bperp, bpar


def heading_at(pos: np.ndarray, vel: np.ndarray) -> float:
    """Ground-track heading (degrees clockwise from north) of a satellite state."""
    lat = np.degrees(np.arctan2(pos[2], np.hypot(pos[0], pos[1])))
    lon = np.degrees(np.arctan2(pos[1], pos[0]))
    east, north, _ = enu_basis(np.array(lat), np.array(lon))
    return float(np.degrees(np.arctan2(vel @ east, vel @ north)))


def read_grid(tif_path: str | Path) -> dict:
    """Read the pixel-centre coordinates and georeference of a geocoded raster."""
    from osgeo import gdal

    gdal.UseExceptions()
    ds = gdal.Open(str(tif_path))
    gt = ds.GetGeoTransform()
    width, length = ds.RasterXSize, ds.RasterYSize
    projection = ds.GetProjection()
    ds = None
    if gt[2] != 0 or gt[4] != 0:
        raise ValueError("Rotated geotransforms are not supported.")
    lon = gt[0] + (np.arange(width) + 0.5) * gt[1]
    lat = gt[3] + (np.arange(length) + 0.5) * gt[5]
    return {"lon": lon, "lat": lat, "width": width, "length": length,
            "geotransform": gt, "projection": projection}


def read_dem(dem_path: str | Path, grid: dict) -> np.ndarray:
    """Read a DEM already resampled onto the stack grid; invalid heights become 0."""
    from osgeo import gdal

    gdal.UseExceptions()
    ds = gdal.Open(str(dem_path))
    if (ds.RasterXSize, ds.RasterYSize) != (grid["width"], grid["length"]):
        raise ValueError(
            f"DEM size {ds.RasterXSize}x{ds.RasterYSize} differs from the stack grid "
            f"{grid['width']}x{grid['length']}; run 'openeo2mintpy prepare-dem' first."
        )
    if not np.allclose(ds.GetGeoTransform(), grid["geotransform"], rtol=0, atol=1e-9):
        raise ValueError("DEM geotransform differs from the stack grid.")
    band = ds.GetRasterBand(1)
    dem = band.ReadAsArray().astype(np.float64)
    nodata = band.GetNoDataValue()
    ds = None
    invalid = ~np.isfinite(dem)
    if nodata is not None:
        invalid |= dem == nodata
    dem[invalid] = 0.0
    return dem


def load_orbits(table: list[dict], cache_dir: str | Path) -> dict[str, tuple[Orbit, str]]:
    """Find, download and read the orbit of every acquisition in the table."""
    orbits = {}
    for row in table:
        t = dt.datetime.fromisoformat(row["azimuth_time"])
        orbit_file = find_orbit_file(row["platform"], t)
        path = download_orbit_file(orbit_file, cache_dir)
        orbits[row["date"]] = (
            Orbit.from_eof(path, t - ORBIT_WINDOW, t + ORBIT_WINDOW),
            orbit_file.name,
        )
    return orbits


def compute_stack_geometry(
    grid_tif: str | Path,
    dem_tif: str | Path,
    table: list[dict],
    orbits: dict[str, tuple[Orbit, str]],
    ref_date: str | None = None,
    baseline_step: int = 50,
    row_chunk: int = 128,
) -> dict:
    """Compute per-pixel angles, per-date baselines and scene metadata.

    Returns
    -------
    dict
        ``incidence``, ``azimuth`` and ``slant_range`` (float32 arrays on the
        stack grid), ``acquisitions`` (the table extended with baselines,
        zero-Doppler time and orbit file) and ``scene`` (MintPy attributes).
    """
    grid = read_grid(grid_tif)
    dem = read_dem(dem_tif, grid)
    rows = {row["date"]: row for row in table}
    ref_date = ref_date or min(rows)
    if ref_date not in rows:
        raise ValueError(f"Reference date {ref_date} is not in the acquisition table.")

    ref_orbit = orbits[ref_date][0]
    ref_t0 = ref_orbit.seconds(dt.datetime.fromisoformat(rows[ref_date]["azimuth_time"]))
    length, width = grid["length"], grid["width"]
    incidence = np.empty((length, width), dtype=np.float32)
    azimuth = np.empty((length, width), dtype=np.float32)
    slant_range = np.empty((length, width), dtype=np.float32)
    lon2d_row = np.broadcast_to(grid["lon"], (row_chunk, width))

    for r0 in range(0, length, row_chunk):
        r1 = min(r0 + row_chunk, length)
        lat = np.broadcast_to(grid["lat"][r0:r1, None], (r1 - r0, width))
        lon = lon2d_row[: r1 - r0]
        xyz = llh_to_ecef(lat, lon, dem[r0:r1]).reshape(-1, 3)
        _, rng, pos, _ = ref_orbit.geo2rdr(xyz, ref_t0)
        inc, az = los_angles(xyz, pos, lat.reshape(-1), lon.reshape(-1))
        incidence[r0:r1] = inc.reshape(r1 - r0, width)
        azimuth[r0:r1] = az.reshape(r1 - r0, width)
        slant_range[r0:r1] = rng.reshape(r1 - r0, width)

    rc, cc = length // 2, width // 2
    centre = llh_to_ecef(grid["lat"][rc], grid["lon"][cc], dem[rc, cc])[None, :]
    t_c, rng_c, pos_c, vel_c = ref_orbit.geo2rdr(centre, ref_t0)
    centre_time = ref_orbit.epoch + dt.timedelta(seconds=float(t_c[0]))
    midnight = dt.datetime.combine(centre_time.date(), dt.time.min)

    sub_r = np.unique(np.r_[np.arange(0, length, baseline_step), length - 1])
    sub_c = np.unique(np.r_[np.arange(0, width, baseline_step), width - 1])
    lat_s, lon_s = np.meshgrid(grid["lat"][sub_r], grid["lon"][sub_c], indexing="ij")
    xyz_s = llh_to_ecef(lat_s, lon_s, dem[np.ix_(sub_r, sub_c)]).reshape(-1, 3)
    _, _, ref_pos, ref_vel = ref_orbit.geo2rdr(xyz_s, ref_t0)

    acquisitions = []
    for date in sorted(rows):
        orbit, orbit_name = orbits[date]
        t_guess = orbit.seconds(dt.datetime.fromisoformat(rows[date]["azimuth_time"]))
        t_sec, _, sec_pos, _ = orbit.geo2rdr(xyz_s, t_guess)
        bperp, bpar = perpendicular_baseline(xyz_s, ref_pos, ref_vel, sec_pos)
        bperp = bperp.reshape(len(sub_r), len(sub_c))
        t_zd, *_ = orbit.geo2rdr(centre, t_guess)
        entry = dict(rows[date])
        entry.update(
            {
                "orbit_file": orbit_name,
                "zero_doppler_time_centre": (
                    orbit.epoch + dt.timedelta(seconds=float(t_zd[0]))
                ).isoformat(),
                "bperp_top": float(bperp[0].mean()),
                "bperp_bottom": float(bperp[-1].mean()),
                "bperp_mean": float(bperp.mean()),
                "bperp_min": float(bperp.min()),
                "bperp_max": float(bperp.max()),
                "bpar_mean": float(bpar.mean()),
            }
        )
        acquisitions.append(entry)
        logger.info("Baseline %s: %.2f m (mean perpendicular)", date, entry["bperp_mean"])

    earth_radius = float(np.linalg.norm(centre[0]))
    scene = {
        "ORBIT_DIRECTION": rows[ref_date]["orbit_direction"],
        "HEADING": heading_at(pos_c[0], vel_c[0]),
        "CENTER_LINE_UTC": (centre_time - midnight).total_seconds(),
        "STARTING_RANGE": float(np.nanmin(slant_range)),
        "SLANT_RANGE_DISTANCE": float(rng_c[0]),
        "INCIDENCE_ANGLE": float(incidence[rc, cc]),
        "EARTH_RADIUS": earth_radius,
        "HEIGHT": float(np.linalg.norm(pos_c[0])) - earth_radius,
        "WAVELENGTH": S1_WAVELENGTH,
    }
    return {
        "grid": grid,
        "reference_date": ref_date,
        "incidence": incidence,
        "azimuth": azimuth,
        "slant_range": slant_range,
        "acquisitions": acquisitions,
        "scene": scene,
    }


def _write_float_tif(path: Path, data: np.ndarray, grid: dict) -> None:
    from osgeo import gdal

    gdal.UseExceptions()
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(str(path), grid["width"], grid["length"], 1, gdal.GDT_Float32,
                       options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(grid["geotransform"])
    ds.SetProjection(grid["projection"])
    ds.GetRasterBand(1).WriteArray(data)
    ds = None


def write_stack_metadata(result: dict, output_dir: str | Path, input_dir: str | Path,
                         dem_tif: str | Path) -> Path:
    """Write the angle rasters and ``stack_metadata.json`` into ``output_dir``."""
    from openeo2mintpy import __version__

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    grid = result["grid"]
    inc_path = output_dir / "incidenceAngle.tif"
    az_path = output_dir / "azimuthAngle.tif"
    _write_float_tif(inc_path, result["incidence"], grid)
    _write_float_tif(az_path, result["azimuth"], grid)

    context = read_job_context(input_dir)
    rlooks = int(context.get("n_rg_looks", 1))
    alooks = int(context.get("n_az_looks", 1))
    first = result["acquisitions"][0]
    radar = dict(result["scene"])
    radar.update(
        {
            "RANGE_PIXEL_SIZE": S1_RANGE_PIXEL_SIZE * rlooks,
            "AZIMUTH_PIXEL_SIZE": S1_AZIMUTH_PIXEL_SIZE * alooks,
            "PRF": S1_PRF,
            "RLOOKS": rlooks,
            "ALOOKS": alooks,
        }
    )
    doc = {
        "software": f"openeo2mintpy {__version__}",
        "created_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "burst_id": first["burst_id"],
        "sub_swath": first["sub_swath"],
        "relative_orbit": first["relative_orbit"],
        "reference_date": result["reference_date"],
        "dem_file": str(Path(dem_tif).resolve()),
        "geometry_files": {"incidenceAngle": str(inc_path.resolve()),
                           "azimuthAngle": str(az_path.resolve())},
        "grid": {
            "width": grid["width"],
            "length": grid["length"],
            "geotransform": list(grid["geotransform"]),
        },
        "radar": radar,
        "statistics": {
            "incidence_min": float(np.nanmin(result["incidence"])),
            "incidence_max": float(np.nanmax(result["incidence"])),
            "azimuth_min": float(np.nanmin(result["azimuth"])),
            "azimuth_max": float(np.nanmax(result["azimuth"])),
            "slant_range_min": float(np.nanmin(result["slant_range"])),
            "slant_range_max": float(np.nanmax(result["slant_range"])),
        },
        "acquisitions": result["acquisitions"],
    }
    try:
        doc["snap_crosscheck"] = crosscheck_baselines(input_dir, result["acquisitions"])
    except (OSError, ValueError, KeyError, SyntaxError) as exc:  # SyntaxError: XML ParseError
        logger.warning("SNAP baseline cross-check skipped: %s", exc)
        doc["snap_crosscheck"] = {"error": str(exc)}
    path = output_dir / "stack_metadata.json"
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    logger.info("Wrote %s", path)
    return path


def load_stack_metadata(path: str | Path) -> dict:
    """Read a ``stack_metadata.json`` file written by :func:`write_stack_metadata`."""
    with open(path) as f:
        doc = json.load(f)
    doc["baselines"] = {
        a["date"]: (a["bperp_top"], a["bperp_bottom"]) for a in doc["acquisitions"]
    }
    return doc


def build_stack_metadata(
    input_dir: str | Path,
    grid_tif: str | Path,
    dem_tif: str | Path,
    output_dir: str | Path,
    orbit_dir: str | Path | None = None,
    burst_id: int | None = None,
    sub_swath: str | None = None,
    ref_date: str | None = None,
) -> Path:
    """Run the full metadata chain for one openEO download directory.

    Acquisition table from the file names and the CDSE catalogue, orbit
    retrieval, geometry and baselines, then the output files.
    """
    from openeo2mintpy.acquisitions import build_acquisition_table

    table = build_acquisition_table(input_dir, burst_id=burst_id, sub_swath=sub_swath)
    orbits = load_orbits(table, orbit_dir or Path(output_dir) / "orbits")
    result = compute_stack_geometry(grid_tif, dem_tif, table, orbits, ref_date=ref_date)
    return write_stack_metadata(result, output_dir, input_dir, dem_tif)
