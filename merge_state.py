#!/usr/bin/env python3
"""Merge two seen_listings.json files: usage: merge_state.py <ours> <theirs>

Writes the merged result into <theirs>. Seen listing IDs are unioned per
search, last_checked takes the newer timestamp, and each deal benchmark keeps
the lower price, so two runs that finish at the same moment never lose each
other's updates.
"""
import json
import sys


def load(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict) and "seen" in data:
        return data.get("seen", {}), data.get("last_checked", {}), data.get("benchmarks", {})
    return {}, {}, {}


def merge_benchmarks(ours, theirs, ours_checked, theirs_checked):
    merged = {}
    for name in set(ours) | set(theirs):
        a, b = ours.get(name), theirs.get(name)
        if a is None or b is None:
            merged[name] = a or b
        elif a.get("seed") == b.get("seed"):
            bests = [x for x in (a.get("best"), b.get("best")) if x is not None]
            merged[name] = {"seed": a.get("seed"), "best": min(bests) if bests else None}
        else:
            # The starting value was edited between the two runs: trust the
            # run that checked this search more recently.
            merged[name] = a if ours_checked.get(name, 0) >= theirs_checked.get(name, 0) else b
    return merged


def main():
    ours_seen, ours_checked, ours_bench = load(sys.argv[1])
    theirs_seen, theirs_checked, theirs_bench = load(sys.argv[2])

    seen = {n: sorted(set(theirs_seen.get(n, [])) | set(ours_seen.get(n, [])))
            for n in set(theirs_seen) | set(ours_seen)}
    checked = {n: max(theirs_checked.get(n, 0), ours_checked.get(n, 0))
               for n in set(theirs_checked) | set(ours_checked)}
    benchmarks = merge_benchmarks(ours_bench, theirs_bench, ours_checked, theirs_checked)

    with open(sys.argv[2], "w") as f:
        json.dump({"seen": seen, "last_checked": checked, "benchmarks": benchmarks}, f)


if __name__ == "__main__":
    main()
