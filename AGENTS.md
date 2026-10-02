# Family Librarian lab agent guide

All agents working in this repository, including Claude Code and Codex, must read and follow this file before
changing, running, or reviewing anything here. It applies on every machine, including the shared lab host
(`toontown-int-srv2`, `/opt/family-librarian-lab`).

## The lab host is a deployment target, not a workspace

The checkout on the lab host exists to *run* committed code. It is not a place to develop.

- Make every change in git: edit in a normal clone, commit, push, then `git pull --ff-only` on the host.
- Do not leave uncommitted edits to tracked files on the lab host. A host edit is allowed only as a short-lived
  experiment, and it must be resolved before the session ends by one of:
  1. **Commit and push it**, then pull, or
  2. **Abandon it**, after saving it (see "Never discard work you did not write").
- Until resolved, an experiment must be declared in `LOCAL_EDITS.md` at the repo root (leave it untracked so it
  shows in `git status`). Each entry states: what changed, why, who or which agent made it, the date, and the
  condition under which it will be removed. An edit with no entry is treated as an accident.
- The same applies to host-only files that git does not track (`external-providers/*.local.yaml`,
  `external-providers.local.yaml`, `lab.env`). They are real configuration, so changing one needs a dated
  `.bak-<reason>` copy beside it and a `LOCAL_EDITS.md` entry. A temporary pin (for example a hand-built
  `image:` standing in for a `build:`) must carry its removal condition in a comment, and the build identity
  `./lab up` prints (the "Build revisions" block) must be checked afterwards so a stale pin is noticed.
- Never copy uncommitted files from a workstation onto the host. The selected branch and the lab checkout must
  contain the intended committed changes first.

## What the lab does to help

Every `./lab` command checks the lab checkout for uncommitted edits to tracked files. If there are any, it
prints a banner on stderr listing them (plus the matching `LOCAL_EDITS.md` entry, or a note that none exists)
and saves a recoverable copy as `refs/lab-snapshots/<UTC time>` (restore with `git stash apply <ref>`;
snapshots are pruned after 30 days). It never blocks a command. Treat the banner as a task: resolve the edits
the way this file describes. Untracked and gitignored files (local overlays, `lab.env`) are not covered, so
keep your own dated `.bak` for those.

To update the host, use `./lab sync` (a `git pull --ff-only` that also updates submodules). It saves a snapshot
first and refuses while tracked files are edited; `./lab sync --stash` sets those edits aside with `git stash`
instead of discarding them. Use it instead of `git restore` or `git reset --hard` to get unblocked.

## Never discard work you did not write

`git restore`, `git checkout -- <path>`, `git reset --hard`, `git clean`, and `git stash drop` permanently
destroy uncommitted work, and nothing here can bring it back. The lab itself runs `reset --hard` against its
source checkouts under `repos/`, so those checkouts are disposable; the lab repository itself is not.

- If `git pull` is blocked by local changes, stop and ask the user. Do not pick a destructive option to make
  the pull succeed.
- To set work aside, use `git stash push -m "<why>"` or `git diff > <name>.patch` outside the work tree. Both
  are reversible; the commands above are not.
- If you did not make a local change in this session, assume it is someone's unfinished work and find out whose
  before touching it.

## Neutrality and secrets

- This repository's tracked code and docs never name a specific acquisition provider. Provider specifics live
  only in the untracked registry and overlay files. Use neutral names (`example-provider`) in anything tracked.
- Real credentials belong in `lab.env`, read through a required interpolation. Do not put literal credentials in
  an overlay or commit them.

## Submodule

`se-lab/` is a git submodule of a separate repository (`Sydney-Elvis/se-lab`), shared with other product labs.
Change it in its own repository, push it there, then bump the submodule pointer here in a separate commit. Do
not leave edits inside `se-lab/` uncommitted, and do not edit it only on the lab host.

## Before reporting work complete

- Run the lab's unit tests (`tests/unit`) after any change to `family_librarian_lab/`. State plainly if a check
  was skipped or could not run.
- On the lab host, finish with `git status`. It must be clean, or every difference must be explained in
  `LOCAL_EDITS.md`. Say which in your summary.
- Never create a Git commit without first asking the user for an explicit yes-or-no confirmation.
