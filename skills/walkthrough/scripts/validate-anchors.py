#!/usr/bin/env python3
"""Validate GitHub review comment anchors against a PR diff.

GitHub rejects an entire review if a single comment anchors to a line outside the
diff, so this runs before every post. Anchors that land near a hunk are snapped to
the nearest valid line; anchors with no hunk to attach to are folded into the
review body so the observation survives.

  validate-anchors.py --pr 1234 --payload payload.json --out payload.fixed.json
  validate-anchors.py --diff pr.diff --payload payload.json

Exit 0 = every anchor was already valid, 2 = anchors were snapped or folded,
1 = usage or parse error.
"""

import argparse
import json
import re
import subprocess
import sys

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SNAP_WINDOW = 20


def clean_path(raw):
    p = raw.split("\t")[0].strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode().decode("unicode_escape")
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_diff(text):
    """Return {path: {"LEFT": {old line nos}, "RIGHT": {new line nos}}}."""
    files, path = {}, None
    old_ln = new_ln = rem_old = rem_new = 0

    for line in text.splitlines():
        # Inside a hunk, consume by the header's line counts. Counting rather than
        # pattern-matching keeps an added line of "++ x" from looking like a header.
        if rem_old > 0 or rem_new > 0:
            if line.startswith("\\"):
                continue
            tag = line[:1]
            if tag == " " or line == "":
                files[path]["LEFT"].add(old_ln)
                files[path]["RIGHT"].add(new_ln)
                old_ln, new_ln, rem_old, rem_new = old_ln + 1, new_ln + 1, rem_old - 1, rem_new - 1
            elif tag == "+":
                files[path]["RIGHT"].add(new_ln)
                new_ln, rem_new = new_ln + 1, rem_new - 1
            elif tag == "-":
                files[path]["LEFT"].add(old_ln)
                old_ln, rem_old = old_ln + 1, rem_old - 1
            else:
                rem_old = rem_new = 0  # malformed hunk, resync on the next header
            continue

        m = HUNK.match(line)
        if m and path:
            old_ln, new_ln = int(m.group(1)), int(m.group(3))
            rem_old = int(m.group(2) or 1)
            rem_new = int(m.group(4) or 1)
        elif line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else clean_path(target)
            if path:
                files.setdefault(path, {"LEFT": set(), "RIGHT": set()})

    return files


def snap(line, valid):
    """Nearest valid line within SNAP_WINDOW, preferring the earlier one on a tie."""
    if not valid:
        return None
    best = min(valid, key=lambda v: (abs(v - line), v))
    return best if abs(best - line) <= SNAP_WINDOW else None


def validate(payload, files):
    kept, folded, report = [], [], []

    for c in payload.get("comments", []):
        path = c.get("path", "")
        side = c.get("side", "RIGHT")
        line = c.get("line")
        valid = files.get(path, {}).get(side, set())

        if line in valid:
            kept.append(c)
            continue

        fixed = snap(line, valid) if isinstance(line, int) else None
        if fixed is None:
            folded.append(c)
            why = "file not in diff" if not valid else "no hunk within %d lines" % SNAP_WINDOW
            report.append("fold  %s:%s %s  (%s)" % (path, line, side, why))
            continue

        report.append("snap  %s %s  %s -> %s" % (path, side, line, fixed))
        c["line"] = fixed
        start = c.get("start_line")
        if start is not None and (start not in valid or start >= fixed):
            c.pop("start_line", None)
            c.pop("start_side", None)
            report.append("      dropped start_line %s (range no longer valid)" % start)
        kept.append(c)

    payload["comments"] = kept

    if folded:
        lines = ["`%s:%s` %s" % (c.get("path"), c.get("line"), c.get("body", "").strip()) for c in folded]
        payload["body"] = (payload.get("body", "").rstrip() + "\n\n" + "\n".join(lines)).strip()

    return report


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pr", help="PR number; fetches the diff with gh")
    src.add_argument("--diff", help="path to a unified diff, or - for stdin")
    ap.add_argument("--payload", required=True, help="review payload JSON")
    ap.add_argument("--out", help="where to write the corrected payload (default stdout)")
    args = ap.parse_args()

    try:
        if args.pr:
            diff = subprocess.run(
                ["gh", "pr", "diff", args.pr],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            ).stdout
        elif args.diff == "-":
            diff = sys.stdin.buffer.read().decode("utf-8")
        else:
            with open(args.diff, encoding="utf-8") as fh:
                diff = fh.read()

        with open(args.payload, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError) as e:
        sys.exit("validate-anchors: %s" % e)

    report = validate(payload, parse_diff(diff))
    out = json.dumps(payload, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
    else:
        print(out)

    if report:
        print("\n".join(report), file=sys.stderr)
        sys.exit(2)

    print("all anchors valid", file=sys.stderr)


if __name__ == "__main__":
    main()
