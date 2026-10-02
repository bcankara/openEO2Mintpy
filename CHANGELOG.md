# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-02

First public release. The repository holds the software only; the input
products and results of the accompanying study are archived separately.
The changes below are listed relative to the unreleased development code.

### Added
- **Orbit-derived metadata** (`openeo2mintpy metadata`, GUI "Orbit metadata"):
  acquisition times from the openEO file names are matched against the public
  CDSE burst catalogue (platform, orbit direction, relative orbit, burst
  azimuth time); Sentinel-1 precise orbits are fetched from the public ASF
  `s1-orbits` bucket; zero-Doppler geometry on the stack grid gives per-pixel
  incidence and azimuth angles and slant range, the scene heading and
  centre-line time, and the perpendicular baseline of every date (ISCE2
  topsStack sign convention). Results go to `stack_metadata.json` and to
  `incidenceAngle.tif` / `azimuthAngle.tif`. New modules `acquisitions.py`,
  `orbit.py`, `geometry.py`.
- **SNAP baseline cross-check** (`snap_header.py`): SNAP's DIMAP header is read
  from TIFF tag 65000 (classic and BigTIFF, either byte order) when the
  products carry it, and the reconstructed pair baselines are compared with
  SNAP's `Baselines` element (sign reversed). `metadata` / `process` write the
  result to `stack_metadata.json` (`snap_crosscheck`) and log a warning above
  1 m.
- **Headless openEO workflow**: `search` (bursts over an area and period),
  `submit` (SBAS pairs, unique-primary job groups, job start, manifest),
  `download` (wait and download) and `process` (split, align, prepare-dem,
  metadata, prepare, generate-config in one command).
- **Lattice-aware alignment**: rasters that share pixel size and integer
  pixel offsets are cropped to the common intersection by array slicing, so
  no value is resampled; `--method auto|crop|warp`; `alignment_report.json`
  records the target grid and per-file offsets.
- `read_job_context` reads the parameters of jobs that call
  `sentinel1_sar_interferogram` as well as of `run_cwl_to_stac` jobs.
- `prepare-dem` / `process --dem-source` accept a single DEM file as well as
  a directory, as the help text already stated.
- Optional dependency group `mintpy`.

### Changed
- `submit` (CLI and GUI) calls the published CDSE process
  `sentinel1_sar_interferogram` (ESA APEx namespace, ClouDInSAR `main`
  branch) instead of running the `keep_snap_metadata` CWL branch through
  `run_cwl_to_stac`, which earlier versions used. That branch is still
  available with `submit --cwl-url URL` (GUI setting `openeo_cwl_url`), and
  `openeo_client.LEGACY_CWL_URL` holds its URL. The job manifest records
  `process_id`, `process_namespace` and `cwl_url`.
- `prepare` / `prepare_stack` take `stack_metadata.json` instead of an ISCE2
  reference XML and baseline directory; interferogram sidecars are refused
  when the geometry or a baseline is missing instead of being filled with
  nominal defaults.
- `generate-config` reads the DEM and angle rasters from the stack metadata and
  enables the topographic-residual step only when baselines are available.
- openEO jobs created by the GUI and `submit` are now started.
- CI installs GDAL from conda-forge, so the alignment, geometry and
  band-split tests run instead of being skipped.

### Fixed
- Descending stacks were labelled `ORBIT_DIRECTION = ASCENDING` with the
  ascending nominal heading when no ISCE2 XML was given.
- Perpendicular baselines defaulted to 0 m for openEO stacks, which made the
  MintPy DEM-error step meaningless.
- `STARTING_RANGE` defaulted to 800 km and `CENTER_LINE_UTC` was missing.
- `generate-config` wrote relative paths, which MintPy resolves against the
  config directory, so `load_data` found no files after a relative
  `--work-dir`; the configuration and `stack_metadata.json` now hold absolute
  paths.

### Removed
- ISCE2 reference-XML and baseline-directory parsing (`parse_isce_xml`,
  `parse_baselines`) and the nominal Sentinel-1 heading fallback.
