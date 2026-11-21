"""Reduce the raw data from the pinned commit into the maps selector.py reads.

Usage: python3 tools/build_maps.py <path to a sqlglot checkout at the pin>

Everything here comes from the pinned commit: per-test line coverage, the data
files each test opens, and the static import graph. Nothing reads the sample
patches or answers.

maps/py/<path with / as __>.json, one per Python source file:
  m  distinct test sets, each a hex bitmask over test numbers
  x  per line: index in m of the tests to run if the line is changed, or -1
  f  per line: index in m of the fallback for code inserted at the line, or -1
  s  lines inside a multi-line string, where a "comment" is really content
maps/data.json: {path: hex bitmask} for data files tests read
maps/meta.json: test count and the directories tests list
"""

import ast
import io
import json
import shutil
import sqlite3
import sys
import tokenize
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
MAPS = ROOT / "maps"


def numbits(blob: bytes):
    for index, byte in enumerate(blob):
        for bit in range(8):
            if byte & (1 << bit):
                yield index * 8 + bit


def load_coverage(number_of):
    """Returns {path: {line: mask}} for runs, and {path: set(lines)} at import."""
    db = sqlite3.connect(CACHE / "coverage.db")
    files = dict(db.execute("select id, path from file"))
    contexts = {}
    for context_id, context in db.execute("select id, context from context"):
        contexts[context_id] = number_of.get(context.rsplit("|", 1)[0]) if context else None
    run = defaultdict(lambda: defaultdict(int))
    imported = defaultdict(set)
    for file_id, context_id, blob in db.execute("select file_id, context_id, numbits from line_bits"):
        path = files[file_id].removeprefix("/src/")
        number = contexts[context_id]
        for line in numbits(blob):
            if number is None:
                imported[path].add(line)
            else:
                run[path][line] |= 1 << number
    return run, imported


def module_name(path: str) -> str:
    name = path[:-3].replace("/", ".")
    return name.removesuffix(".__init__")


def import_graph(src: Path, paths):
    """Returns {path: set(paths it imports)}, counting imports inside functions."""
    by_module = {module_name(path): path for path in paths}
    graph = {}
    for path in paths:
        tree = ast.parse((src / path).read_text())
        package = module_name(path).split(".")
        if not path.endswith("__init__.py"):
            package = package[:-1]
        targets = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = package[: len(package) - node.level + 1] if node.level else []
                module = ".".join(base + (node.module.split(".") if node.module else []))
                targets.add(module)
                targets.update(f"{module}.{alias.name}" for alias in node.names)
        found = set()
        for target in targets:
            parts = target.split(".")
            # Importing a.b.c also runs a and a.b.
            found.update(by_module[".".join(parts[:i])] for i in range(1, len(parts) + 1)
                         if ".".join(parts[:i]) in by_module)
        found.discard(path)
        graph[path] = found
    return graph


def closure(graph, start):
    seen, stack = set(), [start]
    while stack:
        for next_path in graph.get(stack.pop(), ()):
            if next_path not in seen:
                seen.add(next_path)
                stack.append(next_path)
    return seen


def comment_and_string_lines(source: str):
    comments, strings = set(), set()
    code = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments.add(token.start[0])
        elif token.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                            tokenize.ENDMARKER):
            continue
        else:
            code.update(range(token.start[0], token.end[0] + 1))
            if token.type == tokenize.STRING or token.type in (
                getattr(tokenize, "FSTRING_MIDDLE", -1),
            ):
                if token.end[0] > token.start[0]:
                    strings.update(range(token.start[0], token.end[0] + 1))
    return comments - code, code, strings


def line_rules(source, run, wide, doc_mask):
    """Per line: (mask if changed, fallback mask for code inserted there)."""
    count = source.count("\n") + 2
    tree = ast.parse(source)
    comments, code, _ = comment_and_string_lines(source)
    function_of = [None] * (count + 1)
    statement_of = [None] * (count + 1)
    docstrings = set()

    def first_line(node):
        decorators = getattr(node, "decorator_list", None)
        return min([node.lineno] + [item.lineno for item in decorators]) if decorators else node.lineno

    def union(start, end):
        mask = 0
        for line in range(start, end + 1):
            mask |= run.get(line, 0)
        return mask

    def visit(body, function):
        for node in body:
            if not isinstance(node, ast.stmt):
                continue
            start = first_line(node)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                docstrings.update(range(start, node.end_lineno + 1))
            is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            inner = (start, node.end_lineno) if is_function else function
            if is_function:
                for line in range(start, node.end_lineno + 1):
                    function_of[line] = inner
            blocks = [getattr(node, name) for name in ("body", "orelse", "finalbody")
                      if getattr(node, name, None)]
            blocks += [handler.body for handler in getattr(node, "handlers", [])]
            blocks += [case.body for case in getattr(node, "cases", [])]
            if blocks:
                header_end = min(block[0].lineno for block in blocks) - 1
                header_end = max(start, header_end)
            else:
                header_end = node.end_lineno
            for line in range(start, header_end + 1):
                statement_of[line] = (start, header_end)
            for block in blocks:
                visit(block, inner)

    visit(tree.body, None)

    change, insert = {}, {}
    for line in range(1, count + 1):
        function = function_of[line]
        fallback = union(*function) if function else wide
        insert[line] = fallback
        if line in docstrings:
            change[line] = doc_mask | (union(*statement_of[line]) if function else 0)
        elif line in comments or line not in code:
            change[line] = 0
        elif function is None:
            change[line] = wide
        elif statement_of[line] and statement_of[line][0] != function[0]:
            change[line] = union(*statement_of[line])
        else:
            change[line] = fallback  # a def line, decorator, else: or closing bracket
    return change, insert, count


def main() -> None:
    src = Path(sys.argv[1])
    tests = json.loads((ROOT / "sample/tests.json").read_text())["tests"]
    number_of = {test["id"]: test["number"] for test in tests}
    every = (1 << len(tests)) - 1
    by_file = defaultdict(int)
    for test in tests:
        by_file[test["file"]] |= 1 << test["number"]

    run, imported = load_coverage(number_of)
    paths = sorted(str(p.relative_to(src)) for root in ("sqlglot", "tests") for p in (src / root).rglob("*.py"))
    graph = import_graph(src, paths)

    importers = defaultdict(int)
    for test_file, mask in by_file.items():
        for path in closure(graph, test_file) | {test_file}:
            importers[path] |= mask
        # Modules the file really loaded, which includes lazily imported dialects.
        loaded = CACHE / "modules" / (test_file.replace("/", "__") + ".json")
        for path in json.loads(loaded.read_text()):
            importers[path] |= mask

    if MAPS.exists():
        shutil.rmtree(MAPS)
    (MAPS / "py").mkdir(parents=True)

    doc_mask = by_file.get("tests/test_docs.py", 0)
    total = 0
    for path in paths:
        source = (src / path).read_text()
        lines = run.get(path, {})
        used = 0
        for mask in lines.values():
            used |= mask
        wide = used | importers.get(path, 0)
        change, insert, count = line_rules(source, lines, wide, doc_mask)
        _, _, strings = comment_and_string_lines(source)
        masks, index = [], {}

        def intern(mask):
            if mask == 0:
                return -1
            if mask not in index:
                index[mask] = len(masks)
                masks.append(format(mask, "x"))
            return index[mask]

        record = {
            "x": [intern(change[line]) for line in range(1, count + 1)],
            "f": [intern(insert[line]) for line in range(1, count + 1)],
            "s": sorted(strings),
        }
        record["m"] = masks
        target = MAPS / "py" / (path.replace("/", "__") + ".json")
        target.write_text(json.dumps(record, separators=(",", ":")))
        total += target.stat().st_size

    opens = json.loads((CACHE / "opens.json").read_text())
    data, directories = {}, []
    for path, readers in opens.items():
        mask = 0
        for reader in readers:
            if reader.startswith("collect:"):
                mask |= by_file.get(reader[len("collect:"):], every)
            elif reader in number_of:
                mask |= 1 << number_of[reader]
        if (src / path).is_dir():
            directories.append(path)
        data[path] = format(mask, "x")
    (MAPS / "data.json").write_text(json.dumps(data, separators=(",", ":")))
    (MAPS / "meta.json").write_text(json.dumps({"tests": len(tests), "directories": directories}))

    unreachable = [p for p in paths if p.startswith("sqlglot/") and importers.get(p, 0) == 0]
    size = sum(p.stat().st_size for p in MAPS.rglob("*") if p.is_file())
    print(f"{len(paths)} python files, {len(data)} data files, {size / 1e6:.2f} MB")
    print(f"sqlglot modules no test imports statically: {len(unreachable)}", unreachable[:8])
    coverage_of = [bin(importers.get(p, 0)).count("1") for p in paths if p.startswith("sqlglot/dialects/")]
    if coverage_of:
        print("tests importing a dialect: min", min(coverage_of), "max", max(coverage_of))


if __name__ == "__main__":
    main()
