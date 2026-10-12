#!/usr/bin/env python3
"""A stand-in for ffmpeg and ffprobe, so the video tests need neither.

A "video" for it is a small file: MAGIC, then JSON {"probe": what ffprobe
would print for it, "black_until": seconds of black frames at the start,
"no_zscale": true to act like an ffmpeg without the zscale filter,
"zscale_fails": true to make zscale fail on this video (as on one whose
colours it cannot convert), "fail_frames": true to fail every frame,
"frames_until": seconds after which there is no frame (the picture ends
before the sound)}. make_video() writes one.

    python fake_ffmpeg.py ffprobe ARGS...   prints the probe JSON (exit 1 for
                                            a file that is not a fake video)
    python fake_ffmpeg.py ffmpeg ARGS...    a frame (-ss T ... -f image2pipe
                                            -c:v bmp pipe:1) as a BMP on
                                            stdout; a copy without metadata
                                            (-c copy ... file:OUT); a playable
                                            copy (-c:v libx264 ... file:OUT)

The frames are a gradient whose colour follows the time asked for, so
frames at different times differ; they are as large as the video (rotation
applied), within the edge of the scale filter. A copy keeps the probe but
drops every tag and the data streams, as the real command does; a playable
copy says H.264. With the environment variable FAKE_FFMPEG_LOG set, every
command line is appended to that file (one JSON list per line).

run(argv) does the same in the calling process and returns (exit code,
stdout bytes, stderr text): tests patch videos._run with it to save the
start-up time of a process per frame.
"""

import copy
import io
import json
import os
import re
import sys

MAGIC = b"FAKEVIDEO\n"


def make_video(path, probe, **extra):
    """Write a fake video holding probe (the ffprobe JSON) and extra settings."""
    data = dict(extra, probe=probe)
    with open(path, "wb") as handle:
        handle.write(MAGIC + json.dumps(data).encode("utf-8"))
    return path


def probe_json(width=1920, height=1080, duration=24.0, codec="h264", rotation=None,
               format_tags=None, stream_tags=None, audio=True, brand="qt  ",
               pix_fmt="yuv420p", transfer="bt709", data_stream=False, size=None,
               primaries=None, matrix=None):
    """ffprobe-shaped JSON for a video (made-up values, the shapes real files give)."""
    video = {"index": 0, "codec_type": "video", "codec_name": codec,
             "profile": "Main" if codec == "hevc" else "High", "width": width,
             "height": height, "pix_fmt": pix_fmt, "color_transfer": transfer,
             "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
             "duration": "%.6f" % duration if duration else "N/A",
             "tags": dict(stream_tags if stream_tags is not None else {
                 "creation_time": "2021-07-04T18:30:15.000000Z",
                 "handler_name": "Core Media Video"})}
    for key, value in (("color_primaries", primaries), ("color_space", matrix)):
        if value is not None:
            video[key] = value
    if rotation is not None:
        video["side_data_list"] = [{"side_data_type": "Display Matrix",
                                    "displaymatrix": "\n00000000: 0 65536 0\n",
                                    "rotation": rotation}]
    streams = [video]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": "aac",
                        "tags": {"creation_time": "2021-07-04T18:30:15.000000Z"}})
    if data_stream:
        streams.append({"index": len(streams), "codec_type": "data",
                        "codec_tag_string": "mebx", "tags": {"handler_name": "Metadata"}})
    tags = {"major_brand": brand, "creation_time": "2021-07-04T18:30:15.000000Z"}
    tags.update(format_tags or {})
    return {"streams": streams,
            "format": {"filename": "x", "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                       "duration": "%.6f" % duration if duration else "N/A",
                       "size": str(size or 1000), "bit_rate": "8000000", "tags": tags}}


def _path(arg):
    return arg[len("file:"):] if arg.startswith("file:") else arg


def _load(path):
    with open(path, "rb") as handle:
        data = handle.read()
    if not data.startswith(MAGIC):
        return None
    return json.loads(data[len(MAGIC):].decode("utf-8"))


def _option(argv, name, default=None):
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return default


def _shown_size(probe):
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    width, height = int(video["width"]), int(video["height"])
    for item in video.get("side_data_list") or ():
        if abs(int(item.get("rotation", 0))) % 180 == 90:
            width, height = height, width
    return width, height


def _frame(spec, seconds, edge):
    from PIL import Image
    probe = spec["probe"]
    width, height = _shown_size(probe)
    scale = min(1.0, float(edge) / max(width, height)) if edge else 1.0
    size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    if seconds < float(spec.get("black_until") or 0):
        image = Image.new("RGB", size, (0, 0, 0))
    else:
        # A diagonal gradient whose colour moves with the time.
        shade = int(seconds * 37) % 200 + 40
        image = Image.linear_gradient("L").resize(size).convert("RGB")
        tint = Image.new("RGB", size, (shade, 255 - shade, (shade * 3) % 256))
        image = Image.blend(image, tint, 0.5)
    buffer = io.BytesIO()
    image.save(buffer, "BMP")
    return buffer.getvalue()


def _ffprobe(argv):
    spec = _load(_path(argv[-1]))
    if spec is None:
        return 1, b"", "%s: Invalid data found when processing input\n" % argv[-1]
    return 0, json.dumps(spec["probe"]).encode("utf-8"), ""


def _ffmpeg(argv):
    source = _path(_option(argv, "-i", ""))
    spec = _load(source)
    if spec is None:
        return 1, b"", "%s: Invalid data found when processing input\n" % source
    target = argv[-1]
    filters = _option(argv, "-vf", "")
    if target == "pipe:1":
        if spec.get("fail_frames"):
            return 1, b"", "Error while decoding stream #0:0\n"
        if "zscale" in filters and spec.get("no_zscale"):
            return 1, b"", "No such filter: 'zscale'\n"
        if "zscale" in filters and spec.get("zscale_fails"):
            return 1, b"", ("[Parsed_zscale_0 @ 0000] code 3074: no path between "
                            "colorspaces\nError while filtering: Generic error\n")
        seconds = float(_option(argv, "-ss", "0"))
        duration = spec["probe"]["format"].get("duration")
        if duration not in (None, "N/A") and seconds > float(duration):
            return 0, b"", ""           # past the end: no frame
        if spec.get("frames_until") is not None and seconds > float(spec["frames_until"]):
            return 0, b"", ""           # the picture ended before the sound
        match = re.search(r"min\(iw,(\d+)\)", filters)
        return 0, _frame(spec, seconds, int(match.group(1)) if match else 0), ""
    if "libx264" in argv and "zscale" in filters and spec.get("no_zscale"):
        return 1, b"", "No such filter: 'zscale'\nError opening output files: Filter not found\n"
    if "libx264" in argv and "zscale" in filters and spec.get("zscale_fails"):
        return 1, b"", "[Parsed_zscale_0 @ 0000] code 3074: no path between colorspaces\n"
    out = copy.deepcopy(spec)
    probe = out["probe"]
    if "libx264" in argv:
        # A playable copy: H.264 / AAC in an MP4, at most the asked height.
        video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
        video.update(codec_name="h264", pix_fmt="yuv420p", color_transfer="bt709")
        video.pop("side_data_list", None)
        probe["format"]["tags"] = {"major_brand": "isom"}
    else:
        # A copy without metadata: no tags, no data streams.
        probe["streams"] = [stream for stream in probe["streams"]
                            if stream["codec_type"] in ("video", "audio")]
        for stream in probe["streams"]:
            stream["tags"] = {}
        probe["format"]["tags"] = {"major_brand": "isom"}
    with open(_path(target), "wb") as handle:
        handle.write(MAGIC + json.dumps(out).encode("utf-8"))
    return 0, b"", ""


def run(argv):
    """argv: "ffmpeg" or "ffprobe" then its arguments. Returns (code, stdout, stderr)."""
    log = os.environ.get("FAKE_FFMPEG_LOG")
    if log:
        with open(log, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(list(argv)) + "\n")
    program, args = argv[0], list(argv[1:])
    try:
        if program == "ffprobe":
            return _ffprobe(args)
        return _ffmpeg(args)
    except OSError as error:
        return 1, b"", "%s\n" % error


def main(argv=None):
    code, out, err = run(sys.argv[1:] if argv is None else argv)
    if out:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    if err:
        sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main())
