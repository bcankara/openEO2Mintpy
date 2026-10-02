"""Tests for openeo2mintpy.config."""

from pathlib import Path

from openeo2mintpy.config import generate_mintpy_config


def _values(config_path):
    out = {}
    for line in Path(config_path).read_text().splitlines():
        if line.startswith("mintpy.") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def test_relative_inputs_become_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for d in ("work/unw", "work/cor", "work/geometry"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "work/unw/20240101_20240113.unw.tif").write_bytes(b"x")
    (tmp_path / "work/cor/20240101_20240113.cor.tif").write_bytes(b"x")

    cfg = generate_mintpy_config(
        "work/mintpy", "work/unw", cor_dir="work/cor", dem_file="work/geometry/dem.tif",
        inc_angle_file="work/geometry/incidenceAngle.tif",
    )
    assert cfg.is_absolute()
    values = _values(cfg)
    assert values["mintpy.load.unwFile"] == str(tmp_path / "work/unw" / "*.unw.tif")
    assert values["mintpy.load.corFile"] == str(tmp_path / "work/cor" / "*.cor.tif")
    assert values["mintpy.load.demFile"] == str(tmp_path / "work/geometry/dem.tif")
    assert values["mintpy.load.incAngleFile"] == str(tmp_path / "work/geometry/incidenceAngle.tif")
    assert values["mintpy.load.azAngleFile"] == "auto"
