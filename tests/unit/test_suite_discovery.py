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
for group, case in [
    ("abs", "ABS-08"), ("abs", "ABS-09"), ("communications", "COMM-04"),
    ("cwa-local", "CWA-L-11"), ("cwa-local", "CWA-L-12"),
    ("gutenberg", "GUT-12"), ("gutenberg", "GUT-13"),
    ("search-review", "SR-01"), ("search-review", "SG-01"), ("search-review", "SG-08"),
    ("candidate-triage", "CT-01"), ("candidate-triage", "CT-26"),
    ("artifact-decision", "AD-01"), ("artifact-decision", "AD-07"),
    ("artifact-release", "AR-01"), ("artifact-release", "AR-13"),
    ("artifact-excerpt", "AX-01"), ("artifact-excerpt", "AX-11"),
]:
    selected = select_suites(suites, group=group, case=case)
    assert sum(len(suite.cases) for suite in selected) == 1, case
assert "pytest" not in sys.modules
triage = select_suites(suites, group="candidate-triage")
assert {case.test_id for suite in triage for case in suite.cases} == {f"CT-{n:02}" for n in range(1, 31)}
artifact = select_suites(suites, group="artifact-decision")
assert {case.test_id for suite in artifact for case in suite.cases} == {f"AD-{n:02}" for n in range(1, 8)}
release = select_suites(suites, group="artifact-release")
assert {case.test_id for suite in release for case in suite.cases} == {f"AR-{n:02}" for n in range(1, 14)}
excerpts = select_suites(suites, group="artifact-excerpt")
assert {case.test_id for suite in excerpts for case in suite.cases} == {f"AX-{n:02}" for n in range(1, 12)}
print(f"Discovered {len(suites)} integration suites without pytest")
'''
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=root,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "se-lab"), str(root)))},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
