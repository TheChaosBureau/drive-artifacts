# SPDX-License-Identifier: Apache-2.0
"""Every public class, function and method of the package says what it does."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "drive_artifacts"


def _undocumented(tree: ast.Module) -> list[str]:
    """Public definitions at module or class level without a docstring (nested helpers are
    private by position and exempt)."""
    missing: list[str] = []

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            if node.name.startswith("_"):
                continue
            if ast.get_docstring(node) is None:
                missing.append(prefix + node.name)
            if isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.")

    visit(tree.body, "")
    return missing


def test_every_public_definition_has_a_docstring() -> None:
    missing = {
        path.name: names
        for path in sorted(SRC.glob("*.py"))
        if (names := _undocumented(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert missing == {}
