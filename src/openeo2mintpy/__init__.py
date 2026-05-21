"""
openeo2mintpy — Bridge between openEO InSAR outputs and MintPy time-series analysis.

openEO produces 3-band GeoTIFF outputs, which this package splits, processes,
and translates into MintPy-compatible inputs by generating ROI_PAC-style
.rsc metadata files and configuration templates.
"""

__version__ = "0.1.0"
__author__ = "Burak Can Kara"

from openeo2mintpy.postprocess import (
    PostProcessError,
    fix_processor_attribute,
    verify_inputs_dir,
)
from openeo2mintpy.prepare import prepare_rsc, prepare_stack
from openeo2mintpy.align import align_rasters, prepare_dem

__all__ = [
    "__version__",
    "prepare_rsc",
    "prepare_stack",
    "align_rasters",
    "prepare_dem",
    "fix_processor_attribute",
    "verify_inputs_dir",
    "PostProcessError",
]

