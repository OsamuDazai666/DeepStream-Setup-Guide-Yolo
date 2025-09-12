#!/usr/bin/env python3
import gi
import sys
import time
import pyds
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer, GLib
from typing import List
import cv2
import numpy as np
import queue 

MUXER_OUTPUT_WIDTH  = 1920
MUXER_OUTPUT_HEIGHT = 1080
MUXER_BATCH_TIMEOUT_USEC = 10_000
TILED_OUTPUT_WIDTH  = 1280
TILED_OUTPUT_HEIGHT = 720
OSD_PROCESS_MODE    = 0
OSD_DISPLAY_TEXT    = 0
TRACKER_CONFIG      = "/opt/nvidia/deepstream/deepstream-7.1/samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml"
# PGIE_CONFIG         = "/opt/nvidia/deepstream/deepstream-7.1/samples/configs/deepstream-app/config_infer_primary.txt"
PGIE_CONFIG         = "/workspace/deepstream_app/configs/config_infer_primary_yolo11_fp32.txt"
CODEC               = "H264"
BITRATE             = 1_000_000
RTSP_PORT           = 8555
UDP_PORT_BASE       = 5400
file_loop = False

fps_streams = {}

def pgie_src_pad_buffer_probe(pad, info, u_data):
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        return Gst.PadProbeReturn.OK

    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame:
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        source_id = frame_meta.source_id
        fps_streams.setdefault(source_id, {"cnt":0, "start":time.time(), "last":time.time()})
        fps_streams[source_id]["cnt"] += 1

        now = time.time()
        if now - fps_streams[source_id]["last"] >= 5.0:
            elapsed = now - fps_streams[source_id]["last"]
            frames  = fps_streams[source_id]["cnt"]
            print(f"[source-{source_id}]  FPS: {frames/elapsed:.2f}")
            fps_streams[source_id]["last"] = now
            fps_streams[source_id]["cnt"]  = 0

        try:
            l_frame = l_frame.next
        except StopIteration:
            break
    return Gst.PadProbeReturn.OK

def cb_newpad(decodebin, decoder_src_pad, data):
    caps = decoder_src_pad.get_current_caps()
    if not caps:
        return
    gststruct = caps.get_structure(0)
    gstname   = gststruct.get_name()
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
    1. Force every decodebin to use nvv4l2decoder  --> GPU memory (NVMM)
    2. When the rtsp-source shows up, set its latency
    """
    # recurse into sub-decodebins
    if name.find("decodebin") != -1:
        Object.connect("child-added", decodebin_child_added, user_data)

    # rtspsrc appears inside uridecodebin – set latency
    if name.find("source") != -1 and Object.get_factory():
        if Object.get_factory().get_name() == "rtspsrc":
            Object.set_property("latency", 200)      # ms

    # replace software decoders by nvv4l2decoder
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
    Create a source-bin that works for files and RTSP.
    latency is handled in decodebin_child_added above.
    """
    bin_name = f"source-bin-{index:02d}"
    nbin = Gst.Bin.new(bin_name)
    if not nbin:
        sys.stderr.write("Unable to create source bin\n")
        return None

    uri_decode_bin = Gst.ElementFactory.make("uridecodebin", "uri-decode-bin")
    if not uri_decode_bin:
        sys.stderr.write("Unable to create uri decode bin\n")
        return None

    uri_decode_bin.set_property("uri", uri)
    uri_decode_bin.connect("pad-added", cb_newpad, nbin)
    uri_decode_bin.connect("child-added", decodebin_child_added, nbin)

    nbin.add(uri_decode_bin)

    ghost_pad = nbin.add_pad(Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC))
    if not ghost_pad:
        sys.stderr.write("Failed to add ghost pad in source bin\n")
        return None
    return nbin


class RTSP_Server:
    def __init__(self, port, udp_port):
        self.server = GstRtspServer.RTSPServer.new()
        self.server.props.service = str(port)
        self.server.attach(None)
        factory = GstRtspServer.RTSPMediaFactory.new()
        factory.set_launch(
            f'( udpsrc name=pay0 port={udp_port} buffer-size=524288 caps="application/x-rtp, media=video, clock-rate=90000, encoding-name=(string){CODEC}, payload=96" )'
        )
        factory.set_shared(True)
        self.server.get_mount_points().add_factory("/ds-test", factory)
        print(f"\n *** RTSP stream ready at rtsp://127.0.0.1:{port}/ds-test ***\n")

class NvPipeline:
    def __init__(self, uris: List[str]):
        Gst.init(None)
        self.uris = uris
        self.pipeline = Gst.Pipeline.new("pipeline")
        if not self.pipeline:
            raise RuntimeError("Unable to create pipeline")
        self.frame_q = queue.Queue(maxsize=5)

    def build(self):
        mux = Gst.ElementFactory.make("nvstreammux", "mux")
        mux.set_property("width",  MUXER_OUTPUT_WIDTH)
        mux.set_property("height", MUXER_OUTPUT_HEIGHT)
        mux.set_property("batch-size", len(self.uris))
        mux.set_property("batched-push-timeout", MUXER_BATCH_TIMEOUT_USEC)
        mux.set_property("live-source", 1)
        mux.set_property("gpu-id", 0)
        self.pipeline.add(mux)

        # sources
        for i, uri in enumerate(self.uris):
            print("Creating source_bin ", i)
            source_bin = create_source_bin(i, uri)
            self.pipeline.add(source_bin)
            sinkpad = mux.get_request_pad(f"sink_{i}")
            srcpad  = source_bin.get_static_pad("src")
            srcpad.link(sinkpad)

        # inference + tracker
        pgie = Gst.ElementFactory.make("nvinfer", "pgie")
        pgie.set_property("config-file-path", PGIE_CONFIG)

        tracker = Gst.ElementFactory.make("nvtracker", "tracker")
        tracker.set_property("tracker-width", 640)
        tracker.set_property("tracker-height", 384)
        tracker.set_property("ll-lib-file", "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
        tracker.set_property("ll-config-file", TRACKER_CONFIG)

        # osd + tiler
        tiler = Gst.ElementFactory.make("nvmultistreamtiler", "tiler")
        tiler.set_property("width",  TILED_OUTPUT_WIDTH)
        tiler.set_property("height", TILED_OUTPUT_HEIGHT)

        osd   = Gst.ElementFactory.make("nvdsosd", "osd")
        conv  = Gst.ElementFactory.make("nvvideoconvert", "conv")

        # convert to host memory for OpenCV
        conv2cpu = Gst.ElementFactory.make("nvvideoconvert", "conv2cpu")
        capsflt  = Gst.ElementFactory.make("capsfilter", "capsflt")
        caps     = Gst.Caps.from_string("video/x-raw, format=RGBA")
        capsflt.set_property("caps", caps)

        # encoder for RTSP out
        enc  = Gst.ElementFactory.make("nvv4l2h264enc", "enc")
        enc.set_property("bitrate", BITRATE)
        pay  = Gst.ElementFactory.make("rtph264pay", "pay")
        sink = Gst.ElementFactory.make("udpsink", "sink")
        sink.set_property("host", "224.224.255.255")
        sink.set_property("port", UDP_PORT_BASE)
        sink.set_property("async", False)
        sink.set_property("sync", 1)

        for el in (pgie, tracker, tiler, osd, conv, conv2cpu, capsflt, enc, pay, sink):
            self.pipeline.add(el)

        mux.link(pgie)
        pgie.link(tracker)
        tracker.link(tiler)
        tiler.link(osd)
        osd.link(conv)
        conv.link(conv2cpu)
        conv2cpu.link(capsflt)
        capsflt.link(enc)
        enc.link(pay)
        pay.link(sink)

        # OpenCV probe
        cpu_pad = capsflt.get_static_pad("src")
        cpu_pad.add_probe(Gst.PadProbeType.BUFFER, self.opencv_probe, 0)

        print("************** PIPELINE BUILD *************************")


    def opencv_probe(self, pad, info, u_data):
        buf = info.get_buffer()
        if not buf:
            return Gst.PadProbeReturn.OK
        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK

        caps   = pad.get_current_caps()
        struct = caps.get_structure(0)
        w      = struct.get_value("width")
        h      = struct.get_value("height")

        img = np.frombuffer(map_info.data, np.uint8).reshape((h, w, 4))
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)

        # drop old frames if viewer is too slow
        if self.frame_q.full():
            self.frame_q.get_nowait()
        self.frame_q.put(img)

        buf.unmap(map_info)
        return Gst.PadProbeReturn.OK    

    def run(self):
        self.build()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self.pipeline.set_state(Gst.State.PLAYING)
        RTSP_Server(RTSP_PORT, UDP_PORT_BASE)

        # ----  OPENCV GUI IN MAIN THREAD  ----
        cv2.namedWindow("DeepStream", cv2.WINDOW_NORMAL)
        while True:
            try:
                img = self.frame_q.get(timeout=1)   # 1 s so loop can be interrupted
                cv2.imshow("DeepStream", img)
            except queue.Empty:
                img = None
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
        cv2.destroyAllWindows()
        # --------------------------------------

        loop = GLib.MainLoop()
        try:
            loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.pipeline.set_state(Gst.State.NULL)

if __name__ == "__main__":
    uris = [
        "rtsp://admin:InLights@192.168.18.29:554"
        # "file:///opt/nvidia/deepstream/deepstream-7.1/samples/streams/sample_1080p_h264.mp4"
    ] * 2 
    NvPipeline(uris).run()

