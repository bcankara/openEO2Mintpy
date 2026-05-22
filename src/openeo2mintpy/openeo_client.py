"""
Copernicus Data Space Ecosystem (CDSE) openEO client for InSAR automation.

Handles connection, OIDC authentication, catalogue querying to find dates,
pair generation, and batch job submission/monitoring.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import openeo
from openeo.internal.graph_building import PGNode
from openeo.rest.stac_resource import StacResource

logger = logging.getLogger(__name__)

DEFAULT_BACKEND = "https://openeo.dataspace.copernicus.eu"
DEFAULT_CWL_URL = (
    "https://raw.githubusercontent.com/cloudinsar/s1-workflows/"
    "refs/heads/keep_snap_metadata/cwl/sar_interferogram.cwl"
)


def is_headless_or_wsl() -> bool:
    """Detect if running in a WSL or headless Linux environment."""
    # WSL checks
    if "WSL_DISTRO_NAME" in os.environ or "WSL_INTEROP" in os.environ:
        return True

    # Alternative WSL check (by kernel version string)
    if platform.system() == "Linux":
        try:
            with open("/proc/version") as f:
                if "microsoft" in f.read().lower():
                    return True
        except Exception:
            pass

    # Headless Linux check (no display manager)
    if (
        platform.system() == "Linux"
        and not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
    ):
        return True

    return False


def connect_and_auth(
    url: str = DEFAULT_BACKEND,
    display: Callable | None = None,
) -> openeo.Connection:
    """Connect to openEO backend and authenticate via OIDC.

    Falls back to Device Code flow in headless or WSL environments.

    Parameters
    ----------
    url : str
        openEO backend URL.
    display : callable, optional
        Custom display function for Device Code flow instructions.
        Receives the verification URL/code message string.
        If *None*, the openEO library default (``print``) is used.
    """
    logger.info("Connecting to openEO backend at %s", url)
    connection = openeo.connect(url)

    use_device_flow = is_headless_or_wsl()

    # Build extra kwargs for authenticate_oidc_device
    device_kwargs: dict[str, Any] = {}
    if display is not None:
        device_kwargs["display"] = display

    if use_device_flow:
        logger.info("Headless/WSL environment detected. Initiating OIDC Device Code flow...")
        connection.authenticate_oidc_device(store_refresh_token=True, **device_kwargs)
    else:
        logger.info("Authenticating via OIDC...")
        try:
            connection.authenticate_oidc(store_refresh_token=True)
        except Exception as e:
            logger.warning(
                "OIDC authentication failed or blocked: %s. Falling back to Device Code flow...",
                e,
            )
            connection.authenticate_oidc_device(store_refresh_token=True, **device_kwargs)

    logger.info("Authentication successful.")
    return connection


def query_burst_acquisitions(
    start_date: str,
    end_date: str,
    polarisation: str,
    aoi_wkt: str,
) -> list[dict[str, Any]]:
    """Query CDSE Catalogue to find Sentinel-1 burst acquisitions.

    Parameters
    ----------
    start_date : str
        Start date in YYYY-MM-DD format.
    end_date : str
        End date in YYYY-MM-DD format.
    polarisation : str
        Polarisation channel, e.g., 'VV'.
    aoi_wkt : str
        WKT polygon representation of the Area of Interest.

    Returns
    -------
    list of dict
        Burst acquisitions metadata from CDSE catalog.
    """
    query = (
        f"ContentDate/Start ge {start_date}T00:00:00.000Z and "
        f"ContentDate/Start le {end_date}T23:59:59.000Z and "
        f"PolarisationChannels eq '{polarisation.upper()}' and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{aoi_wkt}')"
    )
    url = f"https://catalogue.dataspace.copernicus.eu/odata/v1/Bursts?$filter={urllib.parse.quote(query)}&$top=1000"

    logger.info("Querying CDSE Catalogue with query: %s", query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req) as response:
            content = response.read().decode("utf-8")
            data = json.loads(content)
            return data.get("value", [])
    except Exception as e:
        logger.error("Error querying CDSE catalogue: %s", e)
        raise RuntimeError(f"Failed to query CDSE catalogue: {e}") from e


def filter_bursts(
    bursts: list[dict[str, Any]],
    track: int,
    burst_id: int,
    sub_swath: str,
) -> list[str]:
    """Filter burst acquisitions by track, burst_id, sub_swath, and platform (A/C).

    Returns a sorted list of unique acquisition dates (YYYY-MM-DD).
    """
    dates = set()
    for b in bursts:
        b_track = b.get("RelativeOrbitNumber")
        b_burst_id = b.get("BurstId")
        b_swath = b.get("SwathIdentifier")
        platform = b.get("PlatformSerialIdentifier", "UNKNOWN")
        date_str = b.get("BeginningDateTime", b.get("ContentDate", {}).get("Start", ""))[:10]

        if not date_str or platform not in ["A", "C"]:
            continue

        if b_track == track and b_burst_id == burst_id and b_swath == sub_swath:
            dates.add(date_str)

    return sorted(list(dates))


def extract_unique_bursts(
    bursts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract unique (Track, Direction, Swath, BurstId) combinations.

    Returns a list of dicts, each with keys:
    ``track``, ``direction``, ``swath``, ``burst_id``, ``count``.
    Sorted by track, then burst_id.
    """
    combos: dict[tuple, int] = {}
    for b in bursts:
        platform = b.get("PlatformSerialIdentifier", "UNKNOWN")
        if platform not in ["A", "C"]:
            continue
        key = (
            b.get("RelativeOrbitNumber"),
            b.get("OrbitDirection", "UNKNOWN"),
            b.get("SwathIdentifier", "UNKNOWN"),
            b.get("BurstId"),
        )
        combos[key] = combos.get(key, 0) + 1

    results = []
    for (track, direction, swath, burst_id), count in sorted(combos.items()):
        results.append({
            "track": track,
            "direction": direction,
            "swath": swath,
            "burst_id": burst_id,
            "count": count,
        })
    return results


def generate_pairs(dates: list[str], max_baseline_days: int = 24) -> list[list[str]]:
    """Generate InSAR pairs from a list of sorted dates.

    Creates sequential (i -> i+1) and skip-one (i -> i+2) pairs
    if their temporal baseline is within the max_baseline_days.
    """
    dt_dates = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
    pairs = []
    n = len(dates)

    for i in range(n):
        # 1. Connect to next date (sequential)
        if i + 1 < n:
            delta = (dt_dates[i + 1] - dt_dates[i]).days
            if delta <= max_baseline_days:
                pairs.append([dates[i], dates[i + 1]])
        # 2. Connect to second next date (skip-one)
        if i + 2 < n:
            delta = (dt_dates[i + 2] - dt_dates[i]).days
            if delta <= max_baseline_days:
                pairs.append([dates[i], dates[i + 2]])

    return pairs


def split_pairs_into_groups(pairs: list[list[str]]) -> list[list[list[str]]]:
    """Split pairs into sub-jobs with unique primary (reference) dates.

    This ensures that each batch job does not contain duplicate primary dates,
    satisfying the CWL workflow's constraints.
    """
    groups: list[list[list[str]]] = []
    for pair in pairs:
        primary_date = pair[0]
        placed = False
        for group in groups:
            # Check if this primary date already exists in the group
            if not any(p[0] == primary_date for p in group):
                group.append(pair)
                placed = True
                break
        if not placed:
            groups.append([pair])
    return groups


def submit_insar_job(
    connection: openeo.Connection,
    track: int,
    direction: str,
    burst_id: int,
    sub_swath: str,
    group_pairs: list[list[str]],
    part_num: int,
    total_parts: int,
    cwl_url: str = DEFAULT_CWL_URL,
    test_only: bool = False,
) -> dict[str, Any]:
    """Submit a single InSAR batch job to openEO."""
    params = {
        "burst_id": burst_id,
        "polarization": "vv",
        "sub_swath": sub_swath,
        "InSAR_pairs": group_pairs,
        "coherence_window_rg": 10,
        "coherence_window_az": 2,
        "n_rg_looks": 4,
        "n_az_looks": 1,
    }

    logger.info("Defining run_cwl_to_stac resource for Part %d...", part_num)
    stac_resource = StacResource(
        graph=PGNode(
            process_id="run_cwl_to_stac",
            arguments={
                "cwl_url": cwl_url,
                "context": params,
            },
        ),
        connection=connection,
    )

    pairs_label = (
        f"{group_pairs[0][0].replace('-', '')}_{group_pairs[-1][1].replace('-', '')}"
        if not test_only
        else "test"
    )
    merge_prefix = (
        f"/interferogram/T{track}/{burst_id}/{sub_swath}/{pairs_label}_Part{part_num}"
    )

    stac_resource = stac_resource.export_workspace(
        "insar-results-workspace",
        merge=merge_prefix,
    )

    job_title = f"InSAR_T{track}_{direction}_MintPy_Part{part_num}"
    if test_only:
        job_title += "_TEST"

    logger.info("Creating batch job: '%s'...", job_title)
    job = stac_resource.create_job(
        title=job_title,
        job_options={"python-memory": "4000m"},
    )
    logger.info("Created Job ID: %s", job.job_id)

    return {
        "job_id": job.job_id,
        "track": track,
        "direction": direction,
        "burst_id": burst_id,
        "sub_swath": sub_swath,
        "pairs_count": len(group_pairs),
        "title": job_title,
        "part": part_num,
        "total_parts": total_parts,
    }


def download_job_results(
    connection: openeo.Connection,
    job_id: str,
    output_dir: str | Path,
) -> int:
    """Download results of a completed batch job.

    Returns the number of files downloaded.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    logger.info("Retrieving job %s results...", job_id)
    job = connection.job(job_id)
    results = job.get_results()
    logger.info("Downloading files to %s...", output_path)
    files = results.download_files(target=str(output_path))
    logger.info("Successfully downloaded %d files.", len(files))
    return len(files)
