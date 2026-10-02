"""
Sentinel-1 IW-mode constants and ROI_PAC sidecar templates.

Only mission constants live here. Scene-dependent values (orbit
direction, heading, incidence, slant range, satellite height, centre-line
time, baselines) are computed from the precise orbits by
:mod:`openeo2mintpy.geometry` and never replaced by nominal defaults.
"""

# Physical constants
SPEED_OF_LIGHT = 299_792_458.0  # m/s

# Sentinel-1 C-band SAR parameters
S1_CARRIER_FREQUENCY = 5.405e9  # Hz
S1_WAVELENGTH = SPEED_OF_LIGHT / S1_CARRIER_FREQUENCY  # ~0.05546576 m

# Sentinel-1 IW mode pixel dimensions (single-look)
S1_RANGE_PIXEL_SIZE = 2.329562    # m (slant range)
S1_AZIMUTH_PIXEL_SIZE = 13.932898  # m (along track)

DEFAULT_ANTENNA_SIDE = -1  # right-looking

# MintPy processor label used for GDAL-based reading path
MINTPY_PROCESSOR = "hyp3"

# Supported geometry modes for .rsc generation.
#   - "auto"  : detect from GeoTIFF projection + geotransform
#   - "radar" : force radar geometry (no X_FIRST / Y_FIRST lines)
#   - "geo"   : force geocoded output (always emit geotransform)
GEOMETRY_MODES = ("auto", "radar", "geo")
DEFAULT_GEOMETRY_MODE = "auto"

# Raster description written for every file.
RSC_RASTER_BLOCK = """\
WIDTH                 {width}
LENGTH                {length}
FILE_LENGTH           {length}
XMIN                  0
XMAX                  {xmax}
YMIN                  0
YMAX                  {ymax}
PROCESSOR             {processor}
INSAR_PROCESSOR       {processor}
NUMBER_BANDS          {number_bands}
FILE_TYPE             {file_type}
DATA_TYPE             {data_type}
"""

# Acquisition geometry, written only when orbit-derived metadata is given.
RSC_RADAR_BLOCK = """\
WAVELENGTH            {WAVELENGTH}
RANGE_PIXEL_SIZE      {RANGE_PIXEL_SIZE}
AZIMUTH_PIXEL_SIZE    {AZIMUTH_PIXEL_SIZE}
STARTING_RANGE        {STARTING_RANGE}
SLANT_RANGE_DISTANCE  {SLANT_RANGE_DISTANCE}
INCIDENCE_ANGLE       {INCIDENCE_ANGLE}
PRF                   {PRF}
EARTH_RADIUS          {EARTH_RADIUS}
HEIGHT                {HEIGHT}
PLATFORM              Sen
ORBIT_DIRECTION       {ORBIT_DIRECTION}
HEADING               {HEADING}
CENTER_LINE_UTC       {CENTER_LINE_UTC}
ANTENNA_SIDE          {ANTENNA_SIDE}
ALOOKS                {ALOOKS}
RLOOKS                {RLOOKS}
"""

RADAR_KEYS = (
    "WAVELENGTH", "RANGE_PIXEL_SIZE", "AZIMUTH_PIXEL_SIZE", "STARTING_RANGE",
    "SLANT_RANGE_DISTANCE", "INCIDENCE_ANGLE", "PRF", "EARTH_RADIUS", "HEIGHT",
    "ORBIT_DIRECTION", "HEADING", "CENTER_LINE_UTC", "ALOOKS", "RLOOKS",
)

# Geotransform block appended only in geocoded mode. The presence of
# X_FIRST / Y_FIRST is what MintPy uses to flag a product as "geocoded".
RSC_GEO_BLOCK = """\
X_FIRST               {x_first}
Y_FIRST               {y_first}
X_STEP                {x_step}
Y_STEP                {y_step}
X_UNIT                {x_unit}
Y_UNIT                {y_unit}
"""

# Additional lines appended for interferogram files (unw, cor, conncomp).
# MintPy takes the mean of the top and bottom values as the pair baseline.
RSC_IFG_EXTRA = """\
DATE12                {date12}
P_BASELINE_TOP_HDR    {bperp_top}
P_BASELINE_BOTTOM_HDR {bperp_bottom}
"""
