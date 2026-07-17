from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from qed.runtime import (
    CapabilityRequest,
    ForkThread,
    FreshThread,
    ModelCapability,
    RestrictedNetworkPolicy,
    ResumeThread,
    RunRequest,
    RuntimeBackend,
    SandboxMode,
    TurnCompleted,
    TurnRef,
    WebSearchMode,
    WorkRole,
    resolve_capability,
)


class VerdictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["PASS", "FAIL"]


def _request(**overrides: object) -> RunRequest:
    values: dict[str, object] = {
        "model": "gpt-5.6-sol",
        "prompt": "Inspect the claim.",
        "output_schema": {"type": "object", "additionalProperties": False},
    }
    values.update(overrides)
    return RunRequest.model_validate(values)


def test_run_request_defaults_to_a_fresh_read_only_offline_turn() -> None:
    request = _request()

    assert request.thread == FreshThread()
    assert request.effort == "auto"
    assert request.sandbox is SandboxMode.READ_ONLY
    assert request.web_search is WebSearchMode.DISABLED
    assert request.command_network is None
    assert "approval" not in RunRequest.model_fields


def test_run_request_rejects_an_empty_output_schema() -> None:
    with pytest.raises(ValidationError, match="output_schema"):
        _request(output_schema={})


def test_completed_turn_requires_and_strictly_parses_structured_output() -> None:
    turn = TurnRef(
        thread_id="thread-1",
        turn_id="turn-1",
        backend=RuntimeBackend.MOCK,
    )

    with pytest.raises(ValidationError, match="structured output"):
        TurnCompleted(turn=turn, status="completed", output=None)

    completed = TurnCompleted(
        turn=turn,
        status="completed",
        output='{"verdict":"PASS"}',
    )
    assert completed.parse_output_as(VerdictOutput) == VerdictOutput(verdict="PASS")

    malformed = TurnCompleted(
        turn=turn,
        status="completed",
        output='{"verdict":1}',
    )
    with pytest.raises(ValidationError):
        malformed.parse_output_as(VerdictOutput)


@pytest.mark.parametrize(
    ("thread", "expected_id"),
    [
        (ResumeThread(thread_id="thread-resume"), "thread-resume"),
        (ForkThread(thread_id="thread-fork"), "thread-fork"),
    ],
)
def test_run_request_preserves_resume_and_fork_targets(
    thread: ResumeThread | ForkThread,
    expected_id: str,
) -> None:
    target = _request(thread=thread).thread

    assert isinstance(target, (ResumeThread, ForkThread))
    assert target.thread_id == expected_id


def test_only_literature_roles_may_enable_web_search() -> None:
    with pytest.raises(ValidationError, match="web search"):
        _request(web_search=WebSearchMode.LIVE)

    request = _request(role=WorkRole.LITERATURE, web_search=WebSearchMode.LIVE)

    assert request.web_search is WebSearchMode.LIVE
    assert request.command_network is None


@pytest.mark.parametrize("role", [WorkRole.VERIFIER, WorkRole.CITATION])
def test_verification_roles_require_a_fresh_read_only_thread(role: WorkRole) -> None:
    verifier = _request(role=role)

    assert isinstance(verifier.thread, FreshThread)
    assert verifier.sandbox is SandboxMode.READ_ONLY
    assert verifier.web_search is WebSearchMode.DISABLED

    with pytest.raises(ValidationError, match="verification turns must be fresh"):
        _request(
            role=role,
            thread=ResumeThread(thread_id="prior-verifier"),
        )
    with pytest.raises(ValidationError, match="verification turns must be fresh"):
        _request(
            role=role,
            sandbox=SandboxMode.WORKSPACE_WRITE,
        )


def test_citation_role_may_use_live_search_while_remaining_read_only() -> None:
    request = _request(role=WorkRole.CITATION, web_search=WebSearchMode.LIVE)

    assert isinstance(request.thread, FreshThread)
    assert request.sandbox is SandboxMode.READ_ONLY
    assert request.web_search is WebSearchMode.LIVE


def test_literature_role_accepts_indexed_web_search() -> None:
    request = _request(role=WorkRole.LITERATURE, web_search=WebSearchMode.INDEXED)

    assert request.web_search is WebSearchMode.INDEXED


def test_command_network_requires_an_explicit_restricted_policy() -> None:
    request = _request(
        role=WorkRole.CITATION,
        command_network=RestrictedNetworkPolicy(domains=("api.crossref.org",)),
    )

    assert request.command_network is not None
    assert request.command_network.domains == ("api.crossref.org",)

    for wildcard in ("*", "*.example.com"):
        with pytest.raises(ValidationError, match="restricted"):
            RestrictedNetworkPolicy(domains=(wildcard,))


def test_auto_effort_uses_ultra_only_for_proactive_multi_agent() -> None:
    model = ModelCapability(
        model="gpt-5.6-sol",
        advertised_efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_effort="low",
    )

    proactive = resolve_capability(
        CapabilityRequest(model=model.model, proactive=True), model=model, multi_agent=True
    )
    ordinary = resolve_capability(
        CapabilityRequest(model=model.model, proactive=True), model=model, multi_agent=False
    )

    assert proactive.advertised_efforts == model.advertised_efforts
    assert proactive.selected_effort == "ultra"
    assert proactive.proactive_multi_agent is True
    assert ordinary.selected_effort == "low"
    assert ordinary.proactive_multi_agent is False


def test_proactive_multi_agent_fails_closed_without_selected_ultra() -> None:
    model = ModelCapability(
        model="gpt-5.6-sol",
        advertised_efforts=("low", "high"),
        default_effort="low",
    )

    automatic = resolve_capability(
        CapabilityRequest(model=model.model, proactive=True),
        model=model,
        multi_agent=True,
    )
    explicit = resolve_capability(
        CapabilityRequest(model=model.model, effort="high", proactive=True),
        model=model,
        multi_agent=True,
    )

    assert automatic.selected_effort == "low"
    assert automatic.proactive_multi_agent is False
    assert explicit.selected_effort == "high"
    assert explicit.proactive_multi_agent is False


def test_explicit_effort_is_validated_against_the_exact_model() -> None:
    model = ModelCapability(
        model="gpt-5.6-sol",
        advertised_efforts=("low", "high"),
        default_effort="low",
    )

    capability = resolve_capability(
        CapabilityRequest(model=model.model, effort="high"),
        model=model,
        multi_agent=False,
    )

    assert capability.selected_effort == "high"

    with pytest.raises(ValueError, match="not advertised"):
        resolve_capability(
            CapabilityRequest(model=model.model, effort="ultra"),
            model=model,
            multi_agent=True,
        )
