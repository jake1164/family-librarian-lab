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

## Compare two runs

**Past runs** lists every run under the results directory with its suite, date, providers, status,
harness state and whether it was partial or resumed. Filter by text or status.

Tick exactly two rows and press **Compare selected two**. This shows the semantic diff: which rows truly
changed (outcomes), whether a provider entered or left the run, whether a case definition changed, and
timings that moved by more than half. Timings alone never make a run "semantically different".

## Where results lives

`results/metadata-compare/<UTC timestamp>-<suite>/` containing `result.json`, `ai.json` and
`report.html`, plus `results/metadata-compare/selftest/<version>-<digest>.json` for each passing
self-test. That directory is gitignored; nothing in it is committed. The page can only serve those three
file names from that directory, and only run ids matching `[0-9A-Za-z._-]+`, so no path outside it is
reachable.

## Rate limits — read this before a big run

Inventaire blocks bursts with HTTP 429 and stays blocked for a long time. Practical rules:

- Space runs out: tens of minutes between Inventaire runs, and leave it completely quiet rather than
  retrying — retrying while blocked keeps it blocked.
- Try anything new on one provider at a time, then widen.
- A throttled run is recorded honestly (`throttled` counts, `error` rows with kind `rate_limited`); it is
  not a provider verdict. Re-run it later instead of reading a conclusion into it.

## Command line equivalents

```
.venv\Scripts\python.exe -m metadata_lab selftest
.venv\Scripts\python.exe -m metadata_lab providers
.venv\Scripts\python.exe -m metadata_lab query --type series --series "The Empyrean" --providers openlibrary
.venv\Scripts\python.exe -m metadata_lab run quality-first-six --providers openlibrary --tags first-six
.venv\Scripts\python.exe -m metadata_lab compare <a>/result.json <b>/result.json
```
