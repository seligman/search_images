#!/usr/bin/env python3
"""Synthetic camera RAW files, generated in code (for tests of raw_photos.py).

They have the structure of the real formats -- TIFF directories (DNG, ARW,
CR2, NEF, ORF, RW2, ...), Fujifilm's RAF header, Canon's CR3 boxes -- with
small JPEG previews made by Pillow and a few bytes standing in for the
sensor data. No RAW decoder could make a picture of them; nothing here
comes from a real camera or photo library.
"""

import io
import struct

from PIL import Image

# TIFF field types.
BYTE, ASCII, SHORT, LONG, RATIONAL, UNDEFINED, SRATIONAL, IFD = 1, 2, 3, 4, 5, 7, 10, 13
_SIZES = {BYTE: 1, ASCII: 1, SHORT: 2, LONG: 4, RATIONAL: 8, UNDEFINED: 1, SRATIONAL: 8,
          IFD: 4}

CFA = 32803             # PhotometricInterpretation of sensor data
YCBCR = 6


class Blob:
    """Bytes placed in the file; a LONG field holding a Blob gets its offset."""

    def __init__(self, data):
        self.data = bytes(data)


class Ref:
    """A LONG (or IFD) field pointing to the directory of this name."""

    def __init__(self, name):
        self.name = name


def jpeg(size=(96, 64), color=(200, 40, 40), marker=True, quality=85, exif=None,
         progressive=False):
    """JPEG bytes: a plain picture, with a blue square in its top-left corner (to see
    which way it was turned) when marker is set."""
    image = Image.new("RGB", size, color)
    if marker:
        edge = max(2, min(size) // 3)
        image.paste((20, 20, 220), (0, 0, edge, edge))
    buffer = io.BytesIO()
    options = {"quality": quality, "progressive": progressive}
    if exif is not None:
        options["exif"] = exif
    image.save(buffer, "JPEG", **options)
    return buffer.getvalue()


def lossless_jpeg(width=64, height=48):
    """The start of a lossless JPEG (SOF3), as sensor data is stored in many RAW
    files: no JPEG library decodes it, so it is never a preview."""
    sof = struct.pack(">BHHB", 14, height, width, 2) + b"\x01\x11\x00\x02\x11\x00"
    return (b"\xff\xd8" + b"\xff\xc3" + struct.pack(">H", 2 + len(sof)) + sof
            + b"\xff\xda\x00\x08\x02\x01\x00\x02\x00\x00" + b"\x00" * 64 + b"\xff\xd9")


def _rationals(values):
    pairs = []
    for value in values:
        if isinstance(value, tuple):
            pairs.append(value)
        else:
            pairs.append((int(round(value * 10000)), 10000))
    return pairs


def _value_bytes(endian, kind, value):
    """(bytes, deferred) of a field's value; deferred: [(position, Blob or Ref)] to fill."""
    deferred = []
    if kind == ASCII:
        data = value.encode("latin-1") + b"\x00"
    elif kind in (BYTE, UNDEFINED):
        if isinstance(value, Blob):
            data = value.data
        else:
            data = bytes(value)
    elif kind in (RATIONAL, SRATIONAL):
        code = "I" if kind == RATIONAL else "i"
        data = b"".join(struct.pack(endian + code * 2, *pair) for pair in _rationals(value))
    else:
        values = value if isinstance(value, (list, tuple)) else [value]
        code = "H" if kind == SHORT else "I"
        parts = []
        for index, item in enumerate(values):
            if isinstance(item, (Blob, Ref)):
                deferred.append((index * 4, item))
                item = 0
            parts.append(struct.pack(endian + code, item))
        data = b"".join(parts)
    return data, deferred


def _count(kind, value):
    if kind == ASCII:
        return len(value.encode("latin-1")) + 1
    if kind in (BYTE, UNDEFINED):
        return len(value.data if isinstance(value, Blob) else bytes(value))
    if kind in (RATIONAL, SRATIONAL):
        return len(value)
    return len(value) if isinstance(value, (list, tuple)) else 1


def tiff(directories, endian="<", magic=42, extra_header=b"", chain=("ifd0",)):
    """The bytes of a TIFF structure.

    directories: {name: {tag: (type, value)}}; a value may hold Blob (bytes
    placed in the file, the field gets their offset) and Ref (the offset of
    another directory). chain: the names of the directories chained from
    the header (IFD0, IFD1, ...). extra_header: bytes right after the
    8-byte header (CR2's).
    """
    order = b"II" if endian == "<" else b"MM"
    header_size = 8 + len(extra_header)
    out = bytearray(order + struct.pack(endian + "H", magic) + b"\x00" * 4 + extra_header)
    blob_offsets = {}
    # Blobs first (previews, sensor data), then the directories.
    for entries in directories.values():
        for kind, value in entries.values():
            items = value if isinstance(value, (list, tuple)) else [value]
            for item in items:
                if isinstance(item, Blob) and id(item) not in blob_offsets:
                    if len(out) % 2:
                        out += b"\x00"
                    blob_offsets[id(item)] = len(out)
                    out += item.data
    offsets, fixes = {}, []
    for name, entries in directories.items():
        if len(out) % 2:
            out += b"\x00"
        start = len(out)
        offsets[name] = start
        tags = sorted(entries)
        data_at = start + 2 + 12 * len(tags) + 4
        table, data_area = bytearray(), bytearray()
        for index, tag in enumerate(tags):
            kind, value = entries[tag]
            if kind in (BYTE, UNDEFINED) and isinstance(value, Blob):
                # (The bytes are the value itself, as RW2's JpgFromRaw.)
                data, deferred = value.data, []
            else:
                data, deferred = _value_bytes(endian, kind, value)
            entry_at = start + 2 + 12 * index
            if len(data) <= 4:
                field = data + b"\x00" * (4 - len(data))
                fixes.extend((entry_at + 8 + position, target) for position, target in deferred)
            else:
                if len(data_area) % 2:
                    data_area += b"\x00"
                where = data_at + len(data_area)
                field = struct.pack(endian + "I", where)
                fixes.extend((where + position, target) for position, target in deferred)
                data_area += data
            table += struct.pack(endian + "HHI", tag, kind, _count(kind, value)) + field
        out += struct.pack(endian + "H", len(tags)) + table + b"\x00" * 4 + data_area
    for position, target in fixes:
        value = blob_offsets[id(target)] if isinstance(target, Blob) else offsets[target.name]
        out[position:position + 4] = struct.pack(endian + "I", value)
    out[4:8] = struct.pack(endian + "I", offsets[chain[0]])
    for first, second in zip(chain, chain[1:]):
        count = struct.unpack(endian + "H", out[offsets[first]:offsets[first] + 2])[0]
        at = offsets[first] + 2 + 12 * count
        out[at:at + 4] = struct.pack(endian + "I", offsets[second])
    assert header_size <= offsets[chain[0]]
    return bytes(out)


def exif_entries(date="2021:06:13 13:36:50", make="Camco", model="Model R1",
                 lens="Zoom 18-55mm", exposure=(1, 250), fnumber=(56, 10), iso=200,
                 focal=(35, 1), offset=None):
    """Exif-directory fields of a shot."""
    entries = {0x9003: (ASCII, date), 0x9004: (ASCII, date),
               0x829A: (RATIONAL, [exposure]), 0x829D: (RATIONAL, [fnumber]),
               0x8827: (SHORT, iso), 0x920A: (RATIONAL, [focal])}
    if lens:
        entries[0xA434] = (ASCII, lens)
    if offset:
        entries[0x9011] = (ASCII, offset)
    return entries


def gps_entries(lat=48.8584, lon=2.2945):
    """GPS-directory fields of a position (decimal degrees; S and W negative)."""
    def dms(value):
        value = abs(value)
        degrees = int(value)
        minutes = int((value - degrees) * 60)
        seconds = (value - degrees - minutes / 60.0) * 3600
        return [(degrees, 1), (minutes, 1), (int(round(seconds * 1000)), 1000)]
    return {0: (BYTE, b"\x02\x03\x00\x00"), 1: (ASCII, "N" if lat >= 0 else "S"),
            2: (RATIONAL, dms(lat)), 3: (ASCII, "E" if lon >= 0 else "W"),
            4: (RATIONAL, dms(lon))}


def arw(preview=(96, 64), thumb=(32, 24), orientation=1, make="SONY", model="ILCE-X",
        date="2021:06:13 13:36:50", gps=None, lens="E 18-55mm", endian="<"):
    """A Sony ARW-like file: IFD0 with the preview (JPEGInterchangeFormat), its EXIF and
    the sensor data in a SubIFD, a thumbnail in IFD1."""
    sensor = {0x00FE: (LONG, 0), 0x0100: (SHORT, 120), 0x0101: (SHORT, 80),
              0x0103: (SHORT, 32767), 0x0106: (SHORT, CFA),
              0x0111: (LONG, Blob(b"\x5a" * 512)), 0x0117: (LONG, 512)}
    ifd0 = {0x00FE: (LONG, 1), 0x0103: (SHORT, 6), 0x010F: (ASCII, make),
            0x0110: (ASCII, model), 0x0112: (SHORT, orientation),
            0x0132: (ASCII, date), 0x014A: (LONG, Ref("sensor")),
            0x8769: (LONG, Ref("exif"))}
    directories = {"ifd0": ifd0, "sensor": sensor,
                   "exif": exif_entries(date=date, make=make, model=model, lens=lens)}
    if preview:
        data = jpeg(preview)
        ifd0[0x0201] = (LONG, Blob(data))
        ifd0[0x0202] = (LONG, len(data))
    if gps:
        ifd0[0x8825] = (LONG, Ref("gps"))
        directories["gps"] = gps_entries(*gps)
    chain = ("ifd0",)
    if thumb:
        small = jpeg(thumb, color=(40, 160, 40))
        directories["ifd1"] = {0x0103: (SHORT, 6), 0x0201: (LONG, Blob(small)),
                               0x0202: (LONG, len(small))}
        chain = ("ifd0", "ifd1")
    return tiff(directories, endian=endian, chain=chain)


def dng(preview=(128, 96), orientation=1, make="Camco", model="Model D", gps=None,
        date="2022:01:02 10:20:30", endian=">"):
    """A DNG-like file (big-endian by default): DNGVersion, an uncompressed RGB thumbnail
    in IFD0 (not a JPEG, so no preview), the sensor data and a JPEG preview (a strip,
    compression 7) in SubIFDs. preview None: no preview at all."""
    ifd0 = {0x00FE: (LONG, 1), 0x0100: (SHORT, 4), 0x0101: (SHORT, 4),
            0x0103: (SHORT, 1), 0x0106: (SHORT, 2), 0x010F: (ASCII, make),
            0x0110: (ASCII, model), 0x0111: (LONG, Blob(b"\x80" * 48)),
            0x0112: (SHORT, orientation), 0x0117: (LONG, 48),
            0xC612: (BYTE, b"\x01\x04\x00\x00"), 0x8769: (LONG, Ref("exif"))}
    subs = [Ref("sensor")]
    directories = {"ifd0": ifd0,
                   "sensor": {0x00FE: (LONG, 0), 0x0100: (LONG, 200), 0x0101: (LONG, 100),
                              0x0103: (SHORT, 7), 0x0106: (SHORT, CFA),
                              0x0111: (LONG, Blob(lossless_jpeg(200, 100))),
                              0x0117: (LONG, len(lossless_jpeg(200, 100)))},
                   "exif": exif_entries(date=date, lens="")}
    if preview:
        data = jpeg(preview, color=(60, 60, 200))
        directories["preview"] = {0x00FE: (LONG, 1), 0x0100: (SHORT, preview[0]),
                                  0x0101: (SHORT, preview[1]), 0x0103: (SHORT, 7),
                                  0x0106: (SHORT, YCBCR), 0x0111: (LONG, Blob(data)),
                                  0x0117: (LONG, len(data))}
        subs.append(Ref("preview"))
    ifd0[0x014A] = (LONG, subs)
    if gps:
        ifd0[0x8825] = (LONG, Ref("gps"))
        directories["gps"] = gps_entries(*gps)
    return tiff(directories, endian=endian)


def nef(preview=(160, 106), make="NIKON CORPORATION", model="NIKON D9", endian="<"):
    """A NEF-like file: the preview (JpgFromRaw) and the sensor data in SubIFDs."""
    data = jpeg(preview, color=(200, 200, 40))
    directories = {
        "ifd0": {0x00FE: (LONG, 1), 0x010F: (ASCII, make), 0x0110: (ASCII, model),
                 0x014A: (LONG, [Ref("jpg"), Ref("sensor")]), 0x8769: (LONG, Ref("exif"))},
        "jpg": {0x00FE: (LONG, 1), 0x0103: (SHORT, 6), 0x0201: (LONG, Blob(data)),
                0x0202: (LONG, len(data))},
        "sensor": {0x00FE: (LONG, 0), 0x0100: (LONG, 300), 0x0101: (LONG, 200),
                   0x0103: (SHORT, 34713), 0x0106: (SHORT, CFA),
                   0x0111: (LONG, Blob(b"\x11" * 64)), 0x0117: (LONG, 64)},
        "exif": exif_entries(make=make, model=model),
    }
    return tiff(directories, endian=endian)


def cr2(preview=(120, 80), make="Canon", model="Canon EOS 9", date="2020:05:06 07:08:09"):
    """A CR2-like file: "CR" after the header, the preview as IFD0's strip (old-style
    JPEG) and the sensor data as a lossless JPEG in IFD3."""
    data = jpeg(preview, color=(240, 120, 0))
    raw = lossless_jpeg(400, 300)
    directories = {
        "ifd0": {0x0100: (SHORT, preview[0]), 0x0101: (SHORT, preview[1]),
                 0x0103: (SHORT, 6), 0x010F: (ASCII, make), 0x0110: (ASCII, model),
                 0x0111: (LONG, Blob(data)), 0x0117: (LONG, len(data)),
                 0x0132: (ASCII, date), 0x8769: (LONG, Ref("exif"))},
        "exif": exif_entries(date=date, make=make, model=model),
        "ifd3": {0x0103: (SHORT, 6), 0x0111: (LONG, Blob(raw)), 0x0117: (LONG, len(raw))},
    }
    return tiff(directories, extra_header=b"CR\x02\x00\x00\x00\x00\x00",
                chain=("ifd0", "ifd3"))


def rw2(preview=(100, 75), make="Panasonic", model="DMC-Z1"):
    """An RW2-like file ("IIU" header): the preview is the value of tag 0x2E."""
    data = jpeg(preview, color=(10, 150, 150))
    directories = {"ifd0": {0x002E: (UNDEFINED, Blob(data)), 0x010F: (ASCII, make),
                            0x0110: (ASCII, model), 0x8769: (LONG, Ref("exif"))},
                   "exif": exif_entries(make=make, model=model)}
    return tiff(directories, magic=0x55)


def orf(preview=(80, 60), make="OLYMPUS IMAGING CORP.", model="E-M9"):
    """An ORF-like file ("IIRO" header) with a preview a directory points to."""
    data = jpeg(preview, color=(150, 10, 150))
    directories = {"ifd0": {0x010F: (ASCII, make), 0x0110: (ASCII, model),
                            0x0201: (LONG, Blob(data)), 0x0202: (LONG, len(data)),
                            0x8769: (LONG, Ref("exif"))},
                   "exif": exif_entries(make=make, model=model)}
    return tiff(directories, magic=0x4F52)


def raf(preview=(96, 64), make="FUJIFILM", model="X-T9", date="2019:09:09 09:09:09",
        orientation=1):
    """A RAF-like file: its header points to a JPEG holding the EXIF."""
    # (The EXIF is built here, not with Pillow's Exif: older Pillow versions
    # leave out an Exif directory added with get_ifd.)
    exif = tiff({"ifd0": {0x010F: (ASCII, make), 0x0110: (ASCII, model),
                          0x0112: (SHORT, orientation), 0x8769: (LONG, Ref("exif"))},
                 "exif": {0x9003: (ASCII, date)}})
    data = jpeg(preview, color=(90, 90, 90), exif=b"Exif\x00\x00" + exif)
    header = bytearray(b"FUJIFILMCCD-RAW 0201FF129502" + model.encode("ascii").ljust(32, b"\x00"))
    header += b"\x00" * (84 - len(header))
    start = 160
    header += struct.pack(">II", start, len(data)) + struct.pack(">IIII", 0, 0, 0, 0)
    header += b"\x00" * (start - len(header))
    return bytes(header) + data + b"\x00" * 128


def _box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def cr3(prvw=(160, 106), thumb=(40, 30), full=(200, 133), make="Canon", model="Canon EOS R9",
        date="2023:03:04 05:06:07", gps=None, orientation=1):
    """A CR3-like file (ISO base media boxes): the EXIF in CMT1/CMT2/CMT4, a THMB
    thumbnail, a PRVW preview and a full-size JPEG as the first track's sample."""
    cmt1 = tiff({"ifd0": {0x010F: (ASCII, make), 0x0110: (ASCII, model),
                          0x0112: (SHORT, orientation)}})
    cmt2 = tiff({"ifd0": exif_entries(date=date, make=make, model=model)})
    canon = [_box(b"CMT1", cmt1), _box(b"CMT2", cmt2)]
    if gps:
        canon.append(_box(b"CMT4", tiff({"ifd0": gps_entries(*gps)})))
    if thumb:
        small = jpeg(thumb, color=(30, 30, 30))
        canon.append(_box(b"THMB", struct.pack(">BBBBHHIHH", 0, 0, 0, 0, thumb[0], thumb[1],
                                               len(small), 1, 0) + small))
    canon_box = _box(b"uuid", bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
                     + b"".join(canon))
    full_jpeg = jpeg(full, color=(220, 220, 220)) if full else b""

    def build(sample_at):
        stbl = _box(b"stbl", _box(b"stsz", struct.pack(">IIII", 0, 0, 1, len(full_jpeg)))
                    + _box(b"co64", struct.pack(">IIQ", 0, 1, sample_at)))
        trak = _box(b"trak", _box(b"mdia", _box(b"minf", stbl)))
        moov = _box(b"moov", canon_box + (trak if full else b""))
        ftyp = _box(b"ftyp", b"crx \x00\x00\x00\x01crx isom")
        parts = [ftyp, moov]
        if prvw:
            preview = jpeg(prvw, color=(250, 250, 0))
            parts.append(_box(b"uuid", bytes.fromhex("eaf42b5e1c984b88b9fbb7dc406e4d16")
                              + b"\x00" * 8 + _box(b"PRVW", struct.pack(
                                  ">IHHHHI", 0, 1, prvw[0], prvw[1], 1, len(preview))
                                  + preview)))
        head = b"".join(parts)
        return head, head + _box(b"mdat", full_jpeg)

    head, _ = build(0)
    return build(len(head) + 8)[1]
