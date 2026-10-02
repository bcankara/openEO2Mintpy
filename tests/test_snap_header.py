"""Tests for openeo2mintpy.snap_header."""

from __future__ import annotations

import logging
import struct

import pytest

from openeo2mintpy.snap_header import (
    SNAP_TAG,
    _snap_date,
    crosscheck_baselines,
    read_dimap_header,
    read_tiff_tag,
    snap_pair_baselines,
)


def _tiff(value: bytes | None, order: str = ">", big: bool = False) -> bytes:
    """Minimal TIFF with an ImageWidth entry and, optionally, tag 65000 (ASCII)."""
    entries = [(256, 3, 1, struct.pack(order + "H", 4))]
    if value is not None:
        entries.append((SNAP_TAG, 2, len(value), value))
    head = (b"MM" if order == ">" else b"II")
    if big:
        head += struct.pack(order + "HHHQ", 43, 8, 0, 16)
        ifd_size = 8 + 20 * len(entries) + 8
        field, count_fmt, entry_fmt, off_fmt = 8, "Q", "HHQ", "Q"
    else:
        head += struct.pack(order + "HI", 42, 8)
        ifd_size = 2 + 12 * len(entries) + 4
        field, count_fmt, entry_fmt, off_fmt = 4, "H", "HHI", "I"
    data_offset = len(head) + ifd_size
    ifd, data = struct.pack(order + count_fmt, len(entries)), b""
    for tag, typ, count, raw in entries:
        ifd += struct.pack(order + entry_fmt, tag, typ, count)
        if len(raw) <= field:
            ifd += raw.ljust(field, b"\0")
        else:
            ifd += struct.pack(order + off_fmt, data_offset + len(data))
            data += raw
    ifd += struct.pack(order + off_fmt, 0)
    return head + ifd + data


def _header(ref: str, sec: str, value: float, reverse: float) -> bytes:
    """DIMAP header shaped like the CDSE products: ISO-8859-1, both pair directions."""
    def secondary(name, perp):
        return (f'<MDElem name="Secondary_{name}">'
                f'<MDATTR name="Perp Baseline" type="float64" mode="rw">{perp}</MDATTR>'
                f'<MDATTR name="Temp Baseline" type="float64" mode="rw">6.0</MDATTR></MDElem>')
    text = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<Dimap_Document name="tmp_geocoded_interferogram.dim"><Dataset_Sources>'
        '<MDElem name="metadata"><MDElem name="Abstracted_Metadata">'
        '<MDATTR name="incidence_near" type="float64">99999.0</MDATTR>'
        '<MDATTR name="comment" type="ascii">angle in °</MDATTR></MDElem>'
        '<MDElem name="Baselines">'
        f'<MDElem name="Ref_{ref}">{secondary(ref, 0.0)}{secondary(sec, value)}</MDElem>'
        f'<MDElem name="Ref_{sec}">{secondary(ref, reverse)}{secondary(sec, 0.0)}</MDElem>'
        '</MDElem></MDElem></Dataset_Sources></Dimap_Document>'
    )
    return text.encode("latin-1") + b"\0"


HEADER = _header("25Nov2025", "01Dec2025", -29.221569061279297, 29.20039176940918)


def test_snap_date():
    assert _snap_date("Ref_25Nov2025") == "20251125"
    assert _snap_date("Secondary_01Dec2025") == "20251201"
    with pytest.raises(ValueError):
        _snap_date("Ref_2025-11-25")


@pytest.mark.parametrize("order,big", [(">", False), ("<", False), (">", True), ("<", True)])
def test_read_header_all_layouts(tmp_path, order, big):
    path = tmp_path / "a.tif"
    path.write_bytes(_tiff(HEADER, order, big))
    assert read_tiff_tag(path, SNAP_TAG) == HEADER
    root = read_dimap_header(path)
    assert root.tag == "Dimap_Document"
    assert snap_pair_baselines(root) == {
        ("20251125", "20251125"): 0.0,
        ("20251125", "20251201"): pytest.approx(-29.221569061279297),
        ("20251201", "20251125"): pytest.approx(29.20039176940918),
        ("20251201", "20251201"): 0.0,
    }


def test_inline_value_and_missing_tag(tmp_path):
    path = tmp_path / "small.tif"
    path.write_bytes(_tiff(b"ab\0"))
    assert read_tiff_tag(path, SNAP_TAG) == b"ab\0"
    assert read_tiff_tag(path, 256) == struct.pack(">H", 4)
    path.write_bytes(_tiff(None))
    assert read_tiff_tag(path, SNAP_TAG) is None
    assert read_dimap_header(path) is None


def test_not_a_tiff(tmp_path):
    path = tmp_path / "x.tif"
    path.write_bytes(b"PK\3\4 not a tiff")
    with pytest.raises(ValueError, match="not a TIFF"):
        read_tiff_tag(path, SNAP_TAG)


def _stack(tmp_path):
    (tmp_path / "phase_coh_20251125T153521_20251201T153418.tif").write_bytes(_tiff(HEADER))
    (tmp_path / "phase_coh_20251201T153418_20251207T153520.tif").write_bytes(_tiff(None))
    return tmp_path


def test_crosscheck_agrees(tmp_path, caplog):
    acquisitions = [{"date": "20251125", "bperp_mean": 0.0},
                    {"date": "20251201", "bperp_mean": 29.223455976993154},
                    {"date": "20251207", "bperp_mean": -12.0}]
    with caplog.at_level(logging.INFO):
        out = crosscheck_baselines(_stack(tmp_path), acquisitions)
    assert out["products"] == 2
    assert out["products_with_snap_header"] == 1
    assert out["pairs_compared"] == 1
    pair = out["pairs"][0]
    assert pair["pair"] == "20251125_20251201"
    assert pair["snap_negated_m"] == pytest.approx(29.221569061279297)
    assert pair["difference_m"] == pytest.approx(0.0018869, abs=1e-6)
    assert out["max_abs_difference_m"] == pytest.approx(0.0018869, abs=1e-6)
    assert "agree with SNAP" in caplog.text


def test_crosscheck_warns_on_mismatch(tmp_path, caplog):
    acquisitions = [{"date": "20251125", "bperp_mean": 0.0},
                    {"date": "20251201", "bperp_mean": -29.2}]
    with caplog.at_level(logging.WARNING):
        out = crosscheck_baselines(_stack(tmp_path), acquisitions)
    assert out["max_abs_difference_m"] == pytest.approx(58.42, abs=0.01)
    assert "differ by up to" in caplog.text


def test_crosscheck_without_headers(tmp_path):
    (tmp_path / "phase_coh_20251125T153521_20251201T153418.tif").write_bytes(_tiff(None))
    out = crosscheck_baselines(tmp_path, [{"date": "20251125", "bperp_mean": 0.0}])
    assert out["products_with_snap_header"] == 0
    assert out["pairs_compared"] == 0
    assert "max_abs_difference_m" not in out
