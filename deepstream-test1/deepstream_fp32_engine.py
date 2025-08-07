import sys
sys.path.append("../")
from common.bus_call import bus_call
from common.platform_info import PlatformInfo
import pyds
import platform
import math
import time
from ctypes import *
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer, GLib
import configparser
import datetime

import argparse

MUXER_OUTPUT_WIDTH = 1920
MUXER_OUTPUT_HEIGHT = 1080
MUXER_BATCH_TIMEOUT_USEC = 33000
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
tracker_config = "/workspace/deepstream-test1/configs/config_tracker_NvDCF_perf.yml"


# FPS tracking variables
fps_streams = {}
def pgie_src_pad_buffer_probe(pad, info, u_data):
    frame_number = 0
    num_rects = 0
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    # Retrieve batch metadata from the gst_buffer
    # Note that pyds.gst_buffer_get_nvds_batch_meta() expects the
    # C address of gst_buffer as input, which is obtained with hash(gst_buffer)
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            # Note that l_frame.data needs a cast to pyds.NvDsFrameMeta
            # The casting is done by pyds.NvDsFrameMeta.cast()
            # The casting also keeps ownership of the underlying memory
            # in the C code, so the Python garbage collector will leave
            # it alone.
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        frame_number = frame_meta.frame_num
        source_id = frame_meta.source_id
        
        # FPS calculation - more stable approach
        current_time = time.time()
        if source_id not in fps_streams:
            fps_streams[source_id] = {
                'frame_count': 0,
                'start_time': current_time,
                'last_print_time': current_time,
                'last_print_frame': 0
            }
        
        fps_streams[source_id]['frame_count'] += 1
        
        # Calculate and print average FPS every 5 seconds
        time_elapsed = current_time - fps_streams[source_id]['last_print_time']
        if time_elapsed >= 5.0:  # Print every 5 seconds
            frames_processed = fps_streams[source_id]['frame_count'] - fps_streams[source_id]['last_print_frame']
            avg_fps = frames_processed / time_elapsed
            
            # Also calculate overall average FPS since start
            total_time_elapsed = current_time - fps_streams[source_id]['start_time']
            overall_avg_fps = fps_streams[source_id]['frame_count'] / total_time_elapsed
            
            print(f"Stream {source_id}: Frame {frame_number}, Current FPS: {avg_fps:.2f}, Overall Avg FPS: {overall_avg_fps:.2f}")
            
            fps_streams[source_id]['last_print_time'] = current_time
            fps_streams[source_id]['last_print_frame'] = fps_streams[source_id]['frame_count']
        
        if ts_from_rtsp:
            ts = frame_meta.ntp_timestamp/1000000000 # Retrieve timestamp, put decimal in proper position for Unix format
            print("RTSP Timestamp:",datetime.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')) # Convert timestamp to UTC

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
            "src", Gst.PadDirection.SRC))
    if not bin_pad:
        sys.stderr.write(" Failed to add ghost pad in source bin \n")
        return None
    return nbin


def main(stream_paths):

    platform_info = PlatformInfo()

    # Standard GStreamer initialization
    Gst.init(None)

    # Create Gstream Pipeline
    pipeline = Gst.Pipeline()
    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")

    # create nvstreamux for batch inference and tracking
    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    streammux.set_property("width", 1920)
    streammux.set_property("height", 1080)
    streammux.set_property("batch-size", len(stream_paths))
    streammux.set_property("batched-push-timeout", MUXER_BATCH_TIMEOUT_USEC)

    # add streamux to pipeline
    pipeline.add(streammux)

    is_live = False # temp varaible
    for i, stream_path in enumerate(stream_paths):
        print("Creating source_bin ", i, " \n ")
        if stream_path.find("rtsp://") == 0:
            is_live = True

        source_bin = create_source_bin(i, stream_path)
        if not source_bin:
            print("unable to create source")

        pipeline.add(source_bin)

        padname = "sink_%u" % i
        sinkpad = streammux.request_pad_simple(padname)
        if not sinkpad:
            print("Unable to create sink pad bin \n")

        srcpad = source_bin.get_static_pad("src")
        if not srcpad:
            print("Unable to create src pad bin \n")

        srcpad.link(sinkpad)

    nvinfer = Gst.ElementFactory.make("nvinfer", "primary-inference")
    nvinfer.set_property("config-file-path", "/workspace/deepstream-test1/configs/config_infer_primary_yolo11_fp32.txt")

    tracker = Gst.ElementFactory.make("nvtracker", "tracker")
    tracker.set_property("tracker-width", 640)
    tracker.set_property("tracker-height", 384)
    tracker.set_property("ll-lib-file", "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
    tracker.set_property("ll-config-file", tracker_config)

    tiler = Gst.ElementFactory.make("nvmultistreamtiler", "nvtiler")
    tiler_rows = int(math.sqrt(len(stream_paths)))
    tiler_columns = int(math.ceil((1.0 * len(stream_paths)) / tiler_rows))
    tiler.set_property("rows", tiler_rows)
    tiler.set_property("columns", tiler_columns)
    tiler.set_property("width", TILED_OUTPUT_WIDTH)
    tiler.set_property("height", TILED_OUTPUT_HEIGHT)

    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")

    caps = Gst.ElementFactory.make("capsfilter", "filter")
    caps.set_property(
        "caps", Gst.Caps.from_string("video/x-raw(memory:NVMM), format=I420")
    )

    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    nvosd.set_property("display-clock", 1)

    nvvidconv_postosd = Gst.ElementFactory.make("nvvideoconvert", "convertor_postosd")

    encoder = Gst.ElementFactory.make("nvv4l2h264enc", "encoder")
    encoder.set_property("bitrate", bitrate)
    if platform_info.is_integrated_gpu():
        encoder.set_property("preset-level", 1)
        encoder.set_property("insert-sps-pps", 1)

    rtppay = Gst.ElementFactory.make("rtph264pay", "rtppay")

    sink = Gst.ElementFactory.make("udpsink", "udpsink")
    updsink_port_num = 5400
    sink.set_property("host", "224.224.255.255")
    sink.set_property("port", updsink_port_num)
    sink.set_property("async", False)
    sink.set_property("sync", 1)
    sink.set_property("qos", 0)


    for e in [nvinfer, tracker, tiler, nvvidconv, nvosd, nvvidconv_postosd, caps, encoder, rtppay, sink]:
        if not e:
            print(f"Error: in element {e}")
            return
        pipeline.add(e)

    streammux.link(nvinfer)
    nvinfer.link(tracker)
    tracker.link(nvvidconv)
    nvvidconv.link(tiler)
    tiler.link(nvosd)
    nvosd.link(nvvidconv_postosd)
    nvvidconv_postosd.link(caps)
    caps.link(encoder)
    encoder.link(rtppay)
    rtppay.link(sink)

    # create an event loop and feed gstreamer bus mesages to it
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    pgie_src_pad=nvinfer.get_static_pad("src")
    if not pgie_src_pad:
        sys.stderr.write(" Unable to get src pad \n")
    else:
        pgie_src_pad.add_probe(Gst.PadProbeType.BUFFER, pgie_src_pad_buffer_probe, 0)

    # Start streaming
    rtsp_port_num = 8555

    server = GstRtspServer.RTSPServer.new()
    server.props.service = "%d" % rtsp_port_num
    server.attach(None)

    factory = GstRtspServer.RTSPMediaFactory.new()
    factory.set_launch(
        '( udpsrc name=pay0 port=%d buffer-size=524288 caps="application/x-rtp, media=video, clock-rate=90000, encoding-name=(string)%s, payload=96 " )'
        % (updsink_port_num, codec)
    )
    factory.set_shared(True)
    server.get_mount_points().add_factory("/ds-test", factory)

    print(
        "\n *** DeepStream: Launched RTSP Streaming at rtsp://localhost:%d/ds-test ***\n\n"
        % rtsp_port_num
    )

    # start play back and listen to events
    print("Starting pipeline \n")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except BaseException:
        pass
    # cleanup
    pipeline.set_state(Gst.State.NULL)


if __name__ == '__main__':
    # create 20 instance of the same stream
    stream_path = [
        "rtsp://0.0.0.0:8554/mystream" for i in range(20)
    ] 
    main(stream_path)

