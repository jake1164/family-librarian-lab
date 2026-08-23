# family-librarian-lab

Deterministic integration-test harness for [Family Librarian](https://github.com/jake1164/family-librarian),
built on top of [se-lab](https://github.com/Sydney-Elvis/se-lab).

## Status

This repo is a scaffold. It's the second consumer of se-lab, developed in parallel with the
m3undle-lab migration specifically so se-lab's abstractions get validated against two different
products before either is fully committed to — see se-lab's `.ai_docs/roadmap.md` (Phase 1b) for
the plan and current status.

The proposed Family Librarian test profiles, scenario matrix, and implementation
order are in [the integration-test design](docs/01-family-librarian-integration-test-design.md).

## Base lifecycle (implementation-order step 1)

The initial `base` profile starts only Family Librarian, PostgreSQL, and ClamAV.
The lab is self-contained: `build`/`up`/`run` check Family Librarian's source out
to `repos/family-librarian` themselves (mirroring m3undle-lab-public), so there
is no separate, externally-managed checkout to point at. Its Compose file lives
in this repository so every run receives project-scoped networks and named
volumes rather than sharing the product checkout's development stack.

```bash
# First-time environment setup (venv + dependencies) is se-lab's job, not
# duplicated here. See se-lab/README.md's Quick Start for the manual venv
# steps, or run `se-lab/scripts/setup_vm.sh --product-name family-librarian
# --env-prefix FAMILY_LIBRARIAN` on a fresh host (also runs a preflight check).

cp se-lab/lab.env.example lab.env
# Add the FAMILY_LIBRARIAN_* values from lab.env.example to lab.env.

./lab up mybranch     # check out, build, deploy, and leave running for manual testing
./lab status
./lab base down        # note: base down, not the generic `down` -- see below

# Run each base/security case in its own fresh, isolated Compose project.
./lab run --group base
```

`up [target]`/`build [target]` resolve `target` as a tag first, then a branch
(same convention as m3undle-lab-public), and default to refreshing whatever
branch is already checked out in `repos/family-librarian` if no target is
given. Because the checkout is lab-managed and fetches from GitHub, a branch
under local, uncommitted development needs to be pushed before `./lab up
<branch>` can see it -- same workflow as m3undle-lab-public.

se-lab's generic top-level `down` only knows how to tear down a
`docker-config/docker-compose.yaml`-based stack, which this lab doesn't use
(its Compose file is profile-gated and lives at the repo root) -- use
`./lab base down` to tear down what `./lab up` started; it defaults to the
same standard project name `up` uses, so no `--project-name` is needed for
the common case.

`./lab run --group base` discovers suites through se-lab. Each case receives a
new Compose project, PostgreSQL volume, ClamAV volume, and Family Librarian
storage volume; its results include the deployment artifacts plus a redacted API
trace. `--keep` preserves those projects for investigation, and `--skip-build`
uses an image built by an earlier `./lab build`/`./lab up`. Use `--case SEC-02`
to run one registered scenario while investigating a failure.

se-lab is included as a git submodule at `se-lab/`. After cloning:

```
git submodule update --init
```
