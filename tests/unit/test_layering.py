"""The generated bindings stay behind the codec/mapper boundary.

Decision logic must be testable without protobuf or a socket, so only the two
translation modules are allowed to import bazaar_pb2.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "bazaar_client"

ALLOWED_PB_IMPORTERS = {
    Path("codec/wire.py"),
    Path("domain/mappers.py"),
}


def imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def python_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_package_has_modules_to_check():
    assert python_files(), "no modules found; the layering check would pass vacuously"


def test_only_the_codec_boundary_imports_protobuf():
    offenders = []
    for path in python_files():
        relative = path.relative_to(PACKAGE_ROOT)
        if relative in ALLOWED_PB_IMPORTERS:
            continue
        if any(name.startswith("bazaar_pb2") for name in imported_modules(path.read_text())):
            offenders.append(str(relative))

    assert not offenders, f"protobuf leaked outside the codec boundary: {offenders}"


SIM_ROOT = PACKAGE_ROOT.parent / "bazaar_sim"


def test_the_server_keeps_protobuf_behind_its_own_codec():
    offenders = [
        str(path.relative_to(SIM_ROOT)) for path in sorted(SIM_ROOT.rglob("*.py"))
        if path.name != "codec.py"
        and any(name.startswith("bazaar_pb2") for name in imported_modules(path.read_text()))
    ]

    assert not offenders, f"protobuf leaked outside bazaar_sim/codec.py: {offenders}"


def test_the_client_never_depends_on_the_simulator():
    """The client must run in a class game with nothing of ours on the other end."""
    offenders = [
        str(path.relative_to(PACKAGE_ROOT)) for path in python_files()
        if any(name.startswith("bazaar_sim") for name in imported_modules(path.read_text()))
    ]

    assert not offenders, f"bazaar_client imports bazaar_sim: {offenders}"


def test_policy_and_world_layers_never_import_the_connection_layer():
    """Decision code must stay runnable without a socket."""
    offenders = []
    for path in python_files():
        relative = path.relative_to(PACKAGE_ROOT)
        if relative.parts[0] not in {"policy", "world", "domain"}:
            continue
        for name in imported_modules(path.read_text()):
            if "connection" in name or name.startswith("websockets"):
                offenders.append(f"{relative} -> {name}")

    assert not offenders, f"decision layers reached into transport: {offenders}"
