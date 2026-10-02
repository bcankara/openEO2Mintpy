"""Tests for openeo2mintpy.orbit."""

import datetime as dt

import numpy as np
import pytest

from openeo2mintpy.orbit import Orbit, parse_orbit_name, select_orbit_file

EOF_TEMPLATE = """<?xml version="1.0" ?>
<Earth_Explorer_File>
  <Data_Block type="xml">
    <List_of_OSVs count="{n}">
{osvs}
    </List_of_OSVs>
  </Data_Block>
</Earth_Explorer_File>
"""

OSV_TEMPLATE = """      <OSV>
        <TAI>TAI={tai}</TAI>
        <UTC>UTC={utc}</UTC>
        <X unit="m">{x}</X><Y unit="m">{y}</Y><Z unit="m">{z}</Z>
        <VX unit="m/s">{vx}</VX><VY unit="m/s">{vy}</VY><VZ unit="m/s">{vz}</VZ>
        <Quality>NOMINAL</Quality>
      </OSV>"""


class TestOrbitNames:
    def test_parse_poeorb(self):
        key = ("AUX_POEORB/S1C_OPER_AUX_POEORB_OPOD_20251201T070825_"
               "V20251110T225942_20251112T005942.EOF")
        f = parse_orbit_name(key)
        assert f.platform == "S1C"
        assert f.orbit_type == "AUX_POEORB"
        assert f.valid_start == dt.datetime(2025, 11, 10, 22, 59, 42)
        assert f.name.endswith(".EOF")

    def test_unrelated_key(self):
        assert parse_orbit_name("AUX_POEORB/readme.txt") is None

    def test_select_latest_covering(self):
        keys = [
            "S1A_OPER_AUX_POEORB_OPOD_20251215T070000_V20251124T225942_20251126T005942.EOF",
            "S1A_OPER_AUX_POEORB_OPOD_20251216T070000_V20251124T225942_20251126T005942.EOF",
            "S1A_OPER_AUX_POEORB_OPOD_20251216T070000_V20251125T225942_20251127T005942.EOF",
            "S1C_OPER_AUX_POEORB_OPOD_20251216T070000_V20251124T225942_20251126T005942.EOF",
        ]
        files = [parse_orbit_name(k) for k in keys]
        t = dt.datetime(2025, 11, 25, 15, 35, 20)
        chosen = select_orbit_file(files, "S1A", t)
        assert chosen.production == dt.datetime(2025, 12, 16, 7, 0, 0)
        assert chosen.valid_start == dt.datetime(2025, 11, 24, 22, 59, 42)

    def test_select_none_when_uncovered(self):
        f = parse_orbit_name(
            "S1A_OPER_AUX_POEORB_OPOD_20251216T070000_V20251124T225942_20251126T005942.EOF"
        )
        assert select_orbit_file([f], "S1A", dt.datetime(2025, 11, 26, 0, 58, 0)) is None


class TestOrbitInterpolation:
    def test_lagrange_matches_analytic_orbit(self, circular_orbit):
        orbit, truth, _, _ = circular_orbit(span_s=600.0)
        tq = np.linspace(-500.0, 500.0, 97) + 3.7
        pos, _ = orbit.interpolate(tq + 600.0)
        err = np.linalg.norm(pos - truth(tq), axis=1)
        assert err.max() < 1e-3

    def test_outside_range_raises(self, circular_orbit):
        orbit, *_ = circular_orbit()
        with pytest.raises(ValueError):
            orbit.interpolate(np.array([orbit.t[-1] + 1.0]))

    def test_from_eof(self, tmp_path, circular_orbit):
        orbit, _, epoch, _ = circular_orbit(span_s=100.0)
        osvs = []
        for i, t in enumerate(orbit.t):
            utc = (epoch + dt.timedelta(seconds=float(t))).strftime("%Y-%m-%dT%H:%M:%S.%f")
            p, v = orbit.pos[i], orbit.vel[i]
            osvs.append(OSV_TEMPLATE.format(tai=utc, utc=utc, x=p[0], y=p[1], z=p[2],
                                            vx=v[0], vy=v[1], vz=v[2]))
        path = tmp_path / "orbit.EOF"
        path.write_text(EOF_TEMPLATE.format(n=len(osvs), osvs="\n".join(osvs)))
        read = Orbit.from_eof(path)
        assert np.allclose(read.pos, orbit.pos)
        assert np.allclose(read.t, orbit.t)
        window = Orbit.from_eof(path, epoch + dt.timedelta(seconds=40),
                                epoch + dt.timedelta(seconds=160))
        assert len(window.t) == 13


class TestGeo2Rdr:
    def test_zero_doppler_time_and_range(self, circular_orbit):
        radius, beta = 7_070_000.0, 5.0
        orbit, truth, _, w = circular_orbit(radius=radius, beta_deg=beta)
        earth = 6_371_000.0
        theta0 = np.radians(0.8)
        target = earth * np.array([np.cos(theta0), 0.0, np.sin(theta0)])
        t, rng, pos, vel = orbit.geo2rdr(target[None, :], t_guess=600.0)
        theta = np.arctan(np.tan(theta0) / np.cos(np.radians(beta)))
        assert t[0] == pytest.approx(theta / w + 600.0, abs=1e-6)
        assert rng[0] == pytest.approx(np.linalg.norm(truth(theta / w)[0] - target), abs=1e-3)
        assert abs(np.dot(pos[0] - target, vel[0])) < 1e-3 * np.linalg.norm(vel[0])
