#!/usr/bin/env python3
"""
Architecture dependency checker for search-service.

Enforces layer boundaries:
- providers must not import from routes or mcp
- mcp must not import from routes
- provider implementations must not cross-import each other
- routes must not directly import provider classes (use registry)
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

RULES: list[dict] = [
    {
        "name": "providers must not import routes",
        "files": "app/providers/**/*.py",
        "forbidden": ["app.routes", "app.mcp"],
    },
    {
        "name": "mcp must not import routes",
        "files": "app/mcp/**/*.py",
        "forbidden": ["app.routes"],
    },
    {
        "name": "routes must not import provider classes directly",
        "files": "app/routes/**/*.py",
        "forbidden": [
            "app.providers.brave",
            "app.providers.tavily",
            "app.providers.firecrawl",
            "app.providers.deepseek",
        ],
    },
    {
        "name": "providers must not cross-import",
        "files": "app/providers/brave.py",
        "forbidden": ["app.providers.tavily"],
    },
    {
        "name": "providers must not cross-import",
        "files": "app/providers/tavily.py",
        "forbidden": ["app.providers.brave"],
    },
    {
        "name": "providers must not cross-import",
        "files": "app/providers/firecrawl.py",
        "forbidden": ["app.providers.brave", "app.providers.tavily"],
    },
    {
        "name": "providers must not cross-import",
        "files": "app/providers/deepseek.py",
        "forbidden": ["app.providers.brave", "app.providers.tavily", "app.providers.firecrawl"],
    },
]


def get_imports(filepath: Path) -> list[str]:
    """Extract all import module paths from a Python file."""
    try:
        tree = ast.parse(filepath.read_text())
    except SyntaxError:
        return []

    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def check_rule(rule: dict) -> list[str]:
    """Check a single rule, return list of violation messages."""
    violations: list[str] = []
    pattern = rule["files"]

    # Support both glob and single file
    if "*" in pattern:
        files = list(ROOT.glob(pattern))
    else:
        target = ROOT / pattern
        files = [target] if target.exists() else []

    for filepath in files:
        if filepath.name == "__init__.py" and filepath.stat().st_size == 0:
            continue
        imports = get_imports(filepath)
        for imp in imports:
            for forbidden in rule["forbidden"]:
                if imp == forbidden or imp.startswith(forbidden + "."):
                    rel = filepath.relative_to(ROOT)
                    violations.append(f"  {rel}: imports '{imp}' (rule: {rule['name']})")
    return violations


def main() -> int:
    print("Checking architecture rules...")
    all_violations: list[str] = []

    for rule in RULES:
        violations = check_rule(rule)
        all_violations.extend(violations)

    if all_violations:
        print(f"\nFOUND {len(all_violations)} VIOLATION(S):\n")
        for v in all_violations:
            print(v)
        return 1

    print(f"All {len(RULES)} rules passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
