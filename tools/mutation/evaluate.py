"""Check the selector's rules against the measured mutation results.

Usage:
  python3 tools/mutation/evaluate.py <sqlglot checkout>          report by kind
  python3 tools/mutation/evaluate.py <sqlglot checkout> --order  write the run order

Only reads results to score the rules. Nothing here feeds maps/ or selector.py.
"""

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import selector  # noqa: E402

MUTANTS = ROOT / ".cache" / "mutants"
RESULTS = ROOT / ".cache" / "mutation_results.jsonl"


def setup(source: str):
    selector.SOURCE = Path(source)
    tests = json.loads((ROOT / "sample/tests.json").read_text())["tests"]
    return tests, selector.Maps(tests)


def chosen(name: str, maps) -> int:
    return selector.select(selector.parse((MUTANTS / f"{name}.patch").read_text(errors="replace")), maps)


def write_order(maps) -> None:
    index = json.loads((MUTANTS / "index.json").read_text())
    rng = random.Random(7)
    rng.shuffle(index)
    narrow = [item["id"] for item in index if chosen(item["id"], maps) != maps.every]
    wide = [item["id"] for item in index if chosen(item["id"], maps) == maps.every]
    samples = sorted(path.stem for path in MUTANTS.glob("sample_*.patch"))
    (MUTANTS / "order.txt").write_text("\n".join(["baseline"] + samples + narrow + wide) + "\n")
    print(f"{len(narrow)} patches with a narrowed selection run first, then {len(wide)} that run every test")


def cost(mask: int, millis) -> float:
    workers = selector.pack(selector.numbers(mask), millis)
    if not workers:
        return 0.0
    return (max(sum(millis[n] for n in w) for w in workers) + 2000 * len(workers)) / 1000


def report(tests, maps) -> None:
    kinds = {item["id"]: item["kind"] for item in json.loads((MUTANTS / "index.json").read_text())}
    answers = json.loads((ROOT / "sample/answers.json").read_text())["commits"]
    millis = [round(test["seconds"] * 1000) for test in tests]
    stats = defaultdict(lambda: defaultdict(float))
    missed = []
    label_errors = []
    for row in RESULTS.read_text().splitlines():
        result = json.loads(row)
        name = result["id"]
        if name == "baseline":
            if result["failing"]:
                print("BASELINE HAS FAILURES", len(result["failing"]))
            continue
        if result["status"].startswith("apply_failed"):
            stats[kinds.get(name, "sample")]["unapplied"] += 1
            continue
        if name.startswith("sample_"):
            if set(result["failing"]) != set(answers[name[7:]]["failing"]):
                label_errors.append(name)
            kind = "sample"
        else:
            kind = kinds[name]
        mask = chosen(name, maps)
        failing = set(result["failing"])
        lost = sorted(n for n in failing if not mask >> n & 1)
        entry = stats[kind]
        entry["patches"] += 1
        entry["break"] += bool(failing)
        entry["all"] += mask == maps.every
        entry["cost"] += cost(mask, millis)
        if lost:
            entry["missed"] += 1
            missed.append((kind, name, len(failing), len(lost), tests[lost[0]]["id"].split("/")[-1],
                           result["status"]))

    print(f"{'kind':<20}{'patches':>8}{'break':>7}{'missed':>7}{'run-all':>8}{'avg s':>7}")
    totals = defaultdict(float)
    for kind in sorted(stats):
        entry = stats[kind]
        for key in ("patches", "break", "missed", "all", "cost"):
            totals[key] += entry[key]
        print(f"{kind:<20}{int(entry['patches']):>8}{int(entry['break']):>7}{int(entry['missed']):>7}"
              f"{int(entry['all']):>8}{entry['cost'] / max(entry['patches'], 1):>7.1f}")
    print(f"{'TOTAL':<20}{int(totals['patches']):>8}{int(totals['break']):>7}{int(totals['missed']):>7}"
          f"{int(totals['all']):>8}{totals['cost'] / max(totals['patches'], 1):>7.1f}")
    print("harness labels differing from sample answers:", label_errors)
    for item in missed:
        print("MISS", *item)


def main() -> None:
    tests, maps = setup(sys.argv[1])
    if "--order" in sys.argv:
        write_order(maps)
    else:
        report(tests, maps)


if __name__ == "__main__":
    main()
