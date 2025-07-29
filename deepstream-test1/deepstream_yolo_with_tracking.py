# !/usr/bin/env python3

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GLib
import sys
import pyds
import time

Gst.init(None)

def bus_call(bus, message, loop):
    t = message.type
    if t == Gst.MessageType.EOS:
        print('End-of-stream')
        loop.quit()
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print('Error: %s: %s' % (err, debug))
        loop.quit()
    return True

# Initialize globals
frame_count = 0
start_time = time.time()

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

        frame_count += 1
        frame_number = frame_meta.frame_num
        obj_meta_list = frame_meta.obj_meta_list

        num_objects = 0
        class_count = {}

        while obj_meta_list is not None:
            try:
                obj_meta = pyds.NvDsObjectMeta.cast(obj_meta_list.data)
                num_objects += 1
                class_id = obj_meta.class_id
                if class_id not in class_count:
                    class_count[class_id] = 0
                class_count[class_id] += 1
            except StopIteration:
                break
            obj_meta_list = obj_meta_list.next

        elapsed_time = time.time() - start_time
        avg_fps = frame_count / elapsed_time if elapsed_time > 0 else 0

        print(f"Frame {frame_number} | Objects: {num_objects} | FPS: {avg_fps:.2f}")
        print(f"Class breakdown: {class_count}")

        l_frame = l_frame.next

    return Gst.PadProbeReturn.OK

def main():
    rtsp_url = "rtsp://admin:InLights@192.168.18.29:554"
    config_path = "/workspace/deepstream-test1/configs/config_infer_primary_yolo11.txt"
    tracker_config = "/workspace/deepstream-test1/configs/tracker_config.txt"  # <-- Add tracker config path

    print("Starting pipeline...")
    pipeline = Gst.Pipeline()

    source = Gst.ElementFactory.make("rtspsrc", "rtsp-source")
    source.set_property("location", rtsp_url)
    source.set_property("latency", 200)

    depay = Gst.ElementFactory.make("rtph264depay", "depay")
    h264parse = Gst.ElementFactory.make("h264parse", "h264parse")
    decoder = Gst.ElementFactory.make("nvv4l2decoder", "nvv4l2-decoder")
    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    tracker = Gst.ElementFactory.make("nvtracker", "tracker")  # <-- Add tracker
    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "nvvideo-converter")
    nvosd = Gst.ElementFactory.make("nvdsosd", "nv-onscreendisplay")
    sink = Gst.ElementFactory.make("nveglglessink", "nvvideo-renderer")

    if not all([pipeline, source, depay, h264parse, decoder, streammux, pgie, tracker, nvvidconv, nvosd, sink]):
        print("Missing element. Check your DeepStream installation.")
        return

    pipeline.add(source)
    pipeline.add(depay)
    pipeline.add(h264parse)
    pipeline.add(decoder)
    pipeline.add(streammux)
    pipeline.add(pgie)
    pipeline.add(tracker)  # <-- Add to pipeline
    pipeline.add(nvvidconv)
    pipeline.add(nvosd)
    pipeline.add(sink)

    def cb_newpad(src, pad, depay):
        sinkpad = depay.get_static_pad("sink")
        if not sinkpad.is_linked():
            pad.link(sinkpad)

    source.connect("pad-added", cb_newpad, depay)

    depay.link(h264parse)
    h264parse.link(decoder)

    sinkpad = streammux.get_request_pad("sink_0")
    srcpad = decoder.get_static_pad("src")
    srcpad.link(sinkpad)

    streammux.set_property("width", 1280)
    streammux.set_property("height", 720)
    streammux.set_property("batch-size", 1)
    streammux.set_property("batched-push-timeout", 4000000)

    pgie.set_property("config-file-path", config_path)

    # Configure tracker
    if not tracker:
        print("Unable to create tracker")
        return
    tracker.set_property("tracker-width", 640)
    tracker.set_property("tracker-height", 384)
    tracker.set_property("ll-lib-file", "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
    tracker.set_property("ll-config-file", tracker_config)
    # tracker.set_property("tracker-config-file", "/workspace/deepstream-test1/tracker_config.txt")


    # Link pipeline with tracker
    streammux.link(pgie)
    pgie.link(tracker)          # <-- pgie → tracker
    tracker.link(nvvidconv)     # <-- tracker → nvvidconv
    nvvidconv.link(nvosd)
    nvosd.link(sink)

    # Attach probe after tracker to get tracking metadata
    tracker_src_pad = tracker.get_static_pad("src")
    if not tracker_src_pad:
        print("Unable to get src pad of tracker")
    else:
        tracker_src_pad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe, 0)

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


if __name__ == '__main__':
    sys.exit(main())
