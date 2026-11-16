# Metadata comparison lab — runbook

## Purpose

This is a read-only way to ask real book-metadata catalogues the same questions and see how each one
answers. It measures; it does not decide. There is no overall score, no ranking and no recommendation,
because "which catalogue is best" is a judgement about your own books, not something a tool can settle.

Two ways to drive it: the command line (`metadata_lab ...`) and the local web page (`metadata_lab serve`).
Both call the same engine and write the same three files, so a result from the page is a result from the
command line.

## Start the page

From the repository root on the workstation (Windows paths shown; on Linux/macOS drop the `.venv\Scripts\`
prefix):

```
.venv\Scripts\python.exe -m metadata_lab serve
```

It prints `metadata lab UI: http://127.0.0.1:8765/` — open that in a browser. Stop it with Ctrl-C.

- It binds `127.0.0.1` only. Binding another address needs `--allow-remote` and prints a warning: the
  server spends API quota with keys it holds itself, so do not expose it.
- `--port`, `--results <dir>` are available if 8765 or `results/metadata-compare` is not what you want.
- Keys are read by the server from `lab.env` (or its own environment) at the moment a run starts. The page
  only ever learns "configured" or "not configured"; it never receives a key, and neither does anything
  written to disk.

## Click the self-test first

The first thing on the page is the harness badge with a **Run self-test** button.

- `harness: validated` — the code that would produce a run has passed all 26 offline harness scenarios.
  This is the only state in which a result should be believed.
- `harness: stale` — the code changed after the last passing self-test (an edit, a pull, a branch switch).
  Results produced now are not trustworthy. Click **Run self-test**, wait about three seconds, and the
  badge turns green. The same badge appears in every report.
- `harness: missing` — no self-test has ever run in this checkout. Click the button.

The self-test is entirely offline: it uses a scripted local server, so it makes no provider calls and
costs no quota.

## Run one provider, safely

1. **Providers** — the checkboxes show each catalogue with a *configured* / *not configured* badge and a
   *verified live* badge. "Verified live" only means a real call once succeeded; it says nothing about
   quality. Each provider also lists, per operation, whether it is `supported`, `unsupported` (the
   provider genuinely cannot do it, with a cited reason) or `not_implemented` (our adapter has not built
   it yet — a gap in this tool, not a verdict on the provider).
2. Exactly **one** provider is selected when the page loads, on purpose. A query against one provider
   costs a handful of calls; against five it costs five times as much and can trip rate limits.
3. **One-off** tab: choose the query type (`book` by title, `isbn`, `author`, `series`), fill the field
   that type needs, press **Run query**. Only that field is sent; the page cannot ask for a URL.
4. Watch **Progress**: rows done, calls used, and which case/provider/operation is running. **Cancel**
   stops the run through the same path as Ctrl-C and still writes what it already has (`status:
   cancelled`, unfinished rows `not_run`).

Use **Save as case (YAML)** to download the query as a one-case suite file. It downloads a file; nothing
is written into the repository. Add a real `source`, a `verified_on` date and the expected values by
hand, save it under `metadata_lab/suites/`, and it becomes a repeatable case.

## Suites

The **Suite** tab runs a stored file of cases (for example `quality-first-six`). Filters are optional:
tick case ids, type tags, set an as-of date, and tick **Traversal** to also follow the chain (a title →
its series → the series' members → the author's other series).

Before anything runs the page shows a call **estimate** and a hard ceiling: the number of cases times
providers times operations, four calls per operation in practice, and the per-case budget as the ceiling.
A suite needs a second click on the same button — the label changes to "Click again to spend about N
calls". That is deliberate: no single misclick should spend a hundred calls.

## Reading the result

The generated `report.html` is shown in the page, unchanged and sandboxed. Below it are downloads for
`result.json` (everything) and `ai.json` (a smaller, trimmed view safe to paste into a chat).

The **capability declarations** table (provider x operation) is read from the adapters' own declarations and
says what each provider can do: `supported`, `unsupported` (with the cited reason underneath) or
`not_implemented`. It is static capability, independent of the run. **Fixture coverage** then shows how many
fixtures in the loaded suite exercise each of the seven operations, so a zero is visible as "the fixtures say
nothing here".

The **capability matrix** counts outcomes per provider and operation. It is a tally, not a score. The
outcome words mean exactly this:

| Outcome | Meaning |
| --- | --- |
| `ok` | What was expected came back and matched. |
| `differs_from_reference` | Something came back, but a field does not match the case's reference value. The reference may be wrong, or the sources may legitimately disagree. Check the reference before blaming the provider. |
| `partial` | Usable data came back, and some expected item is genuinely absent. |
| `incomplete` | The answer is visibly cut short (a page cap, a call budget) — our limit, not the provider's. |
| `ambiguous` | More than one plausible answer (for example several same-named series); nothing is chosen. |
| `not_found` | The provider returned nothing usable. |
| `unsupported` | The provider cannot do this operation at all; the reason and a source URL are in the case. |
| `not_implemented` | Our adapter has not built this operation yet. A gap in this tool. |
| `not_configured` | No key for this provider on this machine; nothing was attempted. |
| `error` | The call failed (timeout, HTTP status, auth, parse). The kind is recorded. |

`needs_review` also appears: the tool cannot score that expectation automatically (a manual-evaluation
fact, an expired time-sensitive reference, or nothing to classify). It is excluded from every
denominator rather than counted as a failure.

## Reference facts: comparing providers on the same denominator

The **reference facts** table is the one to compare providers with. Each fixture expects a number of
atomic facts (a series name, a member being present, its position, a date, an author...). Counting them
needs only the fixtures, so every provider is measured against the same N. Each fact lands in exactly one
column, so the columns add up to N:

| Column | Meaning |
| --- | --- |
| `correct` / `differs` | The provider returned a value and it matched or did not match the reference. **agree** is divided by these returned values only, and no percentage is printed below five of them. |
| `ambiguous` | A real conflict between records (one says right, another wrong) or only a fuzzy near match; nothing is chosen. |
| `omitted` | The operation ran and the provider supplied nothing for this fact. |
| `incomplete` | Our call budget or page cap stopped the answer. |
| `needs_review` | Not scored automatically (manual or expired reference). |
| `blocked` | The provider could not be asked. `unsupported` is the provider's gap, `not_implemented` is a gap in this tool, anything else (`excluded`, `cooling_down`, `chain_broken`, `error:...`) is a run condition. A blocked fact is not an omission and not an error. |

**Field presence and series identity are reported apart.** When a provider returns several series with the
same name and exactly one holds a fact while the others merely lack it (no record contradicts it), the fact is
scored from that one observation (it counts as correct or differs) and is also counted as "from one of several
same-named series". The lab never picks or merges a series: which series is the right one stays ambiguous, and the
count shows how much of a provider's score rests on it. Two records that genuinely contradict each other stay
`ambiguous`.

The last column, **evidence (rows)**, says how each row's answer was obtained: `live`, `cache`, `mixed` or `replay`.

Independent and traversal modes are separate tables and are never mixed. The older **executed-check
tallies** below the reference facts count only the checks that actually ran, so a provider that could not
run an operation has a smaller denominator there; do not compare providers with them.

## Where a reference comes from (fixture provenance)

A reference is only as good as its source, and four of the five providers ingest the same public sources
the references like to cite. Every reviewed expectation therefore declares:

| Key | Meaning |
| --- | --- |
| `source` | The URL (and the quoted wording) the value was read from. |
| `source_kind` | `primary` (author, publisher, imprint), `catalogue` (national library, ISBN agency), `secondary` (trade press, retailers, reference works), `aggregator` (community databases) or `provider_derived` (supplied or ingested by a provider under test, such as Open Library or Wikidata). |
| `evidence` | The short quotation or observation, so the fixture stands on its own without the research notes. |
| `upstream_overlap` | Optional list of provider ids whose data derives from the source. |
| `verified_on` / `confidence` | When it was read and how far it can be trusted. `provider_derived` is capped at `low` and rejected above it; `high` is reserved for `primary` or `catalogue`. |

Legitimate ambiguity is recorded, not resolved: a member can carry `accepted_positions` (with the scheme named) and
`accepted_published` (several documented dates). A fact that has no independent source is removed from the reference
rather than kept at medium confidence. `metadata_lab.cases.provenance_summary(suite)` counts expectations by
`source_kind` and confidence, with provider-derived and undeclared ones apart. Expectations from fixtures that have
not been through verification show as `undeclared`.

## Publication status (dated assertions)

A series member can assert where a work stood on a date, with `publication_status:` and the keys `status`, `as_of`, `confidence`
and `evidence` (all required but confidence, which defaults to the member's):

| Status | Meaning |
| --- | --- |
| `planned` | announced as coming, no release date announced |
| `scheduled` | a future release date is announced (the member's `published` date is required) |
| `published` | released on or before the evaluation date |
| `unknown` | the references cannot say; always `needs_review` |

A provider only supplies a date, so its status is derived from that date against the run's as-of date: `published`, `scheduled`, or
`unknown` when a year or month still contains the evaluation date. The rules that keep this from scoring a provider wrongly:

- **No date, no verdict.** A missing release date is never read as "unpublished"; that fact is `omitted`.
- **Uncertain references review, they do not score.** Below `high` confidence a contradiction is `needs_review`; at `high` it is a difference.
  At `low`, even agreement is reviewed.
- **A date cannot confirm `planned`.** Only a contradiction or silence is possible against it.
- `scheduled` and `published` assertions that carry the member's release date follow the calendar, so a scheduled title is published on its
  release day with no edit. A `planned` assertion is bounded by `valid_until`.

The check and its evidence are recorded as `series.member[i].publication_status` (dimension freshness).

## Direct author-work cases

`suites/author-works.yaml` holds five cases that start from an author name alone, so they never depend on the
book-to-series traversal (which stops when the book step returns no author id). They measure two things:

- **Identification:** `author.name` (did the provider find this person or pen name; pseudonyms are cases of their own)
  and `author.forbid_titles` (is a namesake's book wrongly merged into the author; a provider that returned no works at
  all gets no pass).
- **Coverage:** `author.titles[i]` for a verified subset of the bibliography (`exhaustive: false`).

The reasons a fact has no value stay apart in the fact table: `blocked:unsupported` (the provider has no such
operation), `blocked:not_implemented` (our adapter has not built it), `omitted` (the operation ran and returned
nothing for it) and `incomplete` (a truncated listing). `tests/unit/test_metadata_lab_author_works.py` runs the real suite
through the engine against five fake providers to prove it.

## Capping a live run (`--max-requests-per-provider`)

`run --max-requests-per-provider N` is a hard cap on real HTTP requests to each provider for the whole run. Retries count, and the worst case
of one call (the request plus two retries) is reserved before it is sent, so the total cannot pass N even when every call retries. When a
provider's cap is spent, its remaining rows are `not_run` with reason `request_budget` and its facts show as `blocked:request_budget`
(a run condition, not a provider gap and not a miss). An operation that reaches the cap mid-way is truncated and its facts read `incomplete`.
`result.json` records `run.request_budget` (limit and used per provider). The cap is refused with traversal, whose call count cannot be
predicted. Per-case `max_calls` still bounds each operation.

## Response cache (optional, off by default)

`run --cache` reuses a recent provider answer instead of asking again, which matters most for rate-limit-sensitive
providers. It is bounded (`--cache-max-entries`, default 500 and 50 MB) and answers expire after
`--cache-ttl-hours` (default 24). It lives under `results/metadata-compare/cache/`, which git ignores.

What keeps it from distorting a measurement:

- every exchange and result row says `live`, `cache`, `mixed` or `replay`, and a cached row carries the time the
  provider actually answered; the run header records the cache settings and how many answers it served;
- a case whose fixture expects a **freshness fact** (a publication date, "the latest book") is never read from the
  cache, because those facts are about now;
- only successful JSON answers are stored; credentials are scrubbed from stored bodies, and a request that carries a
  secret parameter is never cached;
- an adapter can opt out (`cache_policy = "none"`); Google Books does, since its terms restrict caching;
- a cached answer still uses the operation's call budget, so cached and live runs have the same semantic result.

## Compare two runs

**Past runs** lists every run under the results directory with its suite, date, providers, status,
harness state and whether it was partial or resumed. Filter by text or status.

Tick exactly two rows and press **Compare selected two**. This shows the semantic diff: which rows truly
changed (outcomes), whether a provider entered or left the run, whether a case definition changed, and
timings that moved by more than half. Timings alone never make a run "semantically different".

## Where results lives

`results/metadata-compare/<UTC timestamp>-<suite>/` containing `result.json`, `ai.json` and
`report.html`, plus `results/metadata-compare/selftest/<version>-<digest>.json` for each passing
self-test and `results/metadata-compare/cooldowns.json` for the provider cool-down record (see
"Rate limits and pacing"). That directory is gitignored; nothing in it is committed. The page can only
serve those three file names from that directory, and only run ids matching `[0-9A-Za-z._-]+`, so no path
outside it is reachable.

## Rate limits and pacing

**None of the intervals below is a provider-granted quota.** They are engineering choices derived from
published limits plus a safety margin, or lab judgement where no numeric limit is published. The evidence
each one rests on is in `.ai_docs/metadata/metadata-research.md` ("API rate limits and usage terms"), and
every value is declared next to its adapter (`min_interval_s`, `request_intervals_s`,
`cooldown_default_s`).

| Provider | Base interval | Request-class override | Default cool-down |
|---|---|---|---|
| Open Library | 2.0 s | — | 15 min |
| Inventaire | 3.0 s (lookups: `by-uris`, `serie-parts`, `author-works`) | 10.0 s for `/search` | 60 min |
| BookBrainz | 3.5 s | — | 15 min |
| Hardcover | 2.0 s | — | 15 min |
| Google Books | 3.0 s | — | 15 min |

- **Published limits** (with the margin this lab adds on top): Open Library publishes 1 req/s
  unidentified and 3 req/s with an identifying User-Agent, so 2 s sits between them; Hardcover documents
  1 GraphQL cost unit/second refill with burst 10 on the free tier, so 2 s is half the refill.
- **Source-code evidence, not a deployed guarantee:** BookBrainz's own API code slows down after 100
  search *and* 100 lookup requests per 5 minutes; 3.5 s is a margin derived from that, not a published quota.
- **Lab judgement:** Inventaire publishes no numeric limit (a lab `/search` block lasted over 21 minutes,
  hence the 10 s search pace and the hour-long cool-down), and the Google Books quota is per Cloud project,
  so 3.0 s is a guess with no evidence either way.
- **Hardcover cost:** each query in the adapter selects one top-level GraphQL field, and Hardcover charges
  per top-level field with a minimum of one unit per request, so one request is about one cost unit. A test
  fails if any query gains a second top-level field.
- Spacing applies **per provider across the whole run**, not per operation: one provider-wide queue, so a
  `series_members` call waits for the previous `book_by_title` call.

### Circuit breaker and cool-downs

- The first HTTP 429 whose `Retry-After` exceeds the 20 s retry cap, and the second consecutive 429, both
  stop that provider for the rest of the run. Retrying into a block only makes it worse.
- The row that received the 429 stays `error` / `rate_limited` (it really was attempted). Every remaining
  row for that provider becomes `not_run` with reason `cooling_down`. Other providers carry on.
- A single 429 with a short `Retry-After` is still retried (bounded at 3 attempts, never sleeping longer
  than 20 s).
- The cool-down is written to `results/metadata-compare/cooldowns.json` as
  `{provider, until, reason, observed_at}` per provider. `until` uses `Retry-After` when the provider sent
  it, otherwise the adapter's default cool-down (Inventaire 60 min, everything else 15 min). That file is
  inside the gitignored `results/` tree: it is host-local state, not a repository artefact.
- A provider still cooling at run start is skipped (`not_run` / `cooling_down`) with the remaining time in
  the row's notes. An expired entry is cleared at the next run start. A missing or corrupt file reads as
  "nothing is cooling" and never crashes a run.
- `.venv\Scripts\python.exe -m metadata_lab run <suite> --ignore-cooldown` (and the same flag on `query`)
  runs anyway. The UI shows the state in its provider list and refuses to start a run on a cooling provider
  unless **Call cooling providers anyway** is ticked; the server refuses too (HTTP 409).
- `metadata_lab providers` prints each adapter's pace and any recorded cool-down with the minutes left.
  `run` prints an estimated wall time per provider (planned calls × the slowest declared interval) before
  it starts. That estimate is a floor, not a promise: retries, pagination and multi-call operations add more.

## Rate limits — read this before a big run

Inventaire blocks bursts with HTTP 429 and stays blocked for a long time. The pacing and cool-down rules
above are the tool's answer; the practical rules stay:

- Space runs out: tens of minutes between Inventaire runs, and leave it completely quiet rather than
  retrying — retrying while blocked keeps it blocked. The next run now skips it automatically instead of
  probing it.
- Try anything new on one provider at a time, then widen.
- A throttled run is recorded honestly (`throttled` counts, `error` rows with kind `rate_limited`, later
  rows `not_run` / `cooling_down`); it is not a provider verdict. Re-run it later instead of reading a
  conclusion into it.
- `--ignore-cooldown` is for a deliberate experiment. It does not make the provider less blocked.

## Command line equivalents

```
.venv\Scripts\python.exe -m metadata_lab selftest
.venv\Scripts\python.exe -m metadata_lab providers
.venv\Scripts\python.exe -m metadata_lab query --type series --series "The Empyrean" --providers openlibrary
.venv\Scripts\python.exe -m metadata_lab run quality-first-six --providers openlibrary --tags first-six
.venv\Scripts\python.exe -m metadata_lab run quality-first-six --ignore-cooldown   # override a cool-down
.venv\Scripts\python.exe -m metadata_lab compare <a>/result.json <b>/result.json
```
