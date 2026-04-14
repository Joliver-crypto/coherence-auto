================================================================================
  Coherence Auto  -  IC4 + WebRTC H.264 Live Camera Viewer
================================================================================

Overview
--------
Desktop application for live streaming from The Imaging Source DMK 37BUX252
USB3 Vision camera.  Uses IC4 SDK with the GenTL Producer for camera access,
aiortc for WebRTC H.264 encoding, and Electron for the desktop UI.

Folder layout
-------------
  coherenceauto\
    Coherence Auto.lnk       <- Double-click to launch (camera icon)
    README.txt                <- This file
    photos\                   <- Saved frames go here
    scripts\
      webrtc_camera.py        <- Python backend (IC4 + aiortc + aiohttp)
      requirements.txt        <- Python dependencies
      run_liveview.vbs        <- VBS launcher (called by the shortcut)
      create_shortcut.ps1     <- Re-creates the shortcut if needed
      files\                  <- Extra files
    electron\
      main.js                 <- Electron main process
      preload.js              <- Preload bridge
      index.html              <- Live viewer UI (controls, save button)
      package.json            <- Node/Electron dependencies

Prerequisites
-------------
  1. IC4 runtime installed (from The Imaging Source)
  2. GenTL Producer for USB3 Vision Cameras installed
  3. Python 3.10+ with pip
  4. Node.js 18+ with npm

First-time setup
----------------
  1. Install Python dependencies:
       cd scripts
       pip install -r requirements.txt

  2. Install Electron:
       cd electron
       npm install

  3. Connect the DMK 37BUX252 via USB3.

  4. Double-click "Coherence Auto" in the coherenceauto folder.

What happens when you launch
----------------------------
  1. The shortcut runs run_liveview.vbs
  2. VBS starts Electron (electron\main.js)
  3. Electron spawns the Python backend (scripts\webrtc_camera.py)
  4. Python opens the camera via IC4 / GenTL and starts streaming
  5. Electron connects via WebRTC to show the live H.264 feed
  6. A WebSocket control channel lets you adjust exposure/gain and save frames

Saving frames
-------------
  Click "Save Frame" in the app.  Full-resolution BMP files are saved to:
    coherenceauto\photos\<date>-capture\frame_YYYYMMDD_HHMMSS.bmp

Troubleshooting
---------------
  - "No cameras found" : check USB3 cable, ensure GenTL Producer is installed
  - Black screen       : camera may be in use by IC Capture; close it first
  - Port 5000 in use   : another instance is running; close it or change PORT
                          in webrtc_camera.py
