"""   
================================================================================
  auto_scan.py  --  Automated Stage + Camera Scan Script
================================================================================

This script automates the process of scanning a Thorlabs NRT150/M linear
translation stage while capturing photos at each step with an IC4 camera.

HOW IT WORKS:
  1. Connects to the BSC203 stage controller and initialises the channel.
  2. Sets the stage velocity and acceleration.
  3. Sets the camera exposure and gain.
  4. Homes the stage (finds the zero reference).
  5. Moves to the start position.
  6. At each position:  waits for vibrations to settle, captures a photo,
     waits briefly, then moves to the next position.
  7. After the last photo, returns the stage to the home (0 mm) position.

PHOTO NAMING:
  Photos are saved as <prefix>0.bmp, <prefix>1.bmp, ... in a dated
  subfolder under the project's "results" directory.  The <prefix> is
  chosen in the UI before Start (movable / fixed / both).  The subfolder
  is named like "2-24-2026-auto".  If that folder already exists (from an
  earlier run on the same day), it becomes "2-24-2026-auto2", etc.

  A Markdown log called "scan_log.md" is written at the top of every
  scan folder with the date/time, parameters, laser description, a line
  per captured photo, and the final outcome (completed / cancelled /
  error).

EDITING PARAMETERS:
  All adjustable parameters are in the section below.  Change them directly
  in this file, save, and click "Start" in the app.  The script re-reads
  these values every time it runs.

================================================================================
"""


# ==========================================================================
#
#   SCAN PARAMETERS  --  Edit these values to customise the scan
#
# ==========================================================================

# -- Stage travel ----------------------------------------------------------
# Where the scan begins and ends (in millimetres).

# The NRT150/M has 150 mm of travel.  Min = 0, Max = 150.
# The stage will move from START_DISTANCE toward END_DISTANCE.
# Home position is 0 mm, so starting at 0 avoids a long travel-to-start move.
START_DISTANCE = 0.0# mm  --  starting position (scan begins here)
END_DISTANCE   = 150.0# mm  --  ending position   (scan stops here)

# How far the stage moves between each photo (in mm).
# Smaller = more photos, higher resolution scan, longer total time.
STEP_SIZE = 10.0# mm  --  distance between consecutive photos

# -- Timing ----------------------------------------------------------------
# How long to pause (in seconds) after the stage finishes moving to the
# next position.  This lets mechanical vibrations die down so the photo
# is sharp.  1 second is safe for the NRT150/M at low speed.
WAIT_AFTER_MOVE = 1.0# seconds  --  mechanical settle after each move

# Camera-related waits are derived from EXPOSURE so they automatically
# scale when you change exposure time.  The safety factor ensures the
# frame grabbed (and the one after saving) was exposed entirely AFTER
# the stage came to rest -- i.e. at least one full frame is flushed.
#
#   wait_capture_s = EXPOSURE * EXPOSURE_SAFETY_FACTOR / 1e6
#   wait_photo_s   = EXPOSURE * EXPOSURE_SAFETY_FACTOR / 1e6
#
# Example: EXPOSURE = 100000 us (100 ms), factor = 2.0  ->  200 ms each.
EXPOSURE_SAFETY_FACTOR = 2.0  # unitless  --  >=1; 2x is a safe default

# -- Stage motor -----------------------------------------------------------
# Travel speed and acceleration of the stage motor.
# Keep these low for precise positioning.  NRT150/M max is 30 mm/s.
VELOCITY     = 2.0# mm/s   --  stage travel speed initally 2
ACCELERATION = 2.0# mm/s²  --  stage acceleration

# -- Camera settings -------------------------------------------------------
# These are applied before the first photo is taken, overriding whatever
# the camera is currently set to.
EXPOSURE = 55000# microseconds  (75 ms)
GAIN     = 16.0# dB

# -- Stage connection ------------------------------------------------------
# Serial number of the BSC203 controller (printed on the back of the unit,
# also shown in Kinesis software).  Starts with "70".
SERIAL_NO = "70874683"# BSC203 serial number

# Which channel on the BSC203 the NRT150/M is plugged into (1, 2, or 3).
CHANNEL = 1

# -- Recording / log defaults ---------------------------------------------
# These are defaults shown in the UI "Recording" panel.  The user can
# change them before pressing Start; whatever is in the UI at Start time
# is what gets written into scan_log.md (and used as the photo filename
# prefix).  NAME_PREFIX_DEFAULT must be one of: "movable", "fixed", "both".
NAME_PREFIX_DEFAULT = "movable"
LASER_DEFAULT       = "Tanner"
LASER_PRESETS       = ["Tanner"]   # clickable chips next to the laser input
