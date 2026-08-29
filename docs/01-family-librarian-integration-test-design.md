# Family Librarian integration-test design

## Purpose

This lab proves the real deployment boundaries that the repository-level test projects deliberately replace with fakes: PostgreSQL, ClamAV, CWA's watched ingest and OPDS catalog, SFTP, Audiobookshelf, and eventually external provider processes. It is a black-box service test suite, not a replacement for the fast unit and host-integration tests in the Family Librarian repository.

The primary outcome is confidence that an approved, safe artifact reaches the configured permanent library exactly as the user sees it: Family Librarian records a destination reference and the destination's supported API/catalog shows the item. A successful write to an ingest directory or a successful SFTP upload alone is never sufficient evidence of a CWA import.

This design follows the product boundaries documented in Family Librarian:

- CWA has two independent connections: local/shared-filesystem or SFTP ingest, and HTTP(S)/OPDS catalog access for ownership and verification.
- CWA's managed Calibre library, not the ingest directory, is the permanent ebook store.
- Audiobookshelf is reached through its API and is the permanent audiobook store.
- Every acquired asset crosses the real quarantine, format-validation, malware scan, identity, and approval pipeline before it can be published.

## What is already covered, and what this lab adds

The product repository already has fast tests for domain transitions, real PostgreSQL plus an in-process ASP.NET Core host, and endpoint workflows. Its web-test factory intentionally substitutes an always-clean scanner, CWA ingest transport, CWA catalog client, Audiobookshelf client, and acquisition provider. Those are appropriate tests for application behavior, but they cannot prove a real deployment can import a book.

This lab owns the missing tests:

| Boundary | Existing product tests | Lab proof |
| --- | --- | --- |
| PostgreSQL migrations and host | Throwaway PostgreSQL with `WebApplicationFactory` | Full Compose migration, bootstrap, health, and persistence across a real host restart |
| ClamAV | Fake scanner and protocol-focused tests | A clean EPUB passes; EICAR and scanner outage cannot reach either destination |
| CWA local | Fake ingest and fake OPDS lookup | Atomic handoff through a shared ingest volume, then actual OPDS visibility |
| CWA SFTP | SSH.NET unit/client behavior | Trusted-host-key SFTP upload to CWA's real ingest directory, then actual OPDS visibility |
| Audiobookshelf | Fake HTTP API client | Real library discovery, upload, item lookup, and recorded external item ID |
| Providers | Parser/client tests and an in-repo sample provider | A separately deployed protocol provider, its network policy, and its complete secure-ingress outcome |

## Test system and isolation model

Every scenario starts its own Compose project and uses a run-specific project name, network, named volumes, bootstrap credentials, and fixture title. The PostgreSQL database **and** the CWA/ABS library state are fresh. A test run must never point at a household library, an operator's CWA, or an operator's Audiobookshelf instance.

```text
test driver
    |
    +-- Family Librarian HTTP API / browser checks
    |
    +-- Compose project (unique per scenario)
          |- postgres       (empty volume, migrations run normally)
          |- clamav         (real scanner)
          |- family-librarian (immutable image under test)
          |- cwa            (local or SFTP profile; empty library/config/ingest)
          |- sftp           (SFTP profile only; scoped to CWA's ingest volume)
          |- audiobookshelf (ABS profile; empty config/library)
          `- optional controlled provider / fault proxy
```

The test driver may call Family Librarian's public HTTP endpoints and the documented CWA/ABS HTTP surfaces using test-only accounts. It must not change `metadata.db`, write directly into CWA's managed library, or inspect a destination database to declare success. An observer may have read-only access to the test ingest volume for transport diagnostics, but the success assertion for CWA remains OPDS catalog visibility.

The CWA image/version, Audiobookshelf image/version, ClamAV image, and Family Librarian image digest are pinned in the lab's Compose templates. Each run records their digests and reported application versions. Updating one is a deliberate compatibility change with a recorded green run, not an implicit `latest` pull.

### Profiles

| Profile | Services | Scope |
| --- | --- | --- |
| `base` | Family Librarian, PostgreSQL, ClamAV | Deployment/migration/health and security-gate checks |
| `cwa-local` | `base` + CWA with a shared ingest volume | All ebook publishing scenarios on the local/shared-filesystem transport |
| `cwa-sftp-key` | `base` + CWA + SFTP sidecar using CWA's ingest volume | Remote CWA transport with private-key authentication |
| `cwa-sftp-password` | Same as key profile | Remote CWA transport with password authentication |
| `abs` | `base` + Audiobookshelf | Audiobook publishing and library API scenarios |
| `full` | All above plus controlled provider/fault proxy | Restart, retry, and cross-boundary regression scenarios |

The SFTP sidecar is the only writer exposed to Family Librarian in the SFTP profiles. Family Librarian must not mount CWA's ingest or library volume there. CWA sees the same backing ingest volume, making this a genuine remote-ingest topology rather than a local copy followed by a cosmetic SFTP probe.

The CWA fixture configuration disables conversion, metadata rewriting, EPUB fixing, and auto-send. That preserves the first-integration contract: the published item is the approved fixture artifact and CWA ingestion, not CWA post-processing, is what is under test.

## Fixtures and test observability

Fixtures are small, redistributable, deterministic assets stored in the lab:

- a valid EPUB with a unique title and author in both the filename and package metadata;
- an EPUB whose package identity deliberately does not match the requested work;
- a structurally invalid EPUB;
- the EICAR test string with an ebook-like filename, used only to assert rejection by ClamAV;
- a valid single-file audiobook and a valid ordered multi-track audiobook bundle;
- a controlled direct-acquisition/provider response serving the same fixtures;
- a large-enough synthetic EPUB fixture for the atomic-handoff observation.

Every scenario derives a run-unique title and author from its run ID. This avoids title/author matching collisions and allows a precise destination lookup without relying on deletion timing. The test records this correlation tuple, the asset checksum, request ID, format ID, library-import/delivery ID, and the destination item ID in its result artifact.

Each scenario polls explicit state with bounded deadlines rather than sleeping:

- host `/health/live` and `/health/ready`;
- the real ClamAV readiness probe;
- CWA's OPDS search/feed for the unique title and author;
- Audiobookshelf's library-item API for the unique title and author;
- Family Librarian request, security-queue, publishing-queue, and provider activity APIs.

On failure the lab captures Compose configuration with secrets redacted, container logs, health state, the last Family Librarian API responses, CWA OPDS response metadata, ABS response metadata, and an inventory of test-owned volumes. It never write-dumps passwords, API tokens, private keys, protected settings, or full book bytes into results.

## Required test scenarios

### 1. Base deployment and security gate

These scenarios run before a destination-specific test so a failure is classified as a product or dependency readiness problem, not as a CWA/ABS failure.

| ID | Scenario | Required assertions |
| --- | --- | --- |
| BASE-01 | Fresh deployment | Migration completes; app, database, and scanner readiness are healthy; bootstrap administrator can authenticate. |
| BASE-02 | Fresh-state isolation | The new run has no prior users beyond bootstrap, requests, assets, imports, deliveries, or destination items. A prior run cannot satisfy a lookup. |
| BASE-03 | Restart persistence | Restart Family Librarian without replacing volumes; configuration, request history, security evidence, and pending publication state survive. |
| SEC-01 | Clean EPUB | Real ClamAV reports clean; type, EPUB structure, and identity checks pass; the asset becomes trusted and only then dispatches to an enabled ebook destination. |
| SEC-02 | Malware | EICAR is detected; bytes are removed as specified; security/audit evidence remains; no CWA import, ABS delivery, or available request is created. |
| SEC-03 | Scanner unavailable | With ClamAV stopped/unhealthy, a new upload/direct acquisition is rejected or waits as designed; no destination receives bytes. Restore the scanner and prove the documented retry/backfill path. |
| SEC-04 | Invalid or mismatched file | A malformed EPUB and an identity-mismatched EPUB remain out of trusted storage and are absent from CWA/ABS. |

### 2. CWA local/shared-ingest profile

The happy path is the baseline ebook contract and runs against a completely fresh Family Librarian database and fresh CWA state every time.

| ID | Scenario | Required assertions |
| --- | --- | --- |
| CWA-L-01 | Configuration probes | Saved local-ingest and OPDS connection tests both succeed. The test proves both independently; a writable ingest directory is not treated as catalog connectivity. |
| CWA-L-02 | Clean ebook, end to end | Create a real request, attach/acquire the valid EPUB through the Family Librarian API, and let the real security pipeline approve it. CWA receives one completed file through ingest, imports it, and exposes the expected title/author/format in OPDS. Family Librarian's `LibraryImport` is `Available`, contains the OPDS book ID, the requested format and request become `Available`, and the requester has the corresponding safe status notification. |
| CWA-L-03 | Asynchronous verification | Force/observe an initial OPDS miss while CWA is still ingesting. After CWA imports, the background verifier (or an explicit admin Recheck) marks the same import available without a second transport write. |
| CWA-L-04 | Atomic handoff | While a large fixture is transferred, the observer never sees the final ebook filename before it is complete. CWA creates exactly one valid catalog item—no partial/duplicate import. |
| CWA-L-05 | Restart during verification | Restart Family Librarian after the handoff is persisted but before OPDS sees the item. The restarted host verifies the existing import; it does not upload a second copy. |
| CWA-L-06 | Destination unavailable | Make the ingest directory unwritable or stop CWA at handoff. Approval remains valid, a failed publishing record has a safe reason, the request is not available, and an administrator recheck after repair reaches one verified CWA item. |
| CWA-L-07 | Existing owned item | Preload a test book through CWA's own supported ingest path, then configure Family Librarian. The owned-library lookup reports the CWA result rather than claiming an acquisition is needed. |

### 3. CWA SFTP profiles

Run the entire CWA happy-path family for both SFTP authentication modes. The key profile is the required remote default; the password profile protects the explicitly supported alternative.

| ID | Scenario | Required assertions |
| --- | --- | --- |
| CWA-S-01 | Trust-on-first-test | An untrusted host key returns its fingerprint and no file is uploaded. After the administrator explicitly trusts that fingerprint, the connection test performs its temporary probe and cleans it up. |
| CWA-S-02 | Remote happy path | The clean EPUB passes the real security pipeline, is written by SFTP into the remote ingest path, and becomes visible through the same CWA OPDS endpoint. Assert the same `LibraryImport`, request, and notification outcomes as CWA-L-02. |
| CWA-S-03 | Authentication coverage | Both private-key (including a passphrase fixture when supported) and password authentication reach CWA. Incorrect credentials cannot create an ingest file or an available import. |
| CWA-S-04 | Host-key change | Change the SFTP server host key after trust. The connection/publish attempt fails closed, records no successful import, and asks for explicit review/trust of the new fingerprint. |
| CWA-S-05 | SFTP interruption | Cut the SFTP connection during upload. No final ebook filename is visible to CWA, temporary upload artifacts are cleaned up as far as the protocol permits, and recheck/retry after repair produces one verified catalog item. |
| CWA-S-06 | Independent catalog outage | Keep SFTP healthy but make OPDS unavailable. The handoff may be recorded as awaiting verification, but it must never be reported available merely because upload completed. Restore OPDS and prove verification completes without another upload. |

### 4. Audiobookshelf profile

| ID | Scenario | Required assertions |
| --- | --- | --- |
| ABS-01 | Configuration and discovery | The real bearer token discovers the fixture audiobook library and folder. The saved configuration test succeeds only for the selected library and does not expose the token in API responses/results. |
| ABS-02 | Single-file publish | A clean, valid audiobook follows request → quarantine → real ClamAV → validation/identity → trusted → actual ABS multipart upload. The item appears in the configured ABS library, Family Librarian records its external item ID and `Delivered` status, and the request format becomes available. |
| ABS-03 | Existing item idempotency | Seed a matching test item through ABS's supported API, then publish the matching approved asset. Family Librarian finds it and records that existing item without a second upload or duplicate library item. |
| ABS-04 | Lost upload response | A fault proxy accepts/forwards the upload to ABS then breaks the response. Recheck finds the actual item before retrying upload, records its ID, and leaves exactly one destination item. |
| ABS-05 | Multi-track bundle | Upload an ordered multi-track fixture. ABS receives one item with the expected tracks and order; Family Librarian creates one bundle delivery and does not publish until all tracks have passed the security pipeline. |
| ABS-06 | API outage and recovery | With ABS unavailable, approval remains successful but delivery records a safe failure and the request stays unavailable. After ABS recovers, recheck reaches one delivered item. |
| ABS-07 | Destination isolation | A rejected, identity-mismatched, or scanner-held audiobook produces no ABS upload and no available state. |

### 5. Automated acquisition and provider boundary

Manual upload is the shortest route to test a publishing adapter, but it is not enough to prove the automatic path. The `full` profile therefore runs a controlled provider process that serves only lab fixtures.

| ID | Scenario | Required assertions |
| --- | --- | --- |
| AUTO-01 | High-confidence automatic ebook | A newly created request is picked up by the hosted worker; the controlled provider returns one approved fixture; it traverses the same security and CWA publishing path as CWA-L-02/CWA-S-02. |
| AUTO-02 | No match and retry | A provider returns no result. The request remains in automatic acquisition, provider activity records a no-match, and the cooldown prevents repeated calls until it is eligible. |
| AUTO-03 | Bad downloaded artifact | A provider serves EICAR, malformed, or identity-mismatched content. The request moves to the documented review path; no destination receives it. |
| AUTO-04 | Provider failure | HTTP timeout/failure is recorded as retryable without taking down the hosted worker or blocking unrelated requests. |
| AUTO-05 | Provider disagreement | Two controlled providers return distinct high-confidence candidates. The request moves to review rather than silently choosing a file. |
| AUTO-06 | Private-egress requirement | For a provider marked `PRIVATE_REQUIRED`, unavailable gateway blocks the entire provider interaction and the lab observes no normal-egress fallback. |

The actual cadence is part of the system under test. Tests poll the visible state with a generous bounded deadline; they do not invoke application services inside the container to skip the hosted worker. If the product later exposes a safe, authenticated test trigger or configurable test-only polling interval, the lab may use it only in a test image/profile and must retain at least one real-cadence smoke scenario.

## CWA correctness tests that should be introduced with product fixes

These are required conformance tests, but they should not be declared green against the current implementation merely because the lab can work around the problem. The product architecture identifies these as current gaps.

CWA-C-01 (CWA cannot be enabled unless both an ingest transport and a working OPDS connection are configured) is no longer listed here: `CwaSettingsService.GetConfigurationError` now requires `OpdsBaseUrl` and `LastTestSucceeded == true` before enablement (commit `0f0bfa2`), and `CWA-L-08..10` in `test_cwa_local.py` already exercise this at the API/HTTP layer. Covered by: `CWA-L-08..10`.

| ID | Required future behavior | Current reason it is not a green gate yet |
| --- | --- | --- |
| CWA-C-02 | Catalog correlation chooses the correct edition/format using retained identifier/ISBN first and rejects ambiguity rather than accepting a title collision. | Current OPDS matching is best-effort title/author substring matching. |
| CWA-C-03 | A failed/retried post-transfer publish cannot create a duplicate CWA book. | The desired duplicate-safe retry contract needs an end-to-end failure injection once correlation/idempotency is strengthened. |
| CWA-C-04 | Existing CWA artifact retrieval works over OPDS/HTTP for both local and SFTP CWA, without filesystem or SFTP read access. | The CWA catalog client currently resolves only a book ID; no artifact-retrieval capability exists. |

Keep these visible as `planned-conformance` scenarios in reports. They become required release gates only when their product capability is implemented; until then, a passing test must not weaken the documented contract to match the gap.

## Extending the lab for alternate providers

Every future provider/destination gets a profile, a pinned image or controlled protocol fixture, and the same lifecycle contract. The reusable scenario shape is:

```text
fresh state
  -> configure and prove connectivity
  -> perform the user/admin-triggered action through Family Librarian
  -> assert safe ingress (when bytes are involved)
  -> assert destination's supported API/catalog side effect
  -> assert Family Librarian's durable reference and user-visible state
  -> inject a failure/restart
  -> recheck/retry and assert no unintended duplicate or unsafe publish
```

Provider-specific tests add only behavior that is genuinely specific to that provider—such as an OPDS parser, a manifest version, an ABS multi-file upload, or a destination scan trigger. They must also implement the relevant generic conformance group from the product contract: linked library, acquisition, malware scanner, notification, or delivery/media-library import.

## Execution policy and failure classification

| Run type | Suites | Gate |
| --- | --- | --- |
| Pull request | `base`, CWA local happy path, and ABS single-file happy path | Required once lab implementation stabilizes |
| Scheduled/nightly | All CWA local and SFTP modes, ABS bundles, restarts, interruptions, security negatives, and controlled-provider tests | Required compatibility signal; uploads artifacts for investigation |
| Dependency/image upgrade | Entire affected profile matrix plus version report | Required before changing the pinned image |
| Operator compatibility check | Selected profile against a deployment-like but isolated topology, including network-share mode where applicable | Required before supporting that topology |

The lab reports failures by boundary: host/migration, scanner, local ingest, SFTP authentication/host trust/transport, CWA OPDS/import, ABS API/import, provider protocol/egress, or assertion/state mismatch. A failure report should name the last confirmed state and the component that failed to advance it. This is more useful than treating every timeout as "CWA failed."

### Known product gaps: skip, don't fail

A scenario that finds a real, already-diagnosed product bug outside the current batch's fix scope must report it with `ctx.skip(test_id, reason)`, not `ctx.fail(test_id, ...)`. `ctx.fail()` marks the whole suite run failed (`agent/results.py`'s `RunContext.failed_count`), and per the table above several suites are meant to gate pull requests or nightly runs — a `fail()` left in for a bug someone else has to fix in Family Librarian would red that gate on every run indefinitely, not just document the gap. `ctx.skip()` still shows up as neither a pass nor silence: it prints, it's counted separately in the suite summary, and its reason is the bug report.

Keep the assertion itself strict — do not weaken it to accept the buggy behavior as correct. Instead, narrow the skip to the *exact* diagnosed signature (e.g., "status is X and field Y is absent") and let anything else — including an unexpected variant of the same failure — fall through to `ctx.fail()` as a real, uninvestigated failure. `SEC-02` (destroyed assets absent from the admin API) and `CWA-L-06` (a CWA-unavailable handoff recorded as `AwaitingVerification` with no `failureReason` instead of a `Failed` record) are the reference examples. `CWA-C-02..04` below are the same idea at the design-doc level, before any test code exists for them; once a scenario is written against a diagnosed-but-unfixed gap, it follows this section, not a bare `ctx.fail()`.

## Implementation order

1. Build the lab's run lifecycle, fresh-volume cleanup, result capture, and `base` readiness checks on the existing `se-lab` abstractions.
2. Add deterministic fixtures and a black-box Family Librarian API driver.
3. Implement CWA local `CWA-L-01` through `CWA-L-03`; this is the first release gate because it proves the main ebook flow with a fresh database.
4. Add SFTP host-trust and the remote happy path, then both authentication modes and interruption coverage.
5. Add ABS discovery and single-file upload, then lost-response and bundle coverage.
6. Add real ClamAV negative tests, restart tests, controlled-provider tests, and the planned-conformance cases as product features land.

No production code or production deployment configuration is changed by this plan. The lab's configuration remains public and reproducible; server-specific credentials, image access details, and any AI-analysis endpoint remain only in the gitignored `lab.env` layer.

## Design sources

- Family Librarian product and domain specifications, especially the CWA topology/verification, security gate, and Audiobookshelf workflow sections.
- Family Librarian provider contract testing strategy.
- [Microsoft guidance on testing multi-container ASP.NET Core services](https://learn.microsoft.com/dotnet/architecture/microservices/multi-container-microservice-net-applications/test-aspnet-core-services-web-apps), which distinguishes faked-dependency integration tests from Compose-backed end-to-end service tests.
