from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/build.yml"


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_triggers_dev_tags_and_manual_runs(self):
        self.assertIn("branches: [dev]", self.workflow)
        self.assertIn('tags: ["v*.*.*"]', self.workflow)
        self.assertIn("workflow_dispatch:", self.workflow)

    def test_builds_every_supported_artifact(self):
        expected = (
            "windows-latest",
            "ubuntu-22.04",
            "macos-15-intel",
            "macos-15",
            "SpotMatch-Windows-x64.zip",
            "SpotMatch-Linux-x86_64.AppImage",
            "SpotMatch-macOS-x64.zip",
            "SpotMatch-macOS-arm64.zip",
        )
        for value in expected:
            with self.subTest(value=value):
                self.assertIn(value, self.workflow)

    def test_validates_versions_and_centralizes_release(self):
        self.assertIn("python scripts/validate_release_version.py --tag", self.workflow)
        self.assertIn("contents: write", self.workflow)
        self.assertIn("needs: build", self.workflow)
        self.assertIn("refs/tags/v", self.workflow)
        self.assertIn("gh release create", self.workflow)


if __name__ == "__main__":
    unittest.main()
