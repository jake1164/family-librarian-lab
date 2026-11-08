# Family Librarian lab agent guide

All agents working in this repository, including Claude Code and Codex, must read and follow this file before
changing, running, or reviewing anything here. It applies on every machine, including the shared lab host
(`toontown-int-srv2`, `/opt/family-librarian-lab`).

## Temporary experiments are allowed; leave the shared lab restored

The normal deployed state runs committed code. Within an authorized task, reversible experiments may use
uncommitted workstation files copied to the host, direct host edits, or temporary configuration/image overrides.
A commit or push is not required before an experiment or merely to clean one up. Durable fixes belong in a normal
clone for owner review; committing, pushing and permanent deployment retain their separate authorization rules.

- Before changing the host, record the lab and product revisions, tracked/untracked status, affected configuration
  and running-image identities. Preserve the starting contents of every path you will change. Prefer an isolated
  worktree or temporary overlay when that makes restoration simpler. Do not overwrite or discard pre-existing work.
- Declare the experiment in `LOCAL_EDITS.md` at the lab repo root (leave it untracked). Record what changes, why,
  who made it, the date, the baseline and the restoration steps. Keep any needed patch or backup until restoration
  is verified. A documented experiment is still temporary; the entry alone does not satisfy cleanup.
- The same applies to host-only files that git does not track (`external-providers/*.local.yaml`,
  `external-providers.local.yaml`, `lab.env`). They are real configuration, so changing one needs a dated
  `.bak-<reason>` copy beside it and a `LOCAL_EDITS.md` entry. A temporary pin (for example a hand-built
  `image:` standing in for a `build:`) must carry its removal condition in a comment, and the build identity
  `./lab up` prints (the "Build revisions" block) must be checked afterwards so a stale pin is noticed.
- Before reporting completion, preserve useful changes in the workstation clone or a recoverable patch, then
  restore the affected host paths, configuration, image selections and running services to the captured baseline,
  unless the owner separately authorized the intended permanent deployment. No task-introduced tracked edits,
  untracked experiment files, ignored configuration changes or experimental images may remain active on the lab.
  Remove only your experiment entries and temporary files/backups after restoration is verified.
- Verify both the lab and affected product checkouts, the effective configuration, running build/image identities
  and service health. Clean Git status alone is insufficient: a running experimental image or ignored override
  can survive a source rollback. Preserve legitimate baseline configuration and report pre-existing differences;
  do not delete someone else's files to claim a clean lab. If restoration is blocked, report the unresolved drift
  and do not call the task complete.
- Label results from uncommitted code as experimental and record the baseline plus the patch/source identity.
  Acceptance of a committed revision requires a run of that revision unless the owner explicitly accepts the
  experimental evidence. Workstation changes may remain uncommitted awaiting review; the shared lab must not be
  left running an unfinished experiment.

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
Make durable changes in its own repository, obtain commit/push approval there, then bump the submodule pointer
here in a separate approved commit. Temporary submodule experiments follow the same baseline and restoration
rules above, including restoring the host's submodule checkout and gitlink; do not leave a host-only fix behind.

## Before reporting work complete

- Run the lab's unit tests (`tests/unit`) after any change to `family_librarian_lab/`. State plainly if a check
  was skipped or could not run.
- After host work, finish with `git status` for the lab and affected product/submodule checkouts, and verify the
  intended configuration, running images and service health. Leave no task-introduced experimental drift.
  Report pre-existing differences separately; documenting a new difference is not a substitute for restoring it.
- Never create a Git commit without first asking the user for an explicit yes-or-no confirmation.
