"""
openeo2mintpy — Bridge between CDSE openEO Sentinel-1 InSAR outputs and MintPy.

openEO produces 3-band GeoTIFF outputs, which this package splits and
aligns, completes with the acquisition geometry and perpendicular
baselines derived from the Sentinel-1 precise orbits, describes with
ROI_PAC-style .rsc metadata sidecars, and translates into MintPy-compatible
inputs. It also generates MintPy configuration templates and patches the
HDF5 PROCESSOR attribute after MintPy's load_data step.
"""

__version__ = "1.0.0"
__author__ = "Burak Can Kara"
__email__ = "burakcan.kara@amasya.edu.tr"
__license__ = "MIT"

from openeo2mintpy.align import align_rasters, prepare_dem
from openeo2mintpy.geometry import build_stack_metadata, load_stack_metadata
from openeo2mintpy.postprocess import (
    PostProcessError,
    fix_processor_attribute,
    verify_inputs_dir,
)
from openeo2mintpy.prepare import prepare_rsc, prepare_stack

__all__ = [
    "__version__",
    "prepare_rsc",
    "prepare_stack",
    "align_rasters",
    "prepare_dem",
    "build_stack_metadata",
    "load_stack_metadata",
    "fix_processor_attribute",
    "verify_inputs_dir",
    "PostProcessError",
]
