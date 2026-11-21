"""Generate synthetic patches against the pinned sqlglot commit.

Usage: python3 tools/mutation/generate.py <sqlglot checkout> [patches per kind]
Writes .cache/mutants/<id>.patch and .cache/mutants/index.json.

These patches only test the selector's rules. Nothing in maps/ or selector.py
may be derived from them.
"""

import ast
import difflib
import io
import json
import random
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".cache" / "mutants"
SEED = 20261121
FLIPS = {"==": "!=", "!=": "==", "<": ">=", ">": "<=", "<=": ">", ">=": "<"}
TABLES = {"KEYWORDS", "SINGLE_TOKENS", "COMMANDS", "TOKENS", "QUOTES", "IDENTIFIERS",
          "FUNCTIONS", "TRANSFORMS", "TYPE_MAPPING", "NO_PAREN_FUNCTIONS", "PROPERTY_PARSERS"}


class Generator:
    def __init__(self, src: Path, per_kind: int) -> None:
        self.src, self.per_kind, self.rng = src, per_kind, random.Random(SEED)
        tracked = subprocess.check_output(["git", "-C", str(src), "ls-files"], text=True).split("\n")
        self.tracked = [path for path in tracked if path and (src / path).is_file()
                        and not (src / path).is_symlink()]
        self.source = [p for p in self.tracked if p.startswith("sqlglot/") and p.endswith(".py")
                       and not p.endswith("__main__.py")]
        self.tests = [p for p in self.tracked if re.search(r"^tests/(dialects/)?test_\w+\.py$", p)]
        self.tables = [p for p in self.source if re.search(r"/(dialects|parsers|generators|tokens)", p)
                       or p.endswith("tokens.py")]
        self.items: list[dict] = []
        self.seen: set[str] = set()
        self.cache: dict[str, tuple] = {}

    def text(self, path: str) -> str:
        return (self.src / path).read_text()

    def parsed(self, path: str):
        if path not in self.cache:
            text = self.text(path)
            tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
            self.cache[path] = (text, text.split("\n"), ast.parse(text), tokens)
        return self.cache[path]

    def doc_lines(self, path: str) -> set[int]:
        _, _, tree, _ = self.parsed(path)
        lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.body and isinstance(node.body[0], ast.Expr) \
                        and isinstance(node.body[0].value, ast.Constant) \
                        and isinstance(node.body[0].value.value, str):
                    lines.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
        return lines

    def emit(self, kind: str, path: str, new_text: str) -> bool:
        old = self.text(path).splitlines(keepends=True)
        new = new_text.splitlines(keepends=True)
        body = "".join(difflib.unified_diff(old, new, f"a/{path}", f"b/{path}", n=3))
        return bool(body) and self.save(
            kind, f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n{body}")

    def save(self, kind: str, patch: str) -> bool:
        if not patch.endswith("\n"):
            patch += "\n"
        if patch in self.seen:
            return False
        self.seen.add(patch)
        name = f"m{len(self.items):04d}"
        (OUT / f"{name}.patch").write_text(patch)
        self.items.append({"id": name, "kind": kind})
        return True

    def take(self, kind: str, files, make, compiles: bool | None = True) -> None:
        """Spread a kind's patches across files, one candidate per file per pass."""
        files = list(files)
        self.rng.shuffle(files)
        pools = []
        for path in files:
            try:
                found = list(make(path))
            except (SyntaxError, tokenize.TokenError, UnicodeDecodeError):
                continue
            self.rng.shuffle(found)
            if found:
                pools.append((path, found))
        count = 0
        while count < self.per_kind and pools:
            for pair in list(pools):
                path, found = pair
                if not found:
                    pools.remove(pair)
                    continue
                start, end, replacement = found.pop()
                lines = self.text(path).split("\n")
                lines[start - 1:end] = replacement
                new_text = "\n".join(lines)
                if path.endswith(".py") and compiles is not None:
                    try:
                        compile(new_text, path, "exec", dont_inherit=True)
                        valid = compiles
                    except (SyntaxError, ValueError):
                        valid = not compiles
                    if not valid:
                        continue
                if self.emit(kind, path, new_text):
                    count += 1
                    if count >= self.per_kind:
                        break
        print(f"{kind}: {count}")

    def token_edits(self, path: str, change):
        _, lines, _, tokens = self.parsed(path)
        docs = self.doc_lines(path)
        for token in tokens:
            row, col = token.start
            if token.end[0] != row or row in docs:
                continue
            new = change(token)
            if new is not None and new != token.string:
                line = lines[row - 1]
                yield row, row, [line[:col] + new + line[token.end[1]:]]

    def statements(self, path: str):
        _, _, tree, _ = self.parsed(path)
        for node in ast.walk(tree):
            for name in ("body", "orelse", "finalbody"):
                block = getattr(node, name, None)
                if isinstance(block, list) and len(block) > 1:
                    yield from (item for item in block if isinstance(item, ast.stmt))

    def table_entries(self, path: str, names=None):
        """Single-line entries of class-level dict tables, as (node, key, value)."""
        _, lines, tree, _ = self.parsed(path)
        for owner in ast.walk(tree):
            if not isinstance(owner, ast.ClassDef):
                continue
            for item in owner.body:
                if not isinstance(item, (ast.Assign, ast.AnnAssign)) or item.value is None:
                    continue
                target = item.targets[0] if isinstance(item, ast.Assign) else item.target
                name = getattr(target, "name", getattr(target, "id", ""))
                if names and name not in names:
                    continue
                if isinstance(item.value, ast.Dict):
                    for key, value in zip(item.value.keys, item.value.values):
                        if key is not None and key.lineno == value.end_lineno \
                                and lines[key.lineno - 1].rstrip().endswith(","):
                            yield item, key, value

    def table_changes(self, path: str, names=None):
        _, lines, _, _ = self.parsed(path)
        entries = list(self.table_entries(path, names))
        for index, (_, key, value) in enumerate(entries):
            yield key.lineno, key.lineno, []
            other = entries[(index + 1) % len(entries)][2]
            if other.lineno == other.end_lineno and value.lineno == value.end_lineno:
                source = lines[other.lineno - 1].encode()[other.col_offset:other.end_col_offset].decode()
                raw = lines[value.lineno - 1].encode()
                new = raw[:value.col_offset] + source.encode() + raw[value.end_col_offset:]
                yield key.lineno, key.lineno, [new.decode()]

    def run(self) -> None:
        def literal(token):
            if token.type == tokenize.STRING and token.string[-1] in "'\"" \
                    and not token.string.endswith(('"""', "'''")):
                return token.string[:-1] + "x" + token.string[-1]
            return None

        def number(token):
            return str(int(token.string) + 1) if token.type == tokenize.NUMBER \
                and token.string.isdigit() else None

        def boolean(token):
            return {"True": "False", "False": "True", "None": "False"}.get(token.string) \
                if token.type == tokenize.NAME else None

        src, tests = self.source, self.tests
        self.take("edit_number", src, lambda p: self.token_edits(p, number))
        self.take("edit_string", src, lambda p: self.token_edits(p, literal))
        self.take("edit_bool_none", src, lambda p: self.token_edits(p, boolean))
        self.take("flip_comparison", src, lambda p: self.token_edits(
            p, lambda t: FLIPS.get(t.string) if t.type == tokenize.OP else None))
        self.take("flip_and_or", src, lambda p: self.token_edits(
            p, lambda t: {"and": "or", "or": "and"}.get(t.string) if t.type == tokenize.NAME else None))

        def negate(path):
            for number_, line in enumerate(self.parsed(path)[1], 1):
                match = re.match(r"^(\s*)(if|elif|while) (.+):\s*$", line)
                if match:
                    yield number_, number_, [f"{match[1]}{match[2]} not ({match[3]}):"]

        def delete_statement(path):
            for node in self.statements(path):
                if node.lineno == node.end_lineno and isinstance(
                        node, (ast.Assign, ast.AugAssign, ast.Expr, ast.Return, ast.Raise, ast.AnnAssign)) \
                        and node.lineno not in self.doc_lines(path):
                    yield node.lineno, node.lineno, []

        def return_none(path):
            lines = self.parsed(path)[1]
            for node in ast.walk(self.parsed(path)[2]):
                if isinstance(node, ast.Return) and node.value is not None and node.lineno == node.end_lineno:
                    line = lines[node.lineno - 1]
                    yield node.lineno, node.lineno, [line[:node.col_offset] + "return None"]

        def default_argument(path):
            lines = self.parsed(path)[1]
            for node in ast.walk(self.parsed(path)[2]):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for item in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                        if isinstance(item, ast.Constant) and item.lineno == item.end_lineno \
                                and isinstance(item.value, (bool, int, type(None))):
                            raw = lines[item.lineno - 1].encode()
                            value = {True: "False", False: "True", None: "True"}.get(item.value, "7")
                            yield item.lineno, item.lineno, [
                                (raw[:item.col_offset] + value.encode() + raw[item.end_col_offset:]).decode()]

        def class_attribute(path):
            lines = self.parsed(path)[1]
            for owner in ast.walk(self.parsed(path)[2]):
                if isinstance(owner, ast.ClassDef):
                    for item in owner.body:
                        if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant) \
                                and item.lineno == item.end_lineno \
                                and isinstance(item.value.value, (bool, int, str)):
                            raw = lines[item.lineno - 1].encode()
                            value = item.value.value
                            new = repr(not value) if isinstance(value, bool) else (
                                repr(value + 1) if isinstance(value, int) else repr(value + "x"))
                            yield item.lineno, item.lineno, [
                                (raw[:item.value.col_offset] + new.encode() + raw[item.value.end_col_offset:]).decode()]

        self.take("negate_condition", src, negate)
        self.take("delete_statement", src, delete_statement)
        self.take("return_none", src, return_none)
        self.take("default_argument", src, default_argument)
        self.take("class_attribute", src, class_attribute)
        self.take("dialect_mapping", self.tables, lambda p: self.table_changes(p))
        self.take("tokenizer_table", self.tables, lambda p: self.table_changes(
            p, {"KEYWORDS", "SINGLE_TOKENS", "COMMANDS", "TOKENS", "QUOTES", "IDENTIFIERS"}))

        inits = [p for p in self.tracked if p.endswith("__init__.py") and p.startswith("sqlglot/")]

        def init_edit(path):
            yield from self.token_edits(path, literal)
            yield from delete_statement(path)
            lines = self.parsed(path)[1]
            for number_, line in enumerate(lines, 1):
                if re.match(r'^\s+"\w+",\s*$', line):
                    yield number_, number_, []

        self.take("init_edit", inits, init_edit)

        def add_comment(path):
            lines = self.parsed(path)[1]
            docs = self.doc_lines(path)
            for number_, line in enumerate(lines, 1):
                if line.strip() and number_ not in docs:
                    indent = line[: len(line) - len(line.lstrip())]
                    yield number_, number_, [f"{indent}# mutation note", line]

        def edit_comment(path):
            lines = self.parsed(path)[1]
            for token in self.parsed(path)[3]:
                if token.type == tokenize.COMMENT and lines[token.start[0] - 1].strip().startswith("#"):
                    row = token.start[0]
                    yield row, row, [lines[row - 1] + " edited"]

        def edit_docstring(path):
            lines = self.parsed(path)[1]
            for row in sorted(self.doc_lines(path)):
                line = lines[row - 1]
                if line.strip() and '"""' not in line and "'''" not in line:
                    yield row, row, [line + " edited"]

        self.take("add_comment", src, add_comment)
        self.take("edit_comment", src, edit_comment)
        self.take("edit_docstring", src, edit_docstring)
        self.take("test_file_value", tests, lambda p: self.token_edits(p, literal))
        self.take("test_file_delete", tests, delete_statement)
        helpers = [p for p in self.tracked if p.startswith("tests/") and p.endswith(".py")
                   and not re.search(r"/test_\w+\.py$", p) and "gen_fixtures" not in p]

        def helper_edit(path):
            yield from self.token_edits(path, literal)
            yield from self.token_edits(path, number)
            yield from delete_statement(path)

        self.take("test_helper", helpers, helper_edit)

        fixtures = [p for p in self.tracked if p.startswith("tests/fixtures/")
                    and p.endswith((".sql", ".json", ".txt", ".csv"))]

        def fixture_edit(path):
            for number_, line in enumerate(self.text(path).split("\n"), 1):
                if line.strip():
                    yield number_, number_, ["x " + line if not line.startswith("#") else line + " edited"]

        self.take("fixture", fixtures, fixture_edit, None)

        def text_edit(path):
            for number_, line in enumerate(self.text(path).split("\n"), 1):
                if line.strip():
                    yield number_, number_, [line + " x"]

        config = [p for p in self.tracked if p in ("pyproject.toml", "setup.cfg", "setup.py", "MANIFEST.in",
                                                   "Makefile", ".gitmodules", ".gitignore")
                  or (p.startswith("sqlglotc/"))]
        docs = [p for p in self.tracked if p.endswith((".md", ".yml", ".yaml", ".txt", ".jinja2"))
                and not p.startswith("tests/")]
        self.take("config_file", config, text_edit, None)
        self.take("docs_file", docs, text_edit, None)

        def add_function(path):
            for node in ast.walk(self.parsed(path)[2]):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    indent = " " * node.col_offset
                    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    self_arg = "self" if node.col_offset else ""
                    yield first, first, [f"{indent}def _mutation_added({self_arg}):",
                                         f"{indent}    return 1", "", self.parsed(path)[1][first - 1]]

        def remove_function(path):
            for node in ast.walk(self.parsed(path)[2]):
                if isinstance(node, ast.FunctionDef) and node.end_lineno - node.lineno <= 15:
                    # A function that was its block's only statement fails the compile check.
                    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    yield first, node.end_lineno, []

        def import_at_top(statement):
            def make(path):
                tree = self.parsed(path)[2]
                nodes = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
                         and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")]
                if nodes:
                    row = nodes[-1].end_lineno
                    yield row, row, [self.parsed(path)[1][row - 1], statement]
            return make

        def remove_import(path):
            for node in ast.walk(self.parsed(path)[2]):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno == node.end_lineno \
                        and getattr(node, "module", "") != "__future__":
                    yield node.lineno, node.lineno, []

        self.take("add_function", src, add_function)
        self.take("remove_function", src, remove_function)
        self.take("add_import", src, import_at_top("import colorsys as _mutation_import"))
        self.take("add_bad_import", src, import_at_top("import mutation_missing_module"))
        self.take("remove_import", src, remove_import)

        def syntax_error(path):
            lines = self.parsed(path)[1]
            docs = self.doc_lines(path)
            for token in self.parsed(path)[3]:
                row = token.start[0]
                if row in docs or token.end[0] != row:
                    continue
                if token.type == tokenize.OP and token.string in ")]}":
                    line = lines[row - 1]
                    yield row, row, [line[:token.start[1]] + line[token.end[1]:]]
                elif token.type == tokenize.NAME and token.string in ("def", "return", "if", "class"):
                    yield row, row, [" " + lines[row - 1] + " +"]

        def undefined_name(path):
            lines = self.parsed(path)[1]
            for node in ast.walk(self.parsed(path)[2]):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                        and node.lineno == node.end_lineno:
                    raw = lines[node.lineno - 1].encode()
                    yield node.lineno, node.lineno, [
                        (raw[:node.col_offset] + b"mutation_undefined" + raw[node.end_col_offset:]).decode()]

        self.take("syntax_error", src + tests[:8], syntax_error, False)
        self.take("undefined_name", src, undefined_name)
        self.file_operations()

    def file_operations(self) -> None:
        pool = self.source + [p for p in self.tracked if p.startswith("tests/fixtures/")
                              and p.endswith((".sql", ".json"))] + self.tests
        self.rng.shuffle(pool)
        for kind, count in (("delete_file", self.per_kind), ("rename_file", self.per_kind)):
            for path in pool[:count]:
                if kind == "delete_file":
                    old = self.text(path).splitlines(keepends=True)
                    body = "".join(difflib.unified_diff(old, [], f"a/{path}", "/dev/null", n=3))
                    self.save(kind, f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n"
                                    f"index 1111111..0000000\n{body}")
                else:
                    new = path.rsplit(".", 1)[0] + "_moved." + path.rsplit(".", 1)[1]
                    self.save(kind, f"diff --git a/{path} b/{new}\nsimilarity index 100%\n"
                                    f"rename from {path}\nrename to {new}\n")
            pool = pool[count:]
        for index in range(self.per_kind):
            folder = self.rng.choice(["sqlglot", "sqlglot/dialects", "sqlglot/optimizer", "tests", "tests/fixtures"])
            path = f"{folder}/mutation_new_{index}.{'sql' if 'fixtures' in folder else 'py'}"
            self.save("new_file", f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
                                  f"index 0000000..2222222\n--- /dev/null\n+++ b/{path}\n"
                                  f"@@ -0,0 +1 @@\n+VALUE = 1\n")


def main() -> None:
    src = Path(sys.argv[1])
    per_kind = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    generator = Generator(src, per_kind)
    generator.run()
    (OUT / "index.json").write_text(json.dumps(generator.items, indent=0))
    print(f"{len(generator.items)} patches in {len({i['kind'] for i in generator.items})} kinds")


if __name__ == "__main__":
    main()
