"""One self-contained HTML report (inline CSS, no network resources).

It is a capability profile, not a ranking: there is no composite score and no
"recommended provider" anywhere. Every provider-supplied string is escaped.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .export import EXCLUDED_FROM_CAPABILITY
from .measurements import DIMENSIONS as MEASUREMENT_DIMENSIONS

OUTCOME_CLASS = {
    "ok": "ok",
    "partial": "partial",
    "differs_from_reference": "differs",
    "ambiguous": "ambiguous",
    "incomplete": "incomplete",
    "not_found": "notfound",
    "needs_review": "review",
    "unsupported": "excluded",
    "not_implemented": "excluded",
    "not_configured": "excluded",
    "not_run": "excluded",
    "error": "error",
}

_STYLE = """
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; margin: 1.5rem; line-height: 1.35; }
h1 { font-size: 1.35rem; margin-bottom: .25rem; }
h2 { font-size: 1.1rem; margin-top: 1.75rem; border-bottom: 1px solid #8884; padding-bottom: .15rem; }
h3 { font-size: 1rem; margin-bottom: .35rem; }
table { border-collapse: collapse; margin: .5rem 0 1rem; font-size: .86rem; }
th, td { border: 1px solid #8884; padding: .25rem .45rem; text-align: left; vertical-align: top; }
th { background: #8881; }
.badge { display: inline-block; border: 1px solid currentColor; border-radius: .4rem;
         padding: 0 .35rem; font-size: .75rem; font-weight: 600; }
.ok { background: #1b7f3b22; } .partial { background: #1565c022; } .differs { background: #a15c0022; }
.ambiguous { background: #6a1b9a22; } .incomplete { background: #00796b22; } .notfound { background: #61616122; }
.review { background: #4a148c22; } .excluded { background: #9e9e9e1a; color: #555; } .error { background: #b71c1c22; }
.banner { border-left: 4px solid #a15c00; background: #a15c0015; padding: .4rem .6rem; margin: .4rem 0; }
.banner.info { border-color: #1565c0; background: #1565c015; }
.banner.review { border-color: #4a148c; background: #4a148c15; }
small { color: #666; }
details { margin: .35rem 0; } summary { cursor: pointer; }
pre { white-space: pre-wrap; word-break: break-word; background: #8881; padding: .5rem; font-size: .78rem; }
.panes { display: grid; grid-template-columns: 1fr 1fr; gap: .75rem; }
.note { font-size: .8rem; color: #555; }
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _badge(outcome: str) -> str:
    return f'<span class="badge {OUTCOME_CLASS.get(outcome, "excluded")}">{_e(outcome)}</span>'


def _banners(document: Mapping[str, Any]) -> str:
    run = document.get("run") or {}
    parts: list[str] = []
    if run.get("status") == "cancelled":
        parts.append(
            '<div class="banner">This run was cancelled. Finished rows are real; pending rows are '
            "<code>not_run</code>.</div>"
        )
    if run.get("mode") == "replay":
        parts.append(
            f'<div class="banner info">Replay of run <code>{_e(run.get("replay_of"))}</code>: '
            "outcomes come from a saved capture, not from a live call.</div>"
        )
    harness = run.get("harness") or {}
    if harness.get("status") in {"stale", "not_run"}:
        parts.append(
            f'<div class="banner">Harness status: <strong>{_e(harness.get("status"))}</strong>. '
            "The harness validation suite (selftest) has not validated this code, so treat these "
            "results as exploratory.</div>"
        )
    unverified = [case for case in document.get("cases", []) if not case.get("verified")]
    if unverified:
        names = ", ".join(_e(case.get("id")) for case in unverified)
        parts.append(
            f'<div class="banner">UNVERIFIED FIXTURE: {names}. A '
            "<code>differs_from_reference</code> against an unverified reference may be the "
            "fixture\u2019s fault, not the provider\u2019s.</div>"
        )
    reviews = sum(
        1
        for case in document.get("cases", [])
        for row in case.get("results", [])
        if row.get("outcome") == "needs_review"
    )
    if reviews:
        parts.append(
            f'<div class="banner review">NEEDS-REVIEW: {reviews} operation(s) could not be scored '
            "automatically and are excluded from every denominator.</div>"
        )
    return "".join(parts)


def _matrix(document: Mapping[str, Any]) -> str:
    providers = [entry.get("id") for entry in document.get("providers", [])]
    matrix = document.get("matrix") or {}
    operations = sorted(
        {operation for provider in matrix.values() for operation in provider}
    )
    head = "".join(f"<th>{_e(provider)}</th>" for provider in providers)
    rows: list[str] = []
    for operation in operations:
        cells: list[str] = []
        for provider in providers:
            counts = (matrix.get(provider) or {}).get(operation)
            if not counts:
                cells.append('<td class="excluded">-</td>')
                continue
            items = "".join(
                f'{_badge(name)}&nbsp;{counts[name]}<br>'
                for name in sorted(counts)
                if name != "total"
            )
            cells.append(f"<td>{items or '-'}</td>")
        rows.append(f"<tr><th>{_e(operation)}</th>{''.join(cells)}</tr>")
    return (
        "<h2>Capability matrix</h2>"
        "<p class=\"note\">Outcomes per provider and operation. This is a capability profile, "
        "not a ranking. <code>not_implemented</code> is a gap in this tool; "
        "<code>unsupported</code> is a cited provider limitation.</p>"
        f"<table><tr><th>operation</th>{head}</tr>{''.join(rows)}</table>"
    )


def _best_per_capability(document: Mapping[str, Any]) -> str:
    providers = [entry.get("id") for entry in document.get("providers", [])]
    matrix = document.get("matrix") or {}
    operations = sorted({operation for provider in matrix.values() for operation in provider})
    rows: list[str] = []
    for operation in operations:
        cells: list[str] = []
        for provider in providers:
            counts = (matrix.get(provider) or {}).get(operation)
            if not counts:
                cells.append('<td class="excluded">-</td>')
                continue
            excluded = {
                name: counts.get(name, 0)
                for name in EXCLUDED_FROM_CAPABILITY
                if counts.get(name)
            }
            evaluated = sum(
                value
                for name, value in counts.items()
                if name not in EXCLUDED_FROM_CAPABILITY and name != "total"
            )
            excluded_text = ", ".join(f"{name}={value}" for name, value in sorted(excluded.items()))
            cells.append(
                f"<td>{counts.get('ok', 0)} / {evaluated}"
                f"{'<br><small>' + _e(excluded_text) + '</small>' if excluded_text else ''}</td>"
            )
        rows.append(f"<tr><th>{_e(operation)}</th>{''.join(cells)}</tr>")
    head = "".join(f"<th>{_e(provider)}</th>" for provider in providers)
    return (
        "<h2>Best per capability</h2>"
        "<p class=\"note\">ok / evaluated per operation, where evaluated excludes "
        "unsupported, not_implemented, not_configured, not_run and needs_review (listed "
        "underneath). No provider is ranked overall.</p>"
        f"<table><tr><th>operation</th>{head}</tr>{''.join(rows)}</table>"
    )


def _tallies(document: Mapping[str, Any]) -> str:
    tallies = document.get("matrix_checks") or {}
    if not tallies:
        return ""
    rows: list[str] = []
    for provider, operations in tallies.items():
        for operation, aspects in operations.items():
            for aspect, counts in sorted(aspects.items()):
                total = counts.get("total", 0)
                ok = counts.get("ok", 0)
                others = ", ".join(
                    f"{name}={value}"
                    for name, value in sorted(counts.items())
                    if name not in {"ok", "total"}
                )
                rows.append(
                    f"<tr><th>{_e(provider)}</th><td>{_e(operation)}</td>"
                    f"<td>{_e(aspect)}</td><td>{ok} / {total}</td><td>{_e(others)}</td></tr>"
                )
    return (
        "<h2>Check-level tallies</h2>"
        "<p class=\"note\">One row per check aspect, so a strength in one aspect stays visible "
        "next to a weakness in another.</p>"
        "<table><tr><th>provider</th><th>operation</th><th>check</th><th>ok / scored</th>"
        f"<th>other outcomes</th></tr>{''.join(rows)}</table>"
    )


def _measurements(document: Mapping[str, Any]) -> str:
    by_provider = (document.get("measurements") or {}).get("by_provider") or {}
    if not by_provider:
        return ""
    rows: list[str] = []
    for provider, profile in by_provider.items():
        for dimension in MEASUREMENT_DIMENSIONS:
            counts = profile.get(dimension) or {}
            label = "position coverage" if dimension == "ordering" else "reported / expected"
            rows.append(
                f"<tr><th>{_e(provider)}</th><td>{_e(dimension)}</td>"
                f"<td>{counts.get('ok', 0)} / {counts.get('reported', 0)}</td>"
                f"<td>{counts.get('omitted', 0)}</td>"
                f"<td>{counts.get('reported', 0)} / {counts.get('expected', 0)}</td>"
                f"<td>{counts.get('differs', 0)}</td>"
                f"<td>{counts.get('ambiguous', 0)}</td>"
                f"<td>{counts.get('needs_review', 0)}</td>"
                f"<td><small>{_e(label)}</small></td></tr>"
            )
        performance = profile.get("performance") or {}
        rows.append(
            f"<tr><th>{_e(provider)}</th><td>performance</td>"
            f"<td colspan=\"7\">calls={performance.get('calls', 0)}, "
            f"retries={performance.get('retries', 0)}, "
            f"throttled={performance.get('throttled', 0)}, "
            f"pages={performance.get('pages_fetched', 0)}, "
            f"elapsed={performance.get('elapsed_ms', 0)} ms</td></tr>"
        )
    return (
        "<h2>Measurements (separate, never summed)</h2>"
        "<p class=\"note\">Accuracy is <code>ok / reported</code>: a value the provider omitted is "
        "counted as <em>omitted</em> and against coverage, never against accuracy. "
        "Classification and ordering only divide by values the provider actually reported. "
        "\"Position coverage\" is reported / expected positions.</p>"
        "<table><tr><th>provider</th><th>dimension</th><th>ok / reported</th><th>omitted</th>"
        "<th>reported / expected</th><th>differs</th><th>ambiguous</th><th>needs_review</th>"
        f"<th>note</th></tr>{''.join(rows)}</table>"
    )


def _checks_table(checks: Sequence[Mapping[str, Any]]) -> str:
    if not checks:
        return '<p class="note">No checks (one-off query or no expectations).</p>'
    rows = "".join(
        f"<tr><td>{_e(check.get('name'))}</td><td>{_badge(str(check.get('outcome')))}</td>"
        f"<td>{_e(check.get('dimension'))}</td>"
        f"<td>{_e(json.dumps(check.get('expected'), ensure_ascii=False, default=str))}</td>"
        f"<td>{_e(json.dumps(check.get('observed'), ensure_ascii=False, default=str))}</td>"
        f"<td>{_e(check.get('evidence'))}</td></tr>"
        for check in checks
    )
    return (
        "<table><tr><th>check</th><th>outcome</th><th>dimension</th><th>expected</th>"
        f"<th>observed</th><th>evidence</th></tr>{rows}</table>"
    )


def _case_section(case: Mapping[str, Any]) -> str:
    parts = [
        f"<h3>Case {_e(case.get('id'))}"
        + (f" <small>{_e(case.get('tier'))}</small>" if case.get("tier") else "")
        + "</h3>",
        f'<p class="note">reference: {_e((case.get("reference") or {}).get("source"))}'
        f' &middot; verified_on: {_e((case.get("reference") or {}).get("verified_on"))}'
        f' &middot; input: {_e(json.dumps(case.get("input"), ensure_ascii=False))}</p>',
    ]
    for row in case.get("results", []) or []:
        title = (
            f"{row.get('provider')} &middot; {row.get('operation')} &middot; {row.get('mode')} "
            f"{_badge(str(row.get('outcome')))}"
        )
        notes = "".join(f"<li>{_e(note)}</li>" for note in row.get("notes") or [])
        error = (
            f'<p class="note">error: {_e(row["error"].get("kind"))} &middot; '
            f'{_e(row["error"].get("message"))}</p>'
            if row.get("error")
            else ""
        )
        records = json.dumps(row.get("records"), ensure_ascii=False, indent=1, default=str)
        raw = json.dumps(row.get("raw"), ensure_ascii=False, indent=1, default=str)
        parts.append(
            "<details>"
            f"<summary>{title}</summary>"
            f'<p class="note">elapsed {_e(row.get("elapsed_ms"))} ms &middot; '
            f"truncated {_e(row.get('truncated'))} &middot; "
            f"pages {_e((row.get('pagination') or {}).get('pages_fetched'))} &middot; "
            f"duplicates {_e(len(row.get('duplicates') or []))}</p>"
            f"{error}"
            + (f"<ul>{notes}</ul>" if notes else "")
            + _checks_table(row.get("checks") or [])
            + '<div class="panes">'
            + f"<div><h4>Normalised</h4><pre>{_e(records)}</pre></div>"
            + f"<div><h4>Raw</h4><pre>{_e(raw)}</pre></div>"
            + "</div></details>"
        )
    return "".join(parts)


def render_report(document: Mapping[str, Any]) -> str:
    """Render the whole report as one self-contained HTML string."""

    run = document.get("run") or {}
    suite = document.get("suite") or {}
    title = f"Metadata comparison: {suite.get('name')} ({suite.get('version')})"
    header = (
        f"<h1>{_e(title)}</h1>"
        f'<p class="note">run <code>{_e(run.get("id"))}</code> &middot; mode {_e(run.get("mode"))} '
        f"&middot; status {_e(run.get('status'))} &middot; as_of {_e(run.get('as_of'))} &middot; "
        f"tool {_e(run.get('tool_version'))} &middot; matcher {_e(run.get('matcher_version'))} "
        f"&middot; harness {_e((run.get('harness') or {}).get('status'))}</p>"
    )
    cases = "".join(_case_section(case) for case in document.get("cases", []) or [])
    providers = "".join(
        f"<li><strong>{_e(entry.get('id'))}</strong> &middot; {_e(entry.get('display_name'))} "
        f"&middot; configured={_e(entry.get('configured'))} &middot; "
        f"verified_live={_e(entry.get('verified_live'))}</li>"
        for entry in document.get("providers", []) or []
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{_e(title)}</title><style>{_STYLE}</style></head><body>"
        f"{header}{_banners(document)}"
        f"<h2>Providers</h2><ul>{providers}</ul>"
        f"{_matrix(document)}{_best_per_capability(document)}{_tallies(document)}"
        f"{_measurements(document)}"
        f"<h2>Cases</h2>{cases}"
        "<p class=\"note\">This report is a capability profile. It contains no composite score, "
        "no ranking and no recommendation.</p>"
        "</body></html>"
    )


def write_report(document: Mapping[str, Any], path: Path | str) -> Path:
    target = Path(path)
    target.write_text(render_report(document), encoding="utf-8")
    return target


__all__ = ["render_report", "write_report"]
