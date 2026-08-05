#!/usr/bin/env python3
"""
Render a walkthrough session into a self-contained report page.

Reads <session-dir>/session.json and <session-dir>/beats/*.json, inlines
assets/report.css, and writes one HTML file that makes no external requests.

Output is a body fragment, which is what the Artifact tool wants. Pass
--standalone for a document shell when the page will be opened as a local file.

Beats are ordered by what is owed, not by beat number: open flags first, then
accepted, then walked-and-clean. That ordering is the whole point of the page.

Exit 0  every beat validated
Exit 2  the page rendered, but some beat failed validation and carries an
        UNPROVEN chip. Never fail at the last step of a session.
Exit 1  usage or parse error
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path

SLOTS = ("what", "why", "proof", "risk", "prior", "fix")
STATES = ("clean", "flag", "unverified", "accepted", "dropped")

# state -> (css suffix, token shown before the claim)
STATE_STYLE = {
    "clean": ("clean", "CLEAN"),
    "flag": ("flag", "FLAG"),
    "unverified": ("unver", "UNVERIFIED"),
    "accepted": ("acc", "ACCEPTED"),
    "dropped": ("drop", "DROPPED"),
}

# (heading, hint, states, expanded by default)
SECTIONS = (
    ("Needs your call", "out of beat order, on purpose", ("flag",), True),
    ("Accepted", "became a commit", ("accepted",), True),
    ("Walked and clean", "verified, nothing owed", ("clean", "unverified"), False),
    ("Dropped", "raised, then set aside", ("dropped",), False),
)

LANDS_TAG = {"landed": "Landed", "ready": "Ready", "open": "Your call"}

# proof has to point at something a reader can re-run or open, or say up front
# that it does not. "inferred" is the documented honest answer, not a failure.
PROOF_EVIDENCE = re.compile(r"`[^`]+`|\b[\w./-]+\.\w+:\d+|^inferred\b")

# Only for --standalone. The viewport tag is load-bearing: report.css has a
# 620px breakpoint that never fires without it.
SHELL = (
    '<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
)


def md(text):
    """Escape, then let backticks become <code>. No HTML passthrough."""
    out = html.escape(str(text), quote=False)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", out)


def validate(beat):
    """Return a list of problems. Empty means the beat is shippable."""
    problems = []
    n = beat.get("n", "?")
    state = beat.get("state")
    slots = beat.get("slots") or {}

    if state not in STATES:
        problems.append(f"beat {n}: state {state!r} is not one of {', '.join(STATES)}")
    for key in slots:
        if key not in SLOTS:
            problems.append(f"beat {n}: unknown slot {key!r}")
    if not slots.get("what"):
        problems.append(f"beat {n}: no what")
    if state in ("clean", "accepted") and not slots.get("proof"):
        problems.append(f"beat {n}: {state} with no proof")
    if state == "flag":
        for key in ("risk", "fix"):
            if not slots.get(key):
                problems.append(f"beat {n}: flag with no {key}")
    proof = slots.get("proof")
    if proof and not PROOF_EVIDENCE.search(proof):
        problems.append(f"beat {n}: proof names no command or path:line")
    return problems


def diff_html(lines):
    out = []
    for line in lines:
        head = line[:1]
        kind = {"+": "add", "-": "del", " ": "ctx"}.get(head, "file")
        out.append(f'<span class="l {kind}">{html.escape(line, quote=False)}</span>')
    return f'<div class="diff"><pre>{"".join(out)}</pre></div>'


def beat_html(beat, problems, expanded):
    suffix, token = STATE_STYLE.get(beat.get("state"), ("unver", "UNVERIFIED"))
    slots = beat.get("slots") or {}

    chip = '<span class="unproven">unproven</span>' if problems else ""
    rows = []
    for key in SLOTS:
        value = slots.get(key)
        if not value:
            continue
        cls = ' class="risk"' if key == "risk" else ' class="fix"' if key == "fix" else ""
        rows.append(f"<dt{cls}>{key}</dt><dd{cls}>{md(value)}</dd>")

    body = [f'<dl class="slots">{"".join(rows)}</dl>']
    if beat.get("diff"):
        body.append(diff_html(beat["diff"]))
    if beat.get("call"):
        body.append(
            '<div class="call"><span class="lbl">Your call · beat '
            f'{beat.get("n", "?")}</span><q>{md(beat["call"])}</q></div>'
        )

    return (
        f'<details class="beat s-{suffix}"{" open" if expanded else ""}>'
        f"<summary>"
        f'<span class="b-num">{beat.get("n", "?")}</span>'
        f'<span class="b-tier">{md(beat.get("tier", ""))}</span>'
        f'<span class="b-claim"><span class="state">{token}</span> &nbsp;'
        f'{md(beat.get("claim", ""))}{chip}</span>'
        f'<span class="b-path">{md(beat.get("where", ""))}</span>'
        f"</summary>"
        f'<div class="b-body">{"".join(body)}</div>'
        f"</details>"
    )


def render(session, beats, css, problems_by_n):
    number = session.get("number")
    label = f"#{number}" if number else session.get("head", "")[:7]
    counts = {}
    for beat in beats:
        counts[beat.get("state")] = counts.get(beat.get("state"), 0) + 1
    clean = counts.get("clean", 0) + counts.get("unverified", 0)

    parts = [
        f'<title>{html.escape(label)} walkthrough · {html.escape(session.get("repo", ""))}</title>',
        f"<style>\n{css}\n</style>",
        '<div class="page">',
        '<header class="masthead"><div class="eyebrow">'
        f'<span>{md(session.get("repo", ""))}</span>',
    ]
    if number:
        parts.append(f'<span class="sep">/</span><span>pull/{number}</span>')
    parts.append('<span class="sep">·</span><span>walkthrough</span>')
    if session.get("date"):
        parts.append(f'<span class="sep">·</span><span>{md(session["date"])}</span>')
    parts.append(
        f'</div><h1><span class="num">{html.escape(label)}</span> '
        f'{md(session.get("title", ""))}</h1>'
    )
    if session.get("facts"):
        facts = "".join(f"<span>{md(f)}</span>" for f in session["facts"])
        parts.append(f'<div class="facts">{facts}</div>')
    parts.append("</header>")

    tiles = [
        ("is-clean", clean, "clean"),
        ("is-flag", counts.get("flag", 0), "needs your call"),
        ("is-acc", counts.get("accepted", 0), "accepted"),
        ("is-mute", len(beats), "beats walked"),
    ]
    cells = "".join(
        f'<div class="count {cls}"><span class="n">{n}</span>'
        f'<span class="k">{k}</span></div>'
        for cls, n, k in tiles
    )
    parts.append(f'<div class="counts">{cells}</div>')

    for heading, hint, states, expanded in SECTIONS:
        picked = [b for b in beats if b.get("state") in states]
        if not picked:
            continue
        cards = "".join(
            beat_html(b, problems_by_n.get(b.get("n")), expanded) for b in picked
        )
        parts.append(
            f'<section class="sec"><div class="sec-head"><h2>{heading}</h2>'
            f'<span class="hint">{hint}</span></div>{cards}</section>'
        )

    if session.get("lands"):
        rows = "".join(
            f'<div class="next-row {l.get("state", "open")}">'
            f'<span class="tag">{LANDS_TAG.get(l.get("state"), "Your call")}</span>'
            f'<span class="what">{md(l.get("what", ""))}</span>'
            f'<span class="where">{md(l.get("where", ""))}</span></div>'
            for l in session["lands"]
        )
        hint = session.get("audience", {}).get("why", "")
        parts.append(
            '<section class="sec"><div class="sec-head"><h2>What lands</h2>'
            f'<span class="hint">{md(hint)}</span></div>'
            f'<div class="next">{rows}</div></section>'
        )

    if session.get("footer"):
        parts.append(f"<footer>{md(session['footer'])}</footer>")
    parts.append("</div>")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Render a walkthrough session to HTML.")
    ap.add_argument("session_dir", help="directory holding session.json and beats/")
    ap.add_argument("--out", help="output file (default <session-dir>/report.html)")
    ap.add_argument("--css", help="override assets/report.css")
    ap.add_argument(
        "--standalone",
        action="store_true",
        help="wrap in a document shell for opening as a local file",
    )
    args = ap.parse_args()

    root = Path(args.session_dir).expanduser()
    css_path = (
        Path(args.css).expanduser()
        if args.css
        else Path(__file__).resolve().parent.parent / "assets" / "report.css"
    )
    try:
        session = json.loads((root / "session.json").read_text(encoding="utf-8"))
        beats = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((root / "beats").glob("*.json"))
        ]
        css = css_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as err:
        sys.exit(f"render-report: {err}")

    problems_by_n, all_problems = {}, []
    for beat in beats:
        found = validate(beat)
        if found:
            problems_by_n[beat.get("n")] = found
            all_problems += found

    cursor = session.get("cursor")
    if cursor is not None and cursor != len(beats):
        all_problems.append(f"session cursor is {cursor} but {len(beats)} beat files exist")

    out = Path(args.out).expanduser() if args.out else root / "report.html"
    page = render(session, beats, css, problems_by_n)
    out.write_text(SHELL + page if args.standalone else page, encoding="utf-8")

    if all_problems:
        print("render-report: rendered with problems", file=sys.stderr)
        for problem in all_problems:
            print(f"  {problem}", file=sys.stderr)
        print(f"  wrote {out}", file=sys.stderr)
        sys.exit(2)
    print(f"rendered {len(beats)} beats to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
