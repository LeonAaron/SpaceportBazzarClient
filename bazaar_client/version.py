"""Which build of the client is running.

Run 2's log showed offers our committed code cannot produce, so a different
build had been deployed. Logging the commit at startup makes that visible.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def build_id(repo_root: Path = REPO_ROOT) -> str:
    """The checked-out commit, or "unknown" outside a git checkout.

    Read straight from .git so the container needs no git binary.
    """
    git = repo_root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:12]
        ref = head[len("ref: "):]
        ref_file = git / ref
        if ref_file.exists():
            return f"{ref.rsplit('/', 1)[-1]}@{ref_file.read_text(encoding='utf-8').strip()[:12]}"
        packed = git / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return f"{ref.rsplit('/', 1)[-1]}@{line[:12]}"
    except OSError:
        pass
    return "unknown"
