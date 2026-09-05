"""Runtime discovery must not import optional development dependencies."""

from pathlib import Path
import os
import subprocess
import sys
import unittest


class SuiteDiscoveryTests(unittest.TestCase):
    def test_all_suites_discover_without_pytest(self):
        root = Path(__file__).resolve().parents[2]
        script = r'''
import importlib.abc
from pathlib import Path
import sys

class NoPytest(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pytest" or fullname.startswith("pytest."):
            raise ModuleNotFoundError("pytest deliberately unavailable")

sys.meta_path.insert(0, NoPytest())
from agent.suites import discover_suites, select_suites
suites = discover_suites(Path("tests"))
assert suites, "No integration suites discovered"
for group, case in [("abs", "ABS-08"), ("communications", "COMM-04")]:
    selected = select_suites(suites, group=group, case=case)
    assert sum(len(suite.cases) for suite in selected) == 1, case
assert "pytest" not in sys.modules
print(f"Discovered {len(suites)} integration suites without pytest")
'''
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=root,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "se-lab"), str(root)))},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
