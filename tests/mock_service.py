from __future__ import annotations

import json
from typing import Any

from qed.runtime import MockRuntime, RunRequest, RuntimeCapabilities
from qed.service import ApplicationService
from qed.service_settings import ServiceSettings
from qed.store import RunStore
from qed.workflow import ResearchWorkflow


def default_mock_runtime() -> MockRuntime:
    def verification_response(request: RunRequest) -> dict[str, Any]:
        start = request.prompt.index('<frozen-input encoding="canonical-json">')
        start = request.prompt.index("\n", start) + 1
        end = request.prompt.index("\n</frozen-input>", start)
        payload = json.loads(request.prompt[start:end])
        evidence = payload["evidence"] if request.role.value == "citation" else []
        proof = payload["candidate"]["proof"]
        return {
            "schema_version": 1,
            "checks": [
                {
                    "id": "fixture-check",
                    "category": "fixture-integrity",
                    "status": "pass",
                    "summary": "Deterministic benchmark fixture response.",
                    "rule_ids": [item["id"] for item in payload["verification_rules"]],
                    "evidence_ids": [item["id"] for item in evidence],
                    "citation_support": [
                        {
                            "evidence_id": item["id"],
                            "proof_span": proof,
                            "evidence_excerpt": item["content"],
                            "source_locator": item.get("source_uri")
                            or f"evidence:{item['id']}",
                        }
                        for item in evidence
                    ],
                }
            ],
        }

    runtime = MockRuntime(
        capabilities=RuntimeCapabilities(
            model="gpt-5.6-sol",
            advertised_efforts=("high",),
            default_effort="high",
            selected_effort="high",
            multi_agent=False,
            proactive_multi_agent=False,
        ),
        responses={
            "EvidenceBatch": {
                "schema_version": 1,
                "items": [
                    {
                        "kind": "note",
                        "title": "Benchmark fixture evidence",
                        "content": "Deterministic evidence for local offline validation.",
                    }
                ],
            },
            "PlanDraft": {
                "schema_version": 1,
                "strategy": "Apply the deterministic fixture argument.",
                "steps": [
                    {
                        "id": "fixture-step",
                        "statement": "Establish the requested conclusion.",
                        "rationale": "This only exercises the typed workflow.",
                        "success_criteria": ["The conclusion follows."],
                    }
                ],
            },
            "ProofDraft": {
                "schema_version": 1,
                "proof": "Deterministic benchmark fixture proof.",
            },
            "VerificationDraft": verification_response,
            "AdjudicationDraft": {
                "schema_version": 1,
                "outcome": "accept",
                "rationale": "All stable fixture reports passed.",
            },
        },
    )
    runtime.runtime_version = "fixture-runtime/1"  # type: ignore[attr-defined]
    return runtime


def build_mock_service(settings: ServiceSettings) -> ApplicationService:
    runtime = default_mock_runtime()
    store = RunStore(settings.database_path)
    workflow = ResearchWorkflow(
        store,
        runtime,
        runtime_version="fixture-runtime/1",
        export_root=settings.data_root / "exports",
    )
    return ApplicationService(
        store=store,
        workflow=workflow,
        runtime=runtime,
        managed_root=settings.data_root,
    )
