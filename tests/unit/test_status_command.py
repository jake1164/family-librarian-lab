"""User-facing rendering for Family Librarian's `lab status` command."""

from __future__ import annotations

import json

from family_librarian_lab.commands import _status_readiness_lines, _status_service_lines


def test_status_service_lines_show_a_compact_service_inventory():
    output = json.dumps(
        [
            {
                "Service": "family-librarian",
                "State": "running",
                "Health": "healthy",
                "Status": "Up 22 minutes (healthy)",
                "Publishers": [
                    {"PublishedPort": 18080, "TargetPort": 8080, "Protocol": "tcp"},
                    {"PublishedPort": 18080, "TargetPort": 8080, "Protocol": "tcp"},
                ],
            },
            {"Service": "migrate", "State": "exited", "Status": "Exited (0) 22 minutes ago", "Publishers": []},
        ]
    )

    lines = _status_service_lines(output)

    assert lines[0] == "Lab services:"
    assert "SERVICE" in lines[1]
    assert any("family-librarian" in line and "18080->8080/tcp" in line for line in lines)
    assert sum("18080->8080/tcp" in line for line in lines) == 1
    assert any("migrate" in line and "exited" in line and "Exited (0)" in line for line in lines)


def test_status_readiness_lines_are_concise():
    lines = _status_readiness_lines(
        {
            "live": {"url": "http://127.0.0.1:18080/health/live", "ok": True},
            "ready": {"url": "http://127.0.0.1:18080/health/ready", "ok": False},
            "compose_services": {"family-librarian": {"state": "running"}},
        },
        passed=False,
    )

    assert lines == [
        "Readiness:",
        "  LIVE    OK   http://127.0.0.1:18080/health/live",
        "  READY   FAIL http://127.0.0.1:18080/health/ready",
        "Overall: unhealthy",
    ]
