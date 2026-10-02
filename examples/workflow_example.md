# Example openEO2Mintpy Workflow

This example takes one Sentinel-1 burst over Merzifon (northern Türkiye) from
the openEO request to a MintPy velocity map. It reproduces the ascending stack
of the accompanying study.

## Prerequisites

- A free Copernicus Data Space Ecosystem account (for step 2 only).
- GDAL, MintPy and openeo2mintpy installed (see the README).
- NASADEM tiles (or any DEM GeoTIFF) covering the area in `./nasadem`.

## 1. Find the burst

```bash
openeo2mintpy search --start 2025-11-20 --end 2026-05-20 --bbox 35.3 40.8 35.6 41.0
```

The table lists relative orbit 14, ascending, IW2, burst 28163 among others.

## 2. Request the interferograms

```bash
openeo2mintpy submit --start 2025-11-20 --end 2026-05-20 --bbox 35.3 40.8 35.6 41.0 \
    --track 14 --burst-id 28163 --sub-swath IW2 --max-temporal-baseline 24 \
    --manifest jobs_asc.json
```

Add `--dry-run` first to see the dates, pairs and job groups without submitting.

## 3. Download

```bash
openeo2mintpy download --manifest jobs_asc.json --output-dir ./raw_asc --wait
```

`./raw_asc` now holds files such as
`phase_coh_20251125T153521_20251201T153418.tif` and `job-results.json`.

## 4. Prepare the stack

```bash
openeo2mintpy process --input-dir ./raw_asc --work-dir ./asc --dem-source ./nasadem
```

This runs split, align (crop on the common lattice), prepare-dem, metadata
(burst catalogue + precise orbits), prepare and generate-config. Inspect the
result with:

```bash
openeo2mintpy info --unw-dir ./asc/unw --metadata ./asc/geometry/stack_metadata.json
```

## 5. Run MintPy

```bash
cd asc/mintpy
smallbaselineApp.py mintpy_config.txt --dostep load_data
openeo2mintpy fix-processor --inputs-dir ./inputs --targets ifgramStack.h5 geometryGeo.h5
smallbaselineApp.py mintpy_config.txt --start modify_network
```

## Directory Structure (After)

```
asc/
├── alignment_report.json
├── unw/  20251125_20251201.unw.tif (+ .rsc) ...
├── cor/  20251125_20251201.cor.tif (+ .rsc) ...
├── geometry/
│   ├── dem.tif, incidenceAngle.tif, azimuthAngle.tif (+ .rsc)
│   ├── stack_metadata.json
│   └── orbits/*.EOF
└── mintpy/
    ├── mintpy_config.txt
    ├── inputs/ifgramStack.h5, geometryGeo.h5
    └── timeseries.h5, velocity.h5, demErr.h5, ...
```

## Notes

- The DEM-error correction relies on the perpendicular baselines; on short
  stacks (about six months) its estimates are poorly constrained. Set
  `mintpy.topographicResidual = no` in `mintpy_config.txt` if you do not want it.
- `CENTER_LINE_UTC` is written, so ERA5 tropospheric correction can be enabled
  with `generate-config --tropo-method pyaps` once a CDS API key is configured.
