"""Static dependency boundaries for reusable domain packages."""

from __future__ import annotations

import ast
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_ROOT = REPOSITORY_ROOT / "packages"


def _application_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names if alias.name.startswith("apps."))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "apps" or module.startswith("apps."):
                imports.append(module)
    return imports


def test_packages_do_not_depend_on_application_modules() -> None:
    violations = {
        path.relative_to(REPOSITORY_ROOT).as_posix(): imports
        for path in PACKAGES_ROOT.rglob("*.py")
        if (imports := _application_imports(path))
    }

    assert violations == {}, (
        "Reusable packages must remain independent from apps; move shared domain "
        f"contracts below packages instead: {violations}"
    )
