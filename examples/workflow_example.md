# Example openEO2Mintpy Workflow

This guide demonstrates how to use **openEO2Mintpy** to bridge CDSE openEO Sentinel-1 InSAR outputs into MintPy for time-series analysis.

## Prerequisites

- CDSE openEO InSAR processing completed and GeoTIFFs downloaded.
- ISCE2 topsStack metadata (baselines + reference XML file) available.

## Directory Structure (Before)

```
project/
├── openeo_downloads/
│   ├── phase_coh_20251124T035010_20251130T034907.tif
│   ├── phase_coh_20251130T034907_20251206T035022.tif
│   └── ...
├── baselines/
│   ├── 20251124_20251130/
│   │   └── 20251124_20251130.txt
│   ├── 20251130_20251206/
│   │   └── 20251130_20251206.txt
│   └── ...
└── reference/
    └── IW2.xml
```

---

## Step 1: Split openEO Bands

Since openEO produces 3-band GeoTIFFs, we need to extract Band 2 (Unwrapped Phase) and Band 3 (Coherence) into single-band TIFFs with dates in the format expected by MintPy.

### Option A: Desktop GUI

Launch the GUI:
```bash
openeo2mintpy
```
Navigate to **0. Split openEO Bands**, select your `openeo_downloads` directory and specify target directories for unwrapped phase and coherence. Click **Split Bands**. Once complete, the output directories are automatically forwarded to Tab 1.

### Option B: Command Line

```bash
openeo2mintpy split \
    --input-dir ./openeo_downloads \
    --unw-dir ./unwrapped \
    --cor-dir ./coherence
```

This generates:
- `./unwrapped/20251124_20251130.unw.tif`
- `./coherence/20251124_20251130.cor.tif`
- ...

---

## Step 2: Prepare Sidecars & MintPy Config

Now generate the ROI_PAC style `.rsc` sidecars next to each single-band GeoTIFF and create a pre-configured `mintpy_config.txt`.

### Option A: Desktop GUI

Navigate to **1. Prepare**, select the directories you just generated, pick your baseline folder, reference date, and reference XML. Click **Run**.

### Option B: Command Line

```bash
openeo2mintpy prepare \
    --unw-dir ./unwrapped \
    --cor-dir ./coherence \
    --baseline-dir ./baselines \
    --ref-xml ./reference/IW2.xml \
    --ref-date 20251124

openeo2mintpy generate-config \
    --work-dir ./mintpy \
    --unw-dir ./unwrapped \
    --cor-dir ./coherence
```

---

## Step 3: Run MintPy's Load Data Step

Execute MintPy's `load_data` step. It will read the GeoTIFFs using GDAL because the `.rsc` files contain `PROCESSOR=hyp3`.

```bash
cd mintpy
smallbaselineApp.py mintpy_config.txt --dostep load_data
```

This creates the HDF5 files `./mintpy/inputs/ifgramStack.h5` and `./mintpy/inputs/geometryRadar.h5`.

---

## Step 4: Apply Post-Load Fix

Apply the processor patch so MintPy doesn't fail with the `Unknown InSAR processor: hyp3` error during time-series inversion.

### Option A: Desktop GUI

Navigate to **2. Post-Load Fix**, select `./mintpy/inputs`, click **Verify** to check, and then **Apply fix** to patch HDF5 attributes from `hyp3` to `isce`.

### Option B: Command Line

```bash
openeo2mintpy fix-processor --inputs-dir ./mintpy/inputs
```

---

## Step 5: Complete the Inversion

Now run the remaining steps of MintPy as usual:

```bash
smallbaselineApp.py mintpy_config.txt
```
