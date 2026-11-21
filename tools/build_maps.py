"""Reduce the raw data from the pinned commit into the maps selector.py reads.

Usage: python3 tools/build_maps.py <path to a sqlglot checkout at the pin>

Everything here comes from the pinned commit, measured by running each test
alone in a fresh process (tools/collect_isolated.sh): the lines it executed, the
files it read and the modules it loaded. Nothing reads the sample patches or
answers.

maps/py/<path with / as __>.json, one per Python source file:
  m  distinct test sets, each a hex bitmask over test numbers
  x  per line: index in m of the tests to run if the line is changed, or -1
  f  per line: index in m of the fallback for code inserted at the line, or -1
  s  lines of a multi-line string, where a removed "comment" is really content
  i  lines where text inserted before them lands inside a multi-line string
  t  class-level table or literal statements, as [first line, last line]
  e  {line: [index in m of the tests reading that entry or -1, index in t]}
maps/data.json: {path: hex bitmask} for files tests read as data or text
"""

import ast
import io
import json
import re
import shutil
import sqlite3
import sys
import tokenize
from collections import defaultdict
from pathlib import Path

from table_scan import scan

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
MAPS = ROOT / "maps"


def load_isolated():
    """Returns ({path: {line: mask}}, {path: mask}, {path: mask}) from isolated runs."""
    raw = json.loads((CACHE / "isolated.json").read_text())
    if raw["failed"]:
        raise SystemExit(f"tests failed while measuring: {raw['failed'][:5]}")
    run = defaultdict(lambda: defaultdict(int))
    for path, groups in raw["lines"].items():
        for mask, lines in groups.items():
            for line in lines:
                run[path][line] |= int(mask, 16)
    opens = {path: int(mask, 16) for path, mask in raw["opens"].items()}
    modules = {path: int(mask, 16) for path, mask in raw["modules"].items()}
    return run, opens, modules


def comment_and_string_lines(source: str):
    comments, strings, interior = set(), set(), set()
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
                    interior.update(range(token.start[0] + 1, token.end[0] + 1))
    return comments - code, code, strings, interior


def line_rules(source, run, wide, doc_mask):
    """Per line: (mask if changed, fallback mask for code inserted there)."""
    count = source.count("\n") + 2
    tree = ast.parse(source)
    comments, code, _, _ = comment_and_string_lines(source)
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
    by_file = defaultdict(int)
    for test in tests:
        by_file[test["file"]] |= 1 << test["number"]

    run, opens, modules = load_isolated()
    paths = sorted(str(p.relative_to(src)) for root in ("sqlglot", "tests") for p in (src / root).rglob("*.py"))

    if MAPS.exists():
        shutil.rmtree(MAPS)
    (MAPS / "py").mkdir(parents=True)

    doc_mask = by_file.get("tests/test_docs.py", 0)

    # Which tests read each class-level entry. Tests are not instrumented, so a
    # test file that names the attribute counts as reading all of it.
    traced = json.loads((CACHE / "entries.json").read_text())
    if traced["failed"] or traced["tests"] != len(tests):
        raise SystemExit("entry trace is incomplete")
    readers = {key: int(mask, 16) for key, mask in traced["entries"].items()}
    table_entries = scan(src)["entries"]
    test_text = {name: (src / name).read_text() for name in by_file}
    named: dict[str, int] = {}

    def naming_tests(name: str) -> int:
        if name not in named:
            pattern = re.compile(rf"\b{re.escape(name)}\b")
            named[name] = 0
            for file, text in test_text.items():
                if pattern.search(text):
                    named[name] |= by_file[file]
        return named[name]

    for path in paths:
        source = (src / path).read_text()
        lines = run.get(path, {})
        wide = modules.get(path, 0) | by_file.get(path, 0)
        for mask in lines.values():
            wide |= mask
        change, insert, count = line_rules(source, lines, wide, doc_mask)
        _, _, strings, interior = comment_and_string_lines(source)
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
            "i": sorted(interior),
        }
        statements, statement_index, by_line = [], {}, defaultdict(int)
        line_statement = {}
        for entry in table_entries.get(path, []):
            span = tuple(entry["statement"])
            if span not in statement_index:
                statement_index[span] = len(statements)
                statements.append(list(span))
            mask = readers.get(entry["id"], 0) | naming_tests(entry["name"])
            for line in range(entry["start"], entry["end"] + 1):
                by_line[line] |= mask
                line_statement[line] = statement_index[span]
        record["t"] = statements
        record["e"] = {str(line): [intern(by_line[line]), line_statement[line]] for line in sorted(line_statement)}
        record["m"] = masks
        (MAPS / "py" / (path.replace("/", "__") + ".json")).write_text(json.dumps(record, separators=(",", ":")))

    # Directory listings come from the import system on every run, so only files count.
    data = {path: format(mask, "x") for path, mask in sorted(opens.items()) if (src / path).is_file()}
    (MAPS / "data.json").write_text(json.dumps(data, separators=(",", ":")))

    size = sum(p.stat().st_size for p in MAPS.rglob("*") if p.is_file())
    print(f"{len(paths)} python files, {len(data)} files read by tests, {size / 1e6:.2f} MB, docs tests {bin(doc_mask).count('1')}")


if __name__ == "__main__":
    main()
