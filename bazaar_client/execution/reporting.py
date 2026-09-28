"""Load scripts/analyze_evidence.py without requiring it as an installed package.

`scripts/` has no `__init__.py`, so anything that wants its `report()`/
`html_report()`/`write_reports()` loads the file directly instead of importing
it. This is the one shared loader, used both by the simulator's dashboards
(`bazaar_sim/orchestrate.py`) and by the real-run CLI after a trade/walkthrough
finishes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
ANALYZER = REPO_ROOT / "scripts" / "analyze_evidence.py"


def load_analyzer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("analyze_evidence", ANALYZER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
