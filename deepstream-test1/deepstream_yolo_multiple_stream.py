#!/usr/bin/env python3

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GLib
import pyds
import sys
import time

Gst.init(None)

# Global frame counter and start time for FPS calculation
frame_count = {}
start_time = time.time()

# List of RTSP sources
rtsp_sources = [
    "rtsp://admin:InLights@192.168.18.29:554",
    "rtsp://admin:InLights@192.168.18.29:554",
    "rtsp://admin:InLights@192.168.18.29:554",
    "rtsp://admin:InLights@192.168.18.29:554",
    "rtsp://admin:InLights@192.168.18.29:554",
    "rtsp://admin:InLights@192.168.18.29:554",
]

# Probe function
def osd_sink_pad_buffer_probe(pad, info, u_data):
    global frame_count, start_time

    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer")
        return Gst.PadProbeReturn.OK

    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    if not batch_meta:
        return Gst.PadProbeReturn.OK

    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        source_id = frame_meta.source_id
        frame_number = frame_meta.frame_num
        obj_meta_list = frame_meta.obj_meta_list

        if source_id not in frame_count:
            frame_count[source_id] = 0
        frame_count[source_id] += 1

        num_objects = 0
        class_count = {}

        while obj_meta_list is not None:
            try:
                obj_meta = pyds.NvDsObjectMeta.cast(obj_meta_list.data)
                num_objects += 1
                class_id = obj_meta.class_id
                class_count[class_id] = class_count.get(class_id, 0) + 1
            except StopIteration:
                break
            obj_meta_list = obj_meta_list.next

        elapsed_time = time.time() - start_time
        avg_fps = frame_count[source_id] / elapsed_time if elapsed_time > 0 else 0

        print(f"[Stream {source_id}] Frame {frame_number} | Objects: {num_objects} | FPS: {avg_fps:.2f}")
        print(f"Class breakdown: {class_count}")

        l_frame = l_frame.next

    return Gst.PadProbeReturn.OK

# Callback to link rtspsrc pad to depayloader
def cb_newpad(src, pad, depay):
    sinkpad = depay.get_static_pad("sink")
    if not sinkpad.is_linked():
        pad.link(sinkpad)

def main():
    print("Starting multi-stream DeepStream pipeline...")
    pipeline = Gst.Pipeline()

    streammux = Gst.ElementFactory.make("nvstreammux", "stream-muxer")
    pipeline.add(streammux)

    streammux.set_property("width", 1280)
    streammux.set_property("height", 720)
    streammux.set_property("batch-size", len(rtsp_sources))
    streammux.set_property("batched-push-timeout", 4000000)

    for i, rtsp_url in enumerate(rtsp_sources):
        source = Gst.ElementFactory.make("rtspsrc", f"rtsp-source-{i}")
        source.set_property("location", rtsp_url)
        source.set_property("latency", 200)

        depay = Gst.ElementFactory.make("rtph264depay", f"depay-{i}")
        h264parse = Gst.ElementFactory.make("h264parse", f"h264parse-{i}")
        decoder = Gst.ElementFactory.make("nvv4l2decoder", f"decoder-{i}")

        if not all([source, depay, h264parse, decoder]):
            print(f"Failed to create elements for stream {i}")
            return

        pipeline.add(source)
        pipeline.add(depay)
        pipeline.add(h264parse)
        pipeline.add(decoder)

        source.connect("pad-added", cb_newpad, depay)
        depay.link(h264parse)
        h264parse.link(decoder)

        sinkpad = streammux.get_request_pad(f"sink_{i}")
        srcpad = decoder.get_static_pad("src")
        if not sinkpad or not srcpad:
            print(f"Failed to get pads for linking stream {i}")
            return
        srcpad.link(sinkpad)

    # Shared downstream elements
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    pgie.set_property("config-file-path", "/workspace/deepstream-test1/configs/config_infer_primary_yolo11.txt")

    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "nvvideo-converter")
    nvosd = Gst.ElementFactory.make("nvdsosd", "nv-onscreendisplay")
    sink = Gst.ElementFactory.make("nveglglessink", "nvvideo-renderer")

    if not all([pgie, nvvidconv, nvosd, sink]):
        print("Failed to create shared pipeline elements")
        return

    for elem in [pgie, nvvidconv, nvosd, sink]:
        pipeline.add(elem)

    streammux.link(pgie)
    pgie.link(nvvidconv)
    nvvidconv.link(nvosd)
    nvosd.link(sink)

    # Add probe
    pgie_src_pad = pgie.get_static_pad("src")
    if pgie_src_pad:
        pgie_src_pad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe, 0)
    else:
        print("Failed to get src pad of nvinfer")

    # Bus and Main Loop
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except:
        pass
    pipeline.set_state(Gst.State.NULL)

def bus_call(bus, message, loop):
    msg_type = message.type
    if msg_type == Gst.MessageType.EOS:
        print("End-of-stream")
        loop.quit()
    elif msg_type == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print(f"Error: {err} | Debug Info: {debug}")
        loop.quit()
    return True

if __name__ == "__main__":
    sys.exit(main())
