"""Contract test: every `notifier.<method>(...)` call site in the bot must resolve
to a real public method on `DiscordNotifier`.

This catches the BUG-026-class regression where a notifier method was renamed or
removed but the call sites kept invoking the old name, silently failing in
production because every such call lives inside a try/except or fire-and-forget
asyncio.create_task().
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from notifications import DiscordNotifier

BACKEND_ROOT = Path(__file__).resolve().parent.parent
CALLER_FILES = [
    BACKEND_ROOT / "coordinator.py",
    BACKEND_ROOT / "main.py",
]


def _extract_notifier_method_calls(path: Path) -> set[str]:
    """Return the set of public attribute names accessed on a `notifier`
    or `get_notifier()` expression in the given file."""
    tree = ast.parse(path.read_text())
    methods: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr.startswith("_"):
            continue

        value = node.value
        if isinstance(value, ast.Name) and value.id == "notifier":
            methods.add(node.attr)
            continue
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "get_notifier"
        ):
            methods.add(node.attr)
    return methods


def test_all_notifier_calls_resolve():
    public_api = {
        name for name, _ in inspect.getmembers(DiscordNotifier)
        if not name.startswith("_")
    }
    failures: dict[str, set[str]] = {}
    for caller in CALLER_FILES:
        assert caller.exists(), f"caller file not found: {caller}"
        called = _extract_notifier_method_calls(caller)
        missing = called - public_api
        if missing:
            failures[caller.name] = missing
    assert not failures, (
        "notifier calls reference methods that don't exist on DiscordNotifier: "
        f"{failures}"
    )
