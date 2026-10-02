"""
Sentinel-1 precise orbits: retrieval, interpolation and zero-Doppler geometry.

Precise (AUX_POEORB) and restituted (AUX_RESORB) orbit files are read from
the public ``s1-orbits`` bucket that ASF maintains on the AWS Registry of
Open Data, so no account or token is needed. State vectors are
interpolated with an eight-point Lagrange polynomial and the zero-Doppler
time of each ground target is found by Newton iteration.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

ORBIT_BUCKET_URL = "https://s1-orbits.s3.us-west-2.amazonaws.com"
GM_EARTH = 3.986004418e14

_ORBIT_NAME = re.compile(
    r"(S1[A-D])_OPER_(AUX_(?:POE|RES)ORB)_OPOD_(\d{8}T\d{6})_V(\d{8}T\d{6})_(\d{8}T\d{6})\.EOF"
)


@dataclass
class OrbitFile:
    """Name and validity window of one orbit file in the bucket."""

    key: str
    platform: str
    orbit_type: str
    production: dt.datetime
    valid_start: dt.datetime
    valid_stop: dt.datetime

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]


def parse_orbit_name(key: str) -> OrbitFile | None:
    """Parse an orbit file name; return None for unrelated keys."""
    m = _ORBIT_NAME.search(key)
    if not m:
        return None
    fmt = "%Y%m%dT%H%M%S"
    return OrbitFile(
        key=key,
        platform=m.group(1),
        orbit_type=m.group(2),
        production=dt.datetime.strptime(m.group(3), fmt),
        valid_start=dt.datetime.strptime(m.group(4), fmt),
        valid_stop=dt.datetime.strptime(m.group(5), fmt),
    )


def list_orbit_files(prefix: str, timeout: float = 120.0) -> list[OrbitFile]:
    """List the orbit files in the bucket whose key starts with ``prefix``."""
    files: list[OrbitFile] = []
    token = None
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    while True:
        params = {"list-type": "2", "prefix": prefix}
        if token:
            params["continuation-token"] = token
        url = f"{ORBIT_BUCKET_URL}/?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=timeout) as response:
            root = ET.fromstring(response.read())
        for node in root.findall("s3:Contents/s3:Key", ns):
            parsed = parse_orbit_name(node.text or "")
            if parsed:
                files.append(parsed)
        if root.findtext("s3:IsTruncated", default="false", namespaces=ns) != "true":
            break
        token = root.findtext("s3:NextContinuationToken", namespaces=ns)
    return files


def select_orbit_file(
    candidates: list[OrbitFile],
    platform: str,
    t: dt.datetime,
    margin_s: float = 300.0,
) -> OrbitFile | None:
    """Pick the most recently produced file that covers ``t`` with a margin."""
    margin = dt.timedelta(seconds=margin_s)
    covering = [
        c for c in candidates
        if c.platform == platform and c.valid_start <= t - margin and c.valid_stop >= t + margin
    ]
    if not covering:
        return None
    return max(covering, key=lambda c: c.production)


def _month_prefixes(orbit_type: str, platform: str, months: list[dt.date]) -> list[str]:
    return [f"{orbit_type}/{platform}_OPER_{orbit_type}_OPOD_{m:%Y%m}" for m in months]


def find_orbit_file(platform: str, t: dt.datetime) -> OrbitFile:
    """Find the orbit file for one acquisition, preferring POEORB over RESORB.

    POEORB files are produced about three weeks after acquisition, so the
    production months of the acquisition and the following month are listed.

    Raises
    ------
    LookupError
        If neither a precise nor a restituted orbit covers ``t``.
    """
    first = t.date().replace(day=1)
    following = (first + dt.timedelta(days=32)).replace(day=1)
    for orbit_type, months in (("AUX_POEORB", [first, following]), ("AUX_RESORB", [first])):
        candidates: list[OrbitFile] = []
        for prefix in _month_prefixes(orbit_type, platform, months):
            candidates.extend(list_orbit_files(prefix))
        chosen = select_orbit_file(candidates, platform, t)
        if chosen:
            return chosen
    raise LookupError(f"No {platform} orbit file covers {t:%Y-%m-%dT%H:%M:%S}")


def download_orbit_file(orbit: OrbitFile, cache_dir: str | Path, timeout: float = 300.0) -> Path:
    """Download an orbit file into ``cache_dir`` unless it is already there."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / orbit.name
    if target.exists() and target.stat().st_size > 0:
        return target
    url = f"{ORBIT_BUCKET_URL}/{urllib.parse.quote(orbit.key)}"
    logger.info("Downloading %s", orbit.name)
    tmp = target.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=timeout) as response, open(tmp, "wb") as f:
        f.write(response.read())
    tmp.replace(target)
    return target


class Orbit:
    """State vectors of one orbit file with Lagrange interpolation.

    Times are held as seconds from ``epoch`` so that float64 keeps
    sub-microsecond resolution.
    """

    ORDER = 8

    def __init__(self, times: list[dt.datetime], pos: np.ndarray, vel: np.ndarray):
        if len(times) < self.ORDER:
            raise ValueError("At least eight state vectors are required.")
        self.epoch = times[0]
        self.t = np.array([(ti - self.epoch).total_seconds() for ti in times])
        if np.any(np.diff(self.t) <= 0):
            raise ValueError("State vector times must increase strictly.")
        self.pos = np.asarray(pos, dtype=np.float64)
        self.vel = np.asarray(vel, dtype=np.float64)

    @classmethod
    def from_eof(cls, path: str | Path, start: dt.datetime | None = None,
                 stop: dt.datetime | None = None) -> Orbit:
        """Read an ESA Earth Explorer orbit file (optionally a time window only)."""
        times, pos, vel = [], [], []
        for osv in ET.parse(path).getroot().iter("OSV"):
            t = dt.datetime.strptime(osv.findtext("UTC")[4:], "%Y-%m-%dT%H:%M:%S.%f")
            if (start and t < start) or (stop and t > stop):
                continue
            times.append(t)
            pos.append([float(osv.findtext(k)) for k in ("X", "Y", "Z")])
            vel.append([float(osv.findtext(k)) for k in ("VX", "VY", "VZ")])
        return cls(times, np.array(pos), np.array(vel))

    def seconds(self, t: dt.datetime) -> float:
        """Convert a datetime into seconds from the orbit epoch."""
        return (t - self.epoch).total_seconds()

    def interpolate(self, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Interpolate position and velocity at times ``t`` (seconds from epoch)."""
        t = np.atleast_1d(np.asarray(t, dtype=np.float64))
        if t.min() < self.t[0] or t.max() > self.t[-1]:
            raise ValueError("Requested time lies outside the orbit state vectors.")
        n = self.ORDER
        i0 = np.clip(np.searchsorted(self.t, t) - n // 2, 0, len(self.t) - n)
        idx = i0[:, None] + np.arange(n)[None, :]
        tk = self.t[idx]
        w = np.ones_like(tk)
        for j in range(n):
            for m in range(n):
                if m != j:
                    w[:, j] *= (t - tk[:, m]) / (tk[:, j] - tk[:, m])
        pos = np.einsum("ij,ijk->ik", w, self.pos[idx])
        vel = np.einsum("ij,ijk->ik", w, self.vel[idx])
        return pos, vel

    def geo2rdr(self, xyz: np.ndarray, t_guess: float, tol: float = 1e-9,
                max_iter: int = 30) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Solve the zero-Doppler time of ECEF targets.

        Parameters
        ----------
        xyz : ndarray, shape (N, 3)
            Target positions in ECEF metres.
        t_guess : float
            Starting time in seconds from the orbit epoch.

        Returns
        -------
        t, slant_range, sat_pos, sat_vel
            Zero-Doppler times (seconds from epoch), slant ranges (m) and the
            interpolated satellite state at those times.
        """
        xyz = np.atleast_2d(np.asarray(xyz, dtype=np.float64))
        t = np.full(len(xyz), float(t_guess))
        for _ in range(max_iter):
            pos, vel = self.interpolate(t)
            d = pos - xyz
            acc = -GM_EARTH * pos / np.linalg.norm(pos, axis=1, keepdims=True) ** 3
            f = np.einsum("ij,ij->i", d, vel)
            fp = np.einsum("ij,ij->i", vel, vel) + np.einsum("ij,ij->i", d, acc)
            step = f / fp
            t -= step
            if np.max(np.abs(step)) < tol:
                break
        else:
            raise RuntimeError("Zero-Doppler iteration did not converge.")
        pos, vel = self.interpolate(t)
        return t, np.linalg.norm(pos - xyz, axis=1), pos, vel
