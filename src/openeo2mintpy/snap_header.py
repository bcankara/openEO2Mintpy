"""SNAP metadata embedded in openEO/ClouDInSAR GeoTIFFs.

ESA SNAP writes the DIMAP header of a product into private TIFF tag 65000.
Products of the earlier ``keep_snap_metadata`` workflow branch do not carry
this tag; products of the current CDSE process do. When it is present, the
``Baselines`` element gives SNAP's perpendicular baseline of every pair,
which is used here as an independent check of the reconstructed baselines.

SNAP signs the baseline of secondary date ``d2`` against reference date
``d1`` (element ``Ref_<d1>/Secondary_<d2>``) opposite to the ISCE2
convention used by openeo2mintpy, so the reconstructed pair baseline
``B(d2) - B(d1)`` is compared with the negated SNAP value.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

SNAP_TAG = 65000
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8}
_PAIR = re.compile(r"(\d{8})T\d{6}_(\d{8})T\d{6}")


def read_tiff_tag(path: str | Path, tag: int) -> bytes | None:
    """Return the raw value of ``tag`` in the first IFD of a TIFF or BigTIFF file."""
    with open(path, "rb") as f:
        order = f.read(2)
        if order not in (b"II", b"MM"):
            raise ValueError(f"{path}: not a TIFF file")
        e = "<" if order == b"II" else ">"
        magic = struct.unpack(e + "H", f.read(2))[0]
        if magic == 42:
            f.seek(struct.unpack(e + "I", f.read(4))[0])
            count = struct.unpack(e + "H", f.read(2))[0]
            fmt, entry, offset_fmt = "HHI4s", 12, "I"
        elif magic == 43:
            f.read(4)
            f.seek(struct.unpack(e + "Q", f.read(8))[0])
            count = struct.unpack(e + "Q", f.read(8))[0]
            fmt, entry, offset_fmt = "HHQ8s", 20, "Q"
        else:
            raise ValueError(f"{path}: unknown TIFF version {magic}")
        entries = [struct.unpack(e + fmt, f.read(entry)) for _ in range(count)]
        for t, typ, n, raw in entries:
            if t != tag:
                continue
            size = _TYPE_SIZE.get(typ, 1) * n
            if size <= len(raw):
                return raw[:size]
            f.seek(struct.unpack(e + offset_fmt, raw)[0])
            return f.read(size)
    return None


def read_dimap_header(path: str | Path) -> ET.Element | None:
    """Parse the SNAP DIMAP header of a GeoTIFF, or return None when it has none."""
    raw = read_tiff_tag(path, SNAP_TAG)
    if raw is None:
        return None
    # Bytes, so that the encoding declared by SNAP (ISO-8859-1) is honoured.
    return ET.fromstring(raw.rstrip(b"\0"))


_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}


def _snap_date(name: str) -> str:
    """'Ref_25Nov2025' or 'Secondary_01Dec2025' -> '20251125' (independent of the locale)."""
    m = re.fullmatch(r"(\d{2})([A-Za-z]{3})(\d{4})", name.split("_", 1)[1])
    if not m:
        raise ValueError(f"unexpected SNAP date label: {name}")
    day, month, year = m.groups()
    return dt.date(int(year), _MONTHS[month.upper()], int(day)).strftime("%Y%m%d")


def snap_pair_baselines(root: ET.Element) -> dict[tuple[str, str], float]:
    """Return SNAP's perpendicular baselines keyed by (reference date, secondary date)."""
    out: dict[tuple[str, str], float] = {}
    for element in root.iter("MDElem"):
        if element.get("name") != "Baselines":
            continue
        for ref in element.findall("MDElem"):
            for sec in ref.findall("MDElem"):
                value = sec.find("MDATTR[@name='Perp Baseline']")
                if value is not None and value.text:
                    key = (_snap_date(ref.get("name")), _snap_date(sec.get("name")))
                    out[key] = float(value.text)
        break
    return out


def crosscheck_baselines(input_dir: str | Path, acquisitions: list[dict],
                         tolerance_m: float = 1.0) -> dict:
    """Compare reconstructed pair baselines with SNAP's, where the products carry them.

    Parameters
    ----------
    input_dir : str or Path
        Directory of the three-band openEO GeoTIFFs.
    acquisitions : list of dict
        Acquisition records with ``date`` and ``bperp_mean`` (relative to the
        stack reference, ISCE2 sign convention).
    tolerance_m : float
        Differences above this value are reported as a warning.
    """
    bperp = {a["date"]: float(a["bperp_mean"]) for a in acquisitions}
    files = sorted(Path(input_dir).glob("*.tif"))
    pairs = []
    with_header = 0
    for path in files:
        m = _PAIR.search(path.name)
        if not m:
            continue
        root = read_dimap_header(path)
        if root is None:
            continue
        with_header += 1
        d1, d2 = m.groups()
        snap = snap_pair_baselines(root).get((d1, d2))
        if snap is None or d1 not in bperp or d2 not in bperp:
            continue
        reconstructed = bperp[d2] - bperp[d1]
        pairs.append({"pair": f"{d1}_{d2}", "reconstructed_m": reconstructed,
                      "snap_negated_m": -snap, "difference_m": reconstructed + snap})
    summary = {"products": len(files), "products_with_snap_header": with_header,
               "pairs_compared": len(pairs), "tolerance_m": tolerance_m, "pairs": pairs}
    if pairs:
        diffs = [abs(p["difference_m"]) for p in pairs]
        summary["max_abs_difference_m"] = max(diffs)
        summary["rms_difference_m"] = (sum(d * d for d in diffs) / len(diffs)) ** 0.5
        if summary["max_abs_difference_m"] > tolerance_m:
            logger.warning("Reconstructed and SNAP baselines differ by up to %.2f m "
                           "(tolerance %.2f m).", summary["max_abs_difference_m"], tolerance_m)
        else:
            logger.info("Reconstructed baselines agree with SNAP's within %.3f m over %d pairs.",
                        summary["max_abs_difference_m"], len(pairs))
    return summary
