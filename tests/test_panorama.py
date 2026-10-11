"""Tests for 360-degree photos: detection, stitching and what read_image returns.

The fixtures are drawn in code (see imagegen.py): an unstitched dual-fisheye
frame (two striped circles on black), a stitched panorama with and without
GPano XMP, and look-alikes that must not be taken for 360 photos.
"""

import io
import math
import shutil
import tempfile
import unittest
from unittest import mock

import support

import common
import imageinfo
import prompts

if support.pillow_available():
    from PIL import Image, ImageDraw
    import describe
    import imagegen
    import images
else:
    Image = ImageDraw = describe = imagegen = images = None

needs_pillow = unittest.skipIf(Image is None, "Pillow is not installed")


def new_dir():
    path = tempfile.mkdtemp(prefix="search-images-360-")
    return path


def hamming(first, second):
    return bin(int(first, 16) ^ int(second, 16)).count("1")


def fake_jpeg(*segments):
    """JPEG-shaped bytes: SOI, the given (marker, payload) segments, then a scan."""
    data = b"\xff\xd8"
    for marker, payload in segments:
        data += bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload
    return data + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x12\x34" * 10 + b"\xff\xd9"


TAG_IMAGE_DESCRIPTION = 0x010E

# What Insta360 cameras write into EXIF: Make, Model, and the file's own
# name as ImageDescription (cut short on some models).
ONE_R_INSP = {"Make": "Arashi Vision", "Model": "Insta360 OneR",
              "ImageDescription": "IMG_20190514_142301_00_001.insp"}
X4_JPG = {"Make": "Arashi Vision", "Model": "Insta360 X4",
          "ImageDescription": "DCIM\\Camera01\\IMG_20190514_142301_00_005.jp"}


def exif_of(make, model="", description=""):
    exif = imagegen.make_exif(make=make, model=model, date=None, offset=None)
    if description:
        exif[TAG_IMAGE_DESCRIPTION] = description
    return exif


def lens_position(longitude, latitude, size, params):
    """Where the documented lens model puts a direction in a dual-fisheye frame.

    The test's own copy of the model in images.INSTA360_PARAMS: equidistant
    circles of radius H/2 at the quarter points (moved by the offsets), the
    front lens on the "front" half, each half turned counter-clockwise by
    its rotation.
    """
    width, height = size
    radius = height / 2.0
    lam, phi = math.radians(longitude), math.radians(latitude)
    x, y, z = math.cos(phi) * math.sin(lam), math.sin(phi), math.cos(phi) * math.cos(lam)
    front = 0 if params["front"] == "left" else 1
    half = front if z >= 0 else 1 - front
    if half != front:
        x, z = -x, -z
    theta = math.acos(max(-1.0, min(1.0, z)))
    angle = math.atan2(y, x) + math.radians(params["rotation"][half])
    dx, dy = params["offsets"][half]
    cx = (2 * half + 1) * width / 4.0 + dx * radius
    cy = height / 2.0 + dy * radius
    rho = radius * theta / math.radians(params["fov"] / 2.0)
    return cx + rho * math.cos(angle), cy - rho * math.sin(angle)


def panorama_position(longitude, latitude, size):
    width, height = size
    return ((longitude / 360.0 + 0.5) * width, (0.5 - latitude / 180.0) * height)


def red_pixels(image):
    image = image.convert("RGB")
    width, height = image.size
    data = image.tobytes()
    found = []
    for index in range(0, len(data), 3):
        r, g, b = data[index], data[index + 1], data[index + 2]
        if r > 150 and g < 100 and b < 100:
            pixel = index // 3
            found.append((pixel % width + 0.5, pixel // width + 0.5))
    return found


def red_centroid(image):
    found = red_pixels(image)
    if not found:
        return None
    return (sum(p[0] for p in found) / len(found), sum(p[1] for p in found) / len(found))


class AspectAndMetadataTest(unittest.TestCase):

    def test_panorama_aspect_tolerance(self):
        for size in ((2000, 1000), (1960, 1000), (2040, 1000), (6080, 3040), (11904, 5952)):
            self.assertTrue(imageinfo.is_panorama_aspect(*size), size)
        for size in ((1950, 1000), (2050, 1000), (1000, 1000), (1000, 2000), (0, 0),
                     (100, 0), (None, 5)):
            self.assertFalse(imageinfo.is_panorama_aspect(*size), size)

    def test_gpano_projection_in_both_forms(self):
        element = (b"<x:xmpmeta><rdf:Description><GPano:ProjectionType>equirectangular"
                   b"</GPano:ProjectionType></rdf:Description></x:xmpmeta>")
        attribute = (b"<rdf:Description xmlns:GPano='http://ns.google.com/photos/1.0/panorama/'"
                     b" GPano:ProjectionType=\"Equirectangular\" GPano:UsePanoramaViewer='True'/>")
        single = b"<rdf:Description GPano:ProjectionType = 'cylindrical'/>"
        self.assertEqual(imageinfo.xmp_projection(element), "equirectangular")
        self.assertEqual(imageinfo.xmp_projection(attribute), "equirectangular")
        self.assertEqual(imageinfo.xmp_projection(attribute.decode("ascii")), "equirectangular")
        self.assertEqual(imageinfo.xmp_projection(single), "cylindrical")
        self.assertEqual(imageinfo.xmp_projection(b"<x:xmpmeta/>"), "")
        self.assertEqual(imageinfo.xmp_projection(b""), "")
        self.assertEqual(imageinfo.xmp_projection(None), "")

    def test_partial_panoramas_do_not_cover_the_sphere(self):
        full = (b"<GPano:CroppedAreaImageWidthPixels>8000</GPano:CroppedAreaImageWidthPixels>"
                b"<GPano:FullPanoWidthPixels>8000</GPano:FullPanoWidthPixels>"
                b"<GPano:CroppedAreaImageHeightPixels>4000</GPano:CroppedAreaImageHeightPixels>"
                b"<GPano:FullPanoHeightPixels>4000</GPano:FullPanoHeightPixels>")
        partial = (b"GPano:CroppedAreaImageWidthPixels='8000' GPano:FullPanoWidthPixels='8000' "
                   b"GPano:CroppedAreaImageHeightPixels='1600' GPano:FullPanoHeightPixels='4000'")
        self.assertTrue(imageinfo.xmp_covers_sphere(full))
        self.assertTrue(imageinfo.xmp_covers_sphere(
            b"<GPano:ProjectionType>x</GPano:ProjectionType>"))
        self.assertFalse(imageinfo.xmp_covers_sphere(partial))

    def test_jpeg_xmp_is_found_before_the_image_data(self):
        packet = b"<x:xmpmeta>GPano</x:xmpmeta>"
        data = fake_jpeg((0xE0, b"JFIF\x00\x01\x02"), (0xE1, b"Exif\x00\x00II*\x00"),
                         (0xE1, imageinfo._XMP_HEADER + packet))
        self.assertEqual(imageinfo.jpeg_xmp(data), packet)
        self.assertEqual(imageinfo.jpeg_xmp(fake_jpeg((0xE1, b"Exif\x00\x00"))), b"")
        self.assertEqual(imageinfo.jpeg_xmp(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40), b"")
        self.assertEqual(imageinfo.jpeg_xmp(b""), b"")
        # A damaged segment length ends the search instead of failing.
        self.assertEqual(imageinfo.jpeg_xmp(b"\xff\xd8\xff\xe1\x00"), b"")

    def test_360_cameras(self):
        yes = [("Arashi Vision", "Insta360 OneR"), ("Insta360", "X3"),
               ("RICOH", "RICOH THETA Z1"), ("GoPro", "GoPro Max"),
               ("SAMSUNG", "SM-C200 Gear 360"), ("arashi vision\x00\x00", "insta360 x4")]
        no = [("Apple", "iPhone 12"), ("RICOH IMAGING COMPANY, LTD.", "GR III"),
              ("GoPro", "HERO11 Black"), ("SAMSUNG", "SM-G991B"), ("", "")]
        for make, model in yes:
            self.assertTrue(imageinfo.is_360_camera({"Make": make, "Model": model}), make)
        for make, model in no:
            self.assertFalse(imageinfo.is_360_camera({"Make": make, "Model": model}), make)
        self.assertTrue(imageinfo.is_insta360({"Make": "Arashi Vision"}))
        self.assertFalse(imageinfo.is_insta360({"Make": "RICOH", "Model": "THETA"}))
        self.assertFalse(imageinfo.is_360_camera(None))

    def test_insta360_file_type(self):
        self.assertEqual(imageinfo.insta360_file_type(ONE_R_INSP), "insp")
        self.assertEqual(imageinfo.insta360_file_type(X4_JPG), "jpg")
        cases = {"DCIM\\Camera01\\IMG_20190514_142301_00_004.ins": "insp",
                 "IMG_20190514_142301_00_004.INSP": "insp",
                 "IMG_20190514_142301_00_005.jpg\x00\x00": "jpg",
                 "": "", "Insta360 OneR": "", "IMG_1.insv": ""}
        for description, want in cases.items():
            exif = {"Make": "Arashi Vision", "ImageDescription": description}
            self.assertEqual(imageinfo.insta360_file_type(exif), want, description)
        # Only an Insta360 camera's word counts.
        self.assertEqual(imageinfo.insta360_file_type(
            {"Make": "Apple", "ImageDescription": "IMG_1.insp"}), "")
        self.assertEqual(imageinfo.insta360_file_type(None), "")

    def test_insp_files_are_images(self):
        self.assertIn(".insp", common.IMAGE_EXTENSIONS)
        self.assertTrue(common.is_image_file("IMG_20190514_142301_00_001.INSP"))
        for raw in ("photo.dng", "photo.arw", "photo.cr2", "clip.insv"):
            self.assertFalse(common.is_image_file(raw), raw)

    def test_prompt_note(self):
        for projection in ("equirectangular", "dual_fisheye"):
            note = prompts.projection_note(projection)
            self.assertIn("360-degree", note)
            self.assertIn("equirectangular", note)
        for projection in ("", "?", None):
            self.assertEqual(prompts.projection_note(projection), "")


@needs_pillow
class DetectProjectionTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dual = imagegen.dual_fisheye_image((512, 256))
        cls.panorama = imagegen.equirect_image((512, 256))
        cls.insta = {"Make": "Arashi Vision", "Model": "Insta360 OneR"}
        cls.apple = {"Make": "Apple", "Model": "iPhone 12"}
        cls.night = imagegen.night_panorama((512, 256))

    def detect(self, image, exif=None, xmp=b"", name="photo.jpg"):
        return images.detect_projection(image, exif or {}, xmp, name)

    def test_dual_fisheye_needs_an_unstitched_original(self):
        self.assertEqual(self.detect(self.dual, name="IMG_1.insp"), "dual_fisheye")
        self.assertEqual(self.detect(self.dual, self.insta, name="IMG_1.INSP"), "dual_fisheye")
        # A renamed .insp: the camera's EXIF still names the original.
        self.assertEqual(self.detect(self.dual, ONE_R_INSP, name="IMG_1.jpg"), "dual_fisheye")
        self.assertEqual(self.detect(self.dual, ONE_R_INSP, name=""), "dual_fisheye")
        # The maker and dark corners alone are not enough.
        self.assertEqual(self.detect(self.dual, self.insta, name="IMG_1.jpg"), "")
        self.assertTrue(images.has_dual_fisheye_signature(self.dual))

    def test_dark_insta360_panorama_is_not_stitched_again(self):
        # A night panorama's corners are as dark as a fisheye frame's.
        self.assertTrue(images.has_dual_fisheye_signature(self.night))
        # The camera stitched it (its EXIF says .jpg): a panorama.
        self.assertEqual(self.detect(self.night, X4_JPG, name="IMG_5.jpg"), "equirectangular")
        # Without the camera's word it is left as it is, never stitched.
        self.assertEqual(self.detect(self.night, self.insta, name="IMG_5.jpg"), "")
        self.assertEqual(self.detect(self.night, self.apple, name="IMG_5.jpg"), "")
        self.assertEqual(self.detect(self.panorama, X4_JPG, name="IMG_5.jpg"),
                         "equirectangular")

    def test_night_photo_with_dark_corners_is_not_a_fisheye(self):
        # Dark corners and a bright middle, but an ordinary camera and name.
        self.assertEqual(self.detect(self.dual, self.apple, name="night.jpg"), "")
        self.assertEqual(self.detect(self.dual, {}, name="night.jpg"), "")
        self.assertEqual(self.detect(self.dual, {}, name="night.png"), "")

    def test_gpano_equirectangular_in_both_forms(self):
        for form in ("element", "attribute"):
            xmp = imagegen.gpano_xmp(form=form)
            self.assertEqual(self.detect(self.panorama, {}, xmp), "equirectangular", form)
            # The XMP wins over the dark-corner look and the .insp name.
            self.assertEqual(self.detect(self.dual, self.insta, xmp, "x.insp"), "equirectangular")

    def test_plain_wide_photo_is_not_360(self):
        self.assertEqual(self.detect(self.panorama, self.apple), "")
        self.assertEqual(self.detect(self.panorama, {}), "")

    def test_known_360_camera_without_xmp(self):
        for exif in (self.insta, {"Make": "RICOH", "Model": "RICOH THETA S"}):
            self.assertEqual(self.detect(self.panorama, exif), "equirectangular", exif)
        # A stitched .insp is not taken for an unstitched one.
        self.assertEqual(self.detect(self.panorama, self.insta, name="IMG_1.insp"),
                         "equirectangular")
        # A THETA frame that looks like two circles is left as it is.
        theta = {"Make": "RICOH", "Model": "RICOH THETA Z1"}
        self.assertEqual(self.detect(self.dual, theta), "")

    def test_other_projections_and_partial_panoramas(self):
        cylindrical = imagegen.gpano_xmp("cylindrical")
        partial = imagegen.gpano_xmp(cropped=(512, 100), full=(512, 256))
        self.assertEqual(self.detect(self.panorama, self.insta, cylindrical), "")
        self.assertEqual(self.detect(self.panorama, {}, partial), "")

    def test_wrong_aspect_is_never_360(self):
        square = imagegen.dual_fisheye_image((256, 256), offsets=((0, 0),))
        self.assertEqual(self.detect(square, self.insta, name="single.insp"), "")
        wide = self.panorama.resize((800, 256))
        self.assertEqual(self.detect(wide, {}, imagegen.gpano_xmp()), "")

    def test_over_exposed_frames(self):
        # Lens flare lights the corners: still a .insp frame, but no longer
        # clear enough for the stricter test used without the extension.
        bright = imagegen.dual_fisheye_image((512, 256), background=(70, 40, 60))
        self.assertFalse(images.has_dual_fisheye_signature(bright))
        self.assertTrue(images.has_dual_fisheye_signature(bright, loose=True))
        self.assertEqual(self.detect(bright, self.insta, name="IMG_2.insp"), "dual_fisheye")
        self.assertEqual(self.detect(bright, ONE_R_INSP, name=""), "dual_fisheye")
        self.assertEqual(self.detect(bright, self.insta, name="IMG_2.jpg"), "")

    def test_black_frame_is_not_a_fisheye(self):
        black = Image.new("RGB", (512, 256))
        self.assertFalse(images.has_dual_fisheye_signature(black, loose=True))
        self.assertEqual(self.detect(black, {}, name="IMG_3.insp"), "")
        self.assertNotEqual(self.detect(black, self.insta, name="IMG_3.insp"), "dual_fisheye")


@needs_pillow
class StitchingTest(unittest.TestCase):

    SIZE = (1024, 512)

    def params(self):
        return images.INSTA360_PARAMS

    def stitch_marker(self, longitude, latitude, out_width=1024):
        x, y = lens_position(longitude, latitude, self.SIZE, self.params())
        frame = imagegen.dual_fisheye_image(self.SIZE, offsets=self.params()["offsets"],
                                            markers=[(x, y, 5)])
        return images.dual_fisheye_to_equirect(frame, out_width)

    def assert_marker_at(self, panorama, longitude, latitude, tolerance=3.0):
        found = red_centroid(panorama)
        self.assertIsNotNone(found, "marker missing")
        want = panorama_position(longitude, latitude, panorama.size)
        self.assertLessEqual(abs(found[0] - want[0]), tolerance, (found, want))
        self.assertLessEqual(abs(found[1] - want[1]), tolerance, (found, want))

    def test_output_size(self):
        frame = imagegen.dual_fisheye_image((512, 256))
        self.assertEqual(images.dual_fisheye_to_equirect(frame, 1000).size, (1000, 500))
        self.assertEqual(images.dual_fisheye_to_equirect(frame, 1002).size, (1000, 500))
        self.assertEqual(images.dual_fisheye_to_equirect(frame).size,
                         (images.PREVIEW_EDGE, images.PREVIEW_EDGE // 2))
        self.assertEqual(images.dual_fisheye_to_equirect(frame.convert("L"), 64).mode, "RGB")

    def test_front_lens_centre_is_the_middle_of_the_panorama(self):
        self.assert_marker_at(self.stitch_marker(0, 0), 0, 0)

    def test_scene_up_is_the_outer_edge_of_each_circle(self):
        # Rotation 90 / 270: "up" points to the frame's left and right edges.
        frame_x, frame_y = lens_position(0, 60, self.SIZE, self.params())
        self.assertLess(frame_x, self.SIZE[0] / 4.0)
        self.assertAlmostEqual(frame_y, self.SIZE[1] / 2.0, delta=1)
        self.assert_marker_at(self.stitch_marker(0, 60), 0, 60)

    def test_directions_land_where_the_model_says(self):
        for longitude, latitude in ((45, 20), (-60, -35), (120, 10), (-135, -30), (80, 45)):
            self.assert_marker_at(self.stitch_marker(longitude, latitude), longitude, latitude)

    def test_back_lens_centre_is_at_the_panorama_edges(self):
        panorama = self.stitch_marker(180, 0)
        found = red_pixels(panorama)
        self.assertTrue(found)
        width, height = panorama.size
        for x, y in found:
            self.assertTrue(x < 12 or x > width - 12, x)
            self.assertLess(abs(y - height / 2.0), 8)

    def test_each_cell_comes_from_one_lens(self):
        # The two halves have different colours; the seams are sharp lines at
        # longitude -90 and 90 degrees.
        frame = imagegen.dual_fisheye_image(self.SIZE, offsets=self.params()["offsets"])
        panorama = images.dual_fisheye_to_equirect(frame, 512).convert("RGB")
        pixels = panorama.load()
        for y in (64, 128, 192):
            left_of_seam, right_of_seam = pixels[126, y], pixels[129, y]
            # Front (left half) is green-based, back (right half) blue-based.
            self.assertGreater(right_of_seam[1] - right_of_seam[2], -40)
            self.assertGreater(left_of_seam[2], left_of_seam[1] - 40)
        black = sum(1 for x in range(0, 512, 7) for y in range(0, 256, 7)
                    if max(pixels[x, y]) < 30)
        self.assertEqual(black, 0)      # nothing from outside the circles

    def test_lens_circles_default_and_measured(self):
        frame = imagegen.dual_fisheye_image(self.SIZE)
        offsets = self.params()["offsets"]
        default = images.find_lens_circles(frame)
        self.assertAlmostEqual(default[0][0], 256 + offsets[0][0] * 256, places=3)
        self.assertAlmostEqual(default[0][2], 256)
        self.assertAlmostEqual(default[1][0], 768 + offsets[1][0] * 256, places=3)
        # Smaller circles with a black border all round are measured.
        small = imagegen.dual_fisheye_image(self.SIZE, radius=200, offsets=((0.05, -0.04),
                                                                           (-0.03, 0.02)))
        circles = images.find_lens_circles(small)
        for (cx, cy, r), (want_x, want_y) in zip(circles, ((256 + 10, 256 - 8),
                                                           (768 - 6, 256 + 4))):
            self.assertAlmostEqual(r, 200, delta=5)
            self.assertAlmostEqual(cx, want_x, delta=4)
            self.assertAlmostEqual(cy, want_y, delta=4)

    def test_a_dark_object_at_the_rim_does_not_spoil_the_circle(self):
        frame = imagegen.dual_fisheye_image(self.SIZE, radius=200)
        clean = images.find_lens_circles(frame)
        # Something dark (a selfie stick) across the left circle's bottom edge.
        ImageDraw.Draw(frame).rectangle([237, 420, 277, 456], fill=(5, 5, 5))
        circles = images.find_lens_circles(frame)
        for half in (0, 1):
            for got, want in zip(circles[half], clean[half]):
                self.assertAlmostEqual(got, want, delta=2)
        self.assertAlmostEqual(circles[0][2], 200, delta=4)

    def test_one_measured_circle_is_used_for_both_lenses(self):
        frame = imagegen.dual_fisheye_image(self.SIZE, radius=200, offsets=((0.05, -0.04),
                                                                           (0.0, 0.0)))
        # Lens flare reaches the right half's edge: that circle cannot be measured.
        ImageDraw.Draw(frame).rectangle([1000, 200, 1023, 300], fill=(250, 250, 250))
        (left_x, left_y, left_r), (right_x, right_y, right_r) = images.find_lens_circles(frame)
        self.assertAlmostEqual(left_x, 256 + 10, delta=3)
        self.assertAlmostEqual(left_y, 256 - 8, delta=3)
        self.assertAlmostEqual(left_r, 200, delta=4)
        self.assertAlmostEqual(right_x, left_x + 512, places=6)
        self.assertAlmostEqual(right_y, left_y, places=6)
        self.assertAlmostEqual(right_r, left_r, places=6)

    def test_measured_circles_are_used(self):
        params = dict(self.params(), offsets=((0.0, 0.0), (0.0, 0.0)))
        size = self.SIZE
        radius = 200.0
        x, y = lens_position(30, 15, size, dict(params))
        # Scale the model's position to the smaller circle.
        cx, cy = size[0] / 4.0, size[1] / 2.0
        x, y = cx + (x - cx) * radius / 256.0, cy + (y - cy) * radius / 256.0
        frame = imagegen.dual_fisheye_image(size, radius=radius, markers=[(x, y, 4)])
        panorama = images.dual_fisheye_to_equirect(frame, 1024, params)
        self.assert_marker_at(panorama, 30, 15)

    def test_front_view(self):
        panorama = imagegen.equirect_image((1024, 512), marker=True)
        view = images.front_view(panorama)
        self.assertEqual(view.size, (320, 240))
        centre = red_centroid(view)
        self.assertAlmostEqual(centre[0], 160, delta=2)
        self.assertAlmostEqual(centre[1], 120, delta=2)
        wider = images.equirect_view(panorama, (400, 400), fov=90, yaw=90)
        self.assertEqual(wider.size, (400, 400))
        self.assertIsNone(red_centroid(wider))   # looking to the right

    def test_view_across_the_panorama_edge(self):
        # Red at the left and right edges (longitude 180), blue elsewhere:
        # looking backwards shows one red patch in the middle, not a smear
        # of the whole panorama.
        panorama = Image.new("RGB", (1024, 512), (40, 60, 200))
        draw = ImageDraw.Draw(panorama)
        draw.rectangle([0, 236, 19, 275], fill=imagegen.RED)
        draw.rectangle([1004, 236, 1023, 275], fill=imagegen.RED)
        view = images.equirect_view(panorama, (320, 240), fov=60, yaw=180)
        centre = red_centroid(view)
        self.assertAlmostEqual(centre[0], 160, delta=2)
        self.assertAlmostEqual(centre[1], 120, delta=2)
        # 40 px of 1024 is 14 degrees: 68 px of a 60 degree wide view.
        xs = [x for x, _ in red_pixels(view)]
        self.assertAlmostEqual(max(xs) - min(xs), 68, delta=4)
        for yaw in (179, -179, 540):
            self.assertIsNotNone(red_centroid(images.equirect_view(
                panorama, (160, 120), fov=60, yaw=yaw)), yaw)

    def test_view_of_a_pole(self):
        # Sky above and ground below 60 degrees are plain: looking up or down
        # must show nothing else.
        sky, ground = (90, 140, 230), (110, 90, 40)
        panorama = imagegen.equirect_image((1024, 512))
        draw = ImageDraw.Draw(panorama)
        draw.rectangle([0, 0, 1023, 85], fill=sky)
        draw.rectangle([0, 426, 1023, 511], fill=ground)
        for pitch, yaw, want in ((90, 0, sky), (90, 137, sky), (85, 180, sky),
                                 (-90, 0, ground), (-85, -45, ground)):
            view = images.equirect_view(panorama, (200, 200), fov=30, yaw=yaw, pitch=pitch)
            for colour in set(view.convert("RGB").getdata()):
                self.assertLessEqual(max(abs(a - b) for a, b in zip(colour, want)), 3,
                                     (pitch, yaw, colour))


@needs_pillow
class ReadImage360Test(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = new_dir()
        insta = exif_of("Arashi Vision", "Insta360 OneR", ONE_R_INSP["ImageDescription"])
        params = images.INSTA360_PARAMS
        x, y = lens_position(0, 0, (1024, 512), params)
        cls.frame = imagegen.dual_fisheye_image((1024, 512), offsets=params["offsets"],
                                                markers=[(x, y, 6)])
        cls.insp = imagegen.write_bytes(cls.dir, "IMG_20190514_142301_00_001.insp",
                                        imagegen.jpeg_bytes(cls.frame, insta, quality=95))
        cls.insp_low = imagegen.write_bytes(cls.dir, "copy.insp",
                                            imagegen.jpeg_bytes(cls.frame, insta, quality=60))
        # Over-exposed: lens flare lights the corners (only the loose test passes).
        bright = imagegen.dual_fisheye_image((1024, 512), offsets=params["offsets"],
                                             markers=[(x, y, 6)], background=(70, 40, 60))
        cls.bright = imagegen.write_bytes(cls.dir, "IMG_20190514_142301_00_002.insp",
                                          imagegen.jpeg_bytes(bright, quality=95))
        cls.bright_insta = imagegen.write_bytes(
            cls.dir, "IMG_20190514_142301_00_003.insp", imagegen.jpeg_bytes(bright, insta,
                                                                            quality=95))
        # A dark panorama the camera stitched itself (EXIF names a .jpg).
        x4 = exif_of("Arashi Vision", "Insta360 X4", X4_JPG["ImageDescription"])
        cls.night = imagegen.write_bytes(
            cls.dir, "IMG_20190514_142301_00_005.jpg",
            imagegen.jpeg_bytes(imagegen.night_panorama((1024, 512), marker=True), x4,
                                quality=95))
        cls.equirect = imagegen.write_bytes(
            cls.dir, "pano.jpg", imagegen.jpeg_with_xmp(imagegen.equirect_image((600, 300)),
                                                       imagegen.gpano_xmp(form="attribute")))
        cls.wide = imagegen.write_bytes(cls.dir, "wide.jpg", imagegen.jpeg_bytes(
            imagegen.equirect_image((600, 300)), exif_of("Apple", "iPhone 12")))
        big = imagegen.dual_fisheye_image((4096, 2048), offsets=params["offsets"])
        cls.big = imagegen.write_bytes(cls.dir, "big.insp", imagegen.jpeg_bytes(big, quality=80))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def open(self, data):
        image = Image.open(io.BytesIO(data))
        image.load()
        return image

    def test_dual_fisheye_insp(self):
        result = images.read_image(self.insp)
        self.assertEqual(result["projection"], "dual_fisheye")
        self.assertEqual(result["mime"], "image/jpeg")
        self.assertEqual((result["width"], result["height"]), (1024, 512))
        preview = self.open(result["preview"])
        self.assertEqual(preview.size, (1024, 512))      # never upscaled
        centre = red_centroid(preview)
        self.assertAlmostEqual(centre[0], 512, delta=3)  # the front lens's centre
        self.assertAlmostEqual(centre[1], 256, delta=3)
        thumb = self.open(result["thumb"])
        self.assertEqual(thumb.size, (320, 240))
        centre = red_centroid(thumb)
        self.assertAlmostEqual(centre[0], 160, delta=3)
        self.assertAlmostEqual(centre[1], 120, delta=3)
        # The dHash is the thumbnail's.
        self.assertLessEqual(hamming(result["dhash"], images.compute_dhash(thumb)), 2)

    def test_copies_of_the_same_shot_have_close_dhashes(self):
        first = images.read_image(self.insp)
        second = images.read_image(self.insp_low)
        self.assertLessEqual(hamming(first["dhash"], second["dhash"]), 4)

    def test_large_frame_preview_is_preview_sized(self):
        result = images.read_image(self.big)
        self.assertEqual(result["projection"], "dual_fisheye")
        self.assertEqual((result["width"], result["height"]), (4096, 2048))
        self.assertEqual(self.open(result["preview"]).size, (2048, 1024))
        self.assertEqual(self.open(result["thumb"]).size, (320, 240))
        small = images.read_image(self.big, preview_edge=600)
        self.assertEqual(self.open(small["preview"]).size, (600, 300))

    def test_stitched_panorama(self):
        result = images.read_image(self.equirect)
        self.assertEqual(result["projection"], "equirectangular")
        self.assertEqual((result["width"], result["height"]), (600, 300))
        self.assertEqual(self.open(result["preview"]).size, (600, 300))
        self.assertEqual(self.open(result["thumb"]).size, (320, 240))

    def test_ordinary_wide_photo(self):
        result = images.read_image(self.wide)
        self.assertEqual(result["projection"], "")
        self.assertEqual(self.open(result["thumb"]).size, (320, 160))

    def test_llm_sees_the_panorama(self):
        with open(self.insp, "rb") as handle:
            data = handle.read()
        sent = self.open(images.prepare_for_llm(data))
        self.assertEqual(sent.size, (1024, 512))
        centre = red_centroid(sent)
        self.assertAlmostEqual(centre[0], 512, delta=3)
        # The preview (already a panorama, no EXIF) is only resized.
        preview = images.read_image(self.big)["preview"]
        self.assertEqual(self.open(images.prepare_for_llm(preview)).size, (1536, 768))
        # Without the camera's EXIF an original is sent as it is.
        plain = imagegen.jpeg_bytes(self.frame)
        centre = red_centroid(self.open(images.prepare_for_llm(plain)))
        self.assertLess(centre[0], 300)

    def test_llm_size_of_a_large_original(self):
        with open(self.big, "rb") as handle:
            data = handle.read()
        # big.insp has no EXIF: it is only resized.
        self.assertEqual(self.open(images.prepare_for_llm(data)).size, (1536, 768))
        # Told it is unstitched, it is stitched (and still LLM-sized).
        sent = self.open(images.prepare_for_llm(data, projection="dual_fisheye"))
        self.assertEqual(sent.size, (1536, 768))

    def read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def background_pixels(self, image):
        """How many pixels show the over-exposed frame's corner colour."""
        return sum(1 for r, g, b in image.convert("RGB").getdata()
                   if abs(r - 70) < 12 and abs(g - 40) < 12 and abs(b - 60) < 12)

    def test_llm_uses_the_projection_the_index_knows(self):
        data = self.read(self.bright)
        self.assertEqual(images.read_image(self.bright)["projection"], "dual_fisheye")
        stitched = self.open(images.prepare_for_llm(data, projection="dual_fisheye"))
        self.assertEqual(stitched.size, (1024, 512))
        self.assertEqual(self.background_pixels(stitched), 0)
        self.assertAlmostEqual(red_centroid(stitched)[0], 512, delta=3)
        # Known to be something else (or no EXIF to go on): sent as it is.
        for projection in ("", "equirectangular", None, "?"):
            raw = self.open(images.prepare_for_llm(data, projection=projection))
            self.assertGreater(self.background_pixels(raw), 10000, projection)

    def test_llm_stitches_an_original_the_camera_calls_insp(self):
        # No projection given: the EXIF says .insp, and an over-exposed
        # frame passes the loose test there, as it does in read_image.
        data = self.read(self.bright_insta)
        self.assertEqual(images.read_image(self.bright_insta)["projection"], "dual_fisheye")
        self.assertEqual(self.background_pixels(self.open(images.prepare_for_llm(data))), 0)

    def test_dark_camera_stitched_panorama_is_not_stitched_again(self):
        result = images.read_image(self.night)
        self.assertEqual(result["projection"], "equirectangular")
        centre = red_centroid(self.open(result["preview"]))
        self.assertAlmostEqual(centre[0], 512, delta=2)
        self.assertAlmostEqual(centre[1], 256, delta=2)
        centre = red_centroid(self.open(result["thumb"]))
        self.assertAlmostEqual(centre[0], 160, delta=2)
        # The LLM gets the picture as it is, decoded once: the EXIF rules
        # the frame out before any pixels are looked at.
        with mock.patch.object(images, "_decoded", wraps=images._decoded) as decoded:
            sent = self.open(images.prepare_for_llm(self.read(self.night)))
        self.assertEqual(decoded.call_count, 1)
        self.assertAlmostEqual(red_centroid(sent)[0], 512, delta=2)

    def test_describe_sends_the_stitched_original(self):
        # Previews off: describe falls back to the original, and passes on
        # what the index knows about it.
        row = {"id": 1, "library_id": "lib_family1", "has_preview": 0,
               "path": "IMG_20190514_142301_00_002.insp", "projection": "dual_fisheye"}
        sent = self.open(describe.image_for_llm(row, self.dir, previews=None))
        self.assertEqual(self.background_pixels(sent), 0)
        row["projection"] = ""
        sent = self.open(describe.image_for_llm(row, self.dir, previews=None))
        self.assertGreater(self.background_pixels(sent), 10000)
        # Rows selected without the column fall back to the EXIF rules.
        del row["projection"]
        sent = self.open(describe.image_for_llm(row, self.dir, previews=None))
        self.assertGreater(self.background_pixels(sent), 10000)


if __name__ == "__main__":
    unittest.main()
