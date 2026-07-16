from __future__ import annotations

import pytest
from pydantic import ValidationError

from qed.runtime import (
    CapabilityRequest,
    ForkThread,
    FreshThread,
    ModelCapability,
    RestrictedNetworkPolicy,
    ResumeThread,
    RunRequest,
    SandboxMode,
    WebSearchMode,
    WorkRole,
    resolve_capability,
)


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

    with pytest.raises(ValidationError, match="restricted"):
        RestrictedNetworkPolicy(domains=("*",))


def test_auto_effort_uses_last_advertised_only_for_proactive_multi_agent() -> None:
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
