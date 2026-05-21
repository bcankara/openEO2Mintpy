# Architecture

## Overview

openeo2mintpy is a band-splitting and metadata bridge that converts CDSE openEO Sentinel-1 InSAR GeoTIFF outputs
into a format MintPy can ingest. It does **not** modify any raster values — it splits bands and
generates `.rsc` (ROI_PAC-style) sidecar metadata files.

## Module Dependency Graph

```
cli.py                  ← Entry point, argument parsing
  ├── gui.py            ← Tkinter desktop interface (default command)
  │     ├── settings.py ← Load/save openeo2mintpy_settings.json
  │     ├── split.py    ← Core band-splitting engine (Stage 0)
  │     ├── prepare.py  ← Core .rsc generation engine (Stage 1)
  │     │     ├── metadata.py   ← ISCE2 XML, GDAL, baseline parsing
  │     │     └── constants.py  ← Sentinel-1 defaults, RSC templates
  │     └── config.py   ← MintPy .cfg template generation
  └── (split / prepare / generate-config / fix-processor / info subcommands)
```

## Data Flow

```
                     ┌──────────────────┐
                     │  User Input      │
                     │  (GUI / CLI)     │
                     └────────┬─────────┘
                              │
                     ┌────────▼──────────┐
                     │  split.py         │
                     │                   │  For each 3-band openEO GeoTIFF:
                     │  Band 2 → unw.tif │  1. Extract unwrapped phase
                     │  Band 3 → cor.tif │  2. Extract coherence
                     │  Naming → dates   │  3. Retain coordinates/georef
                     └────────┬──────────┘
                              │
                     ┌────────▼──────────┐
                     │  metadata.py       │
                     │  ┌───────────────┐ │
                     │  │ ISCE2 XML     │ │  radar wavelength, pixel size,
                     │  │ parser        │ │  starting range, PRF, orbit dir
                     │  └───────────────┘ │
                     │  ┌───────────────┐ │
                     │  │ Baseline      │ │  perpendicular baselines
                     │  │ parser        │ │  (Bperp per date pair)
                     │  └───────────────┘ │
                     │  ┌───────────────┐ │
                     │  │ GDAL raster   │ │  WIDTH, LENGTH, geotransform
                     │  │ reader        │ │
                     │  └───────────────┘ │
                     └────────┬──────────┘
                              │
                     ┌────────▼──────────┐
                     │  prepare.py        │
                     │                    │  For each single-band GeoTIFF:
                     │  .unw.tif → .rsc   │  1. Read GDAL dimensions
                     │  .cor.tif → .rsc   │  2. Extract dates from filename
                     │  .conncomp → .rsc  │  3. Look up Bperp
                     │  geometry → .rsc   │  4. Write .rsc sidecar
                     └────────┬──────────┘
                              │
                     ┌────────▼──────────┐
                     │  config.py         │
                     │                    │  Generate mintpy_config.txt
                     │  processor = hyp3  │  with correct glob patterns
                     │  load paths        │  pointing to .rsc-enriched data
                     └────────┬──────────┘
                              │
                     ┌────────▼──────────┐
                     │  Ready for MintPy  │
                     │  smallbaselineApp  │
                     └────────────────────┘
```

## Key Design Decisions

### Why `PROCESSOR=hyp3`?

MintPy routes data reading through processor-specific code paths. The `hyp3`
processor uses `readfile.read_gdal_vrt()`, which correctly handles multi-band
compressed GeoTIFFs. This is the most compatible path for our geocoded / radar GeoTIFF ingestion.

### Why not modify MintPy directly?

1. **Separation of concerns**: openeo2mintpy can evolve independently
2. **No fork maintenance burden**: users don't need a patched MintPy
3. **Standards-based**: uses the existing `.rsc` + `PROCESSOR` mechanism

### Why `.rsc` sidecars instead of HDF5?

MintPy's `load_data` step converts everything to HDF5 internally.
The `.rsc` files are only needed for the initial data ingestion phase.
They are lightweight text files (~500 bytes each) that don't duplicate
any raster data.
