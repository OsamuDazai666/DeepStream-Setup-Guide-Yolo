#!/usr/bin/env python3
import sys
import signal
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
from core import ViolationDetection

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
TILED_HEIGHT = 1080

frame_q = queue.Queue(maxsize=2)
deepstream_meta_data_q = queue.Queue(maxsize=2)

vio_det = ViolationDetection(uris=uris)

# --- Probe for metadata (before tiler) ---
def osd_sink_pad_buffer_probe(pad, info, user_data):
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        return Gst.PadProbeReturn.OK

    deepstream_meta_data = {}

    try:
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
            frame_num = frame_meta.frame_num

            deepstream_meta_data[source_id] = {
                "frame_num": frame_num,
                "boxes": [],
                "confs": [],
                "ids": [],
                "classes": []
            }

            l_obj = frame_meta.obj_meta_list
            while l_obj is not None:
                try:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                except StopIteration:
                    break

                deepstream_meta_data[source_id]["boxes"].append([
                    int(obj_meta.rect_params.left),
                    int(obj_meta.rect_params.top),
                    int(obj_meta.rect_params.width),
                    int(obj_meta.rect_params.height)
                ])
                deepstream_meta_data[source_id]["confs"].append(float(obj_meta.confidence))
                deepstream_meta_data[source_id]["ids"].append(int(obj_meta.object_id))
                deepstream_meta_data[source_id]["classes"].append(str(obj_meta.obj_label))

                l_obj = l_obj.next
            l_frame = l_frame.next

        # Debug print
        # print(deepstream_meta_data)
        try:
            deepstream_meta_data_q.put_nowait(deepstream_meta_data)
        except queue.Full:
            pass

    except Exception as e:
        print("Metadata access error:", e)

    return Gst.PadProbeReturn.OK


# --- Appsink callback for frames ---
def on_new_sample(sink, user_data=None):
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
    if name.find("decodebin") != -1:
        Object.connect("child-added", decodebin_child_added, user_data)

    if name.find("source") != -1 and Object.get_factory():
        if Object.get_factory().get_name() == "rtspsrc":
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
    bin_name = f"source-bin-{index:02d}"
    nbin = Gst.Bin.new(bin_name)
    if not nbin:
        sys.stderr.write("Unable to create source bin\n")
        return None

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

    mux = Gst.ElementFactory.make("nvstreammux", "mux")
    if not mux:
        raise RuntimeError("Failed to create nvstreammux")
    mux.set_property("width", 1920)
    mux.set_property("height", 1080)
    mux.set_property("batch-size", BATCH_SIZE)
    mux.set_property("live-source", 1)
    mux.set_property("gpu-id", 0)
    pipeline.add(mux)

    for i, uri in enumerate(uris):
        src_bin = create_source_bin(i, uri)
        pipeline.add(src_bin)
        sink_pad = mux.get_request_pad(f"sink_{i}")
        src_pad = src_bin.get_static_pad("src")
        if not src_pad:
            raise RuntimeError(f"Source bin {i} has no src pad")
        src_pad.link(sink_pad)

    infer = Gst.ElementFactory.make("nvinfer", "primary-infer")
    if not infer:
        raise RuntimeError("Failed to create nvinfer")
    infer.set_property("config-file-path", "/workspace/deepstream_app/configs/config_infer_primary_yolo11_fp32.txt")
    pipeline.add(infer)

    tracker = Gst.ElementFactory.make("nvtracker", "tracker")
    tracker.set_property("tracker-width", 640)
    tracker.set_property("tracker-height", 384)
    tracker.set_property("ll-lib-file", "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
    tracker.set_property("ll-config-file", "/opt/nvidia/deepstream/deepstream-7.1/samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml")
    pipeline.add(tracker)

    tiler = Gst.ElementFactory.make("nvmultistreamtiler", "tiler")
    if not tiler:
        raise RuntimeError("Failed to create tiler")
    tiler.set_property("width", TILED_WIDTH)
    tiler.set_property("height", TILED_HEIGHT)
    pipeline.add(tiler)

    osd = Gst.ElementFactory.make("nvdsosd", "osd")
    if not osd:
        raise RuntimeError("Failed to create nvdsosd")
    pipeline.add(osd)

    conv = Gst.ElementFactory.make("nvvideoconvert", "conv")
    videoconvert = Gst.ElementFactory.make("videoconvert", "videoconvert")
    capsfilter = Gst.ElementFactory.make("capsfilter", "caps")
    capsfilter.set_property("caps", Gst.Caps.from_string("video/x-raw, format=BGR"))

    appsink = Gst.ElementFactory.make("appsink", "appsink")
    appsink.set_property("emit-signals", True)
    appsink.set_property("sync", True)
    appsink.set_property("max-buffers", 1)
    appsink.set_property("drop", True)

    for el in (conv, videoconvert, capsfilter, appsink):
        if not el:
            raise RuntimeError("Failed to create one of conv/videoconvert/caps/appsink")
        pipeline.add(el)

    # link elements
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

    # connect appsink callback
    appsink.connect("new-sample", on_new_sample, {"loop": loop})

    # 🔑 attach probe BEFORE tiler (on tracker src pad)
    tracker_src_pad = tracker.get_static_pad("src")
    if not tracker_src_pad:
        raise RuntimeError("Unable to get tracker src pad")
    tracker_src_pad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe, None)

    return pipeline


if __name__ == "__main__":
    loop = GLib.MainLoop()
    pipeline = build_pipeline(loop)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("Failed to set pipeline to PLAYING")
        sys.exit(1)

    def sigint_handler(sig, frame):
        print("Interrupted: quitting main loop")
        loop.quit()

    signal.signal(signal.SIGINT, sigint_handler)

    cv2.namedWindow("live inference", cv2.WINDOW_NORMAL)
    while True:
        # get frame from Queue
        try:
            frame = frame_q.get()
        except queue.Empty:
            print("Queue is empty")
            frame = np.ones((680, 680, 3), dtype=np.uint8)

        # get metadata from Queue
        try:
            meta_data = deepstream_meta_data_q.get()
        except queue.Full:
            pass
        
        vio_det.video_processor(meta_data)

        cv2.imshow("live inference", frame)
        if cv2.waitKey(1) & 0xff == ord("q"):
            break

    try:
        loop.run()
    except Exception as e:
        print("Exception in main loop:", e)
    finally:
        pipeline.set_state(Gst.State.NULL)
        cv2.destroyAllWindows()
        print("Pipeline stopped, exiting.")
