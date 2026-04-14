"""
================================================================================
  webrtc_camera.py  --  IC4 + WebRTC H.264 Live Camera Backend
================================================================================

Overview
--------
This is the Python backend for the Coherence Auto live camera viewer.
It uses The Imaging Source IC4 SDK (``imagingcontrol4`` on PyPI) together
with the installed GenTL Producer for USB3 Vision cameras to capture frames
from a DMK 37BUX252 (or any TIS USB3 Vision camera).

Frames are streamed to the Electron frontend over WebRTC (H.264 encoded by
aiortc).  A second WebSocket channel provides two-way communication so the
frontend can:
  - query / set GenICam camera properties  (exposure, gain, FPS ...)
  - request a full-resolution frame save to disk  (PNG)

Architecture
------------
  Electron renderer  <--  WebRTC H.264 video  <--  aiortc VideoStreamTrack
  Electron renderer  <->  WebSocket commands   <->  aiohttp WS handler
                                                       |
                                                 IC4 QueueSink callback
                                                       |
                                                 GenTL Producer (.cti)
                                                       |
                                                 DMK 37BUX252 (USB3)

Usage
-----
  python webrtc_camera.py          # starts server on http://127.0.0.1:5000
  The Electron app launches this automatically.

Dependencies
------------
  pip install imagingcontrol4 aiortc aiohttp av numpy opencv-python

================================================================================
"""

import asyncio
import datetime
import json
import os
import queue
import sys
import threading
import time
import traceback

import numpy as np

# ---------------------------------------------------------------------------
#  Stage controller (Thorlabs BSC203 + NRT150/M via Kinesis .NET)
# ---------------------------------------------------------------------------
try:
    from stage_controller import StageController
    _stage_available = True
except ImportError:
    _stage_available = False

# ---------------------------------------------------------------------------
#  IC4  --  The Imaging Source camera SDK (PyPI: imagingcontrol4)
# ---------------------------------------------------------------------------
try:
    import imagingcontrol4 as ic4
except ImportError:
    sys.exit(
        "ERROR: imagingcontrol4 package not found.\n"
        "Install it with:  pip install imagingcontrol4\n"
        "Also ensure the IC4 runtime and GenTL Producer for USB3 are installed."
    )

# ---------------------------------------------------------------------------
#  aiortc / aiohttp  --  WebRTC + HTTP / WebSocket server
# ---------------------------------------------------------------------------
try:
    from aiohttp import web
    from av import VideoFrame
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc import MediaStreamError, VideoStreamTrack
except ImportError:
    sys.exit(
        "ERROR: aiortc / aiohttp not found.\n"
        "Install with:  pip install aiortc aiohttp av"
    )


# ==========================================================================
#  Configuration
# ==========================================================================

PORT = 5000                          # HTTP / WebSocket / WebRTC signaling port
PHOTOS_DIR = os.path.normpath(       # default folder for saved frames
    os.path.join(os.path.dirname(__file__), "..", "photos")
)


# ==========================================================================
#  Global state
# ==========================================================================

_grabber: ic4.Grabber | None = None           # IC4 grabber (camera handle)
_latest_buffer_lock = threading.Lock()         # protects _latest_buffer
_latest_buffer: np.ndarray | None = None       # most recent full-res frame (numpy)
_latest_ic4_buf: ic4.ImageBuffer | None = None # most recent IC4 buffer (for PNG save)
_camera_info: dict = {}                        # model, serial, etc.
_camera_error: str | None = None               # set if camera fails to open

# Stage controller (Thorlabs BSC203)
_stage: StageController | None = None if _stage_available else None
_stage_error: str | None = None


# ==========================================================================
#  CameraTrack  --  feeds frames into WebRTC via aiortc
# ==========================================================================


class CameraTrack(VideoStreamTrack):
    """
    Custom aiortc VideoStreamTrack.

    IC4's QueueSink callback pushes numpy frames into a thread-safe queue.
    ``recv()`` pulls the next frame, wraps it as an ``av.VideoFrame``, and
    hands it to aiortc which encodes it as H.264 and sends it over WebRTC.
    """

    kind = "video"

    def __init__(self):
        super().__init__()
        # thread-safe queue: IC4 callback thread pushes, asyncio pulls
        self._queue: queue.Queue = queue.Queue(maxsize=4)
        self._stopped = False

    async def recv(self) -> VideoFrame:
        """Called by aiortc to get the next video frame for encoding."""
        if self._stopped:
            raise MediaStreamError

        pts, time_base = await self.next_timestamp()

        # Poll the thread-safe queue with async-friendly sleep
        for _ in range(500):                        # up to ~10 s timeout
            try:
                frame_np = self._queue.get_nowait()
                break
            except queue.Empty:
                await asyncio.sleep(0.02)
        else:
            raise MediaStreamError

        # Mono images need to be converted to 3-channel BGR for H.264
        # numpy_copy() returns (H, W, 1) for Mono8, or (H, W) sometimes
        if frame_np.ndim == 2:
            frame_np = np.stack([frame_np, frame_np, frame_np], axis=-1)
        elif frame_np.ndim == 3 and frame_np.shape[2] == 1:
            frame_np = np.concatenate([frame_np, frame_np, frame_np], axis=-1)

        video_frame = VideoFrame.from_ndarray(frame_np, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame

    def push(self, frame_np: np.ndarray):
        """
        Push a numpy frame into the queue.
        Called from the IC4 callback thread.  Drops the oldest frame if full.
        """
        try:
            self._queue.put_nowait(frame_np)
        except queue.Full:
            # Drop oldest to keep latency low
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(frame_np)
            except queue.Full:
                pass

    def stop(self):
        """Signal the track to stop delivering frames."""
        self._stopped = True
        super().stop()


# Singleton track (shared across WebRTC peer connections)
_track: CameraTrack | None = None


# ==========================================================================
#  IC4 QueueSink Listener
# ==========================================================================


class FrameListener(ic4.QueueSinkListener):
    """
    Receives frame-available notifications from IC4's QueueSink.

    ``frames_queued`` is called on a dedicated IC4 internal thread every time
    a new frame buffer is ready.  We copy the data to numpy, keep a reference
    for full-res saves, and push into the WebRTC CameraTrack queue.
    """

    def sink_connected(self, sink: ic4.QueueSink, image_type: ic4.ImageType,
                        min_buffers_required: int) -> bool:
        """Called when the sink is connected to the stream.  Return True to accept."""
        print(f"[ic4] Sink connected  pixel_format={image_type.pixel_format}  "
              f"{image_type.width}x{image_type.height}")
        # Pre-allocate buffers (at least min_buffers_required, we use 4)
        num = max(min_buffers_required, 4)
        sink.alloc_and_queue_buffers(num)
        return True

    def sink_disconnected(self, sink: ic4.QueueSink):
        """Called when the stream stops."""
        print("[ic4] Sink disconnected.")

    def frames_queued(self, sink: ic4.QueueSink):
        """
        Called when new image(s) are available.

        Pop the latest buffer, copy to numpy, push to WebRTC track,
        and keep a reference for the save-to-disk feature.
        """
        global _latest_buffer, _latest_ic4_buf

        try:
            buffer = sink.pop_output_buffer()
        except Exception as exc:
            print(f"[ic4] pop error: {exc}")
            return

        try:
            frame_np = buffer.numpy_copy()

            # Store for save-to-disk
            with _latest_buffer_lock:
                _latest_buffer = frame_np
                _latest_ic4_buf = buffer       # keep ref for .save_as_png()

            # Push into WebRTC track
            if _track is not None:
                _track.push(frame_np)

        except Exception as exc:
            print(f"[ic4] frame callback error: {exc}")


# ==========================================================================
#  Camera initialisation
# ==========================================================================


def _init_camera() -> bool:
    """
    Initialise IC4, find the first USB3 Vision camera, open it, configure
    it for best FPS, and start streaming via a QueueSink.

    Returns True on success; on failure sets ``_camera_error`` and returns False.
    """
    global _grabber, _track, _camera_info, _camera_error

    try:
        # -- library init -------------------------------------------------
        ic4.Library.init()
        print("[ic4] Library initialised.")

        # -- enumerate devices --------------------------------------------
        #    DeviceEnum.devices() returns a list of DeviceInfo objects
        enum = ic4.DeviceEnum()
        devices = enum.devices()

        if not devices:
            _camera_error = (
                "No cameras found.  Make sure the DMK 37BUX252 is connected "
                "and the GenTL Producer for USB3 Vision is installed."
            )
            print(f"[camera] {_camera_error}")
            return False

        dev = devices[0]
        _camera_info = {
            "model": dev.model_name,
            "serial": dev.serial,
        }
        print(f"[camera] Found: {dev.model_name}  serial={dev.serial}")

        # -- open camera --------------------------------------------------
        _grabber = ic4.Grabber()
        _grabber.device_open(dev)
        print("[camera] Device opened.")

        # -- configure for best FPS (Mono8 = smallest frames) ------------
        props = _grabber.device_property_map

        # Try setting Mono8 pixel format for max throughput
        props.try_set_value("PixelFormat", "Mono8")
        pf = props.get_value_str("PixelFormat")
        print(f"[camera] Pixel format: {pf}")

        # -- full sensor resolution (2048 x 1536 for DMK 37BUX252) ----------
        #    Set ROI to cover the entire sensor so we see the whole frame.
        #    OffsetX/Y must be set to 0 first, then Width/Height to max.
        props.try_set_value("OffsetX", 0)
        props.try_set_value("OffsetY", 0)
        props.try_set_value("Width", 2048)
        props.try_set_value("Height", 1536)
        try:
            w = props.get_value_int("Width")
            h = props.get_value_int("Height")
            print(f"[camera] Resolution: {w} x {h}")
        except Exception:
            pass

        # Set a reasonable default exposure so the image isn't black
        # (DMK 37BUX252 defaults to ~9ms which may be too low for indoor use)
        props.try_set_value("ExposureAuto", "Off")
        props.try_set_value("ExposureTime", 33000.0)    # 33 ms (~30 FPS)
        props.try_set_value("Gain", 10.0)                # some gain for low light
        try:
            exp = props.get_value_float("ExposureTime")
            gain = props.get_value_float("Gain")
            print(f"[camera] Exposure: {exp:.0f} us   Gain: {gain:.1f} dB")
        except Exception:
            pass

        # Print current frame rate
        try:
            fps = props.get_value_float("AcquisitionFrameRate")
            print(f"[camera] Acquisition frame rate: {fps:.1f} FPS")
        except Exception:
            pass

        # -- create WebRTC track ------------------------------------------
        _track = CameraTrack()

        # -- create QueueSink with our listener and start streaming -------
        listener = FrameListener()
        sink = ic4.QueueSink(listener)
        _grabber.stream_setup(sink)
        print("[camera] Streaming started.")

        return True

    except Exception as exc:
        _camera_error = f"Camera init failed: {exc}\n{traceback.format_exc()}"
        print(f"[camera] {_camera_error}")
        return False


def _stop_camera():
    """Cleanly stop streaming and close the camera."""
    global _grabber, _track
    if _grabber is not None:
        try:
            _grabber.stream_stop()
        except Exception:
            pass
        try:
            _grabber.device_close()
        except Exception:
            pass
        _grabber = None
        print("[camera] Camera closed.")
    if _track is not None:
        _track.stop()
        _track = None


# ==========================================================================
#  WebRTC signaling  (WebSocket: /ws)
# ==========================================================================


async def ws_signaling(request):
    """
    WebSocket endpoint for WebRTC signaling.

    Protocol:
      1. Client sends   { "type": "offer", "sdp": "..." }
      2. Server replies  { "type": "answer", "sdp": "..." }
      3. Connection stays open (keeps the peer connection alive).
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    pc = None

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)

            if data.get("type") == "offer":
                pc = RTCPeerConnection()

                if _track is None:
                    await ws.send_json({
                        "type": "error",
                        "message": _camera_error or "No camera track available.",
                    })
                    continue

                pc.addTrack(_track)

                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=data["sdp"], type="offer")
                )
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                await ws.send_json({
                    "type": pc.localDescription.type,
                    "sdp": pc.localDescription.sdp,
                })
                print("[signaling] WebRTC answer sent.")

    except Exception as exc:
        print(f"[signaling] error: {exc}")
    finally:
        if pc:
            await pc.close()
        await ws.close()
    return ws


# ==========================================================================
#  Control channel  (WebSocket: /control)
# ==========================================================================


async def ws_control(request):
    """
    WebSocket endpoint for camera control commands.

    Supported commands (JSON):

      { "cmd": "save", "subfolder": "2-9-2026-test" }
        -> saves the latest full-resolution frame as BMP
        <- { "ok": true, "path": "C:\\\\...\\\\frame_20260209_153012.bmp" }

      { "cmd": "get_property", "name": "ExposureTime" }
        <- { "ok": true, "name": "ExposureTime", "value": 5000.0 }

      { "cmd": "set_property", "name": "ExposureTime", "value": 10000 }
        <- { "ok": true }

      { "cmd": "camera_info" }
        <- { "ok": true, "info": { "model": "...", "serial": "..." } }

      { "cmd": "list_properties" }
        <- { "ok": true, "properties": [...] }
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            cmd = data.get("cmd", "")

            if cmd == "save":
                await _handle_save(ws, data)
            elif cmd == "get_property":
                await _handle_get_property(ws, data)
            elif cmd == "set_property":
                await _handle_set_property(ws, data)
            elif cmd == "camera_info":
                await ws.send_json({"ok": True, "info": _camera_info})
            elif cmd == "list_properties":
                await _handle_list_properties(ws)
            else:
                await ws.send_json({"ok": False, "error": f"Unknown cmd: {cmd}"})

    except Exception as exc:
        print(f"[control] error: {exc}")
    finally:
        await ws.close()
    return ws


# --------------------------------------------------------------------------
#  Command handlers
# --------------------------------------------------------------------------


async def _handle_save(ws, data: dict):
    """
    Save the latest full-resolution frame to disk as PNG.

    Uses IC4's ImageBuffer.save_as_png() if the buffer is still valid,
    otherwise falls back to OpenCV imwrite from the numpy copy.
    """
    subfolder = data.get("subfolder", "")
    save_dir = os.path.join(PHOTOS_DIR, subfolder) if subfolder else PHOTOS_DIR
    os.makedirs(save_dir, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"frame_{ts}.bmp"
    filepath = os.path.join(save_dir, filename)

    def do_save():
        with _latest_buffer_lock:
            ic4_buf = _latest_ic4_buf
            np_buf = _latest_buffer.copy() if _latest_buffer is not None else None

        if ic4_buf is not None:
            try:
                ic4_buf.save_as_bmp(filepath)
                return True
            except Exception:
                pass

        # Fallback: save numpy array via OpenCV
        if np_buf is not None:
            import cv2
            cv2.imwrite(filepath, np_buf)
            return True

        return False

    ok = await asyncio.get_event_loop().run_in_executor(None, do_save)

    if ok:
        await ws.send_json({"ok": True, "path": filepath})
        print(f"[save] {filepath}")
    else:
        await ws.send_json({"ok": False, "error": "No frame available yet."})


async def _handle_get_property(ws, data: dict):
    """Read a GenICam property by name (tries float, then string)."""
    name = data.get("name", "")
    if not _grabber:
        await ws.send_json({"ok": False, "error": "Camera not open"})
        return
    props = _grabber.device_property_map
    try:
        val = props.get_value_float(name)
        await ws.send_json({"ok": True, "name": name, "value": val})
    except Exception:
        try:
            val = props.get_value_str(name)
            await ws.send_json({"ok": True, "name": name, "value": val})
        except Exception as exc:
            await ws.send_json({"ok": False, "error": str(exc)})


async def _handle_set_property(ws, data: dict):
    """Write a GenICam property by name."""
    name = data.get("name", "")
    value = data.get("value")
    if not _grabber:
        await ws.send_json({"ok": False, "error": "Camera not open"})
        return
    props = _grabber.device_property_map
    try:
        if isinstance(value, (int, float)):
            props.set_value(name, float(value))
        else:
            props.set_value(name, str(value))
        await ws.send_json({"ok": True})
    except Exception as exc:
        await ws.send_json({"ok": False, "error": str(exc)})


async def _handle_list_properties(ws):
    """Return a list of all available GenICam property names and types."""
    if not _grabber:
        await ws.send_json({"ok": False, "error": "Camera not open"})
        return
    try:
        props = _grabber.device_property_map
        result = []
        for p in props.all:
            result.append({"name": p.name, "type": str(p.type)})
        await ws.send_json({"ok": True, "properties": result})
    except Exception as exc:
        await ws.send_json({"ok": False, "error": str(exc)})


# ==========================================================================
#  Stage control channel  (WebSocket: /stage)
# ==========================================================================


async def ws_stage(request):
    """
    WebSocket endpoint for stage control commands.

    All commands are JSON objects with a "cmd" field.  Long-running operations
    (home, move_to, move_relative, jog) are executed in a thread pool so they
    don't block the event loop.

    Commands:
      { "cmd": "list_devices" }
      { "cmd": "connect", "serial": "70xxxxxx" }
      { "cmd": "disconnect" }
      { "cmd": "init_channel", "channel": 1, "stage": "NRT150/M" }
      { "cmd": "home", "channel": 1 }
      { "cmd": "move_to", "channel": 1, "position": 75.0 }
      { "cmd": "move_relative", "channel": 1, "distance": 1.0 }
      { "cmd": "jog_forward", "channel": 1 }
      { "cmd": "jog_backward", "channel": 1 }
      { "cmd": "stop", "channel": 1 }
      { "cmd": "get_position", "channel": 1 }
      { "cmd": "get_status", "channel": 1 }
      { "cmd": "get_velocity", "channel": 1 }
      { "cmd": "set_velocity", "channel": 1, "max_velocity": 10, "acceleration": 5 }
      { "cmd": "get_jog_params", "channel": 1 }
      { "cmd": "set_jog_step", "channel": 1, "step": 1.0 }
      { "cmd": "stage_info" }
    """
    global _stage, _stage_error

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    loop = asyncio.get_event_loop()

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            cmd = data.get("cmd", "")

            try:
                if cmd == "list_devices":
                    if not _stage_available:
                        await ws.send_json({"ok": False,
                            "error": "pythonnet/Kinesis not available"})
                        continue
                    serials = await loop.run_in_executor(
                        None, StageController.list_devices)
                    await ws.send_json({"ok": True, "devices": serials})

                elif cmd == "connect":
                    serial = data.get("serial", "")
                    if not serial:
                        await ws.send_json({"ok": False,
                            "error": "serial number required"})
                        continue
                    if _stage and _stage.is_connected:
                        _stage.disconnect()
                    _stage = StageController(serial)
                    await loop.run_in_executor(None, _stage.connect)
                    _stage_error = None
                    await ws.send_json({"ok": True})

                elif cmd == "disconnect":
                    if _stage:
                        await loop.run_in_executor(None, _stage.disconnect)
                        _stage = None
                    await ws.send_json({"ok": True})

                elif cmd == "init_channel":
                    ch_num = int(data.get("channel", 1))
                    stage_name = data.get("stage", "NRT150/M")
                    if not _stage or not _stage.is_connected:
                        await ws.send_json({"ok": False,
                            "error": "Not connected"})
                        continue
                    await loop.run_in_executor(
                        None, _stage.init_channel, ch_num, stage_name)
                    await ws.send_json({"ok": True, "channel": ch_num})

                elif cmd == "home":
                    ch_num = int(data.get("channel", 1))
                    ch = _stage.channel(ch_num)
                    await loop.run_in_executor(None, ch.home)
                    await ws.send_json({"ok": True,
                        "position": ch.get_position()})

                elif cmd == "move_to":
                    ch_num = int(data.get("channel", 1))
                    position = float(data.get("position", 0))
                    ch = _stage.channel(ch_num)
                    await loop.run_in_executor(
                        None, ch.move_to, position)
                    await ws.send_json({"ok": True,
                        "position": ch.get_position()})

                elif cmd == "move_relative":
                    ch_num = int(data.get("channel", 1))
                    distance = float(data.get("distance", 0))
                    ch = _stage.channel(ch_num)
                    await loop.run_in_executor(
                        None, ch.move_relative, distance)
                    await ws.send_json({"ok": True,
                        "position": ch.get_position()})

                elif cmd == "jog_forward":
                    ch_num = int(data.get("channel", 1))
                    ch = _stage.channel(ch_num)
                    await loop.run_in_executor(None, ch.jog_forward)
                    await ws.send_json({"ok": True,
                        "position": ch.get_position()})

                elif cmd == "jog_backward":
                    ch_num = int(data.get("channel", 1))
                    ch = _stage.channel(ch_num)
                    await loop.run_in_executor(None, ch.jog_backward)
                    await ws.send_json({"ok": True,
                        "position": ch.get_position()})

                elif cmd == "stop":
                    ch_num = int(data.get("channel", 1))
                    ch = _stage.channel(ch_num)
                    ch.stop()
                    await ws.send_json({"ok": True})

                elif cmd == "get_position":
                    ch_num = int(data.get("channel", 1))
                    ch = _stage.channel(ch_num)
                    await ws.send_json({"ok": True,
                        "position": ch.get_position()})

                elif cmd == "get_status":
                    ch_num = int(data.get("channel", 1))
                    ch = _stage.channel(ch_num)
                    await ws.send_json({"ok": True, **ch.get_status()})

                elif cmd == "get_velocity":
                    ch_num = int(data.get("channel", 1))
                    ch = _stage.channel(ch_num)
                    await ws.send_json({"ok": True,
                        **ch.get_velocity_params()})

                elif cmd == "set_velocity":
                    ch_num = int(data.get("channel", 1))
                    max_vel = float(data.get("max_velocity", 5))
                    accel = float(data.get("acceleration", 5))
                    ch = _stage.channel(ch_num)
                    ch.set_velocity_params(max_vel, accel)
                    await ws.send_json({"ok": True})

                elif cmd == "get_jog_params":
                    ch_num = int(data.get("channel", 1))
                    ch = _stage.channel(ch_num)
                    await ws.send_json({"ok": True,
                        **ch.get_jog_params()})

                elif cmd == "set_jog_step":
                    ch_num = int(data.get("channel", 1))
                    step = float(data.get("step", 1.0))
                    ch = _stage.channel(ch_num)
                    ch.set_jog_step_size(step)
                    await ws.send_json({"ok": True})

                elif cmd == "stage_info":
                    if not _stage or not _stage.is_connected:
                        await ws.send_json({"ok": True,
                            "connected": False,
                            "channels": []})
                        continue
                    channels = []
                    for ch_num in _stage.active_channels():
                        channels.append(
                            _stage.channel(ch_num).get_status())
                    await ws.send_json({"ok": True,
                        "connected": True,
                        "serial": _stage.serial_no,
                        "channels": channels})

                else:
                    await ws.send_json({"ok": False,
                        "error": f"Unknown stage cmd: {cmd}"})

            except Exception as exc:
                print(f"[stage ws] error handling '{cmd}': {exc}")
                await ws.send_json({"ok": False, "error": str(exc)})

    except Exception as exc:
        print(f"[stage ws] connection error: {exc}")
    finally:
        await ws.close()
    return ws


# ==========================================================================
#  Auto-scan runner  (WebSocket: /run_script)
# ==========================================================================

# Tracks the currently running scan so we can cancel it
_scan_thread: threading.Thread | None = None
_scan_cancel = threading.Event()
_scan_progress: dict = {"running": False}
_scan_stage_channel = None  # active StageChannel during scan (for emergency stop)
_scan_stage_channel_lock = threading.Lock()


def _build_subfolder() -> str:
    """
    Build the dated subfolder name for this scan run.

    First run today  -> "2-24-2026-auto"
    Second run today -> "2-24-2026-auto2"
    Third            -> "2-24-2026-auto3"  etc.
    """
    today = datetime.datetime.now()
    base = f"{today.month}-{today.day}-{today.year}-auto"
    candidate = base
    suffix = 2
    while os.path.exists(os.path.join(PHOTOS_DIR, candidate)):
        candidate = f"{base}{suffix}"
        suffix += 1
    return candidate


def _save_frame_to(filepath: str) -> bool:
    """Save the latest camera frame to the given filepath.  Returns True on success."""
    with _latest_buffer_lock:
        ic4_buf = _latest_ic4_buf
        np_buf = _latest_buffer.copy() if _latest_buffer is not None else None

    if ic4_buf is not None:
        try:
            ic4_buf.save_as_bmp(filepath)
            return True
        except Exception:
            pass

    if np_buf is not None:
        import cv2
        cv2.imwrite(filepath, np_buf)
        return True

    return False


def _run_scan(params: dict, progress: dict, cancel_event: threading.Event):
    """
    The core scan loop.  Runs in a background thread.

    Steps:
      1. Connect stage, set velocity/acceleration
      2. Set camera exposure and gain
      3. Home the stage
      4. Move to start position
      5. Loop: wait -> photo -> wait -> step  (until end position)
      6. Return home
    """
    from stage_controller import StageController

    start_dist   = params["start_distance"]
    end_dist     = params["end_distance"]
    step_size    = params["step_size"]
    wait_move    = params["wait_after_move"]
    wait_photo   = params["wait_after_photo"]
    velocity     = params["velocity"]
    acceleration = params["acceleration"]
    exposure     = params["exposure"]
    gain         = params["gain"]
    serial_no    = params["serial_no"]
    channel_num  = params["channel"]

    # Calculate the list of positions to visit
    positions = []
    if start_dist > end_dist:
        pos = start_dist
        while pos >= end_dist - 1e-9:
            positions.append(round(pos, 4))
            pos -= step_size
    else:
        pos = start_dist
        while pos <= end_dist + 1e-9:
            positions.append(round(pos, 4))
            pos += step_size

    total_steps = len(positions)
    progress["total"] = total_steps
    progress["current"] = 0
    progress["status"] = "[1/6] Connecting to stage..."
    progress["detail"] = ""
    progress["running"] = True
    progress["error"] = None
    progress["folder"] = ""

    global _scan_stage_channel

    ctrl = None
    owns_connection = False       # True if we created the connection (must clean up)
    try:
        # -- 1. Connect stage and set motor params -------------------------
        progress["detail"] = f"Serial: {serial_no}  Channel: {channel_num}"

        # Reuse the existing global stage connection if it matches,
        # otherwise create a fresh one.
        if (_stage is not None
                and _stage.is_connected
                and _stage.serial_no == serial_no
                and channel_num in _stage.active_channels()):
            ctrl = _stage
            ch = ctrl.channel(channel_num)
            print(f"[scan] Reusing existing stage connection "
                  f"(serial={serial_no}, ch={channel_num})")
        else:
            # Disconnect any existing global connection to free the device
            if _stage is not None and _stage.is_connected:
                print("[scan] Disconnecting existing stage connection...")
                _stage.disconnect()

            ctrl = StageController(serial_no)
            ctrl.connect()
            ch = ctrl.init_channel(channel_num, "NRT150/M")
            owns_connection = True
            print(f"[scan] Created new stage connection "
                  f"(serial={serial_no}, ch={channel_num})")

        with _scan_stage_channel_lock:
            _scan_stage_channel = ch

        progress["status"] = "[1/6] Setting velocity & acceleration..."
        progress["detail"] = f"Velocity: {velocity} mm/s  Accel: {acceleration} mm/s²"
        ch.set_velocity_params(velocity, acceleration)
        print(f"[scan] Velocity={velocity} mm/s  Acceleration={acceleration} mm/s²")

        if cancel_event.is_set():
            raise InterruptedError("Cancelled")

        # -- 2. Set camera exposure and gain -------------------------------
        progress["status"] = "[2/6] Setting camera exposure & gain..."
        progress["detail"] = f"Exposure: {exposure} us  Gain: {gain} dB"
        if _grabber:
            props = _grabber.device_property_map
            props.set_value("ExposureTime", float(exposure))
            props.set_value("Gain", float(gain))
            print(f"[scan] Camera: Exposure={exposure} us  Gain={gain} dB")
        time.sleep(0.5)

        if cancel_event.is_set():
            raise InterruptedError("Cancelled")

        # -- 3. Home the stage ---------------------------------------------
        progress["status"] = "[3/6] Homing stage (finding zero)..."
        progress["detail"] = "Stage is moving to home switch..."
        ch.home()
        print("[scan] Homing complete.")

        if cancel_event.is_set():
            raise InterruptedError("Cancelled")

        # -- 4. Move to start position (skip if start is 0, already home) --
        if start_dist > 0.001:
            progress["status"] = f"[4/6] Moving to start position ({start_dist} mm)..."
            progress["detail"] = (
                f"Traveling {start_dist} mm at {velocity} mm/s  "
                f"(~{start_dist / velocity:.0f}s)")
            ch.move_to(start_dist)
        else:
            progress["status"] = "[4/6] Already at start position (home = 0 mm)"
            progress["detail"] = ""

        actual_pos = ch.get_position()
        print(f"[scan] At start position: {actual_pos} mm")

        # Wait 1 second at start position before first photo
        progress["detail"] = "Waiting 1s before first photo..."
        _interruptible_sleep(1.0, cancel_event)

        if cancel_event.is_set():
            raise InterruptedError("Cancelled")

        # -- 5. Create output folder ---------------------------------------
        subfolder = _build_subfolder()
        save_dir = os.path.join(PHOTOS_DIR, subfolder)
        os.makedirs(save_dir, exist_ok=True)
        progress["folder"] = subfolder
        progress["status"] = "[5/6] Starting photo capture..."
        progress["detail"] = f"Saving to: {subfolder}/"
        print(f"[scan] Saving to: {save_dir}")

        # -- 6. Scan loop --------------------------------------------------
        for i, pos_mm in enumerate(positions):
            if cancel_event.is_set():
                raise InterruptedError("Cancelled")

            progress["current"] = i
            progress["position"] = pos_mm

            # Move to this position (skip for the first one, already there)
            if i > 0:
                progress["status"] = f"[5/6] Moving to {pos_mm} mm..."
                progress["detail"] = f"Step {i+1} of {total_steps}"
                ch.move_to(pos_mm)

            # Wait for vibrations to settle
            progress["status"] = f"[5/6] Waiting {wait_move}s for vibrations to settle..."
            progress["detail"] = f"Step {i+1} of {total_steps} | Position: {pos_mm} mm"
            _interruptible_sleep(wait_move, cancel_event)

            if cancel_event.is_set():
                raise InterruptedError("Cancelled")

            # Capture and save photo
            filename = f"auto{i}.bmp"
            filepath = os.path.join(save_dir, filename)
            progress["status"] = f"[5/6] Capturing {filename}..."
            progress["detail"] = f"Step {i+1} of {total_steps} | Position: {pos_mm} mm"
            ok = _save_frame_to(filepath)
            if ok:
                print(f"[scan] [{i+1}/{total_steps}] {filename}  @ {pos_mm} mm")
            else:
                print(f"[scan] [{i+1}/{total_steps}] FAILED to save {filename}")

            # Brief wait after photo
            _interruptible_sleep(wait_photo, cancel_event)

        # -- All photos captured -------------------------------------------
        progress["current"] = total_steps
        progress["status"] = "[6/6] Returning to home position..."
        progress["detail"] = "All photos captured, moving back to 0 mm"

        ch.move_to(0.0)

        progress["status"] = f"Complete! {total_steps} photos saved to {subfolder}/"
        progress["detail"] = ""
        print(f"[scan] Scan complete: {total_steps} photos in {subfolder}")

    except InterruptedError:
        progress["status"] = f"Stopped at step {progress['current']}/{total_steps}"
        print("[scan] Cancelled by user.")

    except Exception as exc:
        progress["error"] = str(exc)
        progress["status"] = f"Error: {exc}"
        print(f"[scan] Error: {exc}\n{traceback.format_exc()}")

    finally:
        with _scan_stage_channel_lock:
            _scan_stage_channel = None
        progress["running"] = False
        # Only disconnect if this scan created its own connection
        if owns_connection and ctrl:
            try:
                ctrl.disconnect()
            except Exception:
                pass


def _interruptible_sleep(seconds: float, cancel_event: threading.Event):
    """Sleep in small increments so we can check for cancellation."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancel_event.is_set():
            return
        time.sleep(min(0.1, end - time.monotonic()))


def _load_scan_params() -> dict:
    """
    Import (or re-import) auto_scan.py and return its parameters as a dict.

    Re-importing ensures the user can edit the file between runs and the
    new values are picked up immediately.
    """
    import importlib
    try:
        import auto_scan
        importlib.reload(auto_scan)
    except ImportError:
        import auto_scan

    return {
        "start_distance":  auto_scan.START_DISTANCE,
        "end_distance":    auto_scan.END_DISTANCE,
        "step_size":       auto_scan.STEP_SIZE,
        "wait_after_move": auto_scan.WAIT_AFTER_MOVE,
        "wait_after_photo": auto_scan.WAIT_AFTER_PHOTO,
        "velocity":        auto_scan.VELOCITY,
        "acceleration":    auto_scan.ACCELERATION,
        "exposure":        auto_scan.EXPOSURE,
        "gain":            auto_scan.GAIN,
        "serial_no":       auto_scan.SERIAL_NO,
        "channel":         auto_scan.CHANNEL,
    }


async def ws_run_script(request):
    """
    WebSocket endpoint for running the auto-scan script.

    Commands:
      { "cmd": "start" }       -- load params from auto_scan.py and begin
      { "cmd": "stop" }        -- cancel the running scan immediately
      { "cmd": "progress" }    -- get current scan progress
      { "cmd": "get_params" }  -- read current params from auto_scan.py
      { "cmd": "get_script_path" }  -- return the file path for "Edit Script"
    """
    global _scan_thread, _scan_cancel, _scan_progress

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    loop = asyncio.get_event_loop()

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            cmd = data.get("cmd", "")

            try:
                if cmd == "start":
                    # Don't start if already running
                    if _scan_progress.get("running"):
                        await ws.send_json({"ok": False,
                            "error": "Scan already running"})
                        continue

                    if not _stage_available:
                        await ws.send_json({"ok": False,
                            "error": "Stage controller not available "
                                     "(pythonnet/Kinesis not installed)"})
                        continue

                    # Load parameters fresh from the script file
                    params = await loop.run_in_executor(
                        None, _load_scan_params)

                    if params["serial_no"] == "70000000":
                        await ws.send_json({"ok": False,
                            "error": "Edit auto_scan.py and set your BSC203 "
                                     "serial number (SERIAL_NO)"})
                        continue

                    # Reset cancel flag and start the scan in a background thread
                    _scan_cancel.clear()
                    _scan_thread = threading.Thread(
                        target=_run_scan,
                        args=(params, _scan_progress, _scan_cancel),
                        daemon=True,
                    )
                    _scan_thread.start()
                    await ws.send_json({"ok": True, "params": params})

                elif cmd == "stop":
                    _scan_cancel.set()
                    with _scan_stage_channel_lock:
                        ch = _scan_stage_channel
                    if ch is not None:
                        try:
                            ch.stop()
                        except Exception:
                            pass
                    await ws.send_json({"ok": True})

                elif cmd == "progress":
                    await ws.send_json({"ok": True, **_scan_progress})

                elif cmd == "get_params":
                    params = await loop.run_in_executor(
                        None, _load_scan_params)
                    await ws.send_json({"ok": True, "params": params})

                elif cmd == "get_script_path":
                    script_path = os.path.normpath(os.path.join(
                        os.path.dirname(__file__), "auto_scan.py"))
                    await ws.send_json({"ok": True, "path": script_path})

                else:
                    await ws.send_json({"ok": False,
                        "error": f"Unknown cmd: {cmd}"})

            except Exception as exc:
                print(f"[run_script ws] error: {exc}")
                await ws.send_json({"ok": False, "error": str(exc)})

    except Exception as exc:
        print(f"[run_script ws] connection error: {exc}")
    finally:
        await ws.close()
    return ws


# ==========================================================================
#  HTTP endpoints
# ==========================================================================


async def camera_status(request):
    """
    GET /camera_status
    Returns JSON with camera availability and device info.
    """
    return web.json_response({
        "camera_ok": _grabber is not None,
        "error": _camera_error,
        "info": _camera_info,
    })


async def stage_status(request):
    """
    GET /stage_status
    Returns JSON with stage controller availability.
    """
    connected = _stage is not None and _stage.is_connected
    channels = []
    if connected:
        for ch_num in _stage.active_channels():
            channels.append(_stage.channel(ch_num).get_status())
    return web.json_response({
        "stage_available": _stage_available,
        "connected": connected,
        "serial": _stage.serial_no if connected else None,
        "channels": channels,
        "error": _stage_error,
    })


FALLBACK_HTML = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Coherence Auto</title>
<style>body{background:#111;color:#eee;font-family:system-ui;margin:0;padding:16px}
h1{font-size:1.3rem}p{color:#8af}</style></head>
<body><h1>Coherence Auto -- camera backend running</h1>
<p>Open the Electron app to see the live stream.</p>
<p>Camera status: <span id="s">...</span></p>
<script>
fetch('/camera_status').then(r=>r.json()).then(j=>{
  document.getElementById('s').textContent=j.camera_ok?
    'Connected ('+j.info.model+')':'Not connected -- '+(j.error||'unknown');
});
</script></body></html>"""


async def index(request):
    """Fallback page when opening http://127.0.0.1:5000 in a browser."""
    return web.Response(text=FALLBACK_HTML, content_type="text/html")


# ==========================================================================
#  Main  --  start camera, start HTTP/WS server
# ==========================================================================


def main():
    print("=" * 60)
    print("  Coherence Auto  --  IC4 + WebRTC H.264 camera backend")
    print("=" * 60)

    # Ensure photos directory exists
    os.makedirs(PHOTOS_DIR, exist_ok=True)

    # Initialise IC4 and start camera
    _init_camera()

    # Build aiohttp application
    app = web.Application()
    app.router.add_get("/", index)                    # fallback HTML page
    app.router.add_get("/ws", ws_signaling)           # WebRTC signaling
    app.router.add_get("/control", ws_control)        # camera commands
    app.router.add_get("/camera_status", camera_status)  # status JSON
    app.router.add_get("/stage", ws_stage)            # stage control WS
    app.router.add_get("/stage_status", stage_status) # stage status JSON
    app.router.add_get("/run_script", ws_run_script)  # auto-scan WS

    # Cleanup on shutdown
    async def on_shutdown(app_):
        _stop_camera()
        if _stage and _stage.is_connected:
            _stage.disconnect()
    app.on_shutdown.append(on_shutdown)

    print(f"  Server:  http://127.0.0.1:{PORT}/")
    print(f"  Photos:  {PHOTOS_DIR}")
    print("=" * 60)

    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
