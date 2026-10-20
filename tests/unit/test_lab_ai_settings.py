"""Unit coverage for turning lab.env's FAMILY_LIBRARIAN_LAB_AI_* values into Family Librarian's model gateway
settings (`./lab up`, `./lab clean-books` and `./lab ai apply` all send what lab_ai_settings() returns)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from family_librarian_lab import commands  # noqa: E402

BASE = {
    "FAMILY_LIBRARIAN_LAB_AI_ENDPOINT_URL": "http://models.example.test:4000/v1",
    "FAMILY_LIBRARIAN_LAB_AI_MODEL": "example/gemma4:12b",
}


def _roles(settings: commands.LabAiSettings) -> dict[str, dict[str, object]]:
    return {str(role["role"]): role for role in settings.roles}


def test_nothing_is_configured_without_an_endpoint():
    assert commands.lab_ai_settings({"FAMILY_LIBRARIAN_LAB_AI_MODEL": "example/model"}) is None


def test_defaults_enable_the_original_four_roles_locally_without_reasoning():
    settings = commands.lab_ai_settings(BASE)
    assert settings is not None
    roles = _roles(settings)
    assert set(roles) == set(commands.FL_MODEL_ROLES), "every role is sent, so lab.env stays the source of truth"
    assert settings.enabled_roles == list(commands.DEFAULT_LAB_AI_ROLES)
    assert roles["VisionCoverIdentification"]["isEnabled"] is False
    assert roles["SearchGuidance"]["isEnabled"] is False, "search guidance remains an explicit opt-in"
    assert roles["CandidateTriage"]["isEnabled"] is False, "decision-bearing triage remains an explicit opt-in"
    assert roles["ArtifactValidationDecision"]["isEnabled"] is False, "artifact holds remain an explicit opt-in"
    for role in roles.values():
        assert role["placement"] == "Local"
        assert role["localModel"] == "example/gemma4:12b"
        assert role["reasoning"] == "Disabled"
    assert settings.api_key is None


def test_listed_roles_overrides_and_reasoning_are_honoured_case_insensitively():
    settings = commands.lab_ai_settings({
        **BASE,
        "FAMILY_LIBRARIAN_LAB_AI_ROLES": "candidateambiguityassessment, VisionCoverIdentification",
        "FAMILY_LIBRARIAN_LAB_AI_ROLE_MODELS": "visioncoveridentification=example/vision",
        "FAMILY_LIBRARIAN_LAB_AI_REASONING": "inherit",
        "FAMILY_LIBRARIAN_LAB_AI_API_KEY": "secret",
    })
    assert settings is not None
    roles = _roles(settings)
    assert sorted(settings.enabled_roles) == ["CandidateAmbiguityAssessment", "VisionCoverIdentification"]
    assert roles["VisionCoverIdentification"]["localModel"] == "example/vision"
    assert roles["ArtifactValidationAssessment"]["isEnabled"] is False
    assert roles["IntentInterpretation"]["reasoning"] == "Inherit"
    assert settings.api_key == "secret"


def test_search_guidance_can_be_explicitly_enabled_after_a_database_reset():
    settings = commands.lab_ai_settings({
        **BASE,
        "FAMILY_LIBRARIAN_LAB_AI_ROLES": "searchguidance",
        "FAMILY_LIBRARIAN_LAB_AI_ROLE_MODELS": "SEARCHGUIDANCE=example/search",
    })
    assert settings is not None
    assert settings.enabled_roles == ["SearchGuidance"]
    roles = _roles(settings)
    assert roles["SearchGuidance"]["localModel"] == "example/search"
    assert roles["CandidateAmbiguityAssessment"]["isEnabled"] is False


def test_candidate_triage_requires_explicit_opt_in_and_supports_model_overrides():
    settings = commands.lab_ai_settings({**BASE,
        "FAMILY_LIBRARIAN_LAB_AI_ROLES": "candidatetriage",
        "FAMILY_LIBRARIAN_LAB_AI_ROLE_MODELS": "CandidateTriage=example/triage"})
    assert settings is not None and settings.enabled_roles == ["CandidateTriage"]
    assert _roles(settings)["CandidateTriage"]["localModel"] == "example/triage"
    assert _roles(settings)["SearchGuidance"]["isEnabled"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"FAMILY_LIBRARIAN_LAB_AI_MODEL": ""},
        {"FAMILY_LIBRARIAN_LAB_AI_ROLES": "NotARole"},
        {"FAMILY_LIBRARIAN_LAB_AI_REASONING": "Deep"},
        {"FAMILY_LIBRARIAN_LAB_AI_ROLE_MODELS": "IntentInterpretation"},
    ],
)
def test_a_mistake_in_lab_env_stops_instead_of_half_configuring(extra: dict[str, str]):
    with pytest.raises(SystemExit):
        commands.lab_ai_settings({**BASE, **extra})


def test_applying_sends_the_settings_and_never_prints_the_key(capsys: pytest.CaptureFixture[str]):
    class _Api:
        sent: dict[str, object] = {}

        def configure_model_gateway(self, **kwargs: object) -> dict[str, object]:
            self.sent = kwargs
            return {}

    api = _Api()
    commands._apply_lab_ai(api, {**BASE, "FAMILY_LIBRARIAN_LAB_AI_API_KEY": "secret-key"})  # type: ignore[arg-type]
    assert api.sent["endpoint_url"] == BASE["FAMILY_LIBRARIAN_LAB_AI_ENDPOINT_URL"]
    assert api.sent["api_key"] == "secret-key"
    assert len(api.sent["roles"]) == len(commands.FL_MODEL_ROLES)  # type: ignore[arg-type]
    assert "secret-key" not in capsys.readouterr().out
