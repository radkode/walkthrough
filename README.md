# walkthrough

Interactive PR review for Claude Code. One beat at a time, driven by you, ending in code.

Most review tooling hands you a wall of findings and leaves the work of deciding to you.
This walks a change in causal order, one coherent unit per turn, and stops after each one
so you steer. When something is worth flagging, the flag arrives with the patch already
written and run, so it is accept-or-drop in one word. Accepted flags become commits.

## Install

```
/plugin marketplace add radkode/walkthrough
/plugin install walkthrough@walkthrough
```

## Use

```
/walkthrough 42          a PR number
/walkthrough my-branch   a branch, diffed against main
/walkthrough             the working tree
```

## How a session goes

**Ingest.** Reads the PR, the diff, the last twenty commits touching those paths, and the
two or three earlier PRs the squash-merge subjects point at. That last one is not padding:
prior work in the same area is reliably where the best finding comes from, because it is
the context a diff cannot show you.

**Orient.** A short reconstruction of what the change is for, plus a claim check comparing
the PR description against what the diff actually does. You confirm or correct it, and
your correction frames the rest.

**Plan.** Every changed file is tiered into core, enabling, follow-through, and risk. You
reorder or skip before anything is walked.

**Walk.** One beat per turn, opening with a verdict token and running on fixed lines:

```
BEAT 5/7  enabling  .github/workflows/ci.yml:22
FLAG   unpinned attw resolves latest on every CI run

WHAT   adds `attw --pack . --profile esm-only` between build and eval
PROOF  `npm view @arethetypeswrong/cli versions` shows every major is 0
RISK   a new rule reddens a PR that changed nothing
PRIOR  #2 pinned break-check to 0.6.0 citing this exact failure mode
FIX    npx --yes @arethetypeswrong/cli@0.18.5
```

`PROOF` names a command that was run or a file that was read. `inferred` is a legal value.
A claim with neither does not ship.

**Land.** The medium is decided at ingest, from whether anyone will read it. A merged or
self-authored PR with no other reviewers lands as commits on a branch created lazily at
the first accepted flag, so a session with no accepts leaves no trace. Anything with a
real audience lands as a single GitHub review, anchor-validated first because one bad
anchor rejects the whole thing.

Either way you get a report page ordered by what is owed rather than by what was walked:
open flags expanded at the top, accepted next, clean beats collapsed to one line each that
still carry their proof.

## Session state

Sessions live in `~/.claude/reviews/<owner>-<repo>-pr<N>/`, outside every repo, so a review
never shows up in `git status`. They are resumable, which matters because the large PRs
that most need a walkthrough are the ones nobody finishes in one sitting.

```
session.json     facts, audience, plan, status, cursor, what lands
beats/01.json    one file per beat, written as it is walked
pr.diff          the saved diff
report.html      rendered, regenerable
```

The two scripts are Python 3 stdlib, no dependencies. `render-report.py` turns a session
into the page and validates it on the way through: a flag with no fix, a clean beat with
no proof, or a proof naming no command gets an `UNPROVEN` chip and exit 2, and the page
still renders. `validate-anchors.py` snaps review comments to lines that exist in the diff
and folds unsnappable ones into the body rather than dropping them.

Iterating on the page design means editing `skills/walkthrough/assets/report.css`, or
passing `--css` to try something without a commit.

## License

MIT
