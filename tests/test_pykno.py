from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pikepdf
from PIL import Image


MODULE_PATH = Path(__file__).parents[1] / "pykno.py"
SPEC = importlib.util.spec_from_file_location("pykno_module", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pykno = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pykno
SPEC.loader.exec_module(pykno)


def compression_args(**overrides: object) -> argparse.Namespace:
    values = {
        "force": False,
        "password_prompt": False,
        "strip_metadata": False,
        "suffix": "-compressed",
        "output": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def make_image_pdf(path: Path) -> None:
    image = Image.new("RGB", (1200, 900), (35, 90, 145))
    image.save(path, "PDF", resolution=300)


def make_image_xobject(pdf: pikepdf.Pdf) -> pikepdf.Object:
    image = pdf.make_stream(bytes((30, 80, 140)) * (1200 * 900))
    image["/Type"] = pikepdf.Name.XObject
    image["/Subtype"] = pikepdf.Name.Image
    image["/Width"] = 1200
    image["/Height"] = 900
    image["/ColorSpace"] = pikepdf.Name.DeviceRGB
    image["/BitsPerComponent"] = 8
    return image


class SettingsTests(unittest.TestCase):
    def test_version_is_publicly_available(self) -> None:
        self.assertRegex(pykno.__version__, r"^\d+\.\d+\.\d+$")

    def test_preset_can_be_partially_overridden(self) -> None:
        args = argparse.Namespace(preset="small", dpi=120, quality=None)
        self.assertEqual(pykno.checked_settings(args), pykno.Settings(120, 50))

    def test_settings_reject_out_of_range_values(self) -> None:
        args = argparse.Namespace(preset="balanced", dpi=35, quality=None)
        with self.assertRaisesRegex(ValueError, "--dpi"):
            pykno.checked_settings(args)

    def test_output_path_uses_requested_directory_and_suffix(self) -> None:
        args = argparse.Namespace(output=Path("~/exports"), suffix="-small")
        result = pykno.output_path(Path("docs/report.PDF"), args)
        self.assertEqual(result.name, "report-small.pdf")
        self.assertEqual(result.parent, Path("~/exports").expanduser())

    def test_batch_rejects_duplicate_destinations_before_processing(self) -> None:
        args = compression_args(output=Path("exports"))
        inputs = [Path("first/report.pdf"), Path("second/report.pdf")]
        with self.assertRaisesRegex(RuntimeError, "same output"):
            pykno.planned_jobs(inputs, args)


class PdfCompressionTests(unittest.TestCase):
    def test_nested_form_and_shared_placements_use_largest_rendered_size(self) -> None:
        pdf = pikepdf.Pdf.new()
        image = make_image_xobject(pdf)
        form = pdf.make_stream(b"q 200 0 0 100 0 0 cm /Image Do Q")
        form["/Type"] = pikepdf.Name.XObject
        form["/Subtype"] = pikepdf.Name.Form
        form["/BBox"] = pikepdf.Array((0, 0, 200, 100))
        form["/Resources"] = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Image=image)
        )

        small_page = pdf.add_blank_page(page_size=(612, 792))
        small_page.Resources = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Form=form)
        )
        small_page.Contents = pdf.make_stream(b"q 0.5 0 0 0.5 0 0 cm /Form Do Q")

        large_page = pdf.add_blank_page(page_size=(612, 792))
        large_page.Resources = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Image=image)
        )
        large_page.Contents = pdf.make_stream(b"q 288 0 0 144 0 0 cm /Image Do Q")

        caps = pykno.image_dimension_caps(pdf, 144)
        self.assertEqual(caps[pykno.object_identity(image)], (576, 288))

    def test_uses_rendered_image_size_instead_of_largest_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            destination = root / "compressed.pdf"
            make_image_pdf(source)
            with pikepdf.open(source, allow_overwriting_input=True) as pdf:
                pdf.add_blank_page(page_size=(7200, 7200))
                pdf.save(source)

            stats = pykno.compress_pdf(
                source,
                destination,
                pykno.Settings(144, 72),
                compression_args(),
            )

            with pikepdf.open(destination) as output:
                images = list(output.pages[0].get_images().values())
                self.assertEqual(len(output.pages), 2)
                self.assertEqual(len(images), 1)
                self.assertLessEqual(int(images[0]["/Width"]), 576)
                self.assertLessEqual(int(images[0]["/Height"]), 432)
            self.assertEqual(stats["changed"], 1)

    def test_strip_metadata_removes_info_and_xmp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            destination = root / "compressed.pdf"
            make_image_pdf(source)
            with pikepdf.open(source, allow_overwriting_input=True) as pdf:
                pdf.docinfo["/Author"] = "Private"
                with pdf.open_metadata() as metadata:
                    metadata["dc:title"] = "Private title"
                pdf.save(source)

            pykno.compress_pdf(
                source,
                destination,
                pykno.Settings(144, 72),
                compression_args(strip_metadata=True),
            )

            with pikepdf.open(destination) as output:
                self.assertIsNone(output.Root.get("/Metadata"))
                self.assertNotIn("/Author", output.docinfo)


class QuickActionTests(unittest.TestCase):
    def test_workflow_is_pdf_only_and_contains_no_username(self) -> None:
        with mock.patch.object(pykno, "__file__", "/opt/pykno/pykno.py"):
            with mock.patch("sys.executable", "/opt/pykno/bin/python"):
                with mock.patch.object(pykno.shutil, "which", return_value=None):
                    command = pykno._quick_action_command()
        document = pykno._quick_action_document(command)
        info = pykno._quick_action_info()

        self.assertIn("/opt/pykno/bin/python", command)
        self.assertNotIn("apaggarwal", command)
        self.assertEqual(info["NSServices"][0]["NSSendFileTypes"], ["com.adobe.pdf"])
        self.assertEqual(
            document["workflowMetaData"]["serviceApplicationBundleID"],
            "com.apple.finder",
        )

    def test_documents_checkout_is_rejected_for_finder_install(self) -> None:
        module = Path.home() / "Documents" / "Code" / "pykno" / "pykno.py"
        with mock.patch.object(pykno, "__file__", str(module)):
            with self.assertRaisesRegex(RuntimeError, "Finder cannot reliably"):
                pykno._quick_action_command()

    @unittest.skipUnless(sys.platform == "darwin", "Finder is macOS-only")
    def test_quick_action_install_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.object(pykno.Path, "home", return_value=home):
                with mock.patch.object(pykno, "__file__", "/opt/pykno/pykno.py"):
                    with mock.patch("sys.executable", "/opt/pykno/bin/python"):
                        with mock.patch.object(
                            pykno.shutil, "which", return_value=None
                        ):
                            with mock.patch.object(pykno, "refresh_services"):
                                pykno.install_quick_action()
                                workflow = pykno.quick_action_path()
                                with (workflow / "Contents" / "Info.plist").open(
                                    "rb"
                                ) as stream:
                                    info = pykno.plistlib.load(stream)
                                self.assertEqual(
                                    info["NSServices"][0]["NSSendFileTypes"],
                                    ["com.adobe.pdf"],
                                )
                                pykno.uninstall_quick_action()
                                self.assertFalse(workflow.exists())


class QuartzFallbackTests(unittest.TestCase):
    def test_failed_fallback_restores_object_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            destination = root / "result.pdf"
            source.write_bytes(b"source")
            destination.write_bytes(b"valid object result")

            with mock.patch.object(
                pykno, "compress_quartz", side_effect=RuntimeError("unavailable")
            ):
                with mock.patch("sys.stderr"):
                    engine = pykno.try_quartz_fallback(
                        source,
                        destination,
                        pykno.Settings(144, 72),
                        argparse.Namespace(),
                        destination.stat().st_size,
                    )

            self.assertEqual(destination.read_bytes(), b"valid object result")
            self.assertIn("fallback unavailable", engine)

    def test_larger_quartz_result_restores_object_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            destination = root / "result.pdf"
            source.write_bytes(b"source")
            destination.write_bytes(b"small")

            def write_larger(*_args: object, **_kwargs: object) -> None:
                destination.write_bytes(b"a much larger quartz result")

            with mock.patch.object(pykno, "compress_quartz", write_larger):
                engine = pykno.try_quartz_fallback(
                    source,
                    destination,
                    pykno.Settings(144, 72),
                    argparse.Namespace(),
                    5,
                )

            self.assertEqual(destination.read_bytes(), b"small")
            self.assertIn("not smaller", engine)


if __name__ == "__main__":
    unittest.main()
