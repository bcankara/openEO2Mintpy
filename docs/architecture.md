# Architecture

## Overview

openeo2mintpy sits between the CDSE openEO/ClouDInSAR interferogram service and
MintPy. It performs no coregistration, interferogram formation, unwrapping or
time-series inversion. It requests the interferograms, prepares the rasters
without changing their values, and reconstructs the acquisition metadata that
MintPy needs. Products of the earlier `keep_snap_metadata` workflow branch
carry none. Products of the published process carry SNAP's DIMAP header,
but MintPy's SNAP reader derives an invalid incidence angle (99999) from it.
The header's baselines are used only as an independent check.

## Module Dependency Graph

```
cli.py                    ← Entry point, argument parsing
  ├── gui.py              ← Tkinter desktop interface (default command)
  │     └── settings.py   ← Load/save openeo2mintpy_settings.json
  ├── openeo_client.py    ← Stage 0: OIDC login, burst search, pairs, jobs, download
  ├── split.py            ← Stage A: band 2 / band 3 to *.unw.tif / *.cor.tif
  ├── align.py            ← Stage A: lattice crop or warp; prepare_dem
  ├── acquisitions.py     ← Stage B: file-name times + CDSE burst catalogue
  ├── orbit.py            ← Stage B: precise orbits, Lagrange interpolation, zero-Doppler
  ├── geometry.py         ← Stage B: angles, heading, centre-line time, baselines
  │     └── snap_header.py← SNAP DIMAP header (TIFF tag 65000), baseline cross-check
  ├── prepare.py          ← Stage B: .rsc sidecars from stack_metadata.json
  │     ├── metadata.py   ← GDAL metadata, file-name dates, pair baselines
  │     └── constants.py  ← Sentinel-1 constants, RSC templates
  ├── config.py           ← Stage B: MintPy configuration
  └── postprocess.py      ← Stage C: HDF5 PROCESSOR attribute patch
```

## Data Flow

```
openEO/ClouDInSAR (cloud, ESA SNAP)       one three-band GeoTIFF per pair
        │  search / submit / download      (wrapped phase, unwrapped phase, coherence)
        ▼
split.py        band 2 → YYYYMMDD_YYYYMMDD.unw.tif, band 3 → .cor.tif (copy)
        ▼
align.py        target grid = intersection of all extents
                common lattice → crop by array slicing (no resampling)
                otherwise      → GDAL Warp with the selected kernel
                alignment_report.json
        ▼
prepare_dem     DEM mosaic resampled onto the stack grid
        ▼
acquisitions.py file-name start times ↔ CDSE burst catalogue records
                (platform, orbit direction, relative orbit, burst azimuth time)
        ▼
orbit.py        AUX_POEORB (or AUX_RESORB) from the public ASF s1-orbits bucket
                8-point Lagrange interpolation, Newton zero-Doppler solver
        ▼
geometry.py     per pixel: incidence angle, azimuth angle, slant range
                scene: heading, centre-line time, satellite height, Earth radius
                per date: perpendicular baseline (ISCE2 topsStack convention)
                cross-check with SNAP's pair baselines when tag 65000 is present
                → incidenceAngle.tif, azimuthAngle.tif, stack_metadata.json
        ▼
prepare.py      .rsc sidecars (raster description, geometry, geotransform,
                DATE12, P_BASELINE_TOP_HDR / P_BASELINE_BOTTOM_HDR)
config.py       mintpy_config.txt (rasters, DEM, angle rasters, DEM-error step)
        ▼
MintPy load_data → postprocess.py (PROCESSOR hyp3 → isce) → SBAS chain
```

## Key Design Decisions

### Why reconstruct the metadata from orbits?

The openEO products carry only the acquisition start times (in the file names)
and a geotransform. MintPy needs the perpendicular baseline of every pair for
the DEM-error correction, the incidence and azimuth angles to relate
line-of-sight changes to ground motion, and the centre-line time for
tropospheric correction. Filling these fields with nominal values lets MintPy
run but silently disables or biases those steps, so openeo2mintpy refuses to
write interferogram sidecars without orbit-derived metadata.

### Why crop instead of warp?

openEO burst products of one stack share a pixel lattice and differ only in
extent. Cropping them to the common intersection by array slicing leaves every
sample value unchanged; warping is used only when the lattice test fails.

### Why `PROCESSOR=hyp3` in the sidecars?

MintPy routes data reading through processor-specific code paths. The `hyp3`
label makes MintPy read the GeoTIFFs through GDAL. After `load_data`, the label
is changed to `isce` in the HDF5 files so that later steps do not look for
HyP3-style lookup tables.

### Why not modify MintPy directly?

1. **Separation of concerns**: openeo2mintpy can evolve independently
2. **No fork maintenance burden**: users don't need a patched MintPy
3. **Standards-based**: uses the existing `.rsc` + `PROCESSOR` mechanism
