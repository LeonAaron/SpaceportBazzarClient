"""Which build of the client is running.

Run 2's log showed offers our committed code cannot produce, so a different
build had been deployed. Logging the commit at startup makes that visible.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
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


def working_tree_dirty(repo_root: Path = REPO_ROOT) -> bool | None:
    """True when tracked files differ from the commit; None when git is unavailable.

    A build id names a commit, but a run from uncommitted edits did not run
    that commit, so the flag travels with the id everywhere it is recorded.
    """
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=repo_root, capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(completed.stdout.strip())


def build_info(repo_root: Path = REPO_ROOT) -> dict:
    """Everything needed to say exactly what code and interpreter produced a result."""
    return {
        "build": build_id(repo_root),
        "dirty": working_tree_dirty(repo_root),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "hash_seed": os.environ.get("PYTHONHASHSEED", "random"),
        "argv": redact_argv(sys.argv[1:]),
    }


SECRET_FLAGS = ("--token",)


def redact_argv(argv: list[str]) -> list[str]:
    """Command line as recorded in logs: secret values replaced, never written."""
    out, hide_next = [], False
    for arg in argv:
        if hide_next:
            out.append("***")
            hide_next = False
        elif arg in SECRET_FLAGS:
            out.append(arg)
            hide_next = True
        elif arg.startswith(tuple(f"{flag}=" for flag in SECRET_FLAGS)):
            out.append(arg.split("=", 1)[0] + "=***")
        else:
            out.append(arg)
    return out


def describe_build(info: dict | None = None) -> str:
    info = info or build_info()
    state = {True: "with uncommitted changes", False: "clean", None: "tree state unknown"}
    return f"{info['build']} ({state[info['dirty']]})"
