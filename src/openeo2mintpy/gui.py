"""
Tkinter-based graphical interface for openEO2Mintpy.

Replaces the terminal wizard with a point-and-click form where users select
directories and files via native file-chooser dialogs. Designed for Linux
desktops (GNOME, KDE, XFCE) but works anywhere Tk is available.

Features:
    - Step 0 split tab to separate 3-band openEO GeoTIFFs
    - Directory / file pickers for every path parameter
    - Per-field "?" help icons that reveal tooltips on hover
    - Reference date auto-detection from baseline directory
    - Load / Save settings to openeo2mintpy_settings.json
    - Background worker thread so the UI stays responsive
    - Progress bar and scrollable log of generator output
"""

from __future__ import annotations

import importlib.util
import logging
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from openeo2mintpy.config import generate_mintpy_config
from openeo2mintpy.metadata import auto_detect_ref_date, count_files
from openeo2mintpy.postprocess import (
    PostProcessError,
    fix_processor_attribute,
    verify_inputs_dir,
)
from openeo2mintpy.prepare import prepare_stack
from openeo2mintpy.settings import (
    SETTINGS_FILENAME,
    find_settings_file,
    load_settings,
    save_settings,
)

try:
    from tkintermapview import TkinterMapView
    HAS_MAP = True
except ImportError:
    HAS_MAP = False

HAS_OPENEO = importlib.util.find_spec("openeo") is not None
if HAS_OPENEO:
    from openeo2mintpy import openeo_client

logger = logging.getLogger(__name__)


# --- Field definitions ---------------------------------------------------
#
# Each entry describes one input row rendered in the form. ``kind`` controls
# the picker widget ("dir" -> directory chooser, "file" -> file chooser,
# "text" -> free text). ``required`` is only enforced at run-time validation.
FIELDS = [
    {
        "key": "unw_dir",
        "label": "Unwrapped directory",
        "kind": "dir",
        "required": True,
        "help": (
            "Directory containing the unwrapped interferograms (*.unw.tif) "
            "produced by Dolphin/SNAPHU.\n\n"
            "Required. File names must follow the YYYYMMDD_YYYYMMDD.unw.tif "
            "pattern so reference/secondary dates can be parsed."
        ),
    },
    {
        "key": "cor_dir",
        "label": "Coherence directory",
        "kind": "dir",
        "required": False,
        "help": (
            "Directory containing coherence rasters (*.cor.tif or "
            "*.int.cor.tif).\n\n"
            "Optional. If left blank the unwrapped directory is used."
        ),
    },
    {
        "key": "ref_date",
        "label": "Reference date",
        "kind": "text",
        "required": False,
        "help": (
            "Reference (super-master) acquisition date in YYYYMMDD format, "
            "e.g. 20240919.\n\n"
            "Use the 'Auto-detect' button to infer it from the unwrapped "
            "directory."
        ),
    },
    {
        "key": "dem_file",
        "label": "DEM file",
        "kind": "file",
        "required": False,
        "help": (
            "Path to the DEM file used by MintPy — typically "
            "hgt.rdr.full produced by ISCE2 topsStack.\n\n"
            "Written to mintpy.load.demFile."
        ),
    },
    {
        "key": "inc_angle_file",
        "label": "Incidence angle file",
        "kind": "file",
        "required": False,
        "help": (
            "Path to the incidence angle raster — typically "
            "los.rdr.full (band 1) produced by ISCE2.\n\n"
            "Written to mintpy.load.incAngleFile."
        ),
    },
    {
        "key": "az_angle_file",
        "label": "Azimuth angle file",
        "kind": "file",
        "required": False,
        "help": (
            "Path to the azimuth angle raster — typically the same "
            "los.rdr.full file used for the incidence angle.\n\n"
            "Written to mintpy.load.azAngleFile."
        ),
    },
    {
        "key": "water_mask_file",
        "label": "Water mask file",
        "kind": "file",
        "required": False,
        "help": (
            "Optional water mask raster.\n\n"
            "Written to mintpy.load.waterMaskFile. Leave empty for 'auto'."
        ),
    },
    {
        "key": "mintpy_processor",
        "label": "MintPy processor",
        "kind": "choice",
        "required": False,
        "default": "isce",
        "choices": ("isce", "hyp3"),
        "help": (
            "Value written to mintpy.load.processor:\n\n"
            "  - isce: recommended when geometry files are ISCE2 "
            "*.rdr.full outputs (hybrid ISCE2 / Dolphin pipeline).\n"
            "  - hyp3: use when every input (ifgs + geometry) is a "
            "geocoded HyP3-style GeoTIFF.\n\n"
            "Note: this is independent of the PROCESSOR field inside "
            "the .rsc sidecars, which openeo2mintpy always writes as "
            "'hyp3' to trigger MintPy's GDAL reader for Dolphin TIFFs."
        ),
    },
    {
        "key": "work_dir",
        "label": "MintPy output directory",
        "kind": "dir_new",
        "required": True,
        "default": "./mintpy",
        "help": (
            "Working directory where mintpy_config.txt will be written.\n\n"
            "Will be created if it does not exist. Default: ./mintpy"
        ),
    },
    {
        "key": "geometry_mode",
        "label": "Geometry mode",
        "kind": "choice",
        "required": False,
        "default": "auto",
        "choices": ("auto", "radar", "geo"),
        "help": (
            "Controls whether .rsc files are written as radar or "
            "geocoded metadata:\n\n"
            "  - auto:  detect from the GeoTIFF (default).\n"
            "  - radar: force radar geometry. Use when Dolphin GeoTIFFs "
            "have no CRS (Origin=0,0 / Pixel=1,1). MintPy will then "
            "produce geometryRadar.h5.\n"
            "  - geo:   force geocoded output. MintPy will expect "
            "geometryGeo.h5.\n\n"
            "If you hit a 'geometryGeo.h5 not found' error after running, "
            "switch to 'radar'."
        ),
    },
]


# Common filename patterns for auto-populating geometry file fields
# from a selected geometry directory. Order matters: first match wins.
GEOMETRY_AUTOFILL = {
    "dem_file": ("hgt.rdr.full", "hgt.rdr", "*dem*.rdr.full", "*dem*.tif", "*height*.rdr.full"),
    "inc_angle_file": ("los.rdr.full", "los.rdr", "*inc*.rdr.full", "*incidence*.tif"),
    "az_angle_file": ("los.rdr.full", "los.rdr", "*az*.rdr.full", "*azimuth*.tif"),
    "water_mask_file": ("waterMask.rdr.full", "water_mask.tif", "*water*.tif"),
}


# ========================================================================
# Tooltip helper
# ========================================================================
class Tooltip:
    """Lightweight tooltip that appears on hover.

    A small borderless Toplevel window is shown near the mouse cursor after
    a short delay. It is destroyed as soon as the pointer leaves the widget
    or the widget is clicked.
    """

    BG = "#ffffe0"
    FG = "#222222"
    BORDER = "#999999"
    DELAY_MS = 400
    WRAP_PX = 340

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        self._after_id: str | None = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        if self._tip is not None:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6

        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tip.configure(bg=self.BORDER)

        label = tk.Label(
            tip,
            text=self.text,
            justify="left",
            background=self.BG,
            foreground=self.FG,
            relief="flat",
            borderwidth=0,
            wraplength=self.WRAP_PX,
            padx=8,
            pady=6,
            font=("TkDefaultFont", 9),
        )
        label.pack(padx=1, pady=1)
        self._tip = tip

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


# ========================================================================
# Main application
# ========================================================================
class QueueHandler(logging.Handler):
    """Custom logging handler to route logs to a Queue."""

    def __init__(self, log_queue: queue.Queue[str]) -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.log_queue.put(msg)
        except Exception:
            self.handleError(record)


class OpenEO2MintpyApp(tk.Tk):
    """Main application window."""

    PAD = 6

    def __init__(self) -> None:
        super().__init__()
        self.title("openEO2Mintpy")
        self.geometry("820x900")
        self.minsize(720, 760)

        self._entries: dict[str, tk.StringVar] = {}
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._openeo_running = False
        self._split_running = False
        self.openeo_connection = None
        self.next_click_corner = "NW"
        self.map_polygon = None
        self.query_groups = None

        self._build_style()
        self._build_widgets()
        self._preload_settings()

        # Route openEO and application logs to GUI
        self.queue_handler = QueueHandler(self._log_queue)
        self.queue_handler.setFormatter(logging.Formatter("%(message)s"))

        logging.getLogger("openeo").setLevel(logging.INFO)
        logging.getLogger("openeo").addHandler(self.queue_handler)

        logging.getLogger("openeo2mintpy").setLevel(logging.INFO)
        logging.getLogger("openeo2mintpy").addHandler(self.queue_handler)

        self.after(120, self._drain_log_queue)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Help.TLabel", foreground="#1565c0", font=("TkDefaultFont", 9, "bold"))
        style.configure("Hint.TLabel", foreground="#666666", font=("TkDefaultFont", 8))
        style.configure("Heading.TLabel", font=("TkDefaultFont", 13, "bold"))
        style.configure("SubHeading.TLabel", foreground="#555555")
        style.configure(
            "Warning.TLabel",
            foreground="#8a6d00",
            background="#fff8d6",
            font=("TkDefaultFont", 9),
        )
        style.configure("WarningTitle.TLabel", foreground="#8a6d00",
                        font=("TkDefaultFont", 10, "bold"))

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="openEO2Mintpy", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text=(
                "Bridge between openEO Sentinel-1 InSAR outputs and MintPy. "
                "Work through the tabs in order."
            ),
            style="SubHeading.TLabel",
        ).pack(anchor="w")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        self._build_dispatcher_tab(self.notebook)
        self._build_split_tab(self.notebook)
        self._build_prepare_tab(self.notebook)
        self._build_postprocess_tab(self.notebook)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(outer, textvariable=self.status_var, style="Hint.TLabel").pack(
            anchor="w", pady=(4, 0)
        )

    def _build_dispatcher_tab(self, notebook: ttk.Notebook) -> None:
        """Tab 0: openEO CDSE Dispatcher & ROI Selection."""
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="0. openEO Dispatcher")

        # Define default values for openEO-related variables
        openeo_defaults = {
            "openeo_backend": "https://openeo.dataspace.copernicus.eu",
            "openeo_cwl_url": (
                "https://raw.githubusercontent.com/cloudinsar/s1-workflows/"
                "refs/heads/keep_snap_metadata/cwl/sar_interferogram.cwl"
            ),
            "openeo_start_date": "2024-01-01",
            "openeo_end_date": "2024-12-31",
            "openeo_polarisation": "VV",
            "openeo_track": "14",
            "openeo_direction": "ASCENDING",
            "openeo_burst_id": "30",
            "openeo_sub_swath": "IW1",
            "openeo_bbox_north": "40.95",
            "openeo_bbox_south": "40.80",
            "openeo_bbox_east": "35.50",
            "openeo_bbox_west": "35.30",
            "openeo_max_baseline_days": "24",
            "openeo_work_dir": "./openeo_inputs",
            "openeo_job_ids": "",
        }

        for key, default in openeo_defaults.items():
            if key not in self._entries:
                self._entries[key] = tk.StringVar(value=default)

        # Setup bbox write trace
        for key in (
            "openeo_bbox_north",
            "openeo_bbox_south",
            "openeo_bbox_east",
            "openeo_bbox_west",
        ):
            self._entries[key].trace_add("write", self._on_bbox_change)

        # Master pane layout: left for inputs (fixed width), right for map + log (expand)
        left_pane = ttk.Frame(tab, padding=5, width=380)
        left_pane.pack(side="left", fill="y", padx=(0, 5))
        left_pane.pack_propagate(False)

        right_pane = ttk.Frame(tab, padding=5)
        right_pane.pack(side="right", fill="both", expand=True)

        # ── Left Pane Contents ──
        # Section 1: openEO Connection
        conn_frame = ttk.LabelFrame(left_pane, text="  1. openEO Connection  ", padding=6)
        conn_frame.pack(fill="x", pady=(0, 6))
        conn_frame.columnconfigure(1, weight=1)

        ttk.Label(conn_frame, text="Backend URL").grid(
            row=0, column=0, sticky="w", pady=2, padx=4
        )
        ttk.Entry(conn_frame, textvariable=self._entries["openeo_backend"]).grid(
            row=0, column=1, sticky="ew", pady=2, padx=4
        )

        self.openeo_connect_btn = ttk.Button(
            conn_frame, text="Connect (Browser Login)", command=self._run_openeo_connect
        )
        self.openeo_connect_btn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4, padx=4)
        Tooltip(
            self.openeo_connect_btn,
            "Connect to openEO backend. Will open browser for OIDC authentication.",
        )

        # Section 2: Catalog Query & ROI
        query_frame = ttk.LabelFrame(left_pane, text="  2. Catalogue Query & ROI  ", padding=6)
        query_frame.pack(fill="x", pady=(0, 6))
        query_frame.columnconfigure(1, weight=1)
        query_frame.columnconfigure(3, weight=1)

        ttk.Label(query_frame, text="Start Date").grid(row=0, column=0, sticky="w", pady=2, padx=2)
        ttk.Entry(query_frame, textvariable=self._entries["openeo_start_date"], width=11).grid(
            row=0, column=1, sticky="ew", pady=2, padx=2
        )

        ttk.Label(query_frame, text="End Date").grid(row=0, column=2, sticky="w", pady=2, padx=2)
        ttk.Entry(query_frame, textvariable=self._entries["openeo_end_date"], width=11).grid(
            row=0, column=3, sticky="ew", pady=2, padx=2
        )

        ttk.Label(query_frame, text="Track").grid(row=1, column=0, sticky="w", pady=2, padx=2)
        ttk.Entry(query_frame, textvariable=self._entries["openeo_track"], width=6).grid(
            row=1, column=1, sticky="ew", pady=2, padx=2
        )

        ttk.Label(query_frame, text="Direction").grid(row=1, column=2, sticky="w", pady=2, padx=2)
        ttk.Combobox(
            query_frame,
            textvariable=self._entries["openeo_direction"],
            values=("ASCENDING", "DESCENDING"),
            state="readonly",
            width=10,
        ).grid(row=1, column=3, sticky="ew", pady=2, padx=2)

        ttk.Label(query_frame, text="Burst ID").grid(row=2, column=0, sticky="w", pady=2, padx=2)
        ttk.Entry(query_frame, textvariable=self._entries["openeo_burst_id"], width=6).grid(
            row=2, column=1, sticky="ew", pady=2, padx=2
        )

        ttk.Label(query_frame, text="Sub-Swath").grid(
            row=2, column=2, sticky="w", pady=2, padx=2
        )
        ttk.Combobox(
            query_frame,
            textvariable=self._entries["openeo_sub_swath"],
            values=("IW1", "IW2", "IW3"),
            state="readonly",
            width=6,
        ).grid(row=2, column=3, sticky="ew", pady=2, padx=2)

        ttk.Label(query_frame, text="Polarisation").grid(
            row=3, column=0, sticky="w", pady=2, padx=2
        )
        ttk.Combobox(
            query_frame,
            textvariable=self._entries["openeo_polarisation"],
            values=("VV", "VH"),
            state="readonly",
            width=6,
        ).grid(row=3, column=1, sticky="ew", pady=2, padx=2)

        # Compact 2x2 Grid for Bounding Box coordinates
        bbox_frame = ttk.LabelFrame(query_frame, text="  Bounding Box (Map Linked)  ", padding=4)
        bbox_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=4)
        bbox_frame.columnconfigure(1, weight=1)
        bbox_frame.columnconfigure(3, weight=1)

        ttk.Label(bbox_frame, text="North Lat").grid(
            row=0, column=0, sticky="w", pady=1, padx=2
        )
        ttk.Entry(
            bbox_frame, textvariable=self._entries["openeo_bbox_north"], width=9
        ).grid(row=0, column=1, sticky="ew", pady=1, padx=2)

        ttk.Label(bbox_frame, text="South Lat").grid(
            row=0, column=2, sticky="w", pady=1, padx=2
        )
        ttk.Entry(
            bbox_frame, textvariable=self._entries["openeo_bbox_south"], width=9
        ).grid(row=0, column=3, sticky="ew", pady=1, padx=2)

        ttk.Label(bbox_frame, text="East Lon").grid(
            row=1, column=0, sticky="w", pady=1, padx=2
        )
        ttk.Entry(
            bbox_frame, textvariable=self._entries["openeo_bbox_east"], width=9
        ).grid(row=1, column=1, sticky="ew", pady=1, padx=2)

        ttk.Label(bbox_frame, text="West Lon").grid(
            row=1, column=2, sticky="w", pady=1, padx=2
        )
        ttk.Entry(
            bbox_frame, textvariable=self._entries["openeo_bbox_west"], width=9
        ).grid(row=1, column=3, sticky="ew", pady=1, padx=2)

        self.openeo_find_bursts_btn = ttk.Button(
            query_frame,
            text="\U0001F50D Find Bursts for ROI",
            command=self._run_find_bursts,
        )
        self.openeo_find_bursts_btn.grid(
            row=5, column=0, columnspan=4, sticky="ew", pady=(4, 1), padx=2
        )
        Tooltip(
            self.openeo_find_bursts_btn,
            "Query CDSE catalogue to find all available Sentinel-1 Track / Burst ID / Swath\n"
            "combinations within the current bounding box and date range.\n"
            "Select a row from the results to auto-populate Track, Burst ID, Direction, and Sub-Swath.",
        )

        self.openeo_query_btn = ttk.Button(
            query_frame, text="Query Catalogue & Select Pairs", command=self._run_openeo_query
        )
        self.openeo_query_btn.grid(row=6, column=0, columnspan=4, sticky="ew", pady=4, padx=2)
        Tooltip(
            self.openeo_query_btn,
            "Search CDSE catalogue for burst dates and list matching pairs."
        )

        # Section 3: Job Submission & Status
        job_frame = ttk.LabelFrame(left_pane, text="  3. InSAR Job Dispatch & Control  ", padding=6)
        job_frame.pack(fill="x", pady=(0, 6))
        job_frame.columnconfigure(1, weight=1)

        ttk.Label(job_frame, text="Max Baseline (days)").grid(
            row=0, column=0, sticky="w", pady=2, padx=4
        )
        ttk.Entry(
            job_frame, textvariable=self._entries["openeo_max_baseline_days"], width=6
        ).grid(row=0, column=1, sticky="w", pady=2, padx=4)

        self.openeo_submit_btn = ttk.Button(
            job_frame, text="Submit Job Parts", command=self._run_openeo_submit, state="disabled"
        )
        self.openeo_submit_btn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4, padx=4)
        Tooltip(
            self.openeo_submit_btn,
            "Submit processing batch job parts to openEO (requires query first)."
        )

        ttk.Label(job_frame, text="Job ID(s)").grid(row=2, column=0, sticky="w", pady=2, padx=4)
        ttk.Entry(
            job_frame, textvariable=self._entries["openeo_job_ids"]
        ).grid(row=2, column=1, sticky="ew", pady=2, padx=4)

        btn_row = ttk.Frame(job_frame)
        btn_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=4, padx=4)
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)

        self.openeo_status_btn = ttk.Button(
            btn_row, text="Query Status", command=self._run_openeo_status
        )
        self.openeo_status_btn.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        Tooltip(self.openeo_status_btn, "Check status of the specified Job ID(s).")

        self.openeo_download_btn = ttk.Button(
            btn_row, text="Download Results", command=self._run_openeo_download
        )
        self.openeo_download_btn.grid(row=0, column=1, sticky="ew", padx=(2, 0))
        Tooltip(self.openeo_download_btn, "Download finished job outputs to local folder.")

        # Section 4: Output Folder Configuration
        out_frame = ttk.LabelFrame(left_pane, text="  4. Output Folder  ", padding=6)
        out_frame.pack(fill="x", pady=(0, 6))
        out_frame.columnconfigure(1, weight=1)

        ttk.Label(out_frame, text="Download Dir").grid(row=0, column=0, sticky="w", pady=2, padx=4)
        ttk.Entry(
            out_frame, textvariable=self._entries["openeo_work_dir"]
        ).grid(row=0, column=1, sticky="ew", pady=2, padx=4)

        browse_btn = ttk.Button(
            out_frame,
            text="Browse...",
            width=9,
            command=lambda: self._browse_split_dir("openeo_work_dir", False),
        )
        browse_btn.grid(row=0, column=2, sticky="e", pady=2, padx=(4, 0))

        # Disable buttons if openeo is not installed
        if not HAS_OPENEO:
            self.openeo_connect_btn.configure(state="disabled")
            self.openeo_find_bursts_btn.configure(state="disabled")
            self.openeo_query_btn.configure(state="disabled")
            self.openeo_submit_btn.configure(state="disabled")
            self.openeo_status_btn.configure(state="disabled")
            self.openeo_download_btn.configure(state="disabled")
            messagebox.showwarning(
                "Missing Dependencies",
                "openEO Python Client is not installed. Please run 'pip install openeo' "
                "to enable openEO job management."
            )

        # ── Right Pane Contents ──
        # Top: Map
        map_frame = ttk.LabelFrame(
            right_pane,
            text="  Interactive ROI Selection (Double-Click / Right-Click)  ",
            padding=5,
        )
        map_frame.pack(fill="both", expand=True)

        if HAS_MAP:
            self.map_widget = TkinterMapView(map_frame, corner_radius=0)
            self.map_widget.pack(fill="both", expand=True)
            # Default center: Merzifon, Turkey (lat=40.897, lng=35.422), zoom 10
            self.map_widget.set_position(40.897, 35.422)
            self.map_widget.set_zoom(10)

            # Bind callbacks
            self.map_widget.add_left_click_map_command(self._map_click_cb)
            self.map_widget.add_right_click_menu_command(
                "Set Northwest (NW) Corner", self._set_nw_corner, pass_coords=True
            )
            self.map_widget.add_right_click_menu_command(
                "Set Southeast (SE) Corner", self._set_se_corner, pass_coords=True
            )

            # Draw initial polygon if values are present
            self._on_bbox_change()
        else:
            placeholder = ttk.Label(
                map_frame,
                text=(
                    "Map view requires 'tkintermapview' package.\n"
                    "Install it using: pip install tkintermapview"
                ),
                justify="center",
                font=("TkDefaultFont", 10, "italic")
            )
            placeholder.pack(expand=True)

        # Bottom: openEO Log
        log_frame = ttk.LabelFrame(right_pane, text="  openEO Dispatcher Log  ", padding=5)
        log_frame.pack(fill="x", side="bottom", pady=(5, 0))
        self.openeo_log = scrolledtext.ScrolledText(
            log_frame,
            height=11,
            state="disabled",
            wrap="word",
            font=("TkFixedFont", 9),
        )
        self.openeo_log.pack(fill="both", expand=True)

    def _on_bbox_change(self, *args) -> None:
        if not hasattr(self, "map_widget") or self.map_widget is None or not HAS_MAP:
            return

        try:
            n = float(self._entries["openeo_bbox_north"].get().strip())
            s = float(self._entries["openeo_bbox_south"].get().strip())
            e = float(self._entries["openeo_bbox_east"].get().strip())
            w = float(self._entries["openeo_bbox_west"].get().strip())
        except ValueError:
            return  # Incomplete or invalid floats, don't draw polygon yet

        # Remove old polygon if exists
        if self.map_polygon:
            try:
                self.map_polygon.delete()
            except Exception:
                pass
            self.map_polygon = None

        # Draw new bounding box polygon
        coords = [(n, w), (n, e), (s, e), (s, w), (n, w)]
        try:
            self.map_polygon = self.map_widget.set_polygon(
                coords,
                outline_color="#1565c0",
                fill_color=None,
                border_width=3
            )
        except Exception as exc:
            logger.debug("Failed to set map polygon: %s", exc)

    def _map_click_cb(self, coords: tuple[float, float]) -> None:
        lat, lon = coords
        if self.next_click_corner == "NW":
            self._entries["openeo_bbox_north"].set(f"{lat:.5f}")
            self._entries["openeo_bbox_west"].set(f"{lon:.5f}")
            self.next_click_corner = "SE"
            self._openeo_log(f"Map Click: Northwest corner set to: Lat {lat:.5f}, Lon {lon:.5f}")
            self.status_var.set("Northwest set. Next click will set Southeast.")
        else:
            self._entries["openeo_bbox_south"].set(f"{lat:.5f}")
            self._entries["openeo_bbox_east"].set(f"{lon:.5f}")
            self.next_click_corner = "NW"
            self._openeo_log(f"Map Click: Southeast corner set to: Lat {lat:.5f}, Lon {lon:.5f}")
            self.status_var.set("Southeast set. Next click will set Northwest.")
            # Normalize after both corners are set
            self._normalize_bbox()

    def _set_nw_corner(self, coords: tuple[float, float]) -> None:
        lat, lon = coords
        self._entries["openeo_bbox_north"].set(f"{lat:.5f}")
        self._entries["openeo_bbox_west"].set(f"{lon:.5f}")
        self.next_click_corner = "SE"
        self._openeo_log(f"Menu Selection: Northwest corner set to: Lat {lat:.5f}, Lon {lon:.5f}")
        self.status_var.set("Northwest corner set.")

    def _set_se_corner(self, coords: tuple[float, float]) -> None:
        lat, lon = coords
        self._entries["openeo_bbox_south"].set(f"{lat:.5f}")
        self._entries["openeo_bbox_east"].set(f"{lon:.5f}")
        self.next_click_corner = "NW"
        self._openeo_log(f"Menu Selection: Southeast corner set to: Lat {lat:.5f}, Lon {lon:.5f}")
        self.status_var.set("Southeast corner set.")
        self._normalize_bbox()

    def _normalize_bbox(self) -> None:
        """Ensure North >= South and East >= West, swapping if necessary."""
        try:
            n = float(self._entries["openeo_bbox_north"].get().strip())
            s = float(self._entries["openeo_bbox_south"].get().strip())
            e = float(self._entries["openeo_bbox_east"].get().strip())
            w = float(self._entries["openeo_bbox_west"].get().strip())
        except ValueError:
            return  # Incomplete values; skip normalization

        swapped = False
        if n < s:
            n, s = s, n
            swapped = True
        if e < w:
            e, w = w, e
            swapped = True

        if swapped:
            # Temporarily remove traces to prevent recursion
            for key in (
                "openeo_bbox_north",
                "openeo_bbox_south",
                "openeo_bbox_east",
                "openeo_bbox_west",
            ):
                self._entries[key].trace_remove(
                    "write",
                    self._entries[key].trace_info()[0][1],
                )

            self._entries["openeo_bbox_north"].set(f"{n:.5f}")
            self._entries["openeo_bbox_south"].set(f"{s:.5f}")
            self._entries["openeo_bbox_east"].set(f"{e:.5f}")
            self._entries["openeo_bbox_west"].set(f"{w:.5f}")

            # Re-attach traces
            for key in (
                "openeo_bbox_north",
                "openeo_bbox_south",
                "openeo_bbox_east",
                "openeo_bbox_west",
            ):
                self._entries[key].trace_add("write", self._on_bbox_change)

            self._openeo_log(
                f"Auto-normalized BBox: N={n:.5f} S={s:.5f} E={e:.5f} W={w:.5f}"
            )
            # Redraw polygon
            self._on_bbox_change()

    def _openeo_log(self, message: str) -> None:
        self.openeo_log.configure(state="normal")
        self.openeo_log.insert("end", message + "\n")
        self.openeo_log.see("end")
        self.openeo_log.configure(state="disabled")

    def _openeo_log_clear(self) -> None:
        self.openeo_log.configure(state="normal")
        self.openeo_log.delete("1.0", "end")
        self.openeo_log.configure(state="disabled")

    def _run_openeo_connect(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Run", "An operation is already in progress.")
            return

        backend_url = self._entries["openeo_backend"].get().strip()
        if not backend_url:
            messagebox.showerror("Invalid inputs", "Backend URL is required.")
            return

        self.openeo_connect_btn.configure(state="disabled")
        self._openeo_running = True
        self.status_var.set("Connecting to openEO (Browser authentication)...")
        self._openeo_log_clear()
        self._openeo_log("Starting openEO OIDC connection sequence...")

        self._worker = threading.Thread(
            target=self._run_openeo_connect_worker,
            args=(backend_url,),
            daemon=True,
        )
        self._worker.start()

    def _run_openeo_connect_worker(self, backend_url: str) -> None:
        def _oidc_display(msg, end="\n"):
            """Route OIDC device-code display messages through the log queue."""
            text = str(msg).strip()
            if text:
                self._log_queue.put(f"[openeo] {text}")

        try:
            conn = openeo_client.connect_and_auth(
                backend_url, display=_oidc_display
            )
            self.openeo_connection = conn
            self._log_queue.put("[openeo] Connection and OIDC authentication successful.")
            self._log_queue.put("__openeo_connect__:ok")
        except Exception as exc:
            logger.exception("openEO Connection failed")
            self._log_queue.put(f"[openeo] Connection failed: {exc}")
            self._log_queue.put("__openeo_connect__:error")

    def _run_find_bursts(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Run", "An operation is already in progress.")
            return

        # Read inputs
        start_date = self._entries["openeo_start_date"].get().strip()
        end_date = self._entries["openeo_end_date"].get().strip()
        polarisation = self._entries["openeo_polarisation"].get().strip()
        n = self._entries["openeo_bbox_north"].get().strip()
        s = self._entries["openeo_bbox_south"].get().strip()
        e = self._entries["openeo_bbox_east"].get().strip()
        w = self._entries["openeo_bbox_west"].get().strip()

        errors = []
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            errors.append("Dates must be in YYYY-MM-DD format.")

        try:
            fn = float(n)
            fs = float(s)
            fe = float(e)
            fw = float(w)
            if not (-90 <= fn <= 90 and -90 <= fs <= 90):
                errors.append("Latitude must be between -90 and 90.")
            if not (-180 <= fe <= 180 and -180 <= fw <= 180):
                errors.append("Longitude must be between -180 and 180.")
            # Auto-swap instead of erroring
            if fn < fs:
                fn, fs = fs, fn
                self._entries["openeo_bbox_north"].set(f"{fn:.5f}")
                self._entries["openeo_bbox_south"].set(f"{fs:.5f}")
            if fe < fw:
                fe, fw = fw, fe
                self._entries["openeo_bbox_east"].set(f"{fe:.5f}")
                self._entries["openeo_bbox_west"].set(f"{fw:.5f}")
            n, s, e, w = f"{fn:.5f}", f"{fs:.5f}", f"{fe:.5f}", f"{fw:.5f}"
        except ValueError:
            errors.append("Bounding Box coordinates must be valid numbers.")

        if errors:
            messagebox.showerror("Invalid inputs", "\n".join(f"- {err}" for err in errors))
            return

        self.openeo_find_bursts_btn.configure(state="disabled")
        self._openeo_running = True
        self.status_var.set("Querying CDSE Catalogue...")
        self._openeo_log_clear()
        self._openeo_log("Starting catalog search for unique bursts in ROI...")

        aoi_wkt = f"POLYGON(({w} {s}, {w} {n}, {e} {n}, {e} {s}, {w} {s}))"

        self._worker = threading.Thread(
            target=self._run_find_bursts_worker,
            args=(start_date, end_date, polarisation, aoi_wkt),
            daemon=True,
        )
        self._worker.start()

    def _run_find_bursts_worker(
        self,
        start_date: str,
        end_date: str,
        polarisation: str,
        aoi_wkt: str,
    ) -> None:
        try:
            self._log_queue.put(
                f"[openeo] Querying burst acquisitions between {start_date} and {end_date}..."
            )
            bursts = openeo_client.query_burst_acquisitions(
                start_date=start_date,
                end_date=end_date,
                polarisation=polarisation,
                aoi_wkt=aoi_wkt
            )
            self._log_queue.put(
                f"[openeo] Total burst acquisitions returned from catalogue: {len(bursts)}"
            )

            unique_bursts = openeo_client.extract_unique_bursts(bursts)
            self._log_queue.put(
                f"[openeo] Found {len(unique_bursts)} unique Track/Burst combinations."
            )

            if not unique_bursts:
                self._log_queue.put("__openeo_find_bursts__:empty")
            else:
                self.found_bursts = unique_bursts
                self._log_queue.put("__openeo_find_bursts__:ok")
        except Exception as exc:
            logger.exception("openEO burst search failed")
            self._log_queue.put(f"[openeo] Burst search failed: {exc}")
            self._log_queue.put("__openeo_find_bursts__:error")

    def _run_openeo_query(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Run", "An operation is already in progress.")
            return

        # Read and validate inputs
        start_date = self._entries["openeo_start_date"].get().strip()
        end_date = self._entries["openeo_end_date"].get().strip()
        pol = self._entries["openeo_polarisation"].get().strip()
        track = self._entries["openeo_track"].get().strip()
        burst_id = self._entries["openeo_burst_id"].get().strip()
        sub_swath = self._entries["openeo_sub_swath"].get().strip()
        max_baseline = self._entries["openeo_max_baseline_days"].get().strip()
        n = self._entries["openeo_bbox_north"].get().strip()
        s = self._entries["openeo_bbox_south"].get().strip()
        e = self._entries["openeo_bbox_east"].get().strip()
        w = self._entries["openeo_bbox_west"].get().strip()

        errors = []
        try:
            t_val = int(track)
        except ValueError:
            errors.append("Track must be an integer.")
        try:
            b_val = int(burst_id)
        except ValueError:
            errors.append("Burst ID must be an integer.")
        try:
            mb_val = int(max_baseline)
        except ValueError:
            errors.append("Max baseline must be an integer.")

        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            errors.append("Dates must be in YYYY-MM-DD format.")

        try:
            fn = float(n)
            fs = float(s)
            fe = float(e)
            fw = float(w)
            if not (-90 <= fn <= 90 and -90 <= fs <= 90):
                errors.append("Latitude must be between -90 and 90.")
            if not (-180 <= fe <= 180 and -180 <= fw <= 180):
                errors.append("Longitude must be between -180 and 180.")
            # Auto-swap instead of erroring
            if fn < fs:
                fn, fs = fs, fn
                self._entries["openeo_bbox_north"].set(f"{fn:.5f}")
                self._entries["openeo_bbox_south"].set(f"{fs:.5f}")
            if fe < fw:
                fe, fw = fw, fe
                self._entries["openeo_bbox_east"].set(f"{fe:.5f}")
                self._entries["openeo_bbox_west"].set(f"{fw:.5f}")
            n, s, e, w = f"{fn:.5f}", f"{fs:.5f}", f"{fe:.5f}", f"{fw:.5f}"
        except ValueError:
            errors.append("Bounding Box coordinates must be valid numbers.")

        if errors:
            messagebox.showerror("Invalid inputs", "\n".join(f"- {err}" for err in errors))
            return

        self.openeo_query_btn.configure(state="disabled")
        self._openeo_running = True
        self.status_var.set("Querying CDSE Catalogue...")
        self._openeo_log_clear()
        self._openeo_log("Starting catalog query...")

        aoi_wkt = f"POLYGON(({w} {s}, {w} {n}, {e} {n}, {e} {s}, {w} {s}))"

        self._worker = threading.Thread(
            target=self._run_openeo_query_worker,
            args=(start_date, end_date, pol, t_val, b_val, sub_swath, aoi_wkt, mb_val),
            daemon=True,
        )
        self._worker.start()

    def _run_openeo_query_worker(
        self,
        start_date,
        end_date,
        polarisation,
        track,
        burst_id,
        sub_swath,
        aoi_wkt,
        max_baseline_days,
    ) -> None:
        try:
            self._log_queue.put(
                f"[openeo] Querying burst acquisitions between {start_date} and {end_date}..."
            )
            bursts = openeo_client.query_burst_acquisitions(
                start_date=start_date,
                end_date=end_date,
                polarisation=polarisation,
                aoi_wkt=aoi_wkt
            )
            self._log_queue.put(
                f"[openeo] Total burst acquisitions returned from catalogue: {len(bursts)}"
            )

            dates = openeo_client.filter_bursts(
                bursts=bursts,
                track=track,
                burst_id=burst_id,
                sub_swath=sub_swath
            )
            self._log_queue.put(f"[openeo] Unique matching dates found: {len(dates)}")
            for d in dates:
                self._log_queue.put(f"  - {d}")

            if not dates:
                self._log_queue.put("[openeo] No matching dates found. Cannot generate pairs.")
                self._log_queue.put("__openeo_query__:error")
                return

            pairs = openeo_client.generate_pairs(dates, max_baseline_days)
            self._log_queue.put(f"[openeo] Generated {len(pairs)} interferogram pairs:")
            for p in pairs:
                self._log_queue.put(f"  - {p[0]} -> {p[1]}")

            if not pairs:
                self._log_queue.put(
                    "[openeo] No pairs generated (temporal baseline too short/long)."
                )
                self._log_queue.put("__openeo_query__:error")
                return

            groups = openeo_client.split_pairs_into_groups(pairs)
            self.query_groups = groups
            self._log_queue.put(f"[openeo] Split pairs into {len(groups)} batch job part(s):")
            for idx, group in enumerate(groups, 1):
                self._log_queue.put(f"  Part {idx}: {len(group)} pairs starting with {group[0][0]}")

            self._log_queue.put("__openeo_query__:ok")
        except Exception as exc:
            logger.exception("openEO query failed")
            self._log_queue.put(f"[openeo] Query failed: {exc}")
            self._log_queue.put("__openeo_query__:error")

    def _run_openeo_submit(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Run", "An operation is already in progress.")
            return

        if not self.openeo_connection:
            messagebox.showerror("Not connected", "Please connect to openEO first.")
            return

        if not self.query_groups:
            messagebox.showerror("No pairs", "Please query dates and generate pairs first.")
            return

        track = int(self._entries["openeo_track"].get().strip())
        direction = self._entries["openeo_direction"].get().strip()
        burst_id = int(self._entries["openeo_burst_id"].get().strip())
        sub_swath = self._entries["openeo_sub_swath"].get().strip()
        cwl_url = self._entries["openeo_cwl_url"].get().strip()

        self.openeo_submit_btn.configure(state="disabled")
        self._openeo_running = True
        self.status_var.set("Submitting InSAR batch jobs...")
        self._openeo_log("Submitting batch jobs to openEO...")

        self._worker = threading.Thread(
            target=self._run_openeo_submit_worker,
            args=(track, direction, burst_id, sub_swath, cwl_url),
            daemon=True,
        )
        self._worker.start()

    def _run_openeo_submit_worker(self, track, direction, burst_id, sub_swath, cwl_url) -> None:
        try:
            submitted_ids = []
            total_parts = len(self.query_groups)
            for idx, group in enumerate(self.query_groups, 1):
                self._log_queue.put(
                    f"[openeo] Submitting Job Part {idx}/{total_parts} ({len(group)} pairs)..."
                )
                job_info = openeo_client.submit_insar_job(
                    connection=self.openeo_connection,
                    track=track,
                    direction=direction,
                    burst_id=burst_id,
                    sub_swath=sub_swath,
                    group_pairs=group,
                    part_num=idx,
                    total_parts=total_parts,
                    cwl_url=cwl_url,
                )
                job_id = job_info["job_id"]
                submitted_ids.append(job_id)
                self._log_queue.put(f"[openeo] Successfully created Job ID: {job_id}")

            self._log_queue.put(f"__openeo_submit__:ok:{','.join(submitted_ids)}")
        except Exception as exc:
            logger.exception("openEO submission failed")
            self._log_queue.put(f"[openeo] Submission failed: {exc}")
            self._log_queue.put("__openeo_submit__:error")

    def _run_openeo_status(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Run", "An operation is already in progress.")
            return

        if not self.openeo_connection:
            messagebox.showerror("Not connected", "Please connect to openEO first.")
            return

        job_ids_str = self._entries["openeo_job_ids"].get().strip()
        job_ids = [jid.strip() for jid in job_ids_str.split(",") if jid.strip()]

        if not job_ids:
            messagebox.showerror("No Job ID", "Please specify one or more comma-separated Job IDs.")
            return

        self.openeo_status_btn.configure(state="disabled")
        self._openeo_running = True
        self.status_var.set("Checking job status...")
        self._openeo_log("Querying status of batch jobs...")

        self._worker = threading.Thread(
            target=self._run_openeo_status_worker,
            args=(job_ids,),
            daemon=True,
        )
        self._worker.start()

    def _run_openeo_status_worker(self, job_ids: list[str]) -> None:
        try:
            for jid in job_ids:
                self._log_queue.put(f"[openeo] Fetching status for Job {jid}...")
                job = self.openeo_connection.job(jid)
                desc = job.describe_job()
                status = desc.get("status", "unknown")
                title = desc.get("title", "no title")
                self._log_queue.put(f"[openeo] Job: '{title}'")
                self._log_queue.put(f"  Status: {status.upper()}")
                if desc.get("error"):
                    self._log_queue.put(f"  Error: {desc['error']}")
                progress = desc.get("progress")
                if progress is not None:
                    self._log_queue.put(f"  Progress: {progress}%")
            self._log_queue.put("__openeo_status__:done")
        except Exception as exc:
            logger.exception("openEO status check failed")
            self._log_queue.put(f"[openeo] Status check failed: {exc}")
            self._log_queue.put("__openeo_status__:done")

    def _run_openeo_download(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Run", "An operation is already in progress.")
            return

        if not self.openeo_connection:
            messagebox.showerror("Not connected", "Please connect to openEO first.")
            return

        job_ids_str = self._entries["openeo_job_ids"].get().strip()
        job_ids = [jid.strip() for jid in job_ids_str.split(",") if jid.strip()]

        if not job_ids:
            messagebox.showerror("No Job ID", "Please specify one or more comma-separated Job IDs.")
            return

        work_dir = self._entries["openeo_work_dir"].get().strip()
        if not work_dir:
            messagebox.showerror("No directory", "Please specify a download directory.")
            return

        self.openeo_download_btn.configure(state="disabled")
        self._openeo_running = True
        self.status_var.set("Downloading job results...")
        self._openeo_log(f"Downloading finished job results to {work_dir}...")

        self._worker = threading.Thread(
            target=self._run_openeo_download_worker,
            args=(job_ids, work_dir),
            daemon=True,
        )
        self._worker.start()

    def _run_openeo_download_worker(self, job_ids: list[str], work_dir: str) -> None:
        try:
            total_files = 0
            for jid in job_ids:
                self._log_queue.put(f"[openeo] Downloading Job {jid} results...")
                # Verify status first to avoid blocking on incomplete jobs
                job = self.openeo_connection.job(jid)
                status = job.describe_job().get("status", "unknown")
                if status != "finished":
                    self._log_queue.put(
                        f"[openeo] Skipping Job {jid} (status: {status}). "
                        "Only 'finished' jobs can be downloaded."
                    )
                    continue
                files_count = openeo_client.download_job_results(
                    self.openeo_connection, jid, work_dir
                )
                self._log_queue.put(f"[openeo] Downloaded {files_count} files for Job {jid}.")
                total_files += files_count
            self._log_queue.put(
                f"[openeo] Download complete. Total files downloaded: {total_files}"
            )
            self._log_queue.put("__openeo_download__:done")
        except Exception as exc:
            logger.exception("openEO download failed")
            self._log_queue.put(f"[openeo] Download failed: {exc}")
            self._log_queue.put("__openeo_download__:done")

    def _build_split_tab(self, notebook: ttk.Notebook) -> None:
        """Tab 0: split openEO 3-band GeoTIFFs."""
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="0. Split & Align")

        banner = ttk.Frame(tab)
        banner.pack(fill="x", pady=(0, 8))
        ttk.Label(
            banner,
            text="Step 0 - Split & Align openEO GeoTIFFs",
            style="SubHeading.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            banner,
            text=(
                "Extracts Band 2 (Unwrapped Phase) and Band 3 (Coherence) from openEO GeoTIFFs, "
                "then aligns all rasters to a common grid (intersection bounding box). "
                "Output files follow 'YYYYMMDD_YYYYMMDD.unw.tif' / '.cor.tif' naming."
            ),
            style="Hint.TLabel",
            wraplength=760,
            justify="left",
        ).pack(anchor="w")

        form = ttk.LabelFrame(tab, text="  Bands Splitter Inputs  ", padding=10)
        form.pack(fill="x", pady=(0, 8))
        form.columnconfigure(1, weight=1)

        # Row 0: openeo_dir
        ttk.Label(form, text="openEO input directory  *").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        openeo_var = tk.StringVar(value="")
        self._entries["openeo_dir"] = openeo_var
        ttk.Entry(form, textvariable=openeo_var).grid(row=0, column=1, sticky="ew", pady=4)

        openeo_btns = ttk.Frame(form)
        openeo_btns.grid(row=0, column=2, sticky="e", padx=(6, 0), pady=4)
        openeo_browse = ttk.Button(
            openeo_btns, text="Browse...", width=10,
            command=lambda: self._browse_split_dir("openeo_dir", True)
        )
        openeo_browse.pack(side="left")
        Tooltip(openeo_browse, "Select directory containing openEO 3-band GeoTIFF files.")

        help0 = ttk.Label(openeo_btns, text=" ? ", style="Help.TLabel", cursor="question_arrow")
        help0.pack(side="left", padx=(6, 0))
        Tooltip(help0, "Directory where openEO TIFFs (e.g. phase_coh_*.tif) are stored.")

        # Row 1: unw_out_dir
        ttk.Label(form, text="Output unwrapped directory  *").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=4
        )
        unw_out_var = tk.StringVar(value="")
        self._entries["unw_out_dir"] = unw_out_var
        ttk.Entry(form, textvariable=unw_out_var).grid(row=1, column=1, sticky="ew", pady=4)

        unw_out_btns = ttk.Frame(form)
        unw_out_btns.grid(row=1, column=2, sticky="e", padx=(6, 0), pady=4)
        unw_out_browse = ttk.Button(
            unw_out_btns, text="Browse...", width=10,
            command=lambda: self._browse_split_dir("unw_out_dir", False)
        )
        unw_out_browse.pack(side="left")
        Tooltip(unw_out_browse, "Select or create output directory for Unwrapped Phase files.")

        help1 = ttk.Label(unw_out_btns, text=" ? ", style="Help.TLabel", cursor="question_arrow")
        help1.pack(side="left", padx=(6, 0))
        Tooltip(help1, "Directory where the split single-band unwrapped phase TIFFs will be saved.")

        # Row 2: cor_out_dir
        ttk.Label(form, text="Output coherence directory  *").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=4
        )
        cor_out_var = tk.StringVar(value="")
        self._entries["cor_out_dir"] = cor_out_var
        ttk.Entry(form, textvariable=cor_out_var).grid(row=2, column=1, sticky="ew", pady=4)

        cor_out_btns = ttk.Frame(form)
        cor_out_btns.grid(row=2, column=2, sticky="e", padx=(6, 0), pady=4)
        cor_out_browse = ttk.Button(
            cor_out_btns, text="Browse...", width=10,
            command=lambda: self._browse_split_dir("cor_out_dir", False)
        )
        cor_out_browse.pack(side="left")
        Tooltip(cor_out_browse, "Select or create output directory for Coherence files.")

        help2 = ttk.Label(cor_out_btns, text=" ? ", style="Help.TLabel", cursor="question_arrow")
        help2.pack(side="left", padx=(6, 0))
        Tooltip(help2, "Directory where the split single-band coherence TIFFs will be saved.")

        action_bar = ttk.Frame(tab)
        action_bar.pack(fill="x", pady=(0, 8))

        split_load_btn = ttk.Button(
            action_bar, text="Load settings", command=self._load_settings_clicked
        )
        split_load_btn.pack(side="left")
        Tooltip(split_load_btn, f"Load previously saved settings from {SETTINGS_FILENAME}.")

        split_save_btn = ttk.Button(
            action_bar, text="Save settings", command=self._save_settings_clicked
        )
        split_save_btn.pack(side="left", padx=(6, 0))
        Tooltip(
            split_save_btn,
            f"Save the current form values to {SETTINGS_FILENAME} for future runs.",
        )

        self.split_run_btn = ttk.Button(
            action_bar, text="Split & Align", command=self._run_split_clicked
        )
        self.split_run_btn.pack(side="right")
        Tooltip(self.split_run_btn, "Split openEO bands and align all rasters to a common grid.")

        split_quit_btn = ttk.Button(action_bar, text="Quit", command=self.destroy)
        split_quit_btn.pack(side="right", padx=(0, 6))

        progress_frame = ttk.Frame(tab)
        progress_frame.pack(fill="x", pady=(0, 6))
        self.split_progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.split_progress.pack(fill="x")

        # Optional sub-step: Prepare DEM (NASADEM tiles -> aligned dem.tif)
        dem_frame = ttk.LabelFrame(
            tab, text="  Prepare DEM (optional)  ", padding=10
        )
        dem_frame.pack(fill="x", pady=(0, 8))
        dem_frame.columnconfigure(1, weight=1)

        ttk.Label(
            dem_frame,
            text=(
                "Extracts, merges and warps NASADEM HGT/zip tiles to match the "
                "aligned stack grid (reference is taken from the 'Output "
                "unwrapped directory' set above). Skip this step if you already "
                "have a co-registered DEM."
            ),
            style="Hint.TLabel",
            wraplength=720,
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        # Row 1: dem_zip_dir
        ttk.Label(dem_frame, text="NASADEM tiles directory  *").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=4
        )
        dem_zip_var = tk.StringVar(value="")
        self._entries["dem_zip_dir"] = dem_zip_var
        ttk.Entry(dem_frame, textvariable=dem_zip_var).grid(
            row=1, column=1, sticky="ew", pady=4
        )

        dem_zip_btns = ttk.Frame(dem_frame)
        dem_zip_btns.grid(row=1, column=2, sticky="e", padx=(6, 0), pady=4)
        dem_zip_browse = ttk.Button(
            dem_zip_btns, text="Browse...", width=10,
            command=lambda: self._browse_split_dir("dem_zip_dir", True),
        )
        dem_zip_browse.pack(side="left")
        Tooltip(
            dem_zip_browse,
            "Select the directory containing NASADEM .zip / .hgt / .dem tiles.",
        )

        help_zip = ttk.Label(
            dem_zip_btns, text=" ? ", style="Help.TLabel", cursor="question_arrow"
        )
        help_zip.pack(side="left", padx=(6, 0))
        Tooltip(
            help_zip,
            "Directory containing downloaded NASADEM .zip files or already "
            "extracted .hgt / .dem tiles that cover the InSAR stack footprint.",
        )

        # Row 2: dem_output_file
        ttk.Label(dem_frame, text="Output DEM file  *").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=4
        )
        dem_out_var = tk.StringVar(value="")
        self._entries["dem_output_file"] = dem_out_var
        ttk.Entry(dem_frame, textvariable=dem_out_var).grid(
            row=2, column=1, sticky="ew", pady=4
        )

        dem_out_btns = ttk.Frame(dem_frame)
        dem_out_btns.grid(row=2, column=2, sticky="e", padx=(6, 0), pady=4)
        dem_out_browse = ttk.Button(
            dem_out_btns, text="Browse...", width=10,
            command=self._browse_dem_output_file,
        )
        dem_out_browse.pack(side="left")
        Tooltip(
            dem_out_browse,
            "Choose the output path for the merged & aligned DEM GeoTIFF "
            "(e.g. .../process/dem/dem.tif).",
        )

        help_out = ttk.Label(
            dem_out_btns, text=" ? ", style="Help.TLabel", cursor="question_arrow"
        )
        help_out.pack(side="left", padx=(6, 0))
        Tooltip(
            help_out,
            "Path where the final co-registered dem.tif will be written. A "
            "companion ROI_PAC .rsc sidecar is generated automatically.",
        )

        # Row 3: action button
        dem_action = ttk.Frame(dem_frame)
        dem_action.grid(row=3, column=0, columnspan=3, sticky="e", pady=(4, 0))
        self.dem_run_btn = ttk.Button(
            dem_action, text="Prepare DEM", command=self._run_prepare_dem_clicked
        )
        self.dem_run_btn.pack(side="right")
        Tooltip(
            self.dem_run_btn,
            "Merge & warp NASADEM tiles to match the aligned interferogram grid.",
        )

        log_frame = ttk.LabelFrame(tab, text="  Step 0 Log  ", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.split_log = scrolledtext.ScrolledText(
            log_frame,
            height=10,
            state="disabled",
            wrap="word",
            font=("TkFixedFont", 9),
        )
        self.split_log.pack(fill="both", expand=True)

        next_frame = ttk.Frame(tab)
        next_frame.pack(fill="x", pady=(6, 0))
        ttk.Label(
            next_frame,
            text=(
                "After a successful split (and optional DEM prepare), set "
                "'Unwrapped directory' and 'Coherence directory' in the next tab "
                "to these output paths."
            ),
            style="Hint.TLabel",
        ).pack(anchor="w")

    def _browse_split_dir(self, key: str, must_exist: bool) -> None:
        initial = self._entries[key].get().strip() or str(Path.cwd())
        if must_exist and not Path(initial).exists():
            initial = str(Path.cwd())
        path = filedialog.askdirectory(
            title=f"Select {key}", initialdir=initial, mustexist=must_exist
        )
        if path:
            self._entries[key].set(path)

    def _run_split_clicked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Run", "An operation is already in progress.")
            return

        openeo_dir = self._entries["openeo_dir"].get().strip()
        unw_out_dir = self._entries["unw_out_dir"].get().strip()
        cor_out_dir = self._entries["cor_out_dir"].get().strip()

        errors = []
        if not openeo_dir:
            errors.append("openEO input directory is required.")
        elif not Path(openeo_dir).is_dir():
            errors.append(f"openEO input directory does not exist: {openeo_dir}")

        if not unw_out_dir:
            errors.append("Output unwrapped directory is required.")
        if not cor_out_dir:
            errors.append("Output coherence directory is required.")

        if errors:
            messagebox.showerror("Invalid inputs", "\n".join(f"- {e}" for e in errors))
            return

        self.split_run_btn.configure(state="disabled")
        self.split_progress.configure(value=0)
        self.status_var.set("Splitting & aligning...")
        self._split_log_clear()
        self._split_log("Starting split & align pipeline...")

        self._worker = threading.Thread(
            target=self._run_split_worker,
            args=(openeo_dir, unw_out_dir, cor_out_dir),
            daemon=True,
        )
        self._worker.start()

    def _run_split_worker(
        self, openeo_dir: str, unw_out_dir: str, cor_out_dir: str
    ) -> None:
        try:
            from openeo2mintpy.align import align_rasters
            from openeo2mintpy.split import split_openeo_bands

            def split_progress_cb(current: int, total: int) -> None:
                pct = (current / total * 50.0) if total else 0.0
                msg = f"__split_progress__:{pct:.1f}:Splitting... {current}/{total}"
                self._log_queue.put(msg)

            def align_progress_cb(current: int, total: int) -> None:
                pct = 50.0 + (current / total * 50.0) if total else 50.0
                msg = f"__split_progress__:{pct:.1f}:Aligning... {current}/{total}"
                self._log_queue.put(msg)

            def log_cb(message: str) -> None:
                self._log_queue.put(message)

            # ── Phase 1/2: Split bands ──
            self._log_queue.put("═══ Phase 1/2: Splitting openEO bands ═══")
            result = split_openeo_bands(
                input_dir=openeo_dir,
                unw_dir=unw_out_dir,
                cor_dir=cor_out_dir,
                progress_callback=split_progress_cb,
                log_callback=log_cb,
            )

            self._log_queue.put(f"Split completed: {result['processed']} files.")
            if result['errors']:
                self._log_queue.put(f"Encountered {len(result['errors'])} split errors:")
                for err in result['errors'][:10]:
                    self._log_queue.put(f"  ! {err['file']}: {err['error']}")
                if len(result['errors']) > 10:
                    self._log_queue.put(f"  ... and {len(result['errors']) - 10} more errors.")

            if result['processed'] == 0:
                self._log_queue.put("No files were split. Skipping alignment.")
                self._log_queue.put("__split_done__:error")
                return

            # ── Phase 2/2: Align rasters ──
            self._log_queue.put("")
            self._log_queue.put("═══ Phase 2/2: Aligning rasters to common grid ═══")
            align_result = align_rasters(
                unw_dir=unw_out_dir,
                cor_dir=cor_out_dir,
                log_callback=log_cb,
                progress_callback=align_progress_cb,
            )

            self._log_queue.put(
                f"Alignment completed: {align_result['aligned']} files aligned, "
                f"{len(align_result['errors'])} errors."
            )

            self._log_queue.put("__split_done__:ok")
        except Exception as exc:
            logger.exception("Split & Align run failed")
            self._log_queue.put(f"ERROR: {exc}")
            self._log_queue.put("__split_done__:error")

    def _split_log(self, message: str) -> None:
        self.split_log.configure(state="normal")
        self.split_log.insert("end", message + "\n")
        self.split_log.see("end")
        self.split_log.configure(state="disabled")

    def _split_log_clear(self) -> None:
        self.split_log.configure(state="normal")
        self.split_log.delete("1.0", "end")
        self.split_log.configure(state="disabled")

    # ------------------------------------------------------------------
    # Optional Prepare DEM sub-step
    # ------------------------------------------------------------------
    def _browse_dem_output_file(self) -> None:
        """Pick an output path for the merged & aligned DEM GeoTIFF."""
        current = self._entries["dem_output_file"].get().strip()
        initial_dir = str(Path(current).parent) if current else str(Path.cwd())
        initial_name = Path(current).name if current else "dem.tif"
        path = filedialog.asksaveasfilename(
            title="Select output DEM file",
            defaultextension=".tif",
            filetypes=[("GeoTIFF", "*.tif"), ("All files", "*.*")],
            initialdir=initial_dir,
            initialfile=initial_name,
        )
        if path:
            self._entries["dem_output_file"].set(path)

    def _run_prepare_dem_clicked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Run", "An operation is already in progress.")
            return

        unw_out_dir = self._entries["unw_out_dir"].get().strip()
        zip_dir = self._entries["dem_zip_dir"].get().strip()
        output_file = self._entries["dem_output_file"].get().strip()

        errors = []
        if not unw_out_dir:
            errors.append(
                "'Output unwrapped directory' is required as the alignment "
                "reference. Fill it in the Bands Splitter Inputs section above."
            )
        elif not Path(unw_out_dir).is_dir():
            errors.append(
                f"'Output unwrapped directory' does not exist: {unw_out_dir}"
            )
        if not zip_dir:
            errors.append("NASADEM tiles directory is required.")
        elif not Path(zip_dir).is_dir():
            errors.append(f"NASADEM tiles directory does not exist: {zip_dir}")
        if not output_file:
            errors.append("Output DEM file path is required.")

        if errors:
            messagebox.showerror("Invalid inputs", "\n".join(f"- {e}" for e in errors))
            return

        # Disable both buttons so log routing stays coherent (existing routing
        # uses split_run_btn state to decide whether messages belong to the
        # Step 0 log).
        self.dem_run_btn.configure(state="disabled")
        self.split_run_btn.configure(state="disabled")
        self.split_progress.configure(value=0)
        self.status_var.set("Preparing DEM...")
        self._split_log("")
        self._split_log("=== Optional step: Preparing DEM ===")

        self._worker = threading.Thread(
            target=self._run_prepare_dem_worker,
            args=(unw_out_dir, zip_dir, output_file),
            daemon=True,
        )
        self._worker.start()

    def _run_prepare_dem_worker(
        self, unw_dir: str, zip_dir: str, output_file: str
    ) -> None:
        try:
            from openeo2mintpy.align import prepare_dem

            def log_cb(message: str) -> None:
                self._log_queue.put(message)

            output_path = prepare_dem(
                unw_dir=unw_dir,
                zip_dir=zip_dir,
                output_file=output_file,
                log_callback=log_cb,
            )
            self._log_queue.put(f"DEM written to: {output_path}")
            self._log_queue.put("__dem_done__:ok")
        except Exception as exc:
            logger.exception("Prepare DEM run failed")
            self._log_queue.put(f"ERROR: {exc}")
            self._log_queue.put("__dem_done__:error")

    def _build_prepare_tab(self, notebook: ttk.Notebook) -> None:
        """Tab 1: generate .rsc sidecars + mintpy_config.txt."""
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="1. Prepare (pre load_data)")

        banner = ttk.Frame(tab)
        banner.pack(fill="x", pady=(0, 8))
        ttk.Label(
            banner,
            text="Step 1 - run BEFORE smallbaselineApp.py --dostep load_data",
            style="SubHeading.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            banner,
            text=(
                "Generates ROI_PAC .rsc sidecars next to every Dolphin "
                "GeoTIFF and writes mintpy_config.txt in the output "
                "directory."
            ),
            style="Hint.TLabel",
            wraplength=760,
            justify="left",
        ).pack(anchor="w")

        form = ttk.LabelFrame(tab, text="  Inputs  ", padding=10)
        form.pack(fill="x", pady=(0, 8))
        form.columnconfigure(1, weight=1)

        for row, field in enumerate(FIELDS):
            self._build_field_row(form, row, field)

        action_bar = ttk.Frame(tab)
        action_bar.pack(fill="x", pady=(0, 8))

        load_btn = ttk.Button(
            action_bar, text="Load settings", command=self._load_settings_clicked
        )
        load_btn.pack(side="left")
        Tooltip(load_btn, f"Load previously saved settings from {SETTINGS_FILENAME}.")

        save_btn = ttk.Button(
            action_bar, text="Save settings", command=self._save_settings_clicked
        )
        save_btn.pack(side="left", padx=(6, 0))
        Tooltip(save_btn, f"Save the current form values to {SETTINGS_FILENAME} for future runs.")

        self.run_btn = ttk.Button(action_bar, text="Run", command=self._run_clicked)
        self.run_btn.pack(side="right")
        Tooltip(self.run_btn, "Generate .rsc sidecar files and write the MintPy configuration.")

        quit_btn = ttk.Button(action_bar, text="Quit", command=self.destroy)
        quit_btn.pack(side="right", padx=(0, 6))

        progress_frame = ttk.Frame(tab)
        progress_frame.pack(fill="x", pady=(0, 6))
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x")

        log_frame = ttk.LabelFrame(tab, text="  Log  ", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(
            log_frame,
            height=10,
            state="disabled",
            wrap="word",
            font=("TkFixedFont", 9),
        )
        self.log.pack(fill="both", expand=True)

        next_frame = ttk.Frame(tab)
        next_frame.pack(fill="x", pady=(6, 0))
        ttk.Label(
            next_frame,
            text="After a successful run, proceed with MintPy:",
            style="Hint.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            next_frame,
            text="    smallbaselineApp.py mintpy_config.txt --dostep load_data",
            font=("TkFixedFont", 9),
        ).pack(anchor="w")
        ttk.Label(
            next_frame,
            text="Then switch to the '2. Post-Load Fix' tab.",
            style="Hint.TLabel",
        ).pack(anchor="w")

    def _build_postprocess_tab(self, notebook: ttk.Notebook) -> None:
        """Tab 2: patch PROCESSOR attribute on HDF5 files after load_data."""
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="2. Post-Load Fix")

        warn_frame = tk.Frame(tab, bg="#fff8d6", highlightbackground="#e0c674",
                              highlightthickness=1)
        warn_frame.pack(fill="x", pady=(0, 10))
        tk.Label(
            warn_frame,
            text="WARNING - run this step AFTER MintPy's load_data succeeds",
            bg="#fff8d6",
            fg="#8a6d00",
            font=("TkDefaultFont", 10, "bold"),
            anchor="w",
            padx=10,
            pady=2,
        ).pack(fill="x", pady=(8, 0))
        tk.Label(
            warn_frame,
            text=(
                "This tab rewrites the PROCESSOR attribute inside\n"
                "    inputs/ifgramStack.h5  and  inputs/geometryGeo.h5\n"
                "from 'hyp3' to 'isce'. Those HDF5 files only exist AFTER you run:\n"
                "    smallbaselineApp.py mintpy_config.txt --dostep load_data\n\n"
                "Fixes the runtime error:\n"
                "    AttributeError: Unknown InSAR processor: hyp3 to locate look up table!"
            ),
            bg="#fff8d6",
            fg="#5a4a00",
            justify="left",
            anchor="w",
            padx=10,
            pady=2,
            font=("TkDefaultFont", 9),
        ).pack(fill="x", pady=(0, 8))

        form = ttk.LabelFrame(tab, text="  Post-processing inputs  ", padding=10)
        form.pack(fill="x", pady=(0, 8))
        form.columnconfigure(1, weight=1)

        self._post_entries: dict[str, tk.StringVar] = {}

        # Row 0: inputs directory
        ttk.Label(form, text="MintPy inputs directory  *").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        inputs_var = tk.StringVar(value="")
        self._post_entries["inputs_dir"] = inputs_var
        inputs_entry = ttk.Entry(form, textvariable=inputs_var)
        inputs_entry.grid(row=0, column=1, sticky="ew", pady=4)

        inputs_btns = ttk.Frame(form)
        inputs_btns.grid(row=0, column=2, sticky="e", padx=(6, 0), pady=4)
        inputs_browse = ttk.Button(
            inputs_btns,
            text="Browse...",
            width=10,
            command=lambda: self._browse_post_inputs_dir(),
        )
        inputs_browse.pack(side="left")
        Tooltip(
            inputs_browse,
            "Select the MintPy inputs/ directory created by load_data, "
            "e.g. /mnt/w/tubitak3501_merzifon/inputs",
        )
        help1 = ttk.Label(inputs_btns, text=" ? ", style="Help.TLabel",
                          cursor="question_arrow")
        help1.pack(side="left", padx=(6, 0))
        Tooltip(
            help1,
            "The 'inputs/' folder inside your MintPy working directory. "
            "It must already contain ifgramStack.h5 and geometryGeo.h5 "
            "produced by 'smallbaselineApp.py --dostep load_data'.",
        )

        # Row 1: old processor
        ttk.Label(form, text="Old processor (from)").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=4
        )
        old_var = tk.StringVar(value="hyp3")
        self._post_entries["old_processor"] = old_var
        ttk.Combobox(
            form, textvariable=old_var, values=("hyp3", "isce"), state="readonly"
        ).grid(row=1, column=1, sticky="ew", pady=4)

        # Row 2: new processor
        ttk.Label(form, text="New processor (to)").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=4
        )
        new_var = tk.StringVar(value="isce")
        self._post_entries["new_processor"] = new_var
        ttk.Combobox(
            form, textvariable=new_var, values=("isce", "hyp3"), state="readonly"
        ).grid(row=2, column=1, sticky="ew", pady=4)

        # Row 3: target files
        ttk.Label(form, text="Target files (comma separated)").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=4
        )
        targets_var = tk.StringVar(value="ifgramStack.h5, geometryGeo.h5")
        self._post_entries["target_files"] = targets_var
        ttk.Entry(form, textvariable=targets_var).grid(
            row=3, column=1, sticky="ew", pady=4
        )

        action_bar = ttk.Frame(tab)
        action_bar.pack(fill="x", pady=(0, 6))

        verify_btn = ttk.Button(
            action_bar, text="Verify", command=self._post_verify_clicked
        )
        verify_btn.pack(side="left")
        Tooltip(
            verify_btn,
            "Inspect the inputs/ directory: report current PROCESSOR "
            "values and check that /latitude + /longitude datasets exist.",
        )

        self.post_apply_btn = ttk.Button(
            action_bar, text="Apply fix", command=self._post_apply_clicked
        )
        self.post_apply_btn.pack(side="left", padx=(6, 0))
        Tooltip(
            self.post_apply_btn,
            "Rewrite PROCESSOR / INSAR_PROCESSOR HDF5 attributes "
            "(hyp3 -> isce by default).",
        )

        log_frame = ttk.LabelFrame(tab, text="  Post-processing log  ", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.post_log = scrolledtext.ScrolledText(
            log_frame,
            height=12,
            state="disabled",
            wrap="word",
            font=("TkFixedFont", 9),
        )
        self.post_log.pack(fill="both", expand=True)

        next_frame = ttk.Frame(tab)
        next_frame.pack(fill="x", pady=(6, 0))
        ttk.Label(
            next_frame,
            text="After a successful patch, resume the full MintPy chain:",
            style="Hint.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            next_frame,
            text="    smallbaselineApp.py mintpy_config.txt",
            font=("TkFixedFont", 9),
        ).pack(anchor="w")

    def _build_field_row(self, parent: ttk.Frame, row: int, field: dict) -> None:
        """Render one labelled input row with picker + help icon."""
        key = field["key"]
        required = field.get("required", False)

        label_text = field["label"] + ("  *" if required else "")
        lbl = ttk.Label(parent, text=label_text)
        lbl.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)

        var = tk.StringVar(value=field.get("default", ""))
        self._entries[key] = var

        kind = field["kind"]
        if kind == "choice":
            entry = ttk.Combobox(
                parent,
                textvariable=var,
                values=list(field.get("choices", ())),
                state="readonly",
            )
        else:
            entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", pady=4)

        button_frame = ttk.Frame(parent)
        button_frame.grid(row=row, column=2, sticky="e", padx=(6, 0), pady=4)

        if kind == "dir" or kind == "dir_new":
            browse = ttk.Button(
                button_frame, text="Browse...", width=10,
                command=lambda k=key, must_exist=(kind == "dir"): self._browse_dir(k, must_exist),
            )
            browse.pack(side="left")
            Tooltip(browse, "Open a directory picker.")
        elif kind == "file":
            browse = ttk.Button(
                button_frame, text="Browse...", width=10,
                command=lambda k=key: self._browse_file(k),
            )
            browse.pack(side="left")
            Tooltip(browse, "Open a file picker.")
        elif kind == "text" and key == "ref_date":
            auto = ttk.Button(
                button_frame, text="Auto-detect", width=12,
                command=self._auto_detect_ref_date,
            )
            auto.pack(side="left")
            Tooltip(
                auto,
                "Try to infer the reference date from the unwrapped directory "
                "by looking at the interferogram file names.",
            )

        help_icon = ttk.Label(
            button_frame, text=" ? ", style="Help.TLabel", cursor="question_arrow"
        )
        help_icon.pack(side="left", padx=(6, 0))
        Tooltip(help_icon, field["help"])

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _preload_settings(self) -> None:
        """Populate form with saved settings if present."""
        path = find_settings_file()
        if not path:
            return
        data = load_settings(path)
        if not data:
            return
        for key, var in self._entries.items():
            value = data.get(key)
            if value:
                var.set(str(value))
        self._log(f"Loaded settings from {path}")

    def _collect(self) -> dict[str, str | None]:
        """Read the form into a settings dict (empty strings become None)."""
        result: dict[str, str | None] = {}
        for key, var in self._entries.items():
            value = var.get().strip()
            result[key] = value if value else None
        return result

    def _load_settings_clicked(self) -> None:
        path = filedialog.askopenfilename(
            title="Load settings",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile=SETTINGS_FILENAME,
        )
        if not path:
            return
        data = load_settings(path)
        if not data:
            messagebox.showwarning("Load settings", "Could not read settings from that file.")
            return
        for key, var in self._entries.items():
            var.set(str(data.get(key) or ""))
        self._log(f"Loaded settings from {path}")
        self.status_var.set(f"Loaded settings: {path}")

    def _save_settings_clicked(self) -> None:
        settings = self._collect()
        path = filedialog.asksaveasfilename(
            title="Save settings",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile=SETTINGS_FILENAME,
        )
        if not path:
            return
        save_settings(settings, settings_path=path)
        self._log(f"Saved settings to {path}")
        self.status_var.set(f"Saved settings: {path}")

    # ------------------------------------------------------------------
    # Pickers / helpers
    # ------------------------------------------------------------------
    def _browse_dir(self, key: str, must_exist: bool) -> None:
        initial = self._entries[key].get().strip() or str(Path.cwd())
        if must_exist and not Path(initial).exists():
            initial = str(Path.cwd())
        path = filedialog.askdirectory(
            title=f"Select {key}", initialdir=initial, mustexist=must_exist
        )
        if path:
            self._entries[key].set(path)
            if key == "unw_dir":
                self._autofill_geometry_files(path)

    def _autofill_geometry_files(self, geometry_dir: str) -> None:
        """Populate empty geometry / lookup file fields from a directory.

        Helps the user by filling in the DEM, incidence, azimuth and
        (critically) lookup Y / X paths when the selected geometry
        directory contains the expected ISCE2 topsStack file names.
        Fields that already have a value are left untouched so the user
        stays in control.
        """
        geom_dir = Path(geometry_dir)
        if not geom_dir.is_dir():
            return

        filled: list[str] = []
        for field_key, patterns in GEOMETRY_AUTOFILL.items():
            if self._entries.get(field_key) is None:
                continue
            if self._entries[field_key].get().strip():
                continue
            resolved = self._first_match(geom_dir, patterns)
            if resolved is not None:
                self._entries[field_key].set(str(resolved))
                filled.append(f"{field_key} -> {resolved.name}")

        if filled:
            self._log("Auto-filled from geometry directory:")
            for line in filled:
                self._log(f"  - {line}")

    @staticmethod
    def _first_match(directory: Path, patterns: tuple[str, ...]) -> Path | None:
        """Return the first path inside *directory* matching any pattern."""
        for pattern in patterns:
            exact = directory / pattern
            if exact.is_file():
                return exact
            matches = sorted(directory.glob(pattern))
            if matches:
                return matches[0]
        return None

    def _browse_file(self, key: str) -> None:
        initial = self._entries[key].get().strip() or str(Path.cwd())
        start_dir = initial if Path(initial).is_dir() else str(Path(initial).parent or Path.cwd())
        path = filedialog.askopenfilename(title=f"Select {key}", initialdir=start_dir)
        if path:
            self._entries[key].set(path)

    def _auto_detect_ref_date(self) -> None:
        unw = self._entries["unw_dir"].get().strip()
        if not unw:
            messagebox.showinfo(
                "Auto-detect reference date",
                "Please select an unwrapped directory first.",
            )
            return
        detected = auto_detect_ref_date(unw)
        if detected:
            self._entries["ref_date"].set(detected)
            self._log(f"Auto-detected reference date: {detected}")
            self.status_var.set(f"Auto-detected reference date: {detected}")
        else:
            messagebox.showwarning(
                "Auto-detect reference date",
                "Could not determine a reference date from the unwrapped directory.",
            )

    # ------------------------------------------------------------------
    # Validation + execution
    # ------------------------------------------------------------------
    def _validate(self, settings: dict) -> list[str]:
        errors: list[str] = []

        unw = settings.get("unw_dir")
        if not unw:
            errors.append("Unwrapped directory is required.")
        elif not Path(unw).is_dir():
            errors.append(f"Unwrapped directory does not exist: {unw}")

        for key in ("cor_dir",):
            value = settings.get(key)
            if value and not Path(value).is_dir():
                errors.append(f"{key} does not exist: {value}")

        for file_key in (
            "dem_file",
            "inc_angle_file",
            "az_angle_file",
            "water_mask_file",
        ):
            value = settings.get(file_key)
            if value and not Path(value).is_file():
                errors.append(f"{file_key} does not exist: {value}")

        processor = settings.get("mintpy_processor")
        if processor and processor not in ("isce", "hyp3"):
            errors.append(
                f"MintPy processor must be 'isce' or 'hyp3' (got {processor!r})."
            )

        ref_date = settings.get("ref_date")
        if ref_date and (len(ref_date) != 8 or not ref_date.isdigit()):
            errors.append("Reference date must be in YYYYMMDD format.")

        if not settings.get("work_dir"):
            errors.append("MintPy output directory is required.")

        mode = settings.get("geometry_mode")
        if mode and mode not in ("auto", "radar", "geo"):
            errors.append(f"Geometry mode must be auto, radar or geo (got {mode!r}).")

        return errors

    def _run_clicked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Run", "A run is already in progress.")
            return

        settings = self._collect()
        errors = self._validate(settings)
        if errors:
            messagebox.showerror("Invalid inputs", "\n".join(f"- {e}" for e in errors))
            return

        unw_count = count_files(settings["unw_dir"], "*.unw.tif")
        if unw_count == 0:
            if not messagebox.askyesno(
                "No unwrapped files",
                "No *.unw.tif files were found in the selected directory. "
                "Run anyway?",
            ):
                return

        self._log(f"Found {unw_count} unwrapped files in {settings['unw_dir']}")
        self.run_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self.status_var.set("Running...")

        self._worker = threading.Thread(
            target=self._run_worker,
            args=(settings,),
            daemon=True,
        )
        self._worker.start()

    def _run_worker(self, settings: dict) -> None:
        """Background worker: runs prepare_stack + generate_mintpy_config."""
        try:
            def progress_cb(current: int, total: int) -> None:
                pct = (current / total * 100.0) if total else 0.0
                self._log_queue.put(f"__progress__:{pct:.1f}:{current}/{total}")

            self._log_queue.put("Generating .rsc sidecar files...")

            result = prepare_stack(
                unw_dir=settings["unw_dir"],
                cor_dir=settings.get("cor_dir"),
                conncomp_dir=settings["unw_dir"],
                geometry_dir=None,
                baseline_dir=None,
                ref_xml=None,
                ref_date=settings.get("ref_date"),
                progress_callback=progress_cb,
                geometry_mode=settings.get("geometry_mode") or "auto",
            )

            self._log_queue.put(
                f"Wrote {result['rsc_written']} .rsc files "
                f"({len(result.get('errors', []))} errors)."
            )
            for err in result.get("errors", [])[:5]:
                self._log_queue.put(f"  ! {err.get('file')}: {err.get('error')}")
            if len(result.get("errors", [])) > 5:
                self._log_queue.put(f"  ... and {len(result['errors']) - 5} more errors")

            self._log_queue.put("Generating MintPy configuration...")
            work_dir = settings.get("work_dir") or "./mintpy"
            config_path = generate_mintpy_config(
                work_dir=work_dir,
                unw_dir=settings["unw_dir"],
                cor_dir=settings.get("cor_dir"),
                conncomp_dir=settings["unw_dir"],
                dem_file=settings.get("dem_file"),
                inc_angle_file=settings.get("inc_angle_file"),
                az_angle_file=settings.get("az_angle_file"),
                lookup_y_file=None,
                lookup_x_file=None,
                water_mask_file=settings.get("water_mask_file"),
                processor=settings.get("mintpy_processor") or "isce",
            )
            self._log_queue.put(f"MintPy config written: {config_path}")
            self._log_queue.put("__done__:ok")
        except Exception as exc:
            logger.exception("GUI run failed")
            self._log_queue.put(f"ERROR: {exc}")
            self._log_queue.put("__done__:error")

    # ------------------------------------------------------------------
    # Log pump (called on the Tk main loop)
    # ------------------------------------------------------------------
    def _drain_log_queue(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                if msg.startswith("__split_progress__:"):
                    _, pct, counts = msg.split(":", 2)
                    self.split_progress.configure(value=float(pct))
                    self.status_var.set(f"{counts} ({float(pct):.1f}%)")
                elif msg == "__split_done__:ok":
                    self.split_progress.configure(value=100)
                    self.status_var.set("Split & Align done.")
                    self.split_run_btn.configure(state="normal")

                    # Auto-populate the next tab's fields:
                    unw_out_dir = self._entries.get("unw_out_dir", "").get().strip()
                    cor_out_dir = self._entries.get("cor_out_dir", "").get().strip()
                    if unw_out_dir:
                        self._entries["unw_dir"].set(unw_out_dir)
                    if cor_out_dir:
                        self._entries["cor_dir"].set(cor_out_dir)

                    messagebox.showinfo(
                        "Done",
                        "openEO bands split and aligned successfully!\n\n"
                        "Paths have been auto-populated in the Prepare tab.",
                    )
                elif msg == "__split_done__:error":
                    self.status_var.set("Split & Align failed. See log.")
                    self.split_run_btn.configure(state="normal")
                    messagebox.showerror(
                        "Split & Align failed",
                        (
                            "The split & align process did not complete successfully. "
                            "See log for details."
                        ),
                    )
                elif msg == "__dem_done__:ok":
                    self.split_progress.configure(value=100)
                    self.status_var.set("DEM prepared.")
                    self.dem_run_btn.configure(state="normal")
                    self.split_run_btn.configure(state="normal")

                    # Auto-populate dem_file on the Prepare tab if it is empty
                    # so the user does not need to retype the path.
                    dem_out = self._entries.get("dem_output_file")
                    dem_target = self._entries.get("dem_file")
                    if dem_out is not None and dem_target is not None:
                        value = dem_out.get().strip()
                        if value and not dem_target.get().strip():
                            dem_target.set(value)

                    messagebox.showinfo(
                        "Done",
                        "DEM prepared successfully.\n\n"
                        "The output path has been propagated to the Prepare "
                        "tab's DEM file field (when empty).",
                    )
                elif msg == "__dem_done__:error":
                    self.status_var.set("DEM preparation failed. See log.")
                    self.dem_run_btn.configure(state="normal")
                    self.split_run_btn.configure(state="normal")
                    messagebox.showerror(
                        "DEM preparation failed",
                        "Preparing the DEM did not complete successfully. See "
                        "the Step 0 log for details.",
                    )
                elif msg == "__openeo_connect__:ok":
                    self._openeo_running = False
                    self.openeo_connect_btn.configure(state="normal")
                    self.status_var.set("openEO connected.")
                    messagebox.showinfo(
                        "Connection Successful",
                        "openEO & CDSE connection and OIDC authentication successful!"
                    )
                elif msg == "__openeo_connect__:error":
                    self._openeo_running = False
                    self.openeo_connect_btn.configure(state="normal")
                    self.status_var.set("Connection failed.")
                    messagebox.showerror(
                        "Connection Failed",
                        "Failed to connect to openEO backend. "
                        "See the openEO Dispatcher Log for details."
                    )
                elif msg == "__openeo_query__:ok":
                    self._openeo_running = False
                    self.openeo_query_btn.configure(state="normal")
                    self.openeo_submit_btn.configure(state="normal")
                    self.status_var.set("Catalogue query complete.")
                    messagebox.showinfo(
                        "Query Complete",
                        f"Catalogue query complete. Successfully generated InSAR pairs.\n\n"
                        f"You can now submit the {len(self.query_groups)} job parts "
                        "using the 'Submit Job Parts' button."
                    )
                elif msg == "__openeo_query__:error":
                    self._openeo_running = False
                    self.openeo_query_btn.configure(state="normal")
                    self.status_var.set("Query failed.")
                    messagebox.showerror(
                        "Query Failed",
                        "Catalogue query failed or no suitable pairs were generated. "
                        "See the openEO Dispatcher Log for details."
                    )
                elif msg.startswith("__openeo_submit__:ok:"):
                    self._openeo_running = False
                    self.openeo_submit_btn.configure(state="normal")
                    job_ids = msg.split(":", 2)[2]
                    self._entries["openeo_job_ids"].set(job_ids)
                    self.status_var.set("Jobs submitted.")
                    messagebox.showinfo(
                        "Submission Complete",
                        f"Successfully submitted all batch job parts!\n\n"
                        f"Job ID(s): {job_ids}\n\n"
                        f"Use 'Query Status' periodically to check their status."
                    )
                elif msg == "__openeo_submit__:error":
                    self._openeo_running = False
                    self.openeo_submit_btn.configure(state="normal")
                    self.status_var.set("Submission failed.")
                    messagebox.showerror(
                        "Submission Failed",
                        "Failed to submit InSAR batch jobs. "
                        "See the openEO Dispatcher Log for details."
                    )
                elif msg == "__openeo_status__:done":
                    self._openeo_running = False
                    self.openeo_status_btn.configure(state="normal")
                    self.status_var.set("Status check complete.")
                elif msg == "__openeo_download__:done":
                    self._openeo_running = False
                    self.openeo_download_btn.configure(state="normal")
                    self.status_var.set("Download complete.")

                    work_dir = self._entries.get("openeo_work_dir", "").get().strip()
                    if work_dir:
                        self._entries["openeo_dir"].set(work_dir)

                    messagebox.showinfo(
                        "Download Complete",
                        f"Finished downloading job results to:\n{work_dir}\n\n"
                        "The download directory has been auto-populated in the "
                        "'0. Split & Align' tab."
                    )
                elif msg == "__openeo_find_bursts__:ok":
                    self._openeo_running = False
                    self.openeo_find_bursts_btn.configure(state="normal")
                    self.status_var.set("Burst search complete.")
                    self._show_burst_selection_dialog()
                elif msg == "__openeo_find_bursts__:empty":
                    self._openeo_running = False
                    self.openeo_find_bursts_btn.configure(state="normal")
                    self.status_var.set("No bursts found.")
                    messagebox.showinfo("No Results", "No Sentinel-1 bursts found within the selected ROI and dates.")
                elif msg == "__openeo_find_bursts__:error":
                    self._openeo_running = False
                    self.openeo_find_bursts_btn.configure(state="normal")
                    self.status_var.set("Burst search failed.")
                    messagebox.showerror("Error", "Failed to query bursts from CDSE catalogue. See log for details.")
                elif (
                    msg.startswith("__progress__:")
                    or msg == "__done__:ok"
                    or msg == "__done__:error"
                ):
                    if msg.startswith("__progress__:"):
                        _, pct, counts = msg.split(":", 2)
                        self.progress.configure(value=float(pct))
                        self.status_var.set(f"Running... {counts} ({float(pct):.1f}%)")
                    elif msg == "__done__:ok":
                        self.progress.configure(value=100)
                        self.status_var.set("Done.")
                        self.run_btn.configure(state="normal")
                        messagebox.showinfo("Done", "All outputs generated successfully.")
                    elif msg == "__done__:error":
                        self.status_var.set("Failed. See log for details.")
                        self.run_btn.configure(state="normal")
                        messagebox.showerror(
                            "Run failed",
                            "The run did not complete successfully. See the log for details.",
                        )
                else:
                    # Route standard logging message to appropriate window:
                    if msg.startswith("[openeo]") or self._openeo_running:
                        display_msg = msg[8:].lstrip() if msg.startswith("[openeo]") else msg
                        self._openeo_log(display_msg)

                        # Check for OIDC Device Flow instructions to show popup
                        if "Visit " in display_msg and "authenticate" in display_msg:
                            url = ""
                            code = ""
                            try:
                                if "and enter user code" in display_msg:
                                    parts = display_msg.split("Visit ")
                                    url_part = parts[1].split(" and enter")[0].strip()
                                    code_parts = display_msg.split("user code '")
                                    code_part = code_parts[1].split("' to authenticate")[0].strip()
                                    url = url_part
                                    code = code_part
                                else:
                                    parts = display_msg.split("Visit ")
                                    url = parts[1].split(" to authenticate")[0].strip()
                            except Exception:
                                pass
                            if url:
                                self._show_device_code_popup(url, code)
                    elif (
                        str(self.split_run_btn.cget("state")) == "disabled"
                        or str(self.dem_run_btn.cget("state")) == "disabled"
                    ):
                        self._split_log(msg)
                    else:
                        self._log(msg)
        except queue.Empty:
            pass
        finally:
            self.after(120, self._drain_log_queue)

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_device_code_popup(self, url: str, code: str) -> None:
        """Display a modal dialog with copyable CDSE URL and user code."""
        popup = tk.Toplevel(self)
        popup.title("openEO CDSE Authentication")
        popup.geometry("540x350")
        popup.resizable(False, False)
        popup.transient(self)
        popup.grab_set()  # Make it modal

        # Center the popup relative to self (parent)
        popup.update_idletasks()
        parent_x = self.winfo_x()
        parent_y = self.winfo_y()
        parent_w = self.winfo_width()
        parent_h = self.winfo_height()
        w = 540
        h = 350
        x = parent_x + (parent_w - w) // 2
        y = parent_y + (parent_h - h) // 2
        popup.geometry(f"{w}x{h}+{x}+{y}")

        # Styles/Colors
        bg_color = "#f8f9fa"
        popup.configure(bg=bg_color)

        main_frame = ttk.Frame(popup, padding=20)
        main_frame.pack(fill="both", expand=True)

        # Header
        ttk.Label(
            main_frame,
            text="Authentication Required (Device Flow)",
            font=("TkDefaultFont", 12, "bold"),
            foreground="#1565c0",
        ).pack(anchor="w", pady=(0, 10))

        desc = (
            "A headless or WSL environment has been detected.\n"
            "Please follow the steps below to authenticate with Copernicus CDSE:"
        )
        ttk.Label(main_frame, text=desc, font=("TkDefaultFont", 10)).pack(anchor="w", pady=(0, 15))

        # Step 1: URL
        ttk.Label(
            main_frame,
            text="1. Copy and open this URL in your browser:",
            font=("TkDefaultFont", 9, "bold"),
        ).pack(anchor="w")

        url_frame = ttk.Frame(main_frame)
        url_frame.pack(fill="x", pady=(2, 10))

        url_entry = ttk.Entry(
            url_frame,
            font=("TkDefaultFont", 9),
        )
        url_entry.insert(0, url)
        url_entry.configure(state="readonly")
        url_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))

        def copy_url():
            self.clipboard_clear()
            self.clipboard_append(url)
            messagebox.showinfo(
                "Copied",
                "Authentication URL copied to clipboard!",
                parent=popup,
            )

        def open_url():
            import webbrowser
            webbrowser.open(url)

        copy_url_btn = ttk.Button(url_frame, text="Copy Link", width=10, command=copy_url)
        copy_url_btn.pack(side="left", padx=(0, 5))

        open_url_btn = ttk.Button(url_frame, text="Open", width=8, command=open_url)
        open_url_btn.pack(side="left")

        # Step 2: Code (if present)
        if code:
            ttk.Label(
                main_frame,
                text="2. Enter this user code on the activation page:",
                font=("TkDefaultFont", 9, "bold"),
            ).pack(anchor="w")

            code_frame = ttk.Frame(main_frame)
            code_frame.pack(fill="x", pady=(2, 15))

            code_entry = ttk.Entry(
                code_frame,
                font=("Courier New", 12, "bold"),
                justify="center",
            )
            code_entry.insert(0, code)
            code_entry.configure(state="readonly")
            code_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))

            def copy_code():
                self.clipboard_clear()
                self.clipboard_append(code)
                messagebox.showinfo(
                    "Copied",
                    "User code copied to clipboard!",
                    parent=popup,
                )

            copy_code_btn = ttk.Button(code_frame, text="Copy Code", width=10, command=copy_code)
            copy_code_btn.pack(side="left")
        else:
            # Spacer if no code
            ttk.Frame(main_frame, height=45).pack()

        # Footer explanation
        footer_text = (
            "Note: The application will automatically detect when you have authorized\n"
            "in your browser and complete the connection process. You may close "
            "this window afterwards."
        )
        ttk.Label(
            main_frame,
            text=footer_text,
            font=("TkDefaultFont", 8, "italic"),
            foreground="#666666",
        ).pack(anchor="w", pady=(0, 15))

        # Close button
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x")

        close_btn = ttk.Button(btn_frame, text="Close Dialog", command=popup.destroy)
        close_btn.pack(side="right")

    def _show_burst_selection_dialog(self) -> None:
        """Display a modal dialog with a Table of unique Sentinel-1 bursts in the ROI."""
        popup = tk.Toplevel(self)
        popup.title("Select Sentinel-1 Burst for ROI")
        popup.geometry("680x450")
        popup.transient(self)
        popup.grab_set()

        # Center the popup relative to self
        popup.update_idletasks()
        parent_x = self.winfo_x()
        parent_y = self.winfo_y()
        parent_w = self.winfo_width()
        parent_h = self.winfo_height()
        w = 680
        h = 450
        x = parent_x + (parent_w - w) // 2
        y = parent_y + (parent_h - h) // 2
        popup.geometry(f"{w}x{h}+{x}+{y}")

        main_frame = ttk.Frame(popup, padding=15)
        main_frame.pack(fill="both", expand=True)

        # Header Label
        ttk.Label(
            main_frame,
            text="CDSE Burst Catalogue Search Results",
            font=("TkDefaultFont", 11, "bold"),
            foreground="#1565c0",
        ).pack(anchor="w", pady=(0, 5))

        ttk.Label(
            main_frame,
            text="Select a row and click 'Select Burst' (or double-click) to populate Track and Burst ID details:",
            font=("TkDefaultFont", 9),
        ).pack(anchor="w", pady=(0, 10))

        # Middle Frame for Table and Scrollbar
        table_frame = ttk.Frame(main_frame)
        table_frame.pack(fill="both", expand=True, pady=(0, 15))

        columns = ("track", "direction", "swath", "burst_id", "count")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        scrollbar.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scrollbar.set)

        # Column headings & widths
        tree.heading("track", text="Relative Orbit (Track)", anchor="center")
        tree.heading("direction", text="Direction", anchor="center")
        tree.heading("swath", text="Sub-Swath", anchor="center")
        tree.heading("burst_id", text="Burst ID", anchor="center")
        tree.heading("count", text="Acquisitions", anchor="center")

        tree.column("track", width=130, anchor="center")
        tree.column("direction", width=110, anchor="center")
        tree.column("swath", width=100, anchor="center")
        tree.column("burst_id", width=110, anchor="center")
        tree.column("count", width=110, anchor="center")

        # Insert data
        for b in getattr(self, "found_bursts", []):
            tree.insert(
                "",
                "end",
                values=(
                    b["track"],
                    b["direction"],
                    b["swath"],
                    b["burst_id"],
                    b["count"],
                ),
            )

        # Selection handler
        def on_select():
            selected = tree.selection()
            if not selected:
                messagebox.showwarning("Selection Required", "Please select a burst from the list.", parent=popup)
                return
            item = tree.item(selected[0])
            val = item["values"]
            
            # Populate entry boxes
            self._entries["openeo_track"].set(str(val[0]))
            self._entries["openeo_direction"].set(str(val[1]))
            self._entries["openeo_sub_swath"].set(str(val[2]))
            self._entries["openeo_burst_id"].set(str(val[3]))
            
            # Log selection
            self._openeo_log(
                f"Selected Burst from Catalog: Track {val[0]} ({val[1]}), Swath {val[2]}, Burst ID {val[3]}"
            )
            popup.destroy()

        def on_double_click(event):
            on_select()

        tree.bind("<Double-1>", on_double_click)

        # Bottom Button Frame
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x")

        cancel_btn = ttk.Button(btn_frame, text="Cancel", command=popup.destroy, width=12)
        cancel_btn.pack(side="right", padx=(5, 0))

        select_btn = ttk.Button(btn_frame, text="Select Burst", command=on_select, width=15)
        select_btn.pack(side="right")

    # ------------------------------------------------------------------
    # Post-load fix tab
    # ------------------------------------------------------------------
    def _browse_post_inputs_dir(self) -> None:
        initial = self._post_entries["inputs_dir"].get().strip() or str(Path.cwd())
        if not Path(initial).is_dir():
            initial = str(Path.cwd())
        path = filedialog.askdirectory(
            title="Select MintPy inputs/ directory",
            initialdir=initial,
            mustexist=True,
        )
        if path:
            self._post_entries["inputs_dir"].set(path)

    def _post_collect(self) -> dict:
        raw_targets = self._post_entries["target_files"].get().strip()
        targets = tuple(
            t.strip() for t in raw_targets.split(",") if t.strip()
        ) or ("ifgramStack.h5", "geometryGeo.h5")
        return {
            "inputs_dir": self._post_entries["inputs_dir"].get().strip(),
            "old_processor": self._post_entries["old_processor"].get().strip() or "hyp3",
            "new_processor": self._post_entries["new_processor"].get().strip() or "isce",
            "target_files": targets,
        }

    def _post_log(self, message: str) -> None:
        self.post_log.configure(state="normal")
        self.post_log.insert("end", message + "\n")
        self.post_log.see("end")
        self.post_log.configure(state="disabled")

    def _post_verify_clicked(self) -> None:
        params = self._post_collect()
        if not params["inputs_dir"]:
            messagebox.showerror(
                "Verify",
                "Please select the MintPy inputs/ directory first.",
            )
            return
        if not Path(params["inputs_dir"]).is_dir():
            messagebox.showerror(
                "Verify",
                f"Directory does not exist: {params['inputs_dir']}",
            )
            return

        self._post_log(f"--- Verifying {params['inputs_dir']} ---")
        try:
            report = verify_inputs_dir(
                params["inputs_dir"],
                target_files=params["target_files"],
                expected_old=params["old_processor"],
            )
        except PostProcessError as exc:
            self._post_log(f"ERROR: {exc}")
            messagebox.showerror("Verify", str(exc))
            return

        any_missing_lookup = False
        for entry in report:
            tag = "OK" if entry["exists"] else "MISSING"
            self._post_log(f"[{tag}] {entry['path'].name}")
            if entry["exists"]:
                self._post_log(f"      PROCESSOR        : {entry['processor']}")
                self._post_log(f"      INSAR_PROCESSOR  : {entry['insar_processor']}")
                if entry["has_lat_lon"] is not None:
                    self._post_log(f"      has /lat /lon    : {entry['has_lat_lon']}")
                    if entry["has_lat_lon"] is False:
                        any_missing_lookup = True
                self._post_log(f"      needs patch      : {entry['needs_patch']}")
            for issue in entry["issues"]:
                self._post_log(f"      ! {issue}")
        self._post_log("")
        self.status_var.set(
            "Verify complete."
            + (" Missing lookup datasets - see log." if any_missing_lookup else "")
        )

    def _post_apply_clicked(self) -> None:
        params = self._post_collect()
        if not params["inputs_dir"]:
            messagebox.showerror(
                "Apply fix",
                "Please select the MintPy inputs/ directory first.",
            )
            return
        if not Path(params["inputs_dir"]).is_dir():
            messagebox.showerror(
                "Apply fix",
                f"Directory does not exist: {params['inputs_dir']}",
            )
            return
        if not messagebox.askyesno(
            "Apply fix",
            f"Rewrite PROCESSOR attribute in:\n"
            f"  {', '.join(params['target_files'])}\n\n"
            f"{params['old_processor']!r} -> {params['new_processor']!r}\n\n"
            "Are you sure 'smallbaselineApp.py --dostep load_data' "
            "has already completed successfully?",
        ):
            return

        self._post_log(
            f"--- Applying fix: {params['old_processor']} -> "
            f"{params['new_processor']} ---"
        )
        self.post_apply_btn.configure(state="disabled")
        try:
            summary = fix_processor_attribute(
                inputs_dir=params["inputs_dir"],
                old=params["old_processor"],
                new=params["new_processor"],
                target_files=params["target_files"],
                dry_run=False,
                require_lookup_datasets=True,
            )
        except PostProcessError as exc:
            self._post_log(f"ERROR: {exc}")
            messagebox.showerror("Apply fix", str(exc))
            return
        finally:
            self.post_apply_btn.configure(state="normal")

        for record in summary["details"]:
            self._post_log(f"  - {record['message']}")
        self._post_log(
            f"Done: patched={summary['patched']}, "
            f"skipped={summary['skipped']}, errors={len(summary['errors'])}."
        )
        self.status_var.set(
            f"Post-load fix: patched={summary['patched']}, "
            f"skipped={summary['skipped']}."
        )

        if summary["patched"] > 0:
            messagebox.showinfo(
                "Apply fix",
                f"Patched {summary['patched']} file(s).\n\n"
                "Next step:\n"
                "    smallbaselineApp.py mintpy_config.txt",
            )


# ========================================================================
# Entry point
# ========================================================================
def run_gui() -> None:
    """Launch the openEO2Mintpy Tkinter GUI."""
    try:
        app = OpenEO2MintpyApp()
    except tk.TclError as exc:
        raise SystemExit(
            "Could not initialize the GUI. Is a display available and is "
            "python3-tk installed?\n"
            f"Original error: {exc}"
        ) from exc
    app.mainloop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    run_gui()
