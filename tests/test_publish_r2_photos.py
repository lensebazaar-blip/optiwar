import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "publish_r2_photos_test", ROOT / "scripts" / "publish_r2_photos.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublishR2PhotosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image_root = self.root / "static"
        self.sources = self.root / "sources"
        self.sources.mkdir()

    def source(self, name, color):
        path = self.sources / name
        Image.new("RGB", (600, 400), color).save(path, "JPEG")
        return str(path)

    def manifest_rows(self):
        manifest = self.image_root / "catalog" / "derivative_manifest.tsv"
        with manifest.open(encoding="utf-8") as stream:
            return {
                path: digest
                for line in stream
                for path, _, digest in [line.rstrip("\n").partition("\t")]
            }

    def test_dry_run_lists_all_outputs_without_writing(self):
        source = self.source("retouched.jpg", (60, 90, 120))
        output = io.StringIO()

        result = publisher.publish(
            "AI20", [source], str(self.image_root), output=output)

        text = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn(str(self.image_root / "catalog" / "r2" / "AI20"
                          / "AI20_r2_01.jpg"), text)
        self.assertIn("AI20_r2_01-200.jpg", text)
        self.assertIn(str(self.image_root / "catalog"
                          / "derivative_manifest.tsv"), text)
        self.assertFalse((self.image_root / "catalog").exists())

    def test_apply_writes_jpegs_derivatives_and_embed_manifest_rows(self):
        source = self.source("retouched.jpg", (60, 90, 120))

        publisher.publish("AI20", [source], str(self.image_root), apply=True)

        target_dir = self.image_root / "catalog" / "r2" / "AI20"
        master = target_dir / "AI20_r2_01.jpg"
        self.assertTrue(master.is_file())
        derivative_paths = sorted((target_dir / "derivatives").iterdir())
        self.assertTrue(any(path.name.endswith("-200.jpg")
                            for path in derivative_paths))
        written = [str(master)] + [str(path) for path in derivative_paths]
        expected = dict(publisher.image_pipeline.manifest_rows(
            written, str(self.image_root / "catalog")))
        self.assertEqual(self.manifest_rows(), expected)
        self.assertTrue(all(len(digest) == 64 for digest in expected.values()))

    def test_existing_masters_require_replace(self):
        first = self.source("first.jpg", (60, 90, 120))
        second = self.source("second.jpg", (120, 90, 60))
        publisher.publish("AI20", [first], str(self.image_root), apply=True)

        with self.assertRaisesRegex(ValueError, "pass --replace"):
            publisher.publish("AI20", [second], str(self.image_root))

    def test_replace_removes_stale_family_and_manifest_rows(self):
        first = self.source("first.jpg", (60, 90, 120))
        second = self.source("second.jpg", (120, 90, 60))
        replacement = self.source("replacement.jpg", (30, 50, 70))
        publisher.publish(
            "AI20", [first, second], str(self.image_root), apply=True)
        publisher.image_pipeline.merge_manifest(
            str(self.image_root / "catalog" / "derivative_manifest.tsv"),
            [("r2/AI64/AI64_r2_01.jpg", "9" * 64)])

        publisher.publish(
            "AI20", [replacement], str(self.image_root),
            apply=True, replace=True)

        target_dir = self.image_root / "catalog" / "r2" / "AI20"
        self.assertTrue((target_dir / "AI20_r2_01.jpg").is_file())
        self.assertFalse((target_dir / "AI20_r2_02.jpg").exists())
        self.assertFalse(any(
            path.name.startswith("AI20_r2_02-")
            for path in (target_dir / "derivatives").iterdir()))
        manifest = self.manifest_rows()
        self.assertIn("r2/AI64/AI64_r2_01.jpg", manifest)
        self.assertFalse(any(
            path.startswith("r2/AI20/") and "AI20_r2_02" in path
            for path in manifest))


if __name__ == "__main__":
    unittest.main()
