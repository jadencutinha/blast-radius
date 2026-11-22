"""Select and pack the tests each patch can break, using maps of the pinned commit.

No tests run here. The maps in maps/ come from tools/build_maps.py and hold only
what was measured or parsed at the pinned commit.
"""

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

DATA = Path("/data")
HERE = Path(__file__).resolve().parent
MAPS = HERE / "maps"
SOURCE = HERE / "src"
MAX_WORKERS = 8
WORKER_COST_MS = 2000

HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Added or removed code that defines or imports names can matter wherever the
# module is used, including through dispatch by method name.
DEFINES = ("def ", "async def ", "class ", "@", "import ", "from ")
# Paths no test can depend on, unless the maps recorded a test reading them.
SAFE_PREFIXES = ("posts/", ".github/", "benchmarks/")
SAFE_FILES = {
    "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", "LICENSE",
    "sqlglot.png", ".gitignore", ".gitpod.yml", ".pre-commit-config.yaml",
}


class Change:
    def __init__(self, old: str, new: str) -> None:
        self.path = old
        self.paths = {old, new}
        self.whole_file = old != new  # rename or copy
        self.has_hunk = False
        self.removed: list[int] = []
        self.removed_text: list[str] = []
        self.inserted: list[tuple[int, str]] = []


def parse(text: str) -> list[Change] | None:
    """The patch's file changes, or None when it cannot be read with confidence."""
    changes: list[Change] = []
    lines = text.split("\n")
    index = 0
    current = None
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/(.+) b/(.+)", line)
            if not match:
                return None  # quoted or unusual paths
            current = Change(match[1], match[2])
            changes.append(current)
        elif current is None:
            continue
        elif line.startswith(("--- /dev/null", "+++ /dev/null", "Binary files", "GIT binary",
                              "old mode", "new mode", "new file", "deleted file", "rename ",
                              "copy ", "similarity ")):
            current.whole_file = True
        elif line.startswith("@@ "):
            hunk = HUNK.match(line)
            if not hunk:
                return None
            current.has_hunk = True
            old_line = int(hunk[1])
            old_left = int(hunk[2]) if hunk[2] is not None else 1
            new_left = int(hunk[4]) if hunk[4] is not None else 1
            if old_left == 0:
                old_line += 1  # git names the line before a pure insertion
            while old_left > 0 or new_left > 0:
                if index >= len(lines):
                    return None  # truncated hunk
                body = lines[index]
                index += 1
                if body.startswith("\\"):
                    continue
                if body.startswith("-"):
                    current.removed.append(old_line)
                    current.removed_text.append(body[1:])
                    old_line += 1
                    old_left -= 1
                elif body.startswith("+"):
                    current.inserted.append((old_line, body[1:]))
                    new_left -= 1
                else:
                    old_line += 1
                    old_left -= 1
                    new_left -= 1
    if not changes and text.strip():
        return None
    for change in changes:
        if not change.has_hunk:
            change.whole_file = True
    return changes


class Maps:
    def __init__(self, tests: list[dict]) -> None:
        self.every = (1 << len(tests)) - 1
        self.by_file: dict[str, int] = {}
        for test in tests:
            self.by_file[test["file"]] = self.by_file.get(test["file"], 0) | 1 << test["number"]
        self.data = json.loads((MAPS / "data.json").read_text())
        self.files: dict[str, dict | None] = {}
        self.fired: list[str] = []  # which run-all rules fired, for tools/mutation
        self.source_words: set[bytes] | None = None  # filled only when a patch needs it

    def run_all(self, rule: str) -> int:
        self.fired.append(rule)
        return self.every

    def source(self, path: str):
        if path not in self.files:
            target = MAPS / "py" / (path.replace("/", "__") + ".json")
            if target.exists():
                record = json.loads(target.read_text())
                record["m"] = [int(mask, 16) for mask in record["m"]]
                record["s"] = set(record["s"])
                record["i"] = set(record["i"])
                self.files[path] = record
            else:
                self.files[path] = None
        return self.files[path]


def at(record: dict, key: str, line: int) -> int | None:
    """The mask stored for a line, or None when the line is past the map."""
    values = record[key]
    if line < 1 or line > len(values):
        return None
    return record["m"][values[line - 1]] if values[line - 1] >= 0 else 0


def is_code(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith("#")


def patched_text(change: Change) -> str | None:
    """The file after the change, or None when the pinned source cannot be read."""
    try:
        original = (SOURCE / change.path).read_text().split("\n")
    except (OSError, UnicodeDecodeError):
        return None
    removed = set(change.removed)
    inserted: dict[int, list[str]] = {}
    for line, text in change.inserted:
        inserted.setdefault(line, []).append(text)
    result: list[str] = []
    for number in range(1, len(original) + 2):
        result.extend(inserted.get(number, ()))
        if number <= len(original) and number not in removed:
            result.append(original[number - 1])
    return "\n".join(result)


def still_compiles(change: Change) -> bool:
    """Apply the change to the pinned source and compile it."""
    text = patched_text(change)
    if text is None:
        return False
    try:
        compile(text, change.path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    return True


def name_is_unused(name: str, maps: "Maps") -> bool:
    """True when the name appears in no pinned Python file, so nothing can refer to it."""
    if maps.source_words is None:
        words: set[bytes] = set()
        for path in SOURCE.rglob("*.py"):
            words.update(re.findall(rb"\w+", path.read_bytes()))
        maps.source_words = words
    return name.encode() not in maps.source_words


def is_inert_statement(node, future_annotations: bool, maps: "Maps") -> bool:
    """True for a new function that never runs, or a new import that cannot fail."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = node.args
        every = args.posonlyargs + args.args + args.kwonlyargs + [a for a in (args.vararg, args.kwarg) if a]
        annotated = node.returns is not None or any(a.annotation is not None for a in every)
        doc = ast.get_docstring(node) or ""
        return not (node.decorator_list or args.defaults or any(args.kw_defaults)
                    or (annotated and not future_annotations) or ">>>" in doc
                    or node.name.startswith("__")) and name_is_unused(node.name, maps)
    if isinstance(node, ast.Import) and len(node.names) == 1:
        item = node.names[0]
        return (item.name in sys.stdlib_module_names and not item.name.startswith("_")
                and item.name not in ("this", "antigravity")
                and importlib.util.find_spec(item.name) is not None
                and name_is_unused(item.asname or item.name, maps))
    return False


def only_adds_inert_code(change: Change, maps: "Maps") -> bool:
    """True when the module is unchanged apart from new unused top-level functions or imports."""
    if change.removed or not change.path.startswith("sqlglot/"):
        return False
    text = patched_text(change)
    try:
        old = ast.parse((SOURCE / change.path).read_text()).body
        new = ast.parse(text).body
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError, TypeError):
        return False
    if len(new) <= len(old) or (old and ast.dump(old[0]) != ast.dump(new[0])):
        return False  # keeps the module docstring and any __future__ import first
    future = any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
                 and any(item.name == "annotations" for item in node.names) for node in old)
    position = 0
    for node in new:
        if position < len(old) and ast.dump(node) == ast.dump(old[position]):
            position += 1
        elif not is_inert_statement(node, future, maps):
            return False
    return position == len(old)


def is_test_module(path: str) -> bool:
    return path.startswith("tests/") and path.rsplit("/", 1)[-1].startswith("test_")


def replaced_positions(change: Change) -> set[int]:
    """Insert positions whose lines replace removed code one for one at the same indent.

    Such a line runs exactly when the line it replaces ran, so the removed line's
    tests already cover it.
    """
    old = dict(zip(change.removed, change.removed_text))
    groups: dict[int, list[str]] = {}
    for line, text in change.inserted:
        groups.setdefault(line, []).append(text)
    indent = lambda text: len(text) - len(text.lstrip())
    replaced = set()
    for line, texts in groups.items():
        before = [old.get(line - len(texts) + offset) for offset in range(len(texts))]
        if all(item is not None and is_code(item) and is_code(text) and indent(item) == indent(text)
               for item, text in zip(before, texts)):
            replaced.add(line)
    return replaced


def shape(node):
    """A node's structure with every constant's value erased."""
    if isinstance(node, ast.Constant):
        return "constant"
    if isinstance(node, ast.AST):
        return type(node).__name__, tuple((name, shape(getattr(node, name, None))) for name in node._fields)
    if isinstance(node, list):
        return tuple(shape(item) for item in node)
    return node


def is_safe_value_edit(old, new, source: str) -> bool:
    """True when a table value's edit cannot raise while the class is being created."""
    if shape(old) == shape(new):
        return True  # only constants changed
    if isinstance(old, ast.Lambda) and isinstance(new, ast.Lambda):
        # A lambda's body runs only when a test reads the entry and calls it.
        return not new.args.defaults and not any(new.args.kw_defaults)
    names = []
    for node in ast.walk(new):
        if isinstance(node, ast.Call):
            return False
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
    return all(re.search(rf"\b{re.escape(name)}\b", source) for name in names)


def parse_statement(lines: list[str]):
    try:
        body = ast.parse("if 1:\n" + "\n".join(lines)).body[0].body
    except (SyntaxError, ValueError, IndexError):
        return None
    return body[0] if len(body) == 1 and isinstance(body[0], (ast.Assign, ast.AnnAssign)) else None


def same_statement_edit(old, new, source: str) -> bool:
    """True when a table keeps its keys, or only loses some, and its values stay safe."""
    if old is None or new is None or type(old) is not type(new):
        return False
    old_target = old.targets if isinstance(old, ast.Assign) else old.target
    new_target = new.targets if isinstance(new, ast.Assign) else new.target
    if ast.dump(ast.Module(old_target if isinstance(old_target, list) else [old_target], [])) != \
            ast.dump(ast.Module(new_target if isinstance(new_target, list) else [new_target], [])):
        return False
    if isinstance(old.value, ast.Dict) and isinstance(new.value, ast.Dict):
        old_items = [(ast.dump(key) if key else None, value)
                     for key, value in zip(old.value.keys, old.value.values)]
        position = 0
        for key, value in zip(new.value.keys, new.value.values):
            wanted = ast.dump(key) if key else None
            while position < len(old_items) and old_items[position][0] != wanted:
                if old_items[position][0] is None:
                    return False  # a ** spread was removed
                position += 1
            if position == len(old_items):
                return False  # a key was added or changed
            previous = old_items[position][1]
            position += 1
            if wanted is None:
                if ast.dump(previous) != ast.dump(value):
                    return False
            elif not is_safe_value_edit(previous, value, source):
                return False
        return all(key is not None for key, _ in old_items[position:])
    try:
        ast.literal_eval(old.value)
        ast.literal_eval(new.value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def narrow_table_edit(change: Change, record: dict) -> int | None:
    """Tests reading the edited table entries, or None when the edit is not eligible."""
    entries = record["e"]
    if not change.removed or any(str(line) not in entries for line in change.removed):
        return None
    replaced = replaced_positions(change)
    if any(line not in replaced for line, _ in change.inserted):
        return None
    groups: dict[int, list[str]] = {}
    for line, text in change.inserted:
        groups.setdefault(line, []).append(text)
    replacement = {line - len(texts) + offset: text for line, texts in groups.items()
                   for offset, text in enumerate(texts)}
    try:
        source = (SOURCE / change.path).read_text()
    except (OSError, UnicodeDecodeError):
        return None
    original = source.split("\n")
    removed = set(change.removed)
    mask = 0
    for index in {entries[str(line)][1] for line in change.removed}:
        first, last = record["t"][index]
        old_lines = original[first - 1:last]
        new_lines = [replacement.get(number, original[number - 1]) for number in range(first, last + 1)
                     if number not in removed or number in replacement]
        if not same_statement_edit(parse_statement(old_lines), parse_statement(new_lines), source):
            return None
    for line in change.removed:
        position = entries[str(line)][0]
        if position >= 0:
            mask |= record["m"][position]
    return mask


def select_python(change: Change, record: dict, maps: Maps) -> int:
    strings = record["s"]
    removed = [text for line, text in zip(change.removed, change.removed_text)
               if is_code(text) or line in strings]
    interior = record["i"]
    added = [text for line, text in change.inserted if is_code(text) or line in interior]
    if not removed and not added:
        return 0  # only blank lines and comments outside any string
    if not still_compiles(change):
        return maps.run_all("syntax_error")
    name = change.path.rsplit("/", 1)[-1]
    if name == "__init__.py" or name == "conftest.py":
        return maps.run_all("init_or_conftest")  # package exports and dialect registration
    if change.path.startswith("tests/") and not is_test_module(change.path):
        return maps.run_all("test_helper")  # helpers shared across test files
    if only_adds_inert_code(change, maps):
        return 0

    narrowed = narrow_table_edit(change, record)
    if narrowed is not None:
        return narrowed

    mask = 0
    if any(text.strip().startswith(DEFINES) for text in removed + added):
        if not is_test_module(change.path):
            return maps.run_all("defines_name")
        mask |= maps.by_file.get(change.path, maps.every)
    for line in change.removed:
        found = at(record, "x", line)
        if found is None:
            return maps.run_all("line_past_map")
        mask |= found
    replaced = replaced_positions(change)
    for line, text in change.inserted:
        if line in replaced or (not is_code(text) and line not in interior):
            continue
        here, before = at(record, "x", line), at(record, "x", line - 1)
        fallback = at(record, "f", line)
        if here is None or fallback is None:
            return maps.run_all("line_past_map")
        mask |= here | (before or 0) | fallback
    return mask


def is_safe(path: str, maps: Maps) -> bool:
    return path not in maps.data and (path in SAFE_FILES or path.startswith(SAFE_PREFIXES))


def select(changes: list[Change] | None, maps: Maps) -> int:
    if changes is None:
        return maps.run_all("unparseable")
    mask = 0
    for change in changes:
        if all(is_safe(path, maps) for path in change.paths):
            continue
        if change.whole_file:
            return maps.run_all("whole_file")
        path = change.path
        if path in maps.data:
            mask |= int(maps.data[path], 16)  # read as data or as source text
        if path.endswith(".py"):
            record = maps.source(path)
            if record is None:
                return maps.run_all("unknown_python")
            mask |= select_python(change, record, maps)
        elif path not in maps.data:
            return maps.run_all("unknown_file")
    return mask


def numbers(mask: int) -> list[int]:
    found = []
    while mask:
        low = mask & -mask
        found.append(low.bit_length() - 1)
        mask ^= low
    return found


def pack(selected: list[int], millis: list[int]) -> list[list[int]]:
    if not selected:
        return []
    ordered = sorted(selected, key=lambda number: millis[number], reverse=True)
    best, best_cost = None, None
    for count in range(1, MAX_WORKERS + 1):
        workers: list[list[int]] = [[] for _ in range(count)]
        loads = [0] * count
        for number in ordered:
            lightest = loads.index(min(loads))
            workers[lightest].append(number)
            loads[lightest] += millis[number]
        cost = max(loads) + WORKER_COST_MS * count
        if best_cost is None or cost < best_cost:
            best, best_cost = workers, cost
    return [worker for worker in best if worker]


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    millis = [round(test["seconds"] * 1000) for test in tests]
    maps = Maps(tests)
    selected_total = 0
    patches = sorted((DATA / "commits").glob("*.patch"))
    for patch in patches:
        changes = parse(patch.read_text(errors="replace"))
        chosen = numbers(select(changes, maps))
        selected_total += len(chosen)
        print(json.dumps({"commit": patch.stem, "workers": pack(chosen, millis)}, separators=(",", ":")))
    print(f"selected {selected_total} tests across {len(patches)} commits", file=sys.stderr)


if __name__ == "__main__":
    main()
