"""Select and pack the tests each patch can break, using maps of the pinned commit.

No tests run here. The maps in maps/ come from tools/build_maps.py and hold only
what was measured or parsed at the pinned commit.
"""

import json
import re
import sys
from pathlib import Path

DATA = Path("/data")
MAPS = Path(__file__).resolve().parent / "maps"
MAX_WORKERS = 8
WORKER_COST_MS = 2000

HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
ROOT_CONFIG = {"pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", "conftest.py"}
# Added code that defines or imports names can matter anywhere the module is used.
DEFINES = ("def ", "async def ", "class ", "@", "import ", "from ")


class Change:
    def __init__(self, old: str, new: str) -> None:
        self.path = new if old == "/dev/null" else old
        self.whole_file = old == "/dev/null" or new == "/dev/null" or old != new
        self.removed: list[int] = []
        self.removed_text: list[str] = []
        self.inserted: list[tuple[int, str]] = []


def parse(text: str) -> list[Change]:
    changes: list[Change] = []
    lines = text.split("\n")
    index = 0
    current = None
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+) b/(.+)$", line)
            current = Change(match[1], match[2]) if match else None
            if current:
                changes.append(current)
        elif current and line.startswith("--- "):
            old = line[4:].split("\t")[0]
            if old == "/dev/null":
                current.whole_file = True
        elif current and line.startswith("+++ "):
            if line[4:].split("\t")[0] == "/dev/null":
                current.whole_file = True
        elif current and (line.startswith("Binary files") or line.startswith("GIT binary")):
            current.whole_file = True
        elif current and line.startswith("@@ "):
            hunk = HUNK.match(line)
            if not hunk:
                current.whole_file = True
                continue
            old_line = int(hunk[1])
            old_left = int(hunk[2]) if hunk[2] is not None else 1
            new_left = int(hunk[4]) if hunk[4] is not None else 1
            if old_left == 0:
                old_line += 1  # git names the line before a pure insertion
            while (old_left > 0 or new_left > 0) and index < len(lines):
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
    return changes


class Maps:
    def __init__(self, test_count: int) -> None:
        self.every = (1 << test_count) - 1
        self.data = json.loads((MAPS / "data.json").read_text())
        self.files: dict[str, dict | None] = {}

    def source(self, path: str):
        if path not in self.files:
            target = MAPS / "py" / (path.replace("/", "__") + ".json")
            if target.exists():
                record = json.loads(target.read_text())
                record["m"] = [int(mask, 16) for mask in record["m"]]
                record["s"] = set(record["s"])
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


def balance(lines: list[str]) -> tuple[int, int, int]:
    joined = "".join(lines)
    return (
        joined.count("(") - joined.count(")"),
        joined.count("[") - joined.count("]"),
        joined.count("{") - joined.count("}"),
    )


def may_break_module(change: Change) -> bool:
    """True when an edit changes brackets or indentation, so the file may not import."""
    old = [text for text in change.removed_text if is_code(text)]
    new = [text for _, text in change.inserted if is_code(text)]
    if balance(old) != balance(new):
        return True
    indent = lambda text: len(text) - len(text.lstrip())
    return len(old) == len(new) and sorted(map(indent, old)) != sorted(map(indent, new))


def select_python(change: Change, record: dict, maps: Maps) -> int:
    if may_break_module(change):
        return maps.every

    mask = 0
    for line in change.removed:
        found = at(record, "x", line)
        if found is None:
            return maps.every
        mask |= found
    for line, text in change.inserted:
        if not is_code(text) and line not in record["s"]:
            continue  # a blank or comment line outside any string runs nothing
        if text.strip().startswith(DEFINES):
            return maps.every
        here, before = at(record, "x", line), at(record, "x", line - 1)
        fallback = at(record, "f", line)
        if here is None or fallback is None:
            return maps.every
        mask |= here | (before or 0) | fallback
    return mask


def select(changes: list[Change], maps: Maps) -> int:
    mask = 0
    for change in changes:
        path = change.path
        record = maps.source(path) if path.endswith(".py") else None
        if record is not None and not change.whole_file:
            mask |= select_python(change, record, maps)
        elif path in maps.data and not change.whole_file:
            mask |= int(maps.data[path], 16)
        elif path in ROOT_CONFIG or path.endswith(".py") or path.startswith(("sqlglot/", "tests/")):
            mask |= maps.every
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
    maps = Maps(len(tests))
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
