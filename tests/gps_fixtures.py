"""Synthetic dashcam GPS data for the tests of tracks.py: NMEA RMC sentences,
Novatek-style freeGPS blocks (with the sentence, or binary fields as doubles
or floats in the layouts exiftool's QuickTimeStream.pl documents) and MP4
files that hold them (with or without the "gps " index box), or NMEA
sentences in a text track. Nothing here comes from a real camera: the files
are only as much of an MP4 as tracks.read_track reads, and every position is
made up.
"""

import calendar
import struct
import time

BLOCK_SIZE = 0x4000
LAYOUT_OFFSETS = {
    # (number, hour, status, lat, lat side, lon, lon side, speed, course, year), as
    # exiftool's ProcessFreeGPS documents them (offsets from the block's size).
    # The Vantrue N4 type: doubles after the clock, the date after them.
    "doubles": ("<d", 0x10, 0x1c, 0x20, 0x28, 0x30, 0x38, 0x40, 0x48, 0x50),
    # Its "Type 2": the same from 0x30.
    "doubles-30": ("<d", 0x30, 0x3c, 0x40, 0x48, 0x50, 0x58, 0x60, 0x68, 0x70),
    # The Novatek "Type 3" (Viofo A119S): the clock and date, then the status
    # and hemispheres as three letters, then floats.
    "floats": ("<f", 0x30, 0x48, 0x4c, 0x49, 0x50, 0x4a, 0x54, 0x58, 0x3c),
    # The Viofo A119 V3: the same from 0x10.
    "floats-10": ("<f", 0x10, 0x28, 0x2c, 0x29, 0x30, 0x2a, 0x34, 0x38, 0x1c),
}


def utc(year, month, day, hour=0, minute=0, second=0.0):
    """Seconds since 1970 of a UTC moment."""
    return calendar.timegm((year, month, day, hour, minute, 0, 0, 0, 0)) + second


def nmea_number(degrees, whole_digits):
    """An NMEA DDMM.MMMM (or DDDMM.MMMM) number of degrees (as text) and its value."""
    value = abs(degrees)
    whole = int(value)
    minutes = (value - whole) * 60.0
    return "%0*d%07.4f" % (whole_digits, whole, minutes), whole * 100 + minutes


def rmc(moment, lat, lon, knots=0.0, course=90.0, status="A", talker="GN", checksum=True,
        year_digits=None):
    """An RMC sentence for a fix at moment (UTC seconds), with its checksum."""
    stamp = time.gmtime(int(moment))
    seconds = stamp.tm_sec + (moment - int(moment))
    clock = "%02d%02d%06.3f" % (stamp.tm_hour, stamp.tm_min, seconds)
    lat_text, _value = nmea_number(lat, 2)
    lon_text, _value = nmea_number(lon, 3)
    date = "%02d%02d%02d" % (stamp.tm_mday, stamp.tm_mon,
                             stamp.tm_year % 100 if year_digits is None else year_digits)
    body = "%sRMC,%s,%s,%s,%s,%s,%s,%.3f,%.2f,%s,,,A" % (
        talker, clock, status, lat_text, "N" if lat >= 0 else "S", lon_text,
        "E" if lon >= 0 else "W", knots, course, date)
    total = 0
    for character in body:
        total ^= ord(character)
    return "$" + body + ("*%02X" % total if checksum else "")


def block(sentence=None, binary=None, layout="doubles", size=BLOCK_SIZE, year=None,
          signed=False):
    """A freeGPS block (bytes from its size on): binary fields (binary: (moment, lat, lon,
    knots, course, status)) laid out as layout, then sentence (NMEA text), the rest zeros.
    year: the year written (default: the moment's, less 2000); signed: the latitude and
    longitude written negative in the south and west too (as some cameras do)."""
    data = bytearray(size)
    struct.pack_into(">I", data, 0, size)
    data[4:12] = b"freeGPS "
    struct.pack_into("<I", data, 12, 0x3f0)
    at = 0x68
    if binary is not None:
        number, clock, status, lat_at, lat_side, lon_at, lon_side, speed_at, course_at, \
            date_at = LAYOUT_OFFSETS[layout]
        moment, lat, lon, knots, course, flag = binary
        stamp = time.gmtime(int(moment))
        struct.pack_into("<3i", data, clock, stamp.tm_hour, stamp.tm_min, stamp.tm_sec)
        data[status:status + 1] = flag.encode("ascii")
        struct.pack_into(number, data, lat_at, nmea_number(lat, 2)[1]
                         * (-1 if signed and lat < 0 else 1))
        data[lat_side:lat_side + 1] = b"N" if lat >= 0 else b"S"
        struct.pack_into(number, data, lon_at, nmea_number(lon, 3)[1]
                         * (-1 if signed and lon < 0 else 1))
        data[lon_side:lon_side + 1] = b"E" if lon >= 0 else b"W"
        struct.pack_into(number, data, speed_at, knots)
        struct.pack_into(number, data, course_at, course)
        struct.pack_into("<3i", data, date_at, stamp.tm_year - 2000 if year is None else year,
                         stamp.tm_mon, stamp.tm_mday)
        # (After the fields, and the camera's three accelerations.)
        at = max(date_at + 12, course_at + struct.calcsize(number)) + 12
    if sentence is not None:
        text = (" " + sentence).encode("ascii")
        data[at:at + len(text)] = text
    return bytes(data)


def box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def full_box(kind, payload, version=0):
    return box(kind, struct.pack(">I", version << 24) + payload)


FTYP = box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")


def _video_trak():
    """A video track that is not a text track (its handler is "vide")."""
    hdlr = full_box(b"hdlr", b"\x00\x00\x00\x00vide" + bytes(12) + b"Video\x00")
    mdhd = full_box(b"mdhd", struct.pack(">IIIIHH", 0, 0, 30000, 30000 * 60, 0, 0))
    return box(b"trak", box(b"mdia", mdhd + hdlr))


def mp4(blocks, index=True, gap=3000, moov_first=False, index_in_udta=False, extra=b""):
    """An MP4 holding blocks (bytes) in its media data, gap bytes of filler before
    each, and (index) a "gps " box in its movie header listing them (in a "udta" box
    with index_in_udta; a block None: an empty entry there, offset and size 0). extra:
    more bytes at the end of the media data."""
    def build(moov_size):
        start = len(FTYP) + (moov_size if moov_first else 0)
        payload = b""
        entries = []
        for item in blocks:
            if item is None:
                entries.append((0, 0))
                continue
            payload += b"\x00" * gap
            entries.append((start + 8 + len(payload), len(item)))
            payload += item
        payload += extra
        mdat = box(b"mdat", payload)
        gps = box(b"gps ", struct.pack(">II", 0x101, len(entries)) + b"".join(
            struct.pack(">II", offset, size) for offset, size in entries))
        inner = _video_trak()
        if index:
            inner += box(b"udta", gps) if index_in_udta else gps
        moov = box(b"moov", full_box(b"mvhd", bytes(96)) + inner)
        return mdat, moov
    mdat, moov = build(0)
    if moov_first:
        mdat, moov = build(len(moov))
        return FTYP + moov + mdat
    return FTYP + mdat + moov


def text_track_mp4(samples, timescale=1000, delta=1000, handler=b"text"):
    """An MP4 whose text track (handler) holds samples (bytes: NMEA text), one each
    delta / timescale seconds, two to a chunk."""
    data = b""
    offsets = []
    sizes = []
    chunks = [samples[index:index + 2] for index in range(0, len(samples), 2)]
    start = len(FTYP) + 8
    for chunk in chunks:
        offsets.append(start + len(data))
        for sample in chunk:
            sizes.append(len(sample))
            data += sample
    mdat = box(b"mdat", data)
    stsd = full_box(b"stsd", struct.pack(">I", 1) + box(b"tx3g", bytes(8)))
    stts = full_box(b"stts", struct.pack(">III", 1, len(samples), delta))
    stsc = full_box(b"stsc", struct.pack(">IIII", 1, 1, 2, 1))
    stsz = full_box(b"stsz", struct.pack(">II", 0, len(sizes)) + b"".join(
        struct.pack(">I", size) for size in sizes))
    stco = full_box(b"stco", struct.pack(">I", len(offsets)) + b"".join(
        struct.pack(">I", offset) for offset in offsets))
    stbl = box(b"stbl", stsd + stts + stsc + stsz + stco)
    minf = box(b"minf", box(b"nmhd", bytes(4)) + stbl)
    hdlr = full_box(b"hdlr", b"\x00\x00\x00\x00" + handler + bytes(12) + b"Text\x00")
    mdhd = full_box(b"mdhd", struct.pack(">IIIIHH", 0, 0, timescale, delta * len(samples),
                                         0, 0))
    trak = box(b"trak", box(b"mdia", mdhd + hdlr + minf))
    moov = box(b"moov", full_box(b"mvhd", bytes(96)) + _video_trak() + trak)
    return FTYP + mdat + moov


def drive_fixes(start, count, lat=-41.2, lon=-150.3, step=0.0002, knots=40.0, first=0):
    """count RMC sentences one a second from start (UTC seconds), moving step degrees
    east each second along a made-up road in the open sea; first: the first second."""
    return [rmc(start + second, lat, lon + step * second, knots=knots, course=90.0)
            for second in range(first, first + count)]
