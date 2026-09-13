"""Scenario failures must retain the logs generated after readiness."""

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from family_librarian_lab.commands import _BaseScenario


class ScenarioArtifactTests(unittest.TestCase):
    def test_failure_refreshes_logs_before_teardown_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario = _BaseScenario(
                {"FAMILY_LIBRARIAN_ADMIN_PASSWORD": "fixture-secret"}, "LOG-01", keep=False)
            scenario._result_directory = Path(directory)
            scenario.api = SimpleNamespace(trace=[])
            log_path = Path(directory) / "compose-logs.txt"
            log_path.write_text("Readiness only")

            def compose(values, project, *arguments, **kwargs):
                if arguments[0] == "logs":
                    return subprocess.CompletedProcess(arguments, 0, "Identity held after readiness; fixture-secret", "")
                self.assertEqual(arguments[0], "down")
                self.assertIn("Identity held after readiness", log_path.read_text())
                self.assertNotIn("fixture-secret", log_path.read_text())
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with patch("family_librarian_lab.commands._compose", side_effect=compose):
                self.assertFalse(scenario.__exit__(AssertionError, AssertionError("wrong language"), None))
            self.assertIn("[REDACTED]", log_path.read_text())
