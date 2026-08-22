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
It builds Family Librarian from the local checkout named by
`FAMILY_LIBRARIAN_SOURCE_DIR`; its Compose file lives in this repository so every
run receives project-scoped networks and named volumes rather than sharing the
product checkout's development stack.

```bash
python3.12 -m venv .venv  # or a newer supported Python
.venv/bin/pip install -r se-lab/requirements.txt
cp se-lab/lab.env.example lab.env
# Add the FAMILY_LIBRARIAN_* values from lab.env.example to lab.env.

./lab build
./lab run --fresh --project-name family-librarian-lab-smoke
./lab status --project-name family-librarian-lab-smoke
./lab base down --project-name family-librarian-lab-smoke

# Build once, then run each base/security case in its own fresh Compose project.
./lab test --group base
```

`--fresh` removes only volumes bearing the selected Compose project name, before
starting that project. A successful `run` stores redacted Compose state, logs,
and the `/health/live` and `/health/ready` results under `results/<run-id>/`.

`./lab test --group base` discovers suites through se-lab. Each case receives a
new Compose project, PostgreSQL volume, ClamAV volume, and Family Librarian
storage volume; its results include the deployment artifacts plus a redacted API
trace. `--keep` preserves those projects for investigation, and `--skip-build`
uses an image built by an earlier `./lab build`. Use `--case SEC-02` to run one
registered scenario while investigating a failure.

se-lab is included as a git submodule at `se-lab/`. After cloning:

```
git submodule update --init
```
