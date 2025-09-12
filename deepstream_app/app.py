#!/usr/bin/env python3
import sys
import signal
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

import numpy as np
import cv2
import queue
import pyds

# init GStreamer
Gst.init(None)

# --- CONFIG ---
uris = [
    "rtsp://admin:InLights@192.168.18.29:554",
] * 6   # change or adjust as needed

BATCH_SIZE = len(uris)
TILED_WIDTH = 1280
TILED_HEIGHT = 720

frame_q = queue.Queue(maxsize=2)

# --- appsink callback ---
def on_new_sample(sink, user_data=None):
    """
    Called when appsink has a new sample.
    Pulls the sample, extracts metadata via pyds,
    converts to numpy and pushes to queue.
    """
    sample = sink.emit("pull-sample")
    if sample is None:
        return Gst.FlowReturn.OK

    buf = sample.get_buffer()
    caps = sample.get_caps()
    if not caps:
        return Gst.FlowReturn.OK

    s = caps.get_structure(0)
    try:
        width = s.get_value('width')
        height = s.get_value('height')
    except Exception:
        return Gst.FlowReturn.OK

    # --- DeepStream metadata access ---
    try:
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        if batch_meta:
            l_frame = batch_meta.frame_meta_list
            while l_frame is not None:
                try:
                    frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
                except StopIteration:
                    break

                print(f"[Frame {frame_meta.frame_num}] "
                      f"Objects detected: {frame_meta.num_obj_meta}")

                l_obj = frame_meta.obj_meta_list
                while l_obj is not None:
                    try:
                        obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                    except StopIteration:
                        break

                    print(f"  - {obj_meta.obj_label} "
                          f"(conf: {obj_meta.confidence:.2f}) "
                          f"bbox: ({obj_meta.rect_params.left}, "
                          f"{obj_meta.rect_params.top}, "
                          f"{obj_meta.rect_params.width}, "
                          f"{obj_meta.rect_params.height})")

                    l_obj = l_obj.next
                l_frame = l_frame.next
    except Exception as e:
        print("Metadata access error:", e)

    # --- Map buffer for frame extraction ---
    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    if not ok:
        return Gst.FlowReturn.OK

    try:
        arr = np.frombuffer(mapinfo.data, dtype=np.uint8)
        try:
            frame = arr.reshape((height, width, 3))
        except Exception as e:
            print("Reshape error:", e)
            return Gst.FlowReturn.OK

        try:
            frame_q.put_nowait(frame)
        except queue.Full:
            pass
    finally:
        buf.unmap(mapinfo)

    return Gst.FlowReturn.OK


# --- helper functions for uridecodebin handling ---
def cb_newpad(decodebin, decoder_src_pad, data):
    caps = decoder_src_pad.get_current_caps()
    if not caps:
        return
    gststruct = caps.get_structure(0)
    gstname = gststruct.get_name()
    if "video" not in gstname:
        return
    features = caps.get_features(0)
    if not features.contains("memory:NVMM"):
        sys.stderr.write("Decodebin did not pick nvidia decoder plugin.\n")
        return
    bin_ghost_pad = data.get_static_pad("src")
    if not bin_ghost_pad.set_target(decoder_src_pad):
        sys.stderr.write("Failed to link decoder src pad to source bin ghost pad\n")


def decodebin_child_added(child_proxy, Object, name, user_data):
    """
    Force decodebins to use nvv4l2decoder and set RTSP latency.
    """
    if name.find("decodebin") != -1:
        Object.connect("child-added", decodebin_child_added, user_data)

    if name.find("source") != -1 and Object.get_factory():
        if Object.get_factory().get_name() == "rtspsrc":
            # tune latency as needed
            Object.set_property("latency", 200)

    for elem in Object.iterate_recurse():
        if not elem.get_factory():
            continue
        fname = elem.get_factory().get_name()
        if fname.startswith("avdec"):
            parent = elem.get_parent()
            if parent:
                parent.remove(elem)
                hwdec = Gst.ElementFactory.make("nvv4l2decoder", None)
                if hwdec:
                    parent.add(hwdec)
                    hwdec.sync_state_with_parent()


def create_source_bin(index, uri):
    """
    Create a source bin with uridecodebin -> ghost src pad.
    """
    bin_name = f"source-bin-{index:02d}"
    nbin = Gst.Bin.new(bin_name)
    if not nbin:
        sys.stderr.write("Unable to create source bin\n")
        return None

    # unique decodebin name helps debugging
    uri_decode_bin = Gst.ElementFactory.make("uridecodebin", f"uri-decode-bin-{index:02d}")
    if not uri_decode_bin:
        sys.stderr.write("Unable to create uri decode bin\n")
        return None

    uri_decode_bin.set_property("uri", uri)
    uri_decode_bin.connect("pad-added", cb_newpad, nbin)
    uri_decode_bin.connect("child-added", decodebin_child_added, nbin)

    nbin.add(uri_decode_bin)

    ghost_pad = Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC)
    nbin.add_pad(ghost_pad)
    if not ghost_pad:
        sys.stderr.write("Failed to add ghost pad in source bin\n")
        return None
    return nbin


def build_pipeline(loop):
    pipeline = Gst.Pipeline.new("deepstream-pipeline")

    # nvstreammux
    mux = Gst.ElementFactory.make("nvstreammux", "mux")
    if not mux:
        raise RuntimeError("Failed to create nvstreammux")
    mux.set_property("width", 1920)
    mux.set_property("height", 1080)
    mux.set_property("batch-size", BATCH_SIZE)
    mux.set_property("live-source", 1)
    mux.set_property("gpu-id", 0)
    pipeline.add(mux)

    # sources
    for i, uri in enumerate(uris):
        src_bin = create_source_bin(i, uri)
        pipeline.add(src_bin)
        sink_pad = mux.get_request_pad(f"sink_{i}")
        src_pad = src_bin.get_static_pad("src")
        if not src_pad:
            raise RuntimeError(f"Source bin {i} has no src pad")
        src_pad.link(sink_pad)

    # inference (primary)
    infer = Gst.ElementFactory.make("nvinfer", "primary-infer")
    if not infer:
        raise RuntimeError("Failed to create nvinfer")
    infer.set_property("config-file-path", "/workspace/deepstream_app/configs/config_infer_primary_yolo11_fp32.txt")
    pipeline.add(infer)

    # tracker (give unique id to each detection)
    tracker = Gst.ElementFactory.make("nvtracker", "tracker")
    tracker.set_property("tracker-width", 640)
    tracker.set_property("tracker-height", 384)
    tracker.set_property("ll-lib-file", "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
    tracker.set_property("ll-config-file", "/opt/nvidia/deepstream/deepstream-7.1/samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml")
    pipeline.add(tracker)

    # tiler (makes tiled visualization)
    tiler = Gst.ElementFactory.make("nvmultistreamtiler", "tiler")
    if not tiler:
        raise RuntimeError("Failed to create tiler")
    tiler.set_property("width", TILED_WIDTH)
    tiler.set_property("height", TILED_HEIGHT)
    pipeline.add(tiler)

    # on-screen display (draw bbox, text)
    osd = Gst.ElementFactory.make("nvdsosd", "osd")
    if not osd:
        raise RuntimeError("Failed to create nvdsosd")
    pipeline.add(osd)

    # Conversion chain:
    # osd (NVMM) -> nvvideoconvert (NVMM) -> videoconvert (system memory) -> caps (BGR) -> appsink
    conv = Gst.ElementFactory.make("nvvideoconvert", "conv")
    videoconvert = Gst.ElementFactory.make("videoconvert", "videoconvert")
    capsfilter = Gst.ElementFactory.make("capsfilter", "caps")
    capsfilter.set_property("caps", Gst.Caps.from_string("video/x-raw, format=BGR"))

    appsink = Gst.ElementFactory.make("appsink", "appsink")
    appsink.set_property("emit-signals", True)
    appsink.set_property("sync", True)       # don't sync to clock
    appsink.set_property("max-buffers", 1)
    appsink.set_property("drop", True)

    # add conversion elements to pipeline
    for el in (conv, videoconvert, capsfilter, appsink):
        if not el:
            raise RuntimeError("Failed to create one of conv/videoconvert/caps/appsink")
        pipeline.add(el)

    # link pipeline: mux -> infer -> tiler -> osd -> conv -> videoconvert -> caps -> appsink
    if not mux.link(infer):
        raise RuntimeError("Failed to link mux -> infer")
    if not infer.link(tracker):
        raise RuntimeError("Failed to link infer -> tracker")
    if not tracker.link(tiler):
        raise RuntimeError("Failed to link tracker -> tiler")
    if not tiler.link(osd):
        raise RuntimeError("Failed to link tiler -> osd")
    if not osd.link(conv):
        raise RuntimeError("Failed to link osd -> conv")
    if not conv.link(videoconvert):
        raise RuntimeError("Failed to link conv -> videoconvert")
    if not videoconvert.link(capsfilter):
        raise RuntimeError("Failed to link videoconvert -> capsfilter")
    if not capsfilter.link(appsink):
        raise RuntimeError("Failed to link capsfilter -> appsink")

    # connect appsink callback. Pass loop so callback can quit on 'q'
    appsink.connect("new-sample", on_new_sample, {"loop": loop})

    return pipeline


if __name__ == "__main__":
    loop = GLib.MainLoop()

    # build pipeline
    pipeline = build_pipeline(loop)

    # start playing
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("Failed to set pipeline to PLAYING")
        sys.exit(1)

    # handle Ctrl+C
    def sigint_handler(sig, frame):
        print("Interrupted: quitting main loop")
        loop.quit()

    signal.signal(signal.SIGINT, sigint_handler)

    ############## opencv loop ###########
    while True:
        try:
            frame = frame_q.get()
        except queue.Empty:
            print("Queue is empty")
            frame = np.ones((680, 680, 3), dtype=np.uint8)

        cv2.imshow("live inference", frame)
        if cv2.waitKey(1) & 0xff == ord("q"):
            break
    #######################################

    try:
        print("Running main loop — press Ctrl+C or focus the window and press 'q' to quit")
        loop.run()
    except Exception as e:
        print("Exception in main loop:", e)
    finally:
        pipeline.set_state(Gst.State.NULL)
        cv2.destroyAllWindows()
        print("Pipeline stopped, exiting.")
