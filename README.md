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

By default the connection details printed by `up` use `127.0.0.1`. Set
se-lab's generic `LAB_EXTERNAL_HOST=toontown-int-srv2` in `lab.env` (or the
process environment) so every product lab uses hosted links such as
`http://toontown-int-srv2:18378`; it changes only displayed links, not the
lab's local readiness checks.

se-lab's generic top-level `down` only knows how to tear down a
`docker-config/docker-compose.yaml`-based stack, which this lab doesn't use
(its Compose file is profile-gated and lives at the repo root) -- use
`./lab base down` to tear down what `./lab up` started; it defaults to the
same standard project name `up` uses, so no `--project-name` is needed for
the common case.

`./lab run` discovers suites through se-lab and defaults to `--test-group all` — every
suite, matching se-lab's own `select_suites()` default and the legacy m3undle-lab
convention (`./lab run <branch>` runs everything; narrow down explicitly, not the other
way around). Each case receives a new Compose project, PostgreSQL volume, and Family
Librarian storage volume; its results include the deployment artifacts plus a redacted
API trace. ClamAV is the one exception: it's shared across a whole suite's cases, not
per-case (see [the design doc's "ClamAV lifecycle"](docs/01-family-librarian-integration-test-design.md#clamav-lifecycle)
for why). `--keep` preserves case projects for investigation, and `--skip-build` uses an
image built by an earlier `./lab build`/`./lab up`. Use `--test-group base` to narrow to
one group, or `--case SEC-02` to run one registered scenario while investigating a
failure.

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

`cwa-sftp-key` covers the full CWA-S-01..06 matrix (trust-on-first-test, remote happy
path, authentication rejection, host-key change, interruption, and independent-catalog-
outage); `cwa-sftp-password` re-runs CWA-S-02/03 under the alternative auth mode. Both
CWA-S-03 cases (authentication rejection) create their ebook request *before* breaking
the credential, not after — `BookRequestService`'s format-readiness gate (product commit
`acb1ff7`) resets a destination's "ready" state on any settings mutation, so breaking the
credential first blocks request creation before the transport-layer behavior these cases
actually test ever runs. Key-mode authentication uses a disposable host-local test
keypair generated automatically under `runtime/` (gitignored, not real deployment
material); password mode uses `FAMILY_LIBRARIAN_SFTP_PASSWORD` from `lab.env`.

## Gutenberg local catalog

`gutenberg` is a Compose profile on top of `base`, adding an HTTPS fixture server
(`gutenberg-fixture`) that serves a small, deterministic RDF archive/feed in place of the
real `gutenberg.org`. This isn't optional plumbing: `GutenbergCatalogOptions`/
`GutenbergMirrorOptions` require every configured archive/mirror URL to be absolute
HTTPS, enforced at startup with no bypass, so the fixture server needs a real certificate
family-librarian is made to trust, not a plain HTTP stand-in. `ensure_gutenberg_fixture_tls()`
in `family_librarian_lab/commands.py` generates a self-signed CA once, appends it to the
app image's own real trust bundle (never replaces it), and points it only at scenarios in
this suite via `SSL_CERT_FILE` — every other suite is unaffected either way:

```bash
./lab run --test-group gutenberg
```

There's no `./lab up --profile gutenberg` yet for manual poking — the fixture server and
certificate only exist for the duration of an automated suite run today.

Implemented: `GUT-01` (first sync from an empty catalog), `GUT-02` (search resolves from
the local Postgres catalog, never a live network call), `GUT-06` (a Sound-type record
never surfaces as an ebook option). Not yet implemented, with reasons — see
[the design doc](docs/01-family-librarian-integration-test-design.md#6-gutenberg-local-catalog-profile)
and `tests/test_gutenberg.py`'s own module docstring for the full detail: `GUT-05`
(the API response contract doesn't expose the field needed to verify format preference),
`GUT-03`/`GUT-04` (need a fixture mirror replicating Gutenberg's real per-mirror path
convention), `GUT-07..10` (need additional archive variants, and two of them hit the same
"not exposed via the API" problem as `GUT-05`).

## SMTP (outbound communications provider)

`smtp` is a Compose profile on top of `base`, adding a real, disposable
[Mailpit](https://github.com/axllent/mailpit) SMTP catcher. family-librarian's own test
suite never exercises `MailKitSmtpTestSender` for real — every in-repo test
force-registers a stub that always reports success — so this profile is the only place
that proves the actual connect/STARTTLS/authenticate/send path works, and the only place
that can independently verify a message actually arrived (Mailpit's own HTTP API,
`MailpitClient` in `family_librarian_lab/clients.py`), not just that Family Librarian says
it did.

`SmtpSettings` intentionally has no plaintext transport option, so STARTTLS is not
optional here either, not even for the happy path: `ensure_smtp_fixture_tls()` in
`family_librarian_lab/commands.py` generates a self-signed CA once (same "extend the app
image's own trust store, never replace it" approach as the Gutenberg fixture's own
`ensure_gutenberg_fixture_tls()`, kept as an independent CA/bundle so the two suites' own
`SSL_CERT_FILE` overrides never collide) and Mailpit is configured to require SMTP AUTH
against a fixed, committed credential (`docker/mailpit/smtp-auth-file`) — the only way to
get a real, deterministic authentication failure out of the app rather than faking one:

```bash
# Manual testing: brings up a real Mailpit, but does NOT pre-configure Family
# Librarian's SMTP settings -- unlike cwa-local/abs, SMTP configuration is the
# thing to manually exercise via the Communications admin page, not a
# prerequisite for something else.
./lab up --profile smtp
./lab base down

./lab run --test-group smtp
```

Implemented: `SMTP-01` (configure, real STARTTLS+AUTH test send, verified via Mailpit's
own API, then enable), `SMTP-02` (enabling requires a fresh passing test of the
*currently saved* settings — a settings change resets that, even to another value that
would itself work), `SMTP-03` (wrong credentials surface a real authentication failure),
`SMTP-04` (an unreachable host surfaces a real connection failure). Not covered, with
reasons — see `tests/test_smtp.py`'s own module docstring for the full detail:
`SmtpSecurityMode.SslOnConnect` (Mailpit's SMTP server only supports STARTTLS, not a
separate implicit-TLS listener), a server with no STARTTLS support at all (MailKit throws
`NotSupportedException` in that case, which `MailKitSmtpTestSender` doesn't catch — a real
product-robustness question, not scoped here), and the settings-backup HTTP API's
encrypted-JSON export/import (`SettingsBackupService`, distinct from `BASE-04`'s
pg_dump-based backup) — that has no lab coverage at all yet, for any setting, and is
tracked as its own item rather than built for SMTP alone.

### Outbound communications dispatcher (real notification trigger)

`SMTP-01..04` above only ever exercise the manual admin "send test email" probe. They
say nothing about the feature that actually motivated building the provider
abstraction: an administrator transitions a book request to Available or
NotAvailable, `BookRequestService.AdminTransitionAsync` enqueues an
`OutboundCommunication`, and a separate background hosted service
(`OutboundCommunicationDispatcherHostedService`, ~15s poll loop) later sends it
through `SmtpOutboundCommunicationProvider` — different code, different (asynchronous)
timing, and a distinct recipient-resolution path than the test-send probe. The
`communications` test group proves that real end-to-end path against the same `smtp`
profile/Mailpit fixture above, plus `cwa-local` (required for request *creation* itself
— `FormatReadinessService` gates a new ebook request on CWA being configured and
passing its own connection test, independent of SMTP entirely; `_wire_destinations()`
auto-configures it the same way `cwa-local`'s own suite relies on):

```bash
./lab run --test-group communications
```

Implemented: `COMM-01` (request becomes Available → a real email is delivered to the
requester), `COMM-02` (request becomes NotAvailable → same), `COMM-03` (SMTP left
unconfigured/disabled → the transition still succeeds and no message is ever sent — the
provider-failure-must-not-block-the-business-operation invariant, end to end). See
`tests/test_communications.py`'s own module docstring for the full detail, including why
`admin_transition_request()` driving `status="Available"` directly is a legitimate call
even though the admin UI never offers it as a button.

se-lab is included as a git submodule at `se-lab/`. After cloning:

```
git submodule update --init
```

## Before adding new lifecycle code here

Read se-lab's `docs/design.md` — specifically its "Guardrail: Where New Lifecycle Code Belongs"
section — before writing a new subprocess wrapper, env-file loader, `compose ps` parser, suite
selector, or status-report section in this repo. This lab was scaffolded ahead of that check and
ended up hand-rolling several things se-lab already provides (or now provides); an audit found a
real bug as a result (`_compose_service_health`'s JSON parsing only handled one of the two shapes
Compose emits). That's fixed, and the se-lab pin is now current (`da67e45`), so
`_compose_service_health` calls `agent.common.parse_compose_ps_json()` directly — no local
duplicate. `handle_run`'s suite/case selection already goes through `agent.suites.select_suites()`
too. Remaining known gaps:

- Route env/setting lookups through `agent.common.resolve_setting()` instead of this module's own
  `_load_lab_env()`, which silently ignores shell-environment overrides that `resolve_setting()`
  supports everywhere else.
- Consider an `agent.status.BaseStatus` subclass for `handle_status` once this lab's own compose
  lifecycle goes through `agent.common` rather than its own `_compose()`.
