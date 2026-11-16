import json
import tempfile
import unittest
from pathlib import Path

from science_checkpoint import archive_pack


class PackStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        (self.source / "objects").mkdir(parents=True)
        (self.source / "objects" / "a").write_bytes(b"alpha")
        (self.source / "objects" / "b").write_bytes(b"beta")

    def test_append_dedupes_and_restores(self):
        store = archive_pack.PackStore(self.root / "pack")
        first = store.add_archive(self.source)
        store.flush_index()
        size_after_first = (self.root / "pack" / "objects.pack").stat().st_size

        (self.source / "objects" / "c").write_bytes(b"gamma")
        second = store.add_archive(self.source)
        store.flush_index()
        size_after_second = (self.root / "pack" / "objects.pack").stat().st_size

        self.assertEqual(size_after_second - size_after_first, len(b"gamma") + len(
            json.dumps({"sha256": second["files"]["objects/c"][0], "size": 5}).encode()) + 4)
        restored = self.root / "restored"
        manifest = {"labels": {"api_audit": second}}
        archive_pack.restore_manifest(manifest, store, restored)
        self.assertEqual((restored / "api_audit" / "objects" / "a").read_bytes(), b"alpha")
        self.assertEqual((restored / "api_audit" / "objects" / "c").read_bytes(), b"gamma")

    def test_manifest_digest_matches_write(self):
        store = archive_pack.PackStore(self.root / "pack")
        entries = store.add_archive(self.source)
        path = self.root / "archives.json"
        payload = archive_pack.write_manifest(path, {"api_audit": entries})
        on_disk = json.loads(path.read_text())
        self.assertEqual(payload["sha256"], on_disk["sha256"])
        self.assertEqual(payload["sha256"],
                         archive_pack.manifest_digest({"api_audit": entries}))


if __name__ == "__main__":
    unittest.main()
