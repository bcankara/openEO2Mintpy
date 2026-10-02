"""
Acquisition metadata for openEO Sentinel-1 burst interferograms.

The openEO/ClouDInSAR products carry no SAR metadata apart from the
acquisition start times embedded in their file names. This module turns
those times into a per-date acquisition table by matching them against
the public CDSE burst catalogue (no authentication required), which
records the platform, orbit direction, relative orbit, burst azimuth
time and parent SLC product of every burst.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

CDSE_BURSTS_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Bursts"

ACQ_PAIR_PATTERN = re.compile(r"(\d{8}T\d{6})_(\d{8}T\d{6})")


def parse_catalogue_time(text: str) -> dt.datetime:
    """Parse an ISO-8601 catalogue timestamp into a naive UTC datetime."""
    s = text.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised catalogue time: {text!r}")


def parse_acquisition_times(input_dir: str | Path) -> dict[str, dt.datetime]:
    """Collect the acquisition start time of every date in an openEO download.

    openEO file names such as ``phase_coh_20251125T153521_20251201T153418.tif``
    hold the burst start time of the primary and secondary acquisitions.

    Returns
    -------
    dict
        ``{YYYYMMDD: datetime}`` for every date found in the file names.

    Raises
    ------
    ValueError
        If one date appears with two different times.
    """
    times: dict[str, dt.datetime] = {}
    for path in sorted(Path(input_dir).glob("*.tif*")):
        m = ACQ_PAIR_PATTERN.search(path.name)
        if not m:
            continue
        for token in m.groups():
            t = dt.datetime.strptime(token, "%Y%m%dT%H%M%S")
            key = t.strftime("%Y%m%d")
            if key in times and times[key] != t:
                raise ValueError(
                    f"Date {key} appears with two start times: {times[key]} and {t}"
                )
            times[key] = t
    return times


def read_job_context(input_dir: str | Path) -> dict:
    """Read the ClouDInSAR job parameters stored in an openEO job-results file.

    ``download_job_results`` saves ``job-results.json`` next to the GeoTIFFs;
    its process graph repeats the parameters (burst id, sub-swath,
    polarisation, looks) that produced the products: as the ``context`` of a
    ``run_cwl_to_stac`` node for a CWL workflow, or as the arguments of a
    ``sentinel1_sar_interferogram`` node for the published CDSE process.

    Returns
    -------
    dict
        The job parameters, or an empty dict when no job-results file is found.
    """
    for path in sorted(Path(input_dir).glob("*.json")):
        try:
            with open(path) as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        for provider in doc.get("providers", []):
            nodes = provider.get("processing:expression", {}).get("expression", {})
            for node in nodes.values():
                if node.get("process_id") == "run_cwl_to_stac":
                    return dict(node.get("arguments", {}).get("context", {}))
                if node.get("process_id") == "sentinel1_sar_interferogram":
                    return dict(node.get("arguments", {}))
    return {}


def query_burst_records(
    burst_id: int,
    sub_swath: str,
    start: dt.datetime,
    end: dt.datetime,
    polarisation: str = "VV",
    timeout: float = 120.0,
) -> list[dict]:
    """Return every catalogue record of one burst between two times."""
    query = (
        f"BurstId eq {int(burst_id)} and "
        f"SwathIdentifier eq '{sub_swath.upper()}' and "
        f"PolarisationChannels eq '{polarisation.upper()}' and "
        f"ContentDate/Start ge {start:%Y-%m-%dT%H:%M:%S}.000Z and "
        f"ContentDate/Start le {end:%Y-%m-%dT%H:%M:%S}.000Z"
    )
    records: list[dict] = []
    url = f"{CDSE_BURSTS_URL}?$filter={urllib.parse.quote(query)}&$top=1000"
    while url:
        req = urllib.request.Request(url, headers={"User-Agent": "openeo2mintpy"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            page = json.loads(response.read().decode("utf-8"))
        records.extend(page.get("value", []))
        url = page.get("@odata.nextLink")
    logger.info("CDSE catalogue returned %d records for burst %s %s", len(records),
                burst_id, sub_swath)
    return records


def match_acquisitions(
    times: dict[str, dt.datetime],
    records: list[dict],
    tolerance_s: float = 2.0,
) -> list[dict]:
    """Pair every acquisition time with its catalogue record.

    Returns
    -------
    list of dict
        One entry per date, sorted by date, with keys ``date``,
        ``start_time``, ``azimuth_time``, ``platform``, ``orbit_direction``,
        ``relative_orbit``, ``burst_id``, ``sub_swath`` and ``parent_product``.

    Raises
    ------
    LookupError
        If a date has no catalogue record within the tolerance.
    """
    table = []
    for date, t in sorted(times.items()):
        best, best_dt = None, None
        for rec in records:
            begin = rec.get("BeginningDateTime") or rec.get("ContentDate", {}).get("Start")
            if not begin:
                continue
            delta = abs((parse_catalogue_time(begin) - t).total_seconds())
            if delta <= tolerance_s and (best_dt is None or delta < best_dt):
                best, best_dt = rec, delta
        if best is None:
            raise LookupError(
                f"No catalogue record within {tolerance_s} s of {t:%Y-%m-%dT%H:%M:%S}"
            )
        table.append(
            {
                "date": date,
                "start_time": parse_catalogue_time(best["BeginningDateTime"]).isoformat(),
                "azimuth_time": parse_catalogue_time(best["AzimuthTime"]).isoformat(),
                "platform": "S1" + best["PlatformSerialIdentifier"],
                "orbit_direction": best["OrbitDirection"].upper(),
                "relative_orbit": int(best["RelativeOrbitNumber"]),
                "burst_id": int(best["BurstId"]),
                "sub_swath": best["SwathIdentifier"],
                "parent_product": best.get("ParentProductName", ""),
            }
        )
    return table


def build_acquisition_table(
    input_dir: str | Path,
    burst_id: int | None = None,
    sub_swath: str | None = None,
    polarisation: str | None = None,
) -> list[dict]:
    """Build the acquisition table for an openEO download directory.

    ``burst_id``, ``sub_swath`` and ``polarisation`` default to the values
    recorded in the directory's ``job-results.json``.

    Raises
    ------
    ValueError
        If the burst cannot be identified or the stack mixes orbit directions.
    """
    context = read_job_context(input_dir)
    burst_id = burst_id if burst_id is not None else context.get("burst_id")
    sub_swath = sub_swath or context.get("sub_swath")
    polarisation = polarisation or context.get("polarization") or "VV"
    if burst_id is None or not sub_swath:
        raise ValueError(
            "Burst id and sub-swath are unknown: pass them explicitly or keep the "
            "openEO job-results.json next to the downloaded GeoTIFFs."
        )

    times = parse_acquisition_times(input_dir)
    if not times:
        raise ValueError(f"No openEO acquisition times found in file names under {input_dir}")

    margin = dt.timedelta(minutes=5)
    records = query_burst_records(
        burst_id, sub_swath, min(times.values()) - margin, max(times.values()) + margin,
        polarisation=polarisation,
    )
    table = match_acquisitions(times, records)

    directions = {row["orbit_direction"] for row in table}
    if len(directions) != 1:
        raise ValueError(f"Stack mixes orbit directions: {sorted(directions)}")
    return table
