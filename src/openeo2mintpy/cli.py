"""
Command-line interface for openeo2mintpy.

Provides subcommands for graphical and non-interactive workflows:
  - gui            : Launch the Tkinter GUI (default when no subcommand given)
  - search         : List Sentinel-1 bursts covering an area and period
  - submit         : Generate SBAS pairs and submit/start openEO InSAR jobs
  - download       : Wait for submitted jobs and download their products
  - split          : Split 3-band openEO GeoTIFFs into single-band TIFFs
  - align          : Bring the split rasters onto one grid
  - prepare-dem    : Mosaic and resample a DEM onto the stack grid
  - metadata       : Orbit-derived geometry and perpendicular baselines
  - prepare        : Non-interactive .rsc generation
  - generate-config: Generate MintPy configuration only
  - process        : split + align + prepare-dem + metadata + prepare + config
  - fix-processor  : Patch PROCESSOR HDF5 attribute (post 'load_data' step)
  - info           : Display stack information
"""

import argparse
import logging
import sys


def main(args=None):
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="openeo2mintpy",
        description=(
            "Bridge between CDSE openEO Sentinel-1 InSAR outputs and MintPy. "
            "Requests interferograms, splits and aligns them, derives the "
            "acquisition geometry and baselines from precise orbits, and "
            "builds the MintPy configuration."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
headless workflow:
  openeo2mintpy search --start 2025-11-20 --end 2026-05-20 \\
      --bbox 35.3 40.8 35.6 41.0
  openeo2mintpy submit --start 2025-11-20 --end 2026-05-20 \\
      --bbox 35.3 40.8 35.6 41.0 --track 14 --burst-id 28163 \\
      --sub-swath IW2 --max-temporal-baseline 24 --manifest jobs_asc.json
  openeo2mintpy download --manifest jobs_asc.json --output-dir ./raw_asc --wait
  openeo2mintpy process --input-dir ./raw_asc --work-dir ./asc \\
      --dem-source ./nasadem
  cd asc/mintpy && smallbaselineApp.py mintpy_config.txt --dostep load_data
  openeo2mintpy fix-processor --inputs-dir ./inputs --targets ifgramStack.h5 geometryGeo.h5
  smallbaselineApp.py mintpy_config.txt
""",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_get_version()}",
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- gui (default) ---
    subparsers.add_parser(
        "gui",
        help="Launch the Tkinter graphical interface (default).",
        description=(
            "Open the point-and-click interface for selecting paths "
            "and running the pipeline."
        ),
    )

    # --- search ---
    search_parser = subparsers.add_parser(
        "search",
        help="List Sentinel-1 bursts covering an area and period (no login needed).",
        description="Query the public CDSE burst catalogue.",
    )
    _add_period_args(search_parser)

    # --- submit ---
    submit_parser = subparsers.add_parser(
        "submit",
        help="Generate SBAS pairs for one burst and submit openEO InSAR jobs.",
        description=(
            "Find the acquisition dates of one burst in the CDSE catalogue, form "
            "SBAS pairs under a temporal-baseline limit, group them into jobs with "
            "unique primary dates, and create and start the jobs. By default the "
            "jobs call the published CDSE process sentinel1_sar_interferogram; "
            "--cwl-url runs a given ClouDInSAR CWL workflow instead. "
            "Requires a (free) CDSE account; login uses OIDC."
        ),
    )
    _add_period_args(submit_parser)
    submit_parser.add_argument("--track", type=int, required=True,
                               help="Relative orbit number.")
    submit_parser.add_argument("--burst-id", type=int, required=True,
                               help="ESA burst id (e.g. 28163).")
    submit_parser.add_argument("--sub-swath", required=True, choices=("IW1", "IW2", "IW3"),
                               help="Sub-swath of the burst.")
    submit_parser.add_argument("--max-temporal-baseline", type=int, default=24,
                               help="Maximum temporal baseline in days. Default: 24.")
    submit_parser.add_argument("--manifest", required=True,
                               help="JSON file recording the submitted jobs.")
    submit_parser.add_argument("--dry-run", action="store_true",
                               help="Only print dates, pairs and job groups.")
    submit_parser.add_argument("--no-start", action="store_true",
                               help="Create the jobs without starting them.")
    submit_parser.add_argument("--cwl-url", default=None,
                               help="Run this CWL workflow through run_cwl_to_stac instead of "
                                    "the published process (e.g. the keep_snap_metadata branch "
                                    "used by the openeo2mintpy development versions).")

    # --- download ---
    dl_parser = subparsers.add_parser(
        "download",
        help="Download the products of the jobs listed in a manifest.",
        description="Optionally wait for the jobs, then download finished results.",
    )
    dl_parser.add_argument("--manifest", required=True, help="Manifest from 'submit'.")
    dl_parser.add_argument("--output-dir", required=True,
                           help="Directory for the three-band GeoTIFFs.")
    dl_parser.add_argument("--wait", action="store_true",
                           help="Poll until every job has finished or failed.")
    dl_parser.add_argument("--poll-seconds", type=float, default=120.0,
                           help="Polling interval with --wait. Default: 120.")

    # --- split ---
    split_parser = subparsers.add_parser(
        "split",
        help="Split 3-band openEO GeoTIFFs into separate single-band TIFFs.",
        description="Extract Band 2 (Unwrapped Phase) and Band 3 (Coherence) from openEO outputs.",
    )
    split_parser.add_argument(
        "--input-dir", "-i", required=True,
        help="Directory containing 3-band openEO GeoTIFF files (*.tif/*.tiff).",
    )
    split_parser.add_argument(
        "--unw-dir", "-u", required=True,
        help="Output directory for split unwrapped phase (*.unw.tif) files.",
    )
    split_parser.add_argument(
        "--cor-dir", "-c", required=True,
        help="Output directory for split coherence (*.cor.tif) files.",
    )

    # --- align ---
    align_parser = subparsers.add_parser(
        "align",
        help="Align split GeoTIFFs to a common grid.",
        description=(
            "Bring all unwrapped phase and coherence GeoTIFFs onto the "
            "intersection of their extents. Rasters on a common pixel lattice "
            "are cropped without resampling; otherwise GDAL Warp is used."
        ),
    )
    align_parser.add_argument(
        "--unw-dir", "-u", required=True,
        help="Directory containing unwrapped phase GeoTIFFs (*.unw.tif).",
    )
    align_parser.add_argument(
        "--cor-dir", "-c", default=None,
        help="Directory containing coherence GeoTIFFs (*.cor.tif). Default: same as --unw-dir.",
    )
    _add_align_args(align_parser)

    # --- prepare-dem ---
    dem_parser = subparsers.add_parser(
        "prepare-dem",
        help="Mosaic DEM tiles and resample them onto the aligned stack grid.",
        description=(
            "Find zip files or HGT/DEM/TIF files, merge them, and warp them "
            "to match the exact extent, resolution, and CRS of aligned InSAR files."
        ),
    )
    dem_parser.add_argument(
        "--unw-dir", "-u", required=True,
        help="Directory containing aligned unwrapped GeoTIFFs.",
    )
    dem_parser.add_argument(
        "--zip-dir", "-z", required=True,
        help="Directory containing NASADEM zip/HGT files or a DEM GeoTIFF.",
    )
    dem_parser.add_argument(
        "--output-file", "-o", required=True,
        help="Path where the aligned dem.tif will be saved.",
    )

    # --- metadata ---
    meta_parser = subparsers.add_parser(
        "metadata",
        help="Derive geometry and perpendicular baselines from precise orbits.",
        description=(
            "Match the acquisition times in the openEO file names against the "
            "CDSE burst catalogue, download the Sentinel-1 precise orbits, and "
            "compute incidence/azimuth angles, heading, centre-line time and "
            "per-date perpendicular baselines on the stack grid."
        ),
    )
    meta_parser.add_argument("--input-dir", required=True,
                             help="openEO download directory (file names carry the times).")
    meta_parser.add_argument("--unw-dir", required=True,
                             help="Directory of aligned *.unw.tif (defines the grid).")
    meta_parser.add_argument("--dem-file", required=True,
                             help="DEM already resampled onto the stack grid.")
    meta_parser.add_argument("--output-dir", required=True,
                             help="Where the angle rasters and stack_metadata.json go.")
    _add_metadata_args(meta_parser)

    # --- prepare ---
    prep_parser = subparsers.add_parser(
        "prepare",
        help="Generate .rsc sidecar files (non-interactive).",
        description="Generate .rsc metadata sidecar files for all GeoTIFFs.",
    )
    prep_parser.add_argument(
        "--unw-dir", required=True,
        help="Directory containing unwrapped phase GeoTIFFs (*.unw.tif).",
    )
    prep_parser.add_argument(
        "--metadata", required=True,
        help="stack_metadata.json written by 'openeo2mintpy metadata'.",
    )
    prep_parser.add_argument(
        "--cor-dir", default=None,
        help="Directory containing coherence GeoTIFFs. Default: same as --unw-dir.",
    )
    prep_parser.add_argument(
        "--conncomp-dir", default=None,
        help="Directory containing connected component GeoTIFFs. Default: same as --unw-dir.",
    )
    prep_parser.add_argument(
        "--geometry-dir", default=None,
        help="Directory containing geometry GeoTIFFs. Default: from the metadata.",
    )
    _add_geometry_mode_arg(prep_parser)

    # --- generate-config ---
    cfg_parser = subparsers.add_parser(
        "generate-config",
        help="Generate MintPy configuration file only.",
        description="Generate a smallbaselineApp.cfg-compatible configuration file.",
    )
    cfg_parser.add_argument(
        "--work-dir", required=True,
        help="MintPy working directory (where config will be written).",
    )
    cfg_parser.add_argument(
        "--unw-dir", required=True,
        help="Directory containing unwrapped phase GeoTIFFs.",
    )
    cfg_parser.add_argument(
        "--metadata", default=None,
        help="stack_metadata.json; supplies the DEM and angle rasters.",
    )
    cfg_parser.add_argument(
        "--cor-dir", default=None,
        help="Directory containing coherence GeoTIFFs.",
    )
    cfg_parser.add_argument(
        "--conncomp-dir", default=None,
        help="Directory containing connected component GeoTIFFs.",
    )
    cfg_parser.add_argument(
        "--dem-file", default=None,
        help="DEM on the stack grid. Default: from --metadata.",
    )
    cfg_parser.add_argument(
        "--inc-angle-file", default=None,
        help="Incidence angle raster. Default: from --metadata.",
    )
    cfg_parser.add_argument(
        "--az-angle-file", default=None,
        help="Azimuth angle raster. Default: from --metadata.",
    )
    cfg_parser.add_argument(
        "--lookup-y-file", default=None,
        help="Latitude lookup table (radar-geometry stacks only).",
    )
    cfg_parser.add_argument(
        "--lookup-x-file", default=None,
        help="Longitude lookup table (radar-geometry stacks only).",
    )
    cfg_parser.add_argument(
        "--water-mask-file", default=None,
        help="Optional water mask file.",
    )
    cfg_parser.add_argument(
        "--processor",
        choices=("isce", "hyp3"),
        default="isce",
        help="Value for mintpy.load.processor. Default: isce.",
    )
    cfg_parser.add_argument(
        "--tropo-method", default="no",
        choices=("no", "pyaps", "height_correlation", "gacos"),
        help="mintpy.troposphericDelay.method. Default: no.",
    )
    cfg_parser.add_argument(
        "--config-name", default="mintpy_config.txt",
        help="Output config filename. Default: mintpy_config.txt.",
    )

    # --- process ---
    proc_parser = subparsers.add_parser(
        "process",
        help="Run split, align, prepare-dem, metadata, prepare and generate-config.",
        description=(
            "Turn one openEO download directory into a MintPy-ready stack. "
            "Creates unw/, cor/, geometry/ and mintpy/ under --work-dir."
        ),
    )
    proc_parser.add_argument("--input-dir", required=True, help="openEO download directory.")
    proc_parser.add_argument("--work-dir", required=True, help="Output working directory.")
    proc_parser.add_argument("--dem-source", required=True,
                             help="Directory with NASADEM zip/HGT files or a DEM GeoTIFF.")
    _add_align_args(proc_parser)
    _add_metadata_args(proc_parser)
    _add_geometry_mode_arg(proc_parser)

    # --- fix-processor ---
    fix_parser = subparsers.add_parser(
        "fix-processor",
        help="Patch PROCESSOR HDF5 attribute (post 'load_data' step).",
        description=(
            "Rewrite the PROCESSOR / INSAR_PROCESSOR attributes inside "
            "MintPy's inputs/ifgramStack.h5 and inputs/geometryRadar.h5 "
            "(hyp3 -> isce by default). Run this AFTER "
            "'smallbaselineApp.py --dostep load_data' has succeeded. "
            "Fixes 'AttributeError: Unknown InSAR processor: hyp3 to "
            "locate look up table!'"
        ),
    )
    fix_parser.add_argument(
        "--inputs-dir", required=True,
        help="MintPy inputs/ directory (e.g. ./mintpy/inputs).",
    )
    fix_parser.add_argument(
        "--from", dest="old_processor", default="hyp3",
        help="Current PROCESSOR value to replace. Default: hyp3.",
    )
    fix_parser.add_argument(
        "--to", dest="new_processor", default="isce",
        help="New PROCESSOR value. Default: isce.",
    )
    fix_parser.add_argument(
        "--targets", nargs="+",
        default=["ifgramStack.h5", "geometryRadar.h5"],
        help=(
            "HDF5 files to patch. Default: ifgramStack.h5 geometryRadar.h5."
        ),
    )
    fix_parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without modifying anything.",
    )
    fix_parser.add_argument(
        "--verify-only", action="store_true",
        help="Only inspect the inputs directory; do not modify files.",
    )
    fix_parser.add_argument(
        "--skip-lookup-check", action="store_true",
        help=(
            "Allow patching even when geometryRadar.h5 lacks /latitude "
            "or /longitude datasets (not recommended)."
        ),
    )

    # --- info ---
    info_parser = subparsers.add_parser(
        "info",
        help="Display stack information.",
        description="Show summary information about a data stack.",
    )
    info_parser.add_argument(
        "--unw-dir", required=True,
        help="Directory containing unwrapped phase GeoTIFFs.",
    )
    info_parser.add_argument(
        "--cor-dir", default=None,
        help="Directory containing coherence GeoTIFFs.",
    )
    info_parser.add_argument(
        "--metadata", default=None,
        help="stack_metadata.json to summarise (geometry and baselines).",
    )

    parsed = parser.parse_args(args)

    log_level = logging.DEBUG if parsed.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    commands = {
        "search": _cmd_search,
        "submit": _cmd_submit,
        "download": _cmd_download,
        "split": _cmd_split,
        "align": _cmd_align,
        "prepare-dem": _cmd_prepare_dem,
        "metadata": _cmd_metadata,
        "prepare": _cmd_prepare,
        "generate-config": _cmd_generate_config,
        "process": _cmd_process,
        "fix-processor": _cmd_fix_processor,
        "info": _cmd_info,
    }
    if parsed.command is None or parsed.command == "gui":
        _cmd_gui()
    elif parsed.command in commands:
        commands[parsed.command](parsed)
    else:
        parser.print_help()
        sys.exit(1)


def _add_period_args(p):
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD.")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD.")
    p.add_argument("--bbox", nargs=4, type=float, required=True,
                   metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                   help="Area of interest in degrees.")
    p.add_argument("--polarisation", default="VV", help="Polarisation. Default: VV.")


def _add_align_args(p):
    p.add_argument(
        "--method", default="auto", choices=("auto", "crop", "warp"),
        help=(
            "auto: crop without resampling when the rasters share a pixel "
            "lattice, warp otherwise (default); crop: never resample; warp: "
            "always resample."
        ),
    )
    p.add_argument(
        "--resample", default="bilinear",
        choices=("near", "bilinear", "cubic", "cubicspline", "lanczos"),
        help="Resampling kernel for the warp path. Default: bilinear.",
    )


def _add_metadata_args(p):
    p.add_argument("--orbit-dir", default=None,
                   help="Cache directory for orbit files. Default: <output>/orbits.")
    p.add_argument("--burst-id", type=int, default=None,
                   help="Burst id. Default: read from job-results.json.")
    p.add_argument("--sub-swath", default=None,
                   help="Sub-swath. Default: read from job-results.json.")
    p.add_argument("--ref-date", default=None,
                   help="Baseline reference date YYYYMMDD. Default: first date.")


def _add_geometry_mode_arg(p):
    p.add_argument(
        "--geometry-mode",
        choices=("auto", "radar", "geo"),
        default="auto",
        help=(
            "How to populate geotransform metadata in the .rsc sidecars: "
            "'auto' detects from the GeoTIFF (default), 'radar' forces "
            "radar geometry (omits X_FIRST/Y_FIRST so MintPy produces "
            "geometryRadar.h5), 'geo' forces geocoded output (emits the "
            "geotransform so MintPy produces geometryGeo.h5)."
        ),
    )


def _print_errors_and_exit(result, key="errors"):
    if result[key]:
        print(f"Errors: {len(result[key])}")
        for e in result[key]:
            print(f"  ! {e['file']}: {e['error']}")
        sys.exit(2)


def _cmd_gui(args=None):
    """Launch the Tkinter GUI."""
    from openeo2mintpy.gui import run_gui

    run_gui()


def _cmd_search(args):
    """List the bursts covering the area and period."""
    from openeo2mintpy import openeo_client as oc

    records = oc.query_burst_acquisitions(
        args.start, args.end, args.polarisation, oc.bbox_to_wkt(*args.bbox)
    )
    bursts = oc.extract_unique_bursts(records)
    print(f"\n{len(bursts)} burst(s), {len(records)} acquisition records")
    print(f"{'Track':>6} {'Direction':<11} {'Swath':<6} {'BurstId':>8} {'Dates':>6}")
    for b in bursts:
        print(f"{b['track']:>6} {b['direction']:<11} {b['swath']:<6} "
              f"{b['burst_id']:>8} {b['count']:>6}")


def _cmd_submit(args):
    """Form SBAS pairs for one burst and submit the openEO jobs."""
    from openeo2mintpy import openeo_client as oc

    records = oc.query_burst_acquisitions(
        args.start, args.end, args.polarisation, oc.bbox_to_wkt(*args.bbox)
    )
    dates = oc.filter_bursts(records, args.track, args.burst_id, args.sub_swath)
    if len(dates) < 2:
        print("ERROR: fewer than two acquisitions found for this burst.")
        sys.exit(2)
    directions = {
        r.get("OrbitDirection") for r in records
        if r.get("BurstId") == args.burst_id and r.get("RelativeOrbitNumber") == args.track
    }
    direction = (directions.pop() if len(directions) == 1 else "unknown").lower()
    pairs = oc.generate_pairs(dates, args.max_temporal_baseline)
    groups = oc.split_pairs_into_groups(pairs)
    print(f"\n{len(dates)} dates, {len(pairs)} pairs, {len(groups)} job(s) ({direction})")
    for i, g in enumerate(groups, 1):
        print(f"  job {i}: {len(g)} pairs")

    request = {k: getattr(args, k) for k in (
        "start", "end", "bbox", "polarisation", "track", "burst_id", "sub_swath",
        "max_temporal_baseline", "cwl_url")}
    request.update({"direction": direction, "dates": dates, "pairs": pairs})
    if args.dry_run:
        oc.save_manifest(args.manifest, [], request)
        print(f"Dry run: pairs written to {args.manifest}; nothing submitted.")
        return

    connection = oc.connect_and_auth()
    jobs = []
    for i, group in enumerate(groups, 1):
        jobs.append(
            oc.submit_insar_job(
                connection, args.track, direction, args.burst_id, args.sub_swath,
                group, i, len(groups), cwl_url=args.cwl_url, start=not args.no_start,
            )
        )
        oc.save_manifest(args.manifest, jobs, request)
    print(f"Submitted {len(jobs)} job(s); manifest: {args.manifest}")


def _cmd_download(args):
    """Download the results of the jobs listed in a manifest."""
    from openeo2mintpy import openeo_client as oc

    manifest = oc.load_manifest(args.manifest)
    job_ids = [j["job_id"] for j in manifest["jobs"]]
    if not job_ids:
        print("ERROR: the manifest lists no jobs.")
        sys.exit(2)
    connection = oc.connect_and_auth()
    if args.wait:
        statuses = oc.wait_for_jobs(connection, job_ids, poll_seconds=args.poll_seconds)
    else:
        statuses = {jid: connection.job(jid).status() for jid in job_ids}
    total = 0
    for jid in job_ids:
        if statuses.get(jid) != "finished":
            print(f"  skip {jid}: {statuses.get(jid)}")
            continue
        total += oc.download_job_results(connection, jid, args.output_dir)
    print(f"Downloaded {total} file(s) to {args.output_dir}")
    if any(s != "finished" for s in statuses.values()):
        sys.exit(3)


def _cmd_split(args):
    """Run non-interactive openEO bands splitting."""
    from openeo2mintpy.split import split_openeo_bands

    result = split_openeo_bands(
        input_dir=args.input_dir,
        unw_dir=args.unw_dir,
        cor_dir=args.cor_dir,
    )

    print(f"\nDone: split {result['processed']} openEO files.")
    _print_errors_and_exit(result)


def _cmd_align(args):
    """Run non-interactive raster alignment."""
    from openeo2mintpy.align import align_rasters

    print("\nAligning rasters to a common grid...")
    result = align_rasters(
        unw_dir=args.unw_dir,
        cor_dir=args.cor_dir,
        resample_alg=args.resample,
        method=args.method,
    )

    print(f"\nDone: aligned {result['aligned']} GeoTIFF files ({result['method']}).")
    _print_errors_and_exit(result)


def _cmd_prepare_dem(args):
    """Run non-interactive DEM preparation."""
    from openeo2mintpy.align import prepare_dem

    print("\nPreparing DEM...")
    try:
        output_path = prepare_dem(
            unw_dir=args.unw_dir,
            zip_dir=args.zip_dir,
            output_file=args.output_file,
        )
        print(f"\nDone: DEM successfully prepared at {output_path}")
    except Exception as e:
        print(f"\nERROR: Failed to prepare DEM: {e}")
        sys.exit(2)


def _first_unw(unw_dir):
    from pathlib import Path

    files = sorted(Path(unw_dir).glob("*.unw.tif"))
    if not files:
        print(f"ERROR: no *.unw.tif in {unw_dir}")
        sys.exit(2)
    return files[0]


def _cmd_metadata(args):
    """Compute orbit-derived geometry and baselines."""
    from openeo2mintpy.geometry import build_stack_metadata

    path = build_stack_metadata(
        input_dir=args.input_dir,
        grid_tif=_first_unw(args.unw_dir),
        dem_tif=args.dem_file,
        output_dir=args.output_dir,
        orbit_dir=args.orbit_dir,
        burst_id=args.burst_id,
        sub_swath=args.sub_swath,
        ref_date=args.ref_date,
    )
    print(f"\nDone: {path}")


def _cmd_prepare(args):
    """Run non-interactive .rsc generation."""
    from openeo2mintpy.prepare import prepare_stack

    result = prepare_stack(
        unw_dir=args.unw_dir,
        stack_metadata=args.metadata,
        cor_dir=args.cor_dir,
        conncomp_dir=args.conncomp_dir,
        geometry_dir=args.geometry_dir,
        geometry_mode=args.geometry_mode,
    )

    print(f"\nDone: {result['rsc_written']} .rsc files written.")
    _print_errors_and_exit(result)


def _cmd_generate_config(args):
    """Generate MintPy configuration file."""
    from openeo2mintpy.config import generate_mintpy_config

    config_path = generate_mintpy_config(
        work_dir=args.work_dir,
        unw_dir=args.unw_dir,
        cor_dir=args.cor_dir,
        conncomp_dir=args.conncomp_dir,
        dem_file=args.dem_file,
        inc_angle_file=args.inc_angle_file,
        az_angle_file=args.az_angle_file,
        lookup_y_file=args.lookup_y_file,
        lookup_x_file=args.lookup_x_file,
        water_mask_file=args.water_mask_file,
        processor=args.processor,
        config_name=args.config_name,
        stack_metadata=args.metadata,
        tropo_method=args.tropo_method,
    )

    print(f"Config written: {config_path}")
    print(f"\nNext step: smallbaselineApp.py {config_path.name} --dostep load_data")


def _cmd_process(args):
    """Run the whole preparation chain for one openEO download directory."""
    from pathlib import Path

    from openeo2mintpy.align import align_rasters, prepare_dem
    from openeo2mintpy.config import generate_mintpy_config
    from openeo2mintpy.geometry import build_stack_metadata
    from openeo2mintpy.prepare import prepare_stack
    from openeo2mintpy.split import split_openeo_bands

    work = Path(args.work_dir)
    unw, cor, geom, mp = (work / d for d in ("unw", "cor", "geometry", "mintpy"))

    print("\n[1/6] split")
    result = split_openeo_bands(args.input_dir, unw, cor)
    _print_errors_and_exit(result)
    print("[2/6] align")
    result = align_rasters(unw, cor, resample_alg=args.resample, method=args.method,
                           report_path=work / "alignment_report.json")
    _print_errors_and_exit(result)
    print(f"      method: {result['method']}")
    print("[3/6] prepare-dem")
    dem = prepare_dem(unw, args.dem_source, geom / "dem.tif")
    print("[4/6] metadata (catalogue + precise orbits)")
    meta = build_stack_metadata(
        args.input_dir, _first_unw(unw), dem, geom, orbit_dir=args.orbit_dir,
        burst_id=args.burst_id, sub_swath=args.sub_swath, ref_date=args.ref_date,
    )
    print("[5/6] prepare")
    result = prepare_stack(unw, meta, cor_dir=cor, geometry_dir=geom,
                           geometry_mode=args.geometry_mode)
    _print_errors_and_exit(result)
    print("[6/6] generate-config")
    cfg = generate_mintpy_config(mp, unw, cor_dir=cor, stack_metadata=meta)
    print(f"\nDone. Next, in {mp}:")
    print(f"  smallbaselineApp.py {cfg.name} --dostep load_data")
    print("  openeo2mintpy fix-processor --inputs-dir ./inputs "
          "--targets ifgramStack.h5 geometryGeo.h5")
    print(f"  smallbaselineApp.py {cfg.name}")


def _cmd_fix_processor(args):
    """Patch PROCESSOR HDF5 attributes after MintPy load_data."""
    from openeo2mintpy.postprocess import (
        PostProcessError,
        fix_processor_attribute,
        verify_inputs_dir,
    )

    print(f"\n[openeo2mintpy fix-processor] inputs dir: {args.inputs_dir}")

    try:
        report = verify_inputs_dir(
            args.inputs_dir,
            target_files=tuple(args.targets),
            expected_old=args.old_processor,
        )
    except PostProcessError as exc:
        print(f"ERROR: {exc}")
        sys.exit(3)

    print("\n-- Verification --")
    for entry in report:
        tag = "OK " if entry["exists"] else "MISS"
        print(f"  [{tag}] {entry['path'].name}")
        print(f"        exists           : {entry['exists']}")
        if entry["exists"]:
            print(f"        PROCESSOR        : {entry['processor']}")
            print(f"        INSAR_PROCESSOR  : {entry['insar_processor']}")
            if entry["has_lat_lon"] is not None:
                print(f"        has /lat /lon    : {entry['has_lat_lon']}")
            print(f"        needs patch      : {entry['needs_patch']}")
        for issue in entry["issues"]:
            print(f"        ! {issue}")

    if args.verify_only:
        return

    missing_lookup = any(
        e["exists"] and e["has_lat_lon"] is False for e in report
    )
    if missing_lookup and not args.skip_lookup_check:
        print(
            "\nERROR: geometryRadar.h5 is missing /latitude or /longitude "
            "datasets. Delete it and re-run 'smallbaselineApp.py --dostep "
            "load_data' first, or pass --skip-lookup-check to force."
        )
        sys.exit(4)

    print("\n-- Applying patch --" if not args.dry_run else "\n-- Dry run --")
    try:
        summary = fix_processor_attribute(
            inputs_dir=args.inputs_dir,
            old=args.old_processor,
            new=args.new_processor,
            target_files=tuple(args.targets),
            dry_run=args.dry_run,
            require_lookup_datasets=not args.skip_lookup_check,
        )
    except PostProcessError as exc:
        print(f"ERROR: {exc}")
        sys.exit(5)

    for record in summary["details"]:
        print(f"  - {record['message']}")

    print(
        f"\nDone: patched={summary['patched']}, "
        f"skipped={summary['skipped']}, errors={len(summary['errors'])}."
    )
    if summary["errors"]:
        sys.exit(2)
    if summary["patched"] > 0 and not args.dry_run:
        print(
            "\nNext step: smallbaselineApp.py mintpy_config.txt "
            "(resume the full SBAS chain)."
        )


def _cmd_info(args):
    """Display stack information."""
    from pathlib import Path

    from openeo2mintpy.metadata import count_files, extract_dates_from_filename

    unw_dir = Path(args.unw_dir)
    cor_dir = Path(args.cor_dir) if args.cor_dir else unw_dir

    print(f"\n{'-' * 50}")
    print("  openEO2Mintpy Stack Information")
    print(f"{'-' * 50}")

    unw_count = count_files(unw_dir, "*.unw.tif")
    cor_count = count_files(cor_dir, "*.cor.tif") + count_files(cor_dir, "*.int.cor.tif")
    conn_count = count_files(unw_dir, "*.conncomp.tif")

    print(f"\n  Unwrapped files:     {unw_count}")
    print(f"  Coherence files:     {cor_count}")
    print(f"  ConnComp files:      {conn_count}")

    dates = set()
    for f in unw_dir.glob("*.unw.tif"):
        result = extract_dates_from_filename(f.name)
        if result:
            dates.add(result[0])
            dates.add(result[1])

    if dates:
        sorted_dates = sorted(dates)
        print(f"\n  Date range:          {sorted_dates[0]} -> {sorted_dates[-1]}")
        print(f"  Unique dates:        {len(sorted_dates)}")
        print(f"  Interferogram pairs: {unw_count}")

    if args.metadata:
        from openeo2mintpy.geometry import load_stack_metadata

        meta = load_stack_metadata(args.metadata)
        radar = meta["radar"]
        bperp = [a["bperp_mean"] for a in meta["acquisitions"]]
        print(f"\n  Orbit direction:     {radar['ORBIT_DIRECTION']}")
        print(f"  Heading:             {radar['HEADING']:.3f} deg")
        print(f"  Incidence (centre):  {radar['INCIDENCE_ANGLE']:.3f} deg")
        print(f"  Reference date:      {meta['reference_date']}")
        print(f"  Bperp range:         {min(bperp):.1f} .. {max(bperp):.1f} m")

    rsc_count = count_files(unw_dir, "*.rsc")
    if rsc_count > 0:
        print(f"\n  Existing .rsc files: {rsc_count}")
    else:
        print("\n  WARNING: No .rsc files found -- run 'openeo2mintpy prepare' to generate them.")

    print(f"\n{'-' * 50}\n")


def _get_version():
    """Get package version string."""
    try:
        from openeo2mintpy import __version__
        return __version__
    except ImportError:
        return "unknown"


if __name__ == "__main__":
    main()
