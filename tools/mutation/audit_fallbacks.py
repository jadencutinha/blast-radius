"""Report how often each run-all rule fires and what it costs.

Usage: python3 tools/mutation/audit_fallbacks.py <sqlglot checkout>
Uses the working selector. Results are only read to score the rules.
"""

import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MUTANTS = ROOT / ".cache" / "mutants"
RESULTS = ROOT / ".cache" / "mutation_results.jsonl"


def main() -> None:
    spec = importlib.util.spec_from_file_location("working_selector", ROOT / "selector.py")
    selector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selector)
    selector.SOURCE = Path(sys.argv[1])
    tests = json.loads((ROOT / "sample/tests.json").read_text())["tests"]
    millis = [round(t["seconds"] * 1000) for t in tests]
    maps = selector.Maps(tests)
    full = selector.pack(list(range(len(tests))), millis)
    full_cost = (max(sum(millis[n] for n in w) for w in full) + 2000 * len(full)) / 1000
    results = {}
    for row in RESULTS.read_text().splitlines():
        item = json.loads(row)
        results[item["id"]] = item
    stats = defaultdict(lambda: defaultdict(float))
    names = [p.stem for p in sorted(MUTANTS.glob("*.patch"))]
    for name in names:
        maps.fired = []
        selector.select(selector.parse((MUTANTS / f"{name}.patch").read_text(errors="replace")), maps)
        if not maps.fired:
            continue
        rule = maps.fired[0]
        entry = stats[rule]
        entry["sample" if name.startswith("sample_") else "mutants"] += 1
        if name in results:
            entry["measured"] += 1
            failing = results[name]["failing"]
            entry["broke"] += bool(failing)
            entry["most_failing"] = max(entry["most_failing"], len(failing))
    print(f"run-all costs {full_cost:.1f} s per commit")
    print(f"{'rule':<20}{'sample':>7}{'mutants':>8}{'measured':>9}{'broke':>6}{'max failing':>12}{'sample cost':>12}")
    for rule, entry in sorted(stats.items(), key=lambda pair: -pair[1]["mutants"]):
        print(f"{rule:<20}{int(entry['sample']):>7}{int(entry['mutants']):>8}{int(entry['measured']):>9}"
              f"{int(entry['broke']):>6}{int(entry['most_failing']):>12}{entry['sample'] * full_cost:>11.1f}s")


if __name__ == "__main__":
    main()
