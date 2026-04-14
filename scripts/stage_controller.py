"""
================================================================================
  stage_controller.py  --  Thorlabs BSC203 + NRT150/M Stage Controller
================================================================================

Wraps the Thorlabs Kinesis .NET API (via pythonnet) to provide a clean Python
interface for controlling a BSC203 benchtop stepper motor controller with
NRT150/M linear translation stages (150 mm travel, stepper motor).

The Kinesis .NET DLLs must be installed at:
    C:\\Program Files\\Thorlabs\\Kinesis\\

Requirements:
    pip install pythonnet

Reference:
    https://github.com/Thorlabs/Motion_Control_Examples

================================================================================
"""

import os
import sys
import time
import threading
import traceback

# ---------------------------------------------------------------------------
#  Kinesis .NET via pythonnet
# ---------------------------------------------------------------------------

KINESIS_PATH = r"C:\Program Files\Thorlabs\Kinesis"

_kinesis_loaded = False
_load_error: str | None = None

# .NET types (populated by _load_kinesis)
_DeviceManagerCLI = None
_BenchtopStepperMotor = None
_Decimal = None


def _load_kinesis():
    """Load Kinesis .NET assemblies.  Safe to call multiple times."""
    global _kinesis_loaded, _load_error
    global _DeviceManagerCLI, _BenchtopStepperMotor, _Decimal

    if _kinesis_loaded:
        return True
    if _load_error:
        return False

    try:
        import clr                                       # pythonnet

        clr.AddReference(os.path.join(
            KINESIS_PATH, "Thorlabs.MotionControl.DeviceManagerCLI.dll"))
        clr.AddReference(os.path.join(
            KINESIS_PATH, "Thorlabs.MotionControl.GenericMotorCLI.dll"))
        clr.AddReference(os.path.join(
            KINESIS_PATH, "Thorlabs.MotionControl.Benchtop.StepperMotorCLI.dll"))

        from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI
        from Thorlabs.MotionControl.Benchtop.StepperMotorCLI import BenchtopStepperMotor
        from System import Decimal

        _DeviceManagerCLI = DeviceManagerCLI
        _BenchtopStepperMotor = BenchtopStepperMotor
        _Decimal = Decimal

        _kinesis_loaded = True
        print("[stage] Kinesis .NET assemblies loaded.")
        return True

    except Exception as exc:
        _load_error = f"Failed to load Kinesis: {exc}\n{traceback.format_exc()}"
        print(f"[stage] {_load_error}")
        return False


# ==========================================================================
#  StageController  --  manages one BSC203 device with up to 3 channels
# ==========================================================================


class StageChannel:
    """Represents a single motor channel on the BSC203."""

    def __init__(self, channel_obj, channel_num: int):
        self._ch = channel_obj
        self.channel_num = channel_num
        self._polling = False
        self._enabled = False
        self._homed = False

    # -- lifecycle --------------------------------------------------------

    def initialise(self, stage_name: str = "NRT150/M"):
        """Wait for settings, start polling, enable, and load motor config."""
        if not self._ch.IsSettingsInitialized():
            self._ch.WaitForSettingsInitialized(10000)

        self._ch.StartPolling(250)
        self._polling = True
        time.sleep(0.25)

        self._ch.EnableDevice()
        self._enabled = True
        time.sleep(0.25)

        channel_config = self._ch.LoadMotorConfiguration(self._ch.DeviceID)
        chan_settings = self._ch.MotorDeviceSettings
        self._ch.GetSettings(chan_settings)
        channel_config.DeviceSettingsName = stage_name
        channel_config.UpdateCurrentConfiguration()
        self._ch.SetSettings(chan_settings, True, False)

        print(f"[stage] Channel {self.channel_num} initialised "
              f"(stage={stage_name}).")

    def shutdown(self):
        if self._polling:
            try:
                self._ch.StopPolling()
            except Exception:
                pass
            self._polling = False

    # -- info -------------------------------------------------------------

    def get_info(self) -> dict:
        try:
            di = self._ch.GetDeviceInfo()
            return {
                "description": str(di.Description),
                "serial": str(di.SerialNumber),
                "channel": self.channel_num,
            }
        except Exception:
            return {"channel": self.channel_num}

    # -- position ---------------------------------------------------------

    def get_position(self) -> float:
        return float(str(self._ch.DevicePosition))

    # -- homing -----------------------------------------------------------

    def home(self, timeout_ms: int = 300000):
        """Home the stage (blocking).  Default timeout 5 min."""
        print(f"[stage] Channel {self.channel_num} homing...")
        self._ch.Home(timeout_ms)
        self._homed = True
        print(f"[stage] Channel {self.channel_num} homed.")

    @property
    def is_homed(self) -> bool:
        try:
            status = self._ch.Status
            return bool(status.IsHomed)
        except Exception:
            return self._homed

    # -- motion -----------------------------------------------------------

    def move_to(self, position_mm: float, timeout_ms: int = 300000):
        """Move to an absolute position in mm (blocking).  Default timeout 5 min."""
        pos = _Decimal(float(position_mm))
        self._ch.MoveTo(pos, timeout_ms)

    def move_relative(self, distance_mm: float, timeout_ms: int = 300000):
        """Move a relative distance in mm (blocking).  Default timeout 5 min."""
        current = self.get_position()
        target = current + distance_mm
        target = max(0.0, min(150.0, target))
        self.move_to(target, timeout_ms)

    def jog_forward(self, timeout_ms: int = 60000):
        """Jog forward by the device's configured jog step."""
        from Thorlabs.MotionControl.GenericMotorCLI import \
            MotorDirection
        self._ch.MoveJog(MotorDirection.Forward, timeout_ms)

    def jog_backward(self, timeout_ms: int = 60000):
        """Jog backward by the device's configured jog step."""
        from Thorlabs.MotionControl.GenericMotorCLI import \
            MotorDirection
        self._ch.MoveJog(MotorDirection.Backward, timeout_ms)

    def stop(self):
        """Immediately stop motion."""
        self._ch.Stop(0)

    # -- velocity / jog parameters ----------------------------------------

    def get_velocity_params(self) -> dict:
        """Return current velocity parameters (max velocity, acceleration)."""
        try:
            vp = self._ch.GetVelocityParams()
            return {
                "max_velocity": float(str(vp.MaxVelocity)),
                "acceleration": float(str(vp.Acceleration)),
            }
        except Exception as exc:
            return {"error": str(exc)}

    def set_velocity_params(self, max_velocity: float, acceleration: float):
        """Set velocity and acceleration (mm/s, mm/s^2)."""
        self._ch.SetVelocityParams(
            _Decimal(float(max_velocity)),
            _Decimal(float(acceleration)),
        )

    def get_jog_params(self) -> dict:
        """Return current jog parameters."""
        try:
            jp = self._ch.GetJogParams()
            return {
                "step_size": float(str(jp.StepSize)),
                "max_velocity": float(str(jp.MaxVelocity)),
                "acceleration": float(str(jp.Acceleration)),
            }
        except Exception as exc:
            return {"error": str(exc)}

    def set_jog_step_size(self, step_mm: float):
        """Set jog step size in mm."""
        jp = self._ch.GetJogParams()
        jp.StepSize = _Decimal(float(step_mm))
        self._ch.SetJogParams(jp)

    # -- status -----------------------------------------------------------

    def is_moving(self) -> bool:
        try:
            status = self._ch.Status
            return bool(status.IsInMotion)
        except Exception:
            return False

    def get_status(self) -> dict:
        try:
            status = self._ch.Status
            return {
                "position": self.get_position(),
                "is_moving": bool(status.IsInMotion),
                "is_homed": bool(status.IsHomed),
                "is_enabled": bool(status.IsEnabled),
                "channel": self.channel_num,
            }
        except Exception as exc:
            return {"error": str(exc), "channel": self.channel_num}


class StageController:
    """
    Manages a single BSC203 controller (up to 3 channels).

    Usage:
        ctrl = StageController("70xxxxxx")
        ctrl.connect()
        ctrl.init_channel(1, "NRT150/M")
        ctrl.channel(1).home()
        ctrl.channel(1).move_to(75.0)
        ctrl.disconnect()
    """

    def __init__(self, serial_no: str):
        self.serial_no = serial_no
        self._device = None
        self._channels: dict[int, StageChannel] = {}
        self._connected = False

    # -- connection -------------------------------------------------------

    def connect(self):
        if not _load_kinesis():
            raise RuntimeError(_load_error or "Cannot load Kinesis DLLs")

        _DeviceManagerCLI.BuildDeviceList()
        self._device = _BenchtopStepperMotor.CreateBenchtopStepperMotor(
            self.serial_no)
        self._device.Connect(self.serial_no)
        time.sleep(0.25)
        self._connected = True
        print(f"[stage] Connected to BSC203 serial={self.serial_no}")

    def disconnect(self):
        for ch in self._channels.values():
            ch.shutdown()
        self._channels.clear()
        if self._device is not None:
            try:
                self._device.Disconnect()
            except Exception:
                pass
            self._device = None
        self._connected = False
        print("[stage] Disconnected.")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # -- channel management -----------------------------------------------

    def init_channel(self, channel_num: int, stage_name: str = "NRT150/M"):
        if not self._connected:
            raise RuntimeError("Not connected")
        ch_obj = self._device.GetChannel(channel_num)
        sc = StageChannel(ch_obj, channel_num)
        sc.initialise(stage_name)
        self._channels[channel_num] = sc
        return sc

    def channel(self, channel_num: int) -> StageChannel:
        if channel_num not in self._channels:
            raise KeyError(f"Channel {channel_num} not initialised")
        return self._channels[channel_num]

    def active_channels(self) -> list[int]:
        return list(self._channels.keys())

    # -- enumerate devices ------------------------------------------------

    @staticmethod
    def list_devices() -> list[str]:
        """Return serial numbers of all connected BSC203 controllers."""
        if not _load_kinesis():
            return []
        _DeviceManagerCLI.BuildDeviceList()
        device_list = _DeviceManagerCLI.GetDeviceList()
        serials = []
        for i in range(device_list.Count):
            s = str(device_list[i])
            if s.startswith("70"):
                serials.append(s)
        return serials
