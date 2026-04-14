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
  Photos are saved as auto0.bmp, auto1.bmp, auto2.bmp, ... in a dated
  subfolder under the project's "photos" directory.  The subfolder is named
  like "2-24-2026-auto".  If that folder already exists (from an earlier run
  on the same day), it becomes "2-24-2026-auto2", then "2-24-2026-auto3", etc.

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
START_DISTANCE = 131.0        # mm  --  starting position (scan begins here)
END_DISTANCE   = 133.0      # mm  --  ending position   (scan stops here)

# How far the stage moves between each photo (in mm).
# Smaller = more photos, higher resolution scan, longer total time.
STEP_SIZE = 0.05             # mm  --  distance between consecutive photos

# -- Timing ----------------------------------------------------------------
# How long to pause (in seconds) after the stage finishes moving to the
# next position.  This lets mechanical vibrations die down so the photo
# is sharp.  1 second is safe for the NRT150/M at low speed.
WAIT_AFTER_MOVE = 1.0       # seconds  --  settle time after each move

# How long to pause (in seconds) after saving each photo, to ensure the
# file write completes before the next move begins.
WAIT_AFTER_PHOTO = 0.1      # seconds  --  buffer after each photo save

# -- Stage motor -----------------------------------------------------------
# Travel speed and acceleration of the stage motor.
# Keep these low for precise positioning.  NRT150/M max is 30 mm/s.
VELOCITY     = 2.0          # mm/s   --  stage travel speed
ACCELERATION = 2.0          # mm/s²  --  stage acceleration

# -- Camera settings -------------------------------------------------------
# These are applied before the first photo is taken, overriding whatever
# the camera is currently set to.
EXPOSURE = 83000            # microseconds  (80 ms = 80000 us)
GAIN     = 16.8             # dB

# -- Stage connection ------------------------------------------------------
# Serial number of the BSC203 controller (printed on the back of the unit,
# also shown in Kinesis software).  Starts with "70".
SERIAL_NO = "70874683"      # BSC203 serial number

# Which channel on the BSC203 the NRT150/M is plugged into (1, 2, or 3).
CHANNEL = 1
