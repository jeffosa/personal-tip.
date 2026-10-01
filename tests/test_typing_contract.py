"""AST-enforced typing rules (ARCHITECTURE-ESSENTIALS 2.1, R-TYPE-1 to R-TYPE-3), three per module.

These tests check that annotations are present, not that they are correct: no static type checker
has been run (R-TYPE-10 is [N]).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "prop_alpha"
MODULES = sorted(
    path.relative_to(PACKAGE.parent).as_posix()
    for path in PACKAGE.rglob("*.py")
    if path.name != "__init__.py"
)
UPPER_CASE = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


def _tree(module: str) -> ast.Module:
    return ast.parse((PACKAGE.parent / module).read_text(encoding="utf-8"), filename=module)


def test_every_package_marker_is_empty() -> None:
    markers = list(PACKAGE.rglob("__init__.py"))
    assert len(markers) == 6
    assert all(marker.read_text(encoding="utf-8") == "" for marker in markers)


@pytest.mark.parametrize("module", MODULES)
def test_module_uses_postponed_annotations(module: str) -> None:
    body = _tree(module).body
    first = 1 if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) else 0
    statement = body[first]
    assert isinstance(statement, ast.ImportFrom), f"{module}: first statement is not an import"
    assert statement.module == "__future__" and [alias.name for alias in statement.names] == ["annotations"]


def _missing_annotations(tree: ast.Module) -> list[str]:
    problems: list[str] = []

    def visit(node: ast.AST, in_class: bool, qualname: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, True, f"{qualname}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{qualname}{child.name}"
                positional = child.args.posonlyargs + child.args.args
                is_static = any(
                    isinstance(d, ast.Name) and d.id == "staticmethod" for d in child.decorator_list
                )
                skip_first = in_class and not is_static and positional and positional[0].arg in ("self", "cls")
                arguments = positional[1:] if skip_first else positional
                arguments = [*arguments, *child.args.kwonlyargs]
                arguments += [a for a in (child.args.vararg, child.args.kwarg) if a is not None]
                problems.extend(f"{name}({arg.arg})" for arg in arguments if arg.annotation is None)
                if child.returns is None:
                    problems.append(f"{name} -> ?")
                visit(child, False, f"{name}.")
            else:
                visit(child, in_class, qualname)

    visit(tree, False, "")
    return problems


@pytest.mark.parametrize("module", MODULES)
def test_every_function_is_fully_annotated(module: str) -> None:
    assert _missing_annotations(_tree(module)) == []


def _non_final_constants(tree: ast.Module) -> list[str]:
    problems: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            is_typevar = (
                isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "TypeVar"
            )
            for target in node.targets:
                if isinstance(target, ast.Name) and UPPER_CASE.match(target.id) and not is_typevar:
                    problems.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and UPPER_CASE.match(node.target.id):
            annotation = node.annotation
            head = annotation.value if isinstance(annotation, ast.Subscript) else annotation
            if not (isinstance(head, ast.Name) and head.id == "Final"):
                problems.append(node.target.id)
    return problems


@pytest.mark.parametrize("module", MODULES)
def test_module_constants_are_final(module: str) -> None:
    assert _non_final_constants(_tree(module)) == []


@pytest.mark.parametrize(
    ("source", "detector"),
    [
        ("def f(x, y: int) -> int:\n    return y\n", _missing_annotations),
        ("def f(x: int):\n    return x\n", _missing_annotations),
        ("def outer() -> None:\n    def inner(cur):\n        return None\n", _missing_annotations),
        ("LIMIT = 3\n", _non_final_constants),
        ("from typing import Sequence\nLIMIT: Sequence[int] = (3,)\n", _non_final_constants),
    ],
)
def test_typing_detectors_flag_deliberate_violations(source: str, detector: object) -> None:
    assert callable(detector)
    assert detector(ast.parse(source))
