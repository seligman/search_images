"""Tests for cache.py: the thumbnail database and the preview files."""

import os
import threading
import unittest

import support

import cache
import common


class ThumbStoreTest(support.TempBaseMixin, unittest.TestCase):

    def open_store(self):
        store = cache.ThumbStore()
        self.addCleanup(store.close)
        return store

    def test_put_get_delete(self):
        store = self.open_store()
        self.assertEqual(store.path, common.thumbs_db_path())
        self.assertIsNone(store.get(1))
        self.assertEqual(store.ids(), set())
        store.put(1, b"\xff\xd8one")
        store.put(2, bytearray(b"\xff\xd8two"))
        store.put(3, b"\xff\xd8three")
        store.put(1, b"\xff\xd8uno")
        self.assertEqual(store.get(1), b"\xff\xd8uno")
        self.assertEqual(store.get("2"), b"\xff\xd8two")
        self.assertIn(3, store)
        self.assertNotIn(4, store)
        self.assertEqual(store.ids(), {1, 2, 3})
        self.assertEqual(store.delete([1, 3, 99]), 2)
        self.assertEqual(store.delete(2), 1)
        self.assertEqual(store.ids(), set())

    def test_persists_and_uses_wal(self):
        with cache.ThumbStore() as store:
            for image_id in range(1, 1201):
                store.put(image_id, b"x%d" % image_id, commit=False)
            store.commit()
            mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")
        with cache.ThumbStore() as store:
            self.assertEqual(len(store.ids()), 1200)
            self.assertEqual(store.get(1200), b"x1200")
            self.assertEqual(store.delete(range(1, 1201)), 1200)

    def test_custom_path_and_threads(self):
        path = self.base.join("elsewhere", "t.db")
        store = cache.ThumbStore(path)
        self.addCleanup(store.close)
        self.assertTrue(os.path.isfile(path))
        errors = []

        def work(offset):
            try:
                for n in range(50):
                    store.put(offset + n, b"%d" % (offset + n))
                    store.get(offset + n)
            except Exception as error:      # reported below
                errors.append(error)
        threads = [threading.Thread(target=work, args=(i * 1000,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(store.ids()), 200)

    def test_close_is_idempotent(self):
        store = cache.ThumbStore()
        store.close()
        store.close()


class PreviewStoreTest(support.TempBaseMixin, unittest.TestCase):

    def test_layout(self):
        store = cache.PreviewStore()
        root = common.previews_dir()
        self.assertEqual(store.root, root)
        self.assertEqual(store.path_for("lib_00000001", 5),
                         os.path.join(root, "lib_00000001", "000", "5.jpg"))
        self.assertEqual(store.path_for("lib_00000001", 12345),
                         os.path.join(root, "lib_00000001", "012", "12345.jpg"))
        self.assertEqual(store.path_for("lib_00000001", 1234567),
                         os.path.join(root, "lib_00000001", "1234", "1234567.jpg"))

    def test_rejects_unsafe_ids(self):
        store = cache.PreviewStore(self.base.join("p"))
        for library_id in ("..", "../x", "a/b", "a\\b", "", None, "c:x"):
            with self.assertRaises(ValueError, msg=repr(library_id)):
                store.path_for(library_id, 1)
        with self.assertRaises(ValueError):
            store.path_for("lib_00000001", -1)

    def test_write_read_delete(self):
        store = cache.PreviewStore(self.base.join("p"))
        self.assertIsNone(store.read("lib_00000001", 7))
        self.assertFalse(store.exists("lib_00000001", 7))
        path = store.write("lib_00000001", 7, b"\xff\xd8first")
        self.assertEqual(path, store.path_for("lib_00000001", 7))
        store.write("lib_00000001", 7, b"\xff\xd8second")
        store.write("lib_00000001", 1500, b"\xff\xd8other")
        store.write("lib_00000002", 7, b"\xff\xd8another library")
        self.assertEqual(store.read("lib_00000001", 7), b"\xff\xd8second")
        self.assertTrue(store.exists("lib_00000001", 7))
        # Only the final files exist; no temporary files are left behind.
        self.assertEqual(os.listdir(os.path.dirname(path)), ["7.jpg"])
        self.assertTrue(store.delete("lib_00000001", 7))
        self.assertFalse(store.delete("lib_00000001", 7))
        self.assertTrue(store.delete_library("lib_00000001"))
        self.assertFalse(store.delete_library("lib_00000001"))
        self.assertIsNone(store.read("lib_00000001", 1500))
        self.assertEqual(store.read("lib_00000002", 7), b"\xff\xd8another library")

    def test_panos_live_next_to_their_previews(self):
        store = cache.PreviewStore(self.base.join("p"))
        self.assertEqual(store.pano_path_for("lib_00000001", 12345),
                         os.path.join(store.root, "lib_00000001", "012", "12345.pano.jpg"))
        with self.assertRaises(ValueError):
            store.pano_path_for("../x", 1)
        self.assertIsNone(store.read_pano("lib_00000001", 7))
        self.assertFalse(store.pano_exists("lib_00000001", 7))
        store.write("lib_00000001", 7, b"\xff\xd8preview")
        path = store.write_pano("lib_00000001", 7, b"\xff\xd8pano")
        self.assertEqual(path, store.pano_path_for("lib_00000001", 7))
        store.write("lib_00000001", 8, b"\xff\xd8preview only")
        store.write_pano("lib_00000001", 1009, b"\xff\xd8pano only")
        self.assertEqual(store.read_pano("lib_00000001", 7), b"\xff\xd8pano")
        self.assertEqual(store.read("lib_00000001", 7), b"\xff\xd8preview")
        self.assertTrue(store.pano_exists("lib_00000001", 7))
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))),
                         ["7.jpg", "7.pano.jpg", "8.jpg"])
        # One listing tells previews and panos apart.
        self.assertEqual(store.stored_ids("lib_00000001"), ({7, 8}, {7, 1009}))
        self.assertEqual(store.stored_ids("lib_00000003"), (set(), set()))
        # A preview is never deleted without its pano.
        self.assertTrue(store.delete_both("lib_00000001", 7))
        self.assertFalse(store.exists("lib_00000001", 7))
        self.assertFalse(store.pano_exists("lib_00000001", 7))
        self.assertFalse(store.delete_both("lib_00000001", 7))
        self.assertTrue(store.delete_both("lib_00000001", 1009))
        self.assertFalse(store.delete_pano("lib_00000001", 8))
        store.write_pano("lib_00000001", 8, b"\xff\xd8pano")
        self.assertTrue(store.delete_pano("lib_00000001", 8))
        self.assertTrue(store.exists("lib_00000001", 8))
        store.write_pano("lib_00000001", 8, b"\xff\xd8pano")
        self.assertTrue(store.delete_library("lib_00000001"))
        self.assertIsNone(store.read_pano("lib_00000001", 8))


if __name__ == "__main__":
    unittest.main()
