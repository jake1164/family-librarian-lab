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
./lab run --test-group base
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

`./lab run --test-group base` discovers suites through se-lab. Each case receives a
new Compose project, PostgreSQL volume, ClamAV volume, and Family Librarian
storage volume; its results include the deployment artifacts plus a redacted API
trace. `--keep` preserves those projects for investigation, and `--skip-build`
uses an image built by an earlier `./lab build`/`./lab up`. Use `--case SEC-02`
to run one registered scenario while investigating a failure.

## CWA and Audiobookshelf destinations

`cwa-local` and `abs` are Compose profiles on top of `base`, adding real
[Calibre-Web-Automated](https://github.com/crocodilestick/calibre-web-automated) and
[Audiobookshelf](https://github.com/advplyr/audiobookshelf) containers. The same wiring
code (`_wire_destinations` in `family_librarian_lab/commands.py`) configures Family
Librarian to point at whichever of them is up, whether you're clicking around manually
or running the automated suites — one code path, not two hand-maintained copies of it:

```bash
# Manual testing: bring up base + CWA (or + Audiobookshelf, or both), already
# wired into Family Librarian -- nothing left to configure by hand.
./lab up --profile cwa-local
./lab up --profile abs
./lab base down

# Automated testing: one real case per destination (CWA-L-02, ABS-02 from the
# design doc), each in its own fresh, isolated project exactly like the base
# suites -- CWA/ABS come up and tear down with it.
./lab run --test-group cwa-local
./lab run --test-group abs
```

CWA ships a working default admin account (`admin` / `admin123`) — no bootstrap needed.
Audiobookshelf needs a first-run root user and a library pointed at its `/audiobooks`
folder; `AbsClient.ensure_bootstrapped()` does that idempotently before Family Librarian
is configured. Both images are pinned in `compose.base.yaml`
(`FAMILY_LIBRARIAN_CWA_IMAGE`/`FAMILY_LIBRARIAN_ABS_IMAGE` override them), same
convention as this project's other pinned dependency images.

This intentionally covers only the happy path for each destination — the full CWA/ABS
scenario matrix (restarts, interruptions, negative cases, `AUTO-*`, `SEC-*`, the
`CWA-C-*` correctness tests) stays as documented, larger follow-on work in
[the design doc](docs/01-family-librarian-integration-test-design.md).

## CWA over SFTP

`cwa-sftp-key` and `cwa-sftp-password` are Compose profiles on top of `base`, adding an
[atmoz/sftp](https://github.com/atmoz/sftp) sidecar in front of the same `cwa` service
`cwa-local` uses, exercising CWA's remote-ingest transport instead of the local
bind-mounted one. Only one of the two profiles is active per run; `configure_cwa_sftp`
in `family_librarian_lab/api.py` drives Family Librarian's real trust-on-first-test SSH
host-key flow (probe untrusted, trust the observed fingerprint, probe again) the same
way an admin would through the UI:

```bash
./lab up --profile cwa-sftp-key
./lab up --profile cwa-sftp-password
./lab base down

./lab run --test-group cwa-sftp-key
./lab run --test-group cwa-sftp-password
```

`cwa-sftp-key` covers both CWA-S-01 (trust-on-first-test) and CWA-S-02 (remote happy
path); `cwa-sftp-password` re-runs CWA-S-02 under the alternative auth mode. Key-mode
authentication uses a disposable host-local test keypair generated automatically under
`runtime/` (gitignored, not real deployment material); password mode uses
`FAMILY_LIBRARIAN_SFTP_PASSWORD` from `lab.env`. The remaining CWA-S-* scenarios
(host-key mismatch, credential rotation, independent-catalog-outage, and the rest of
the design doc's matrix) are not yet implemented.

se-lab is included as a git submodule at `se-lab/`. After cloning:

```
git submodule update --init
```
