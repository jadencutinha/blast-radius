"""Find class-level tables and literal attributes in sqlglot's source.

Used while measuring (tools/trace_entries.py) and while building maps. An entry's
id is "<path>:<line>", the line of a dict key or of a literal assignment.
"""

import ast
from pathlib import Path


def class_scope_loads(body) -> set[str]:
    """Names read directly in a class body, where attribute tracing cannot see them."""
    names: set[str] = set()
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        stack.extend(ast.iter_child_nodes(node))
    return names


def is_literal(node) -> bool:
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def scan_file(path: str, source: str) -> tuple[set[str], dict[str, str], list[dict]]:
    module = path[:-3].replace("/", ".").removesuffix(".__init__")
    names: set[str] = set()
    registry: dict[str, str] = {}
    entries: list[dict] = []

    def visit(body, prefix: str) -> None:
        for node in body:
            if not isinstance(node, ast.ClassDef):
                continue
            qualname = f"{prefix}{node.name}"
            loads = class_scope_loads(node.body)
            for item in node.body:
                if isinstance(item, ast.Assign) and len(item.targets) == 1:
                    target = item.targets[0]
                elif isinstance(item, ast.AnnAssign) and item.value is not None:
                    target = item.target
                else:
                    continue
                if not isinstance(target, ast.Name):
                    continue
                name = target.id
                names.add(name)
                registry[f"{module}:{qualname}:{name}"] = f"{path}:{item.lineno}"
                statement = [item.lineno, item.end_lineno]
                if isinstance(item.value, ast.Dict):
                    for key, value in zip(item.value.keys, item.value.values):
                        if key is not None:
                            entries.append({"id": f"{path}:{key.lineno}", "start": key.lineno,
                                            "end": value.end_lineno, "statement": statement, "name": name})
                elif is_literal(item.value) and name not in loads:
                    entries.append({"id": f"{path}:{item.lineno}", "start": item.lineno,
                                    "end": item.end_lineno, "statement": statement, "name": name})
            visit(node.body, f"{qualname}.")

    visit(ast.parse(source).body, "")
    return names, registry, entries


def scan(root: Path) -> dict:
    names: set[str] = set()
    registry: dict[str, str] = {}
    entries: dict[str, list[dict]] = {}
    for file in sorted((root / "sqlglot").rglob("*.py")):
        path = str(file.relative_to(root))
        found_names, found_registry, found_entries = scan_file(path, file.read_text())
        names |= found_names
        registry.update(found_registry)
        if found_entries:
            entries[path] = found_entries
    return {"names": sorted(names), "registry": registry, "entries": entries}
