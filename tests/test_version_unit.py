#!/usr/bin/env python3
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys


PROJECT_DIR = Path(__file__).resolve().parent.parent
GATEWAY_DIR = PROJECT_DIR / "gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import version as version_module


class VersionParsingTests(unittest.TestCase):
    def write_version_file(self, payload) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "version.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_normalize_version_accepts_semver_and_optional_v_prefix(self) -> None:
        self.assertEqual(version_module._normalize_version("1.2.3"), "1.2.3")
        self.assertEqual(version_module._normalize_version("v1.2.3"), "1.2.3")
        self.assertEqual(version_module._normalize_version(" 1.2.3 "), "1.2.3")

    def test_normalize_version_rejects_invalid_or_non_canonical_versions(self) -> None:
        invalid_values = ["", "1", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.beta"]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    version_module._normalize_version(value)

    def test_read_version_file_requires_json_object(self) -> None:
        path = self.write_version_file(["not", "an", "object"])
        with mock.patch.object(version_module, "VERSION_FILE", path):
            with self.assertRaisesRegex(RuntimeError, "must contain a JSON object"):
                version_module._read_version_file()

    def test_load_app_version_accepts_version_and_default_tag(self) -> None:
        path = self.write_version_file({"version": "2.3.4"})
        with mock.patch.object(version_module, "VERSION_FILE", path):
            self.assertEqual(version_module.load_app_version(), ("2.3.4", "v2.3.4"))

    def test_load_app_version_accepts_tag_without_version(self) -> None:
        path = self.write_version_file({"tag": "v2.3.4"})
        with mock.patch.object(version_module, "VERSION_FILE", path):
            self.assertEqual(version_module.load_app_version(), ("2.3.4", "v2.3.4"))

    def test_load_app_version_rejects_mismatched_tag(self) -> None:
        path = self.write_version_file({"version": "2.3.4", "tag": "v2.3.5"})
        with mock.patch.object(version_module, "VERSION_FILE", path):
            with self.assertRaisesRegex(RuntimeError, "tag must be v2.3.4"):
                version_module.load_app_version()

    def test_load_app_version_rejects_missing_version_and_tag(self) -> None:
        path = self.write_version_file({"name": "gateway"})
        with mock.patch.object(version_module, "VERSION_FILE", path):
            with self.assertRaisesRegex(RuntimeError, "invalid version"):
                version_module.load_app_version()


class VersionPayloadTests(unittest.TestCase):
    def test_get_version_payload_includes_contract_and_feature_metadata(self) -> None:
        with mock.patch.object(version_module, "load_app_version", return_value=("9.8.7", "v9.8.7")):
            with mock.patch.dict(os.environ, {}, clear=True):
                payload = version_module.get_version_payload()

        self.assertEqual(payload["version"], "9.8.7")
        self.assertEqual(payload["tag"], "v9.8.7")
        self.assertEqual(payload["api_contract_version"], 1)
        self.assertEqual(payload["active_execution_version"], "v1")
        self.assertEqual(payload["supported_execution_versions"], ["v1"])
        self.assertEqual(payload["active_rendering_version"], "v1")
        self.assertEqual(payload["supported_rendering_versions"], ["v1", "v2"])
        self.assertTrue(payload["features"]["persistent_sessions"])
        self.assertTrue(payload["features"]["slide_rendering"])

    def test_get_version_payload_marks_beta_from_app_env_or_public_beta_flag(self) -> None:
        with mock.patch.object(version_module, "load_app_version", return_value=("1.0.0", "v1.0.0")):
            with mock.patch.dict(os.environ, {"APP_ENV": "public-beta"}, clear=True):
                self.assertTrue(version_module.get_version_payload()["beta"])
            with mock.patch.dict(os.environ, {"PUBLIC_BETA_MODE": "yes"}, clear=True):
                self.assertTrue(version_module.get_version_payload()["beta"])
            with mock.patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
                self.assertFalse(version_module.get_version_payload()["beta"])

    def test_get_version_payload_prefers_explicit_rendering_version_over_beta_flag(self) -> None:
        with mock.patch.object(version_module, "load_app_version", return_value=("1.0.0", "v1.0.0")):
            with mock.patch.dict(os.environ, {"SLIDE_RENDERING_VERSION": " V2 ", "BETA": "0"}, clear=True):
                self.assertEqual(version_module.get_version_payload()["active_rendering_version"], "v2")
            with mock.patch.dict(os.environ, {"RENDERING_VERSION": "v2"}, clear=True):
                self.assertEqual(version_module.get_version_payload()["active_rendering_version"], "v2")
            with mock.patch.dict(os.environ, {"ACTIVE_RENDERING_VERSION": "v2"}, clear=True):
                self.assertEqual(version_module.get_version_payload()["active_rendering_version"], "v2")
            with mock.patch.dict(os.environ, {"BETA": "true"}, clear=True):
                self.assertEqual(version_module.get_version_payload()["active_rendering_version"], "v2")

    def test_module_globals_are_loaded_from_version_file(self) -> None:
        self.assertRegex(version_module.APP_VERSION, r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
        self.assertEqual(version_module.APP_VERSION_TAG, f"v{version_module.APP_VERSION}")

    def test_reloading_module_keeps_version_globals_consistent(self) -> None:
        reloaded = importlib.reload(version_module)
        self.assertEqual(reloaded.APP_VERSION_TAG, f"v{reloaded.APP_VERSION}")


if __name__ == "__main__":
    unittest.main()
