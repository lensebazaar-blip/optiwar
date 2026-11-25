import importlib.util
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageCms

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
        avif_available = patch.object(
            publisher.image_pipeline, "avif_encoder_available",
            return_value=True)
        avif_available.start()
        self.addCleanup(avif_available.stop)
        avif_writer = patch.object(
            publisher.image_pipeline, "_save_avif",
            side_effect=self._save_fake_avif)
        avif_writer.start()
        self.addCleanup(avif_writer.stop)

    @staticmethod
    def _save_fake_avif(image, path):
        image.save(path, "PNG")
        return True

    def source(self, name, color):
        path = self.sources / name
        Image.new("RGB", (2100, 300), color).save(path, "JPEG")
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
        self.assertIn("AI20_r2_01-2000.avif", text)
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
        self.assertEqual(len(derivative_paths), 15)
        self.assertTrue(any(path.name.endswith("-200.jpg")
                            for path in derivative_paths))
        written = [str(master)] + [str(path) for path in derivative_paths]
        expected = dict(publisher.image_pipeline.manifest_rows(
            written, str(self.image_root / "catalog")))
        self.assertEqual(self.manifest_rows(), expected)
        self.assertTrue(all(len(digest) == 64 for digest in expected.values()))

    def test_avif_is_required_before_a_dry_run_writes_anything(self):
        source = self.source("retouched.jpg", (60, 90, 120))
        stderr = io.StringIO()

        with patch.object(publisher.image_pipeline, "avif_encoder_available",
                          return_value=False), \
                patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as raised:
                publisher.main([
                    "--sku", "AI20", "--files", source,
                    "--image-root", str(self.image_root)])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("AVIF encoding support is required", stderr.getvalue())
        self.assertFalse(self.image_root.exists())

    def test_preflight_uses_the_shared_avif_encoder_check(self):
        with patch.object(
                publisher.image_pipeline, "avif_encoder_available",
                return_value=True) as available:
            publisher._require_avif_support()

        available.assert_called_once_with()

    def test_missing_variant_fails_before_adding_manifest_rows(self):
        source = self.source("retouched.jpg", (60, 90, 120))
        build_derivatives = publisher.image_pipeline.build_derivatives

        def omit_avif(master_path):
            paths = build_derivatives(master_path)
            missing = next(path for path in paths
                           if path.endswith("-200.avif"))
            os.remove(missing)
            return [path for path in paths if path != missing]

        with patch.object(publisher.image_pipeline, "build_derivatives",
                          side_effect=omit_avif):
            with self.assertRaisesRegex(
                    ValueError, "incomplete derivative ladder.*200.avif"):
                publisher.publish(
                    "AI20", [source], str(self.image_root), apply=True)

        self.assertFalse((self.image_root / "catalog"
                          / "derivative_manifest.tsv").exists())
        self.assertFalse((self.image_root / "catalog" / "r2"
                          / "AI20").exists())

    def test_exif_orientation_is_applied_to_master_and_derivatives(self):
        source = self.sources / "oriented.jpg"
        image = Image.new("RGB", (2400, 2200), (60, 90, 120))
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, "JPEG", exif=exif)

        publisher.publish("AI20", [str(source)], str(self.image_root),
                          apply=True)

        target_dir = self.image_root / "catalog" / "r2" / "AI20"
        master = target_dir / "AI20_r2_01.jpg"
        with Image.open(master) as result:
            self.assertEqual(result.size, (2200, 2400))
            self.assertIsNone(result.getexif().get(274))

        derivatives = sorted((target_dir / "derivatives").iterdir())
        self.assertEqual(len(derivatives), 15)
        for derivative in derivatives:
            width = int(derivative.name.split("-")[1].split(".")[0])
            with Image.open(derivative) as result:
                self.assertEqual(
                    result.size, (width, int(round(2400 * width / 2200))))
                self.assertIsNone(result.getexif().get(274))

    def test_output_files_are_mode_0644_for_mode_0600_source(self):
        source = self.source("private.jpg", (60, 90, 120))
        os.chmod(source, 0o600)

        previous_umask = os.umask(0o077)
        try:
            publisher.publish(
                "AI20", [source], str(self.image_root), apply=True)
        finally:
            os.umask(previous_umask)

        target_dir = self.image_root / "catalog" / "r2" / "AI20"
        output_files = [target_dir / "AI20_r2_01.jpg"]
        output_files.extend((target_dir / "derivatives").iterdir())
        self.assertEqual(len(output_files), 16)
        self.assertTrue(all(
            stat.S_IMODE(path.stat().st_mode) == 0o644
            for path in output_files))

    def test_icc_profiles_are_converted_only_when_present(self):
        lab_profile = ImageCms.ImageCmsProfile(
            ImageCms.createProfile("LAB")).tobytes()
        profiled = self.sources / "profiled.jpg"
        Image.new("RGB", (2100, 300), (60, 90, 120)).save(
            profiled, "JPEG", icc_profile=lab_profile)
        unprofiled = self.source("unprofiled.jpg", (120, 90, 60))
        calls = []

        def convert_to_srgb(image, input_profile, output_profile,
                            outputMode):
            calls.append((input_profile, output_profile, outputMode))
            return image.convert(outputMode)

        with patch.object(publisher.ImageCms, "profileToProfile",
                          side_effect=convert_to_srgb):
            publisher.publish(
                "AI20", [str(profiled), unprofiled], str(self.image_root),
                apply=True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "RGB")
        for index in (1, 2):
            master = (self.image_root / "catalog" / "r2" / "AI20"
                      / ("AI20_r2_%02d.jpg" % index))
            with Image.open(master) as result:
                self.assertEqual(result.mode, "RGB")
                self.assertIsNone(result.info.get("icc_profile"))

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
