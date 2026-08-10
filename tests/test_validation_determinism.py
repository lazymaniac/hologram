from __future__ import annotations

import dataclasses
import hashlib
import inspect
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from hologram import pipeline
from hologram.config import ProjectConfig, default_config
from validation import run as validation_run
from validation.run import assert_byte_determinism, main, validate_corpora

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_ROOT = PROJECT_ROOT / "validation" / "fixtures" / "advertised"


def _config() -> ProjectConfig:
    return replace(default_config(), agents=(), output="PROJECT_DIGEST.md")


class StaticDeterminismTest(unittest.TestCase):
    def test_public_api_signatures_are_exact(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(validate_corpora).parameters),
            ("registry", "environ", "runs"),
        )
        self.assertEqual(
            tuple(inspect.signature(assert_byte_determinism).parameters),
            ("root", "config", "runs"),
        )
        self.assertEqual(tuple(inspect.signature(main).parameters), ("argv",))

    def test_reversed_scanner_enumeration_is_byte_identical(self) -> None:
        original = pipeline.scan_project
        calls = 0

        def shuffled(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            nonlocal calls
            result = original(*args, **kwargs)  # type: ignore[arg-type]
            calls += 1
            if calls == 2:
                return replace(result, entries=tuple(reversed(result.entries)))
            return result

        with mock.patch("hologram.pipeline.scan_project", side_effect=shuffled):
            assert_byte_determinism(SYNTHETIC_ROOT, _config(), runs=3)
        self.assertEqual(calls, 3)

    def test_first_differing_byte_and_hashes_are_actionable(self) -> None:
        original = validation_run._capture_once
        calls = 0

        def drifting(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            nonlocal calls
            artifact = original(*args, **kwargs)
            calls += 1
            if calls == 2:
                return dataclasses.replace(
                    artifact,
                    rendered_bytes=artifact.rendered_bytes + b"drift",
                )
            return artifact

        with (
            mock.patch("validation.run._capture_once", side_effect=drifting),
            self.assertRaisesRegex(
                ValueError,
                r"rendered map nondeterministic at run 2: first differing byte "
                r"\d+; sha256 [0-9a-f]{64} != [0-9a-f]{64}",
            ),
        ):
            assert_byte_determinism(SYNTHETIC_ROOT, _config(), runs=3)

    def test_run_count_is_strict_and_environment_is_restored(self) -> None:
        for runs in (True, 0, 1):
            with self.subTest(runs=runs), self.assertRaises(ValueError):
                assert_byte_determinism(SYNTHETIC_ROOT, _config(), runs=runs)  # type: ignore[arg-type]

        before = {
            name: validation_run.os.environ.get(name)
            for name in ("LC_ALL", "TZ", "SOURCE_DATE_EPOCH")
        }
        assert_byte_determinism(SYNTHETIC_ROOT, _config(), runs=2)
        after = {
            name: validation_run.os.environ.get(name)
            for name in ("LC_ALL", "TZ", "SOURCE_DATE_EPOCH")
        }
        self.assertEqual(after, before)

    def test_machine_payload_is_canonical_and_hash_stable(self) -> None:
        first = validation_run._validate_synthetic(SYNTHETIC_ROOT, runs=2)
        second = validation_run._validate_synthetic(SYNTHETIC_ROOT, runs=2)
        first_bytes = validation_run._result_bytes(first)
        second_bytes = validation_run._result_bytes(second)
        self.assertEqual(first_bytes, second_bytes)
        self.assertTrue(first_bytes.endswith(b"\n"))
        self.assertEqual(first_bytes.count(b"\n"), 1)
        payload = json.loads(first_bytes)
        self.assertEqual(payload["byte_equal"], True)
        self.assertEqual(payload["runs"], 2)
        self.assertEqual(payload["census"], 0)
        self.assertEqual(payload["sample"], 0)
        self.assertEqual(payload["synthetic_files"], 33)
        self.assertEqual(
            hashlib.sha256(first_bytes).hexdigest(),
            hashlib.sha256(second_bytes).hexdigest(),
        )


class StaticValidationCliTest(unittest.TestCase):
    def test_synthetic_cli_writes_only_the_requested_machine_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                code = main(
                    (
                        "--synthetic",
                        str(SYNTHETIC_ROOT),
                        "--runs",
                        "2",
                        "--output",
                        str(output),
                    )
                )
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            payload = json.loads(output.read_bytes())
            self.assertTrue(payload["passed"])
            self.assertTrue(payload["byte_equal"])
            self.assertEqual(payload["runs"], 2)
            self.assertEqual(payload["synthetic_files"], 33)

    def test_stdout_default_and_validation_tree_output_rejection(self) -> None:
        result = validation_run._validate_synthetic(SYNTHETIC_ROOT, runs=2)
        expected = validation_run._result_bytes(result).decode("utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("validation.run._validate_synthetic", return_value=result),
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            code = main(("--synthetic", str(SYNTHETIC_ROOT), "--runs", "2"))
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), expected)
        self.assertEqual(stderr.getvalue(), "")

        forbidden = PROJECT_ROOT / "validation" / "generated-report.json"
        with (
            mock.patch("validation.run._validate_synthetic") as validate,
            mock.patch("sys.stderr", io.StringIO()) as rejected,
        ):
            code = main(
                (
                    "--synthetic",
                    str(SYNTHETIC_ROOT),
                    "--output",
                    str(forbidden),
                )
            )
        self.assertEqual(code, 2)
        validate.assert_not_called()
        self.assertIn("validation", rejected.getvalue())
        self.assertFalse(forbidden.exists())

    def test_usage_errors_are_concise_and_do_not_run_validation(self) -> None:
        for argv in (
            (),
            ("--synthetic", str(SYNTHETIC_ROOT), "--runs", "1"),
            ("--registry", "corpora.toml", "--synthetic", str(SYNTHETIC_ROOT)),
            ("--census", "census.jsonl"),
        ):
            with (
                self.subTest(argv=argv),
                mock.patch("validation.run._validate_synthetic") as synthetic,
                mock.patch("validation.run._validate_public") as public,
                mock.patch("sys.stderr", io.StringIO()) as stderr,
            ):
                code = main(argv)
                self.assertEqual(code, 2)
                self.assertTrue(stderr.getvalue().startswith("hologram validation: "))
                synthetic.assert_not_called()
                public.assert_not_called()


if __name__ == "__main__":
    unittest.main()
