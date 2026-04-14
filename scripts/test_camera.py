"""Quick diagnostic: test if IC4 delivers non-black frames."""
import imagingcontrol4 as ic4
import numpy as np
import time

ic4.Library.init()
enum = ic4.DeviceEnum()
devs = enum.devices()
dev = devs[0]
print(f"Camera: {dev.model_name}  serial={dev.serial}")

g = ic4.Grabber()
g.device_open(dev)
props = g.device_property_map

# Print current exposure
try:
    exp = props.get_value_float("ExposureTime")
    print(f"ExposureTime: {exp} us")
except Exception as e:
    print(f"ExposureTime read error: {e}")

# Try setting a reasonable exposure (e.g. 33ms = ~30fps)
try:
    props.try_set_value("ExposureAuto", "Off")
    props.set_value("ExposureTime", 33000.0)
    exp = props.get_value_float("ExposureTime")
    print(f"ExposureTime set to: {exp} us")
except Exception as e:
    print(f"ExposureTime set error: {e}")

# Try gain
try:
    gain = props.get_value_float("Gain")
    print(f"Gain: {gain}")
except Exception as e:
    print(f"Gain read error: {e}")

pf = props.get_value_str("PixelFormat")
print(f"PixelFormat: {pf}")

class Listener(ic4.QueueSinkListener):
    def __init__(self):
        self.count = 0
    def sink_connected(self, sink, image_type, min_buffers_required):
        print(f"Sink connected: {image_type.width}x{image_type.height}")
        sink.alloc_and_queue_buffers(max(min_buffers_required, 4))
        return True
    def sink_disconnected(self, sink):
        print("Sink disconnected.")
    def frames_queued(self, sink):
        buf = sink.pop_output_buffer()
        arr = buf.numpy_copy()
        self.count += 1
        if self.count <= 5 or self.count % 50 == 0:
            print(f"Frame {self.count}: shape={arr.shape} dtype={arr.dtype} min={arr.min()} max={arr.max()} mean={arr.mean():.1f}")

listener = Listener()
sink = ic4.QueueSink(listener)
g.stream_setup(sink)
print("Streaming... waiting 3 seconds")
time.sleep(3)
g.stream_stop()
g.device_close()
print(f"Total frames: {listener.count}")
