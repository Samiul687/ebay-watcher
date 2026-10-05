#!/usr/bin/env python3
"""Merge two seen_listings.json files: usage: merge_state.py <ours> <theirs>

Writes the merged result into <theirs>. Seen listing IDs are unioned per
search and last_checked takes the newer timestamp, so two runs that finish
at the same moment never lose each other's updates.
"""
import json
import sys


def load(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict) and "seen" in data:
        return data.get("seen", {}), data.get("last_checked", {})
    return {}, {}


def main():
    ours_seen, ours_checked = load(sys.argv[1])
    theirs_seen, theirs_checked = load(sys.argv[2])

    seen = {n: sorted(set(theirs_seen.get(n, [])) | set(ours_seen.get(n, [])))
            for n in set(theirs_seen) | set(ours_seen)}
    checked = {n: max(theirs_checked.get(n, 0), ours_checked.get(n, 0))
               for n in set(theirs_checked) | set(ours_checked)}

    with open(sys.argv[2], "w") as f:
        json.dump({"seen": seen, "last_checked": checked}, f)


if __name__ == "__main__":
    main()
