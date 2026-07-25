from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "pykno.py"
SPEC = importlib.util.spec_from_file_location("pykno_module", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pykno = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pykno
SPEC.loader.exec_module(pykno)


class SettingsTests(unittest.TestCase):
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
