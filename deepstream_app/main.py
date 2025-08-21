import gi
import sys
import time
sys.path.append("../")
from utils import PlatformInfo
import pyds
from ctypes import *
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer, GLib
from typing import List
from redis_tools import RedisPublisherManager
from ws_client import WebSocketClient
import logging, logging.handlers, sys, os, pathlib

LOG_DIR   = pathlib.Path(__file__).with_suffix('')   # ./deepstream_app
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE  = LOG_DIR / "main.log"
MAX_BYTES = 5 * 1024 * 1024   # 5 MiB
BACKUPS   = 2

handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUPS
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[handler, logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("main")


MUXER_OUTPUT_WIDTH = 1920
MUXER_OUTPUT_HEIGHT = 1080
MUXER_BATCH_TIMEOUT_USEC = 10000
TILED_OUTPUT_WIDTH = 1280
TILED_OUTPUT_HEIGHT = 720
GST_CAPS_FEATURES_NVMM = "memory:NVMM"
OSD_PROCESS_MODE = 0
OSD_DISPLAY_TEXT = 0
pgie_classes_str = ["Vehicle", "TwoWheeler", "Person", "RoadSign"]
gie = "nvinfer"
codec = "H264"
bitrate = 4000000
ts_from_rtsp = False
tracker_config = "/workspace/deepstream_app/configs/config_tracker_NvDCF_perf.yml"


redis_pub_manager = RedisPublisherManager()
ws = WebSocketClient("ws://localhost:5000")
ws.connect()

# FPS tracking variables
fps_streams = {}
def pgie_src_pad_buffer_probe(pad, info, u_data):

    frame_number = 0
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer")
        return Gst.PadProbeReturn.OK

    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list


    while l_frame is not None:
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        frame_number = frame_meta.frame_num
        source_id = frame_meta.source_id

        # --- NEW: Lists for your attributes ---
        filtered_boxes = []
        filtered_classes = []
        filtered_confs = []
        filtered_ids = []

        # Traverse detected objects in the frame
        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            try:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break

            # Bounding box
            rect_params = obj_meta.rect_params
            x, y, w, h = rect_params.left, rect_params.top, rect_params.width, rect_params.height
            filtered_boxes.append([x, y, w, h])

            # Class
            filtered_classes.append(obj_meta.class_id)

            # Confidence
            filtered_confs.append(obj_meta.confidence)

            # Object ID (if tracker enabled)
            filtered_ids.append(obj_meta.object_id)

            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        # --- publish data via redis manager ---
        data = {
            "Boxes" : filtered_boxes,
            "Classes" : filtered_classes,
            "Confs" : filtered_confs,
            "IDs" : filtered_ids,
        }
        redis_pub_manager.publish_data(source_id, data)

        # --- FPS calculation ---
        current_time = time.time()
        if source_id not in fps_streams:
            fps_streams[source_id] = {
                'frame_count': 0,
                'start_time': current_time,
                'last_print_time': current_time,
                'last_print_frame': 0
            }

        fps_streams[source_id]['frame_count'] += 1

        time_elapsed = current_time - fps_streams[source_id]['last_print_time']
        if time_elapsed >= 5.0:
            frames_processed = fps_streams[source_id]['frame_count'] - fps_streams[source_id]['last_print_frame']
            avg_fps = frames_processed / time_elapsed

            total_time_elapsed = current_time - fps_streams[source_id]['start_time']
            overall_avg_fps = fps_streams[source_id]['frame_count'] / total_time_elapsed

            print(f"Stream {source_id}: Current FPS: {avg_fps:.2f}, Overall Avg FPS: {overall_avg_fps:.2f}")
            ws.ping()

            fps_streams[source_id]['last_print_time'] = current_time
            fps_streams[source_id]['last_print_frame'] = fps_streams[source_id]['frame_count']

        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    return Gst.PadProbeReturn.OK


def cb_newpad(decodebin, decoder_src_pad, data):
    print("In cb_newpad\n")
    caps = decoder_src_pad.get_current_caps()
    gststruct = caps.get_structure(0)
    gstname = gststruct.get_name()
    source_bin = data
    features = caps.get_features(0)

    # Need to check if the pad created by the decodebin is for video and not
    # audio.
    print("gstname=", gstname)
    if gstname.find("video") != -1:
        # Link the decodebin pad only if decodebin has picked nvidia
        # decoder plugin nvdec_*. We do this by checking if the pad caps contain
        # NVMM memory features.
        print("features=", features)
        if features.contains("memory:NVMM"):
            # Get the source bin ghost pad
            bin_ghost_pad = source_bin.get_static_pad("src")
            if not bin_ghost_pad.set_target(decoder_src_pad):
                sys.stderr.write(
                    "Failed to link decoder src pad to source bin ghost pad\n"
                )
        else:
            sys.stderr.write(
                " Error: Decodebin did not pick nvidia decoder plugin.\n")


def decodebin_child_added(child_proxy, Object, name, user_data):
    print("Decodebin child added:", name, "\n")
    if name.find("decodebin") != -1:
        Object.connect("child-added", decodebin_child_added, user_data)

    if ts_from_rtsp:
        if name.find("source") != -1:
            pyds.configure_source_for_ntp_sync(hash(Object))


def create_source_bin(index, uri):
    print("Creating source bin")

    # Create a source GstBin to abstract this bin's content from the rest of the
    # pipeline
    bin_name = "source-bin-%02d" % index
    print(bin_name)
    nbin = Gst.Bin.new(bin_name)
    if not nbin:
        sys.stderr.write(" Unable to create source bin \n")

    # Source element for reading from the uri.
    # We will use decodebin and let it figure out the container format of the
    # stream and the codec and plug the appropriate demux and decode plugins.
    uri_decode_bin = Gst.ElementFactory.make("uridecodebin", "uri-decode-bin")
    if not uri_decode_bin:
        sys.stderr.write(" Unable to create uri decode bin \n")
    # We set the input uri to the source element
    uri_decode_bin.set_property("uri", uri)
    # Connect to the "pad-added" signal of the decodebin which generates a
    # callback once a new pad for raw data has beed created by the decodebin
    uri_decode_bin.connect("pad-added", cb_newpad, nbin)
    uri_decode_bin.connect("child-added", decodebin_child_added, nbin)

    # We need to create a ghost pad for the source bin which will act as a proxy
    # for the video decoder src pad. The ghost pad will not have a target right
    # now. Once the decode bin creates the video decoder and generates the
    # cb_newpad callback, we will set the ghost pad target to the video decoder
    # src pad.
    Gst.Bin.add(nbin, uri_decode_bin)
    bin_pad = nbin.add_pad(
        Gst.GhostPad.new_no_target(
            "src", 
            Gst.PadDirection.SRC
        )
    )
    if not bin_pad:
        sys.stderr.write(" Failed to add ghost pad in source bin \n")
        return None
    return nbin


class NvPipeline:
    def __init__(self, stream_paths: List):
        Gst.init(None)
        
        self.platform_info = PlatformInfo()
        self.stream_paths = stream_paths

        self.rtsp_port_num = 8555
        self.udpsink_port_num = 5400

        self.pipeline = Gst.Pipeline()
        if not self.pipeline:
            print("Unable to create pipeline")

        self.output_streams = []

    def _plugins_created_succesfully(self, plugins: List) -> bool:
        for e in plugins:
            if not e:
                print(f"Error: in element {e}")
                return False
            self.pipeline.add(e)
        
        return True

    def build_pipeline(self):
        streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
        streammux.set_property("width", 1920)
        streammux.set_property("height", 1080)
        streammux.set_property("batch-size", len(self.stream_paths))
        streammux.set_property("batched-push-timeout", MUXER_BATCH_TIMEOUT_USEC)
        streammux.set_property("live-source", 1)

        # add streamux to pipeline
        self.pipeline.add(streammux)
        is_live = False # temp varaible
        for i, stream_path in enumerate(self.stream_paths):
            if stream_path.find("rtsp://") == 0:
                is_live = True

            source_bin = create_source_bin(i, stream_path)
            if not source_bin:
                print("unable to create source")

            self.pipeline.add(source_bin)

            padname = "sink_%u" % i
            sinkpad = streammux.request_pad_simple(padname)
            if not sinkpad:
                print("Unable to create sink pad bin")

            srcpad = source_bin.get_static_pad("src")
            if not srcpad:
                print("Unable to create src pad bin")

            srcpad.link(sinkpad)

        nvinfer = Gst.ElementFactory.make("nvinfer", "primary-inference")
        nvinfer.set_property("config-file-path", "/workspace/deepstream_app/configs/config_infer_primary_yolo11_fp32.txt")

        # attach a probe to nvinfer plugin
        pgie_src_pad=nvinfer.get_static_pad("src")
        if not pgie_src_pad:
            sys.stderr.write(" Unable to get src pad \n")
        else:
            pgie_src_pad.add_probe(Gst.PadProbeType.BUFFER, pgie_src_pad_buffer_probe, 0)

        tracker = Gst.ElementFactory.make("nvtracker", "tracker")
        tracker.set_property("tracker-width", 640)
        tracker.set_property("tracker-height", 384)
        tracker.set_property("ll-lib-file", "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
        tracker.set_property("ll-config-file", tracker_config)

        demux = Gst.ElementFactory.make("nvstreamdemux", "demux")
        for e in (nvinfer, tracker, demux):
            self.pipeline.add(e)

        streammux.link(nvinfer)
        nvinfer.link(tracker)
        tracker.link(demux)

        for i, uri in enumerate(self.stream_paths):
            q = Gst.ElementFactory.make("queue", f"queue_{i}")
            osd = Gst.ElementFactory.make("nvdsosd", f"osd_{i}") 
            conv = Gst.ElementFactory.make("nvvideoconvert", f"video_convert_{i}")
            enc = Gst.ElementFactory.make("nvv4l2h264enc", f"encoder_{i}")
            enc.set_property("bitrate", 1000_000)
            pay = Gst.ElementFactory.make("rtph264pay", f"pay_{i}")
            sink = Gst.ElementFactory.make("udpsink", f"sink_{i}")
            sink.set_property("host", "224.224.255.255")
            sink.set_property("port", self.udpsink_port_num + i)
            sink.set_property("async", False)
            sink.set_property("sync", 1)
            sink.set_property("qos", 0)

            for e in (q, osd, conv, enc, pay, sink):
                self.pipeline.add(e)

            demux.link_pads(f"src_{i}", q, "sink")
            q.link(osd)
            osd.link(conv)
            conv.link(enc)
            enc.link(pay)
            pay.link(sink)

            self.output_streams.append(
                RTSP_Stream(self.rtsp_port_num + i, self.udpsink_port_num + i)
            )

    def run(self):
        self.build_pipeline()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()

        # Start Pipeline
        self.pipeline.set_state(Gst.State.PLAYING)

        # stream = RTSP_Stream(self.rtsp_port_num, self.udpsink_port_num)
        for stream in self.output_streams:
            stream.run()

        self.pipeline.set_state(Gst.State.NULL)


class RTSP_Stream:
    def __init__(self, rtsp_port_num, udp_port_num) -> None:
        self.rtsp_port_num = rtsp_port_num
        self.udp_port_num = udp_port_num

        self.server = GstRtspServer.RTSPServer.new()
        self.server.props.service = "%d" % self.rtsp_port_num
        self.server.attach(None)

        self.factory = GstRtspServer.RTSPMediaFactory.new()
        self.factory.set_launch(
            '( udpsrc name=pay0 port=%d buffer-size=524288 caps="application/x-rtp, media=video, clock-rate=90000, encoding-name=(string)%s, payload=96 " )'
            % (self.udp_port_num, codec)
        )
        self.factory.set_shared(True)

        self.server.get_mount_points().add_factory("/ds-test", self.factory)

        self.stream_uri = f"Output RTSP: rtsp://localhost:{self.rtsp_port_num}/ds-test" 

        print(
            "\n\n",
            self.stream_uri,
            "\n\n"
        )

    
    def run(self):
        loop = GLib.MainLoop()
        try:
            loop.run()
        except BaseException:
            pass

if __name__ == '__main__':
    stream_paths = [
        "rtsp://admin:InLights@192.168.18.29:554" for _ in range(1)
    ] 

    obj = NvPipeline(stream_paths)
    obj.run()