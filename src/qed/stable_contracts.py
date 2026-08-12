"""Stable-candidate contracts shared by policy, export, and release tooling.

These objects are intentionally independent of the runtime and persistence
implementations.  They are the small immutable boundary that lets application
code recompute decisions instead of trusting model prose.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from qed.schemas import Sha256, StrictModel, canonical_sha256, sha256_text


class EvidenceSource(StrEnum):
    RUNTIME_OBSERVED = "runtime_observed"
    MODEL_REPORTED = "model_reported"
    SERVER_CAPTURED = "server_captured"
    BENCHMARK_FIXTURE = "benchmark_fixture"
    OPERATOR_SUPPLIED = "operator_supplied"


class VerifierRole(StrEnum):
    STRUCTURAL = "structural"
    DETAILED_STEP = "detailed_step"
    ASSUMPTIONS_QUANTIFIERS = "assumptions_quantifiers"
    COUNTEREXAMPLE_EDGE_CASE = "counterexample_edge_case"
    RECONSTRUCTION = "reconstruction"
    CITATION = "citation"
    ADJUDICATOR = "adjudicator"


class RuntimeProvenance(StrictModel):
    """Immutable identity of the runtime that produced a turn."""

    schema_version: Literal[1] = 1
    model_provider: Literal["OpenAI"]
    model: Annotated[str, Field(min_length=1)]
    model_version: Annotated[str, Field(min_length=1)]
    backend: Literal["sdk", "app_server", "exec", "fixture"]
    codex_runtime_version: Annotated[str, Field(min_length=1)]
    codex_cli_version: Annotated[str, Field(min_length=1)]
    sdk_version: Annotated[str, Field(min_length=1)]
    app_server_version: Annotated[str, Field(min_length=1)]
    requested_effort: Annotated[str, Field(min_length=1)]
    selected_effort: Annotated[str, Field(min_length=1)]
    model_catalog_sha256: Sha256
    config_sha256: Sha256
    prompt_sha256: Sha256
    schema_sha256: Sha256
    executable_sha256: Sha256
    capability_response_sha256: Sha256
    protocol_version: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def validate_production_identity(self) -> Self:
        if self.backend != "fixture" and self.model_provider != "OpenAI":
            raise ValueError("production runtime provider must be OpenAI")
        if self.backend != "fixture" and self.model == "":
            raise ValueError("production runtime model must be exact and nonempty")
        if self.selected_effort != self.requested_effort and self.requested_effort != "auto":
            raise ValueError("explicit reasoning effort cannot be silently changed")
        return self


class ClaimType(StrEnum):
    ASSUMPTION = "assumption"
    DEFINITION = "definition"
    LEMMA = "lemma"
    STEP = "step"
    CONCLUSION = "conclusion"
    CITATION = "citation"


class ClaimCoverage(StrictModel):
    role: VerifierRole
    status: Literal["pass", "fail", "uncertain"]
    report_id: Annotated[str, Field(min_length=1)]
    check_id: Annotated[str, Field(min_length=1)]


class ProofObligation(StrictModel):
    claim_id: Annotated[str, Field(pattern=r"^claim-[A-Za-z0-9._-]+$")]
    byte_start: int = Field(ge=0)
    byte_end: int = Field(ge=1)
    span_sha256: Sha256
    claim_text: Annotated[str, Field(min_length=1)]
    claim_type: ClaimType
    dependencies: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    definitions_used: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    coverage: tuple[ClaimCoverage, ...] = ()

    @model_validator(mode="after")
    def validate_local_graph_fields(self) -> Self:
        if self.byte_end <= self.byte_start:
            raise ValueError("proof obligation byte span must be nonempty")
        for values, name in (
            (self.dependencies, "dependencies"),
            (self.assumptions, "assumptions"),
            (self.definitions_used, "definitions_used"),
            (self.evidence_ids, "evidence_ids"),
            (self.rule_ids, "rule_ids"),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must be unique")
        return self


class ProofObligationGraph(StrictModel):
    schema_version: Literal[1] = 1
    candidate_sha256: Sha256
    proof_sha256: Sha256
    proof_utf8_sha256: Sha256
    nodes: tuple[ProofObligation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        ids = [node.claim_id for node in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("claim IDs must be unique")
        known = set(ids)
        dependencies = {node.claim_id: set(node.dependencies) for node in self.nodes}
        unknown = {
            dependency
            for values in dependencies.values()
            for dependency in values
            if dependency not in known
        }
        if unknown:
            raise ValueError(f"claim graph has unknown dependencies: {sorted(unknown)}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(claim_id: str) -> None:
            if claim_id in visiting:
                raise ValueError("claim graph dependencies must be acyclic")
            if claim_id in visited:
                return
            visiting.add(claim_id)
            for dependency in dependencies[claim_id]:
                visit(dependency)
            visiting.remove(claim_id)
            visited.add(claim_id)

        for claim_id in ids:
            visit(claim_id)
        return self

    @classmethod
    def from_proof(
        cls,
        *,
        candidate_sha256: str,
        proof: str,
        nodes: tuple[ProofObligation, ...],
    ) -> ProofObligationGraph:
        proof_sha256 = sha256_text(proof)
        encoded = proof.encode("utf-8")
        for node in nodes:
            if node.byte_end > len(encoded):
                raise ValueError(f"claim span exceeds proof bytes: {node.claim_id}")
            if (
                hashlib.sha256(encoded[node.byte_start : node.byte_end]).hexdigest()
                != node.span_sha256
            ):
                raise ValueError(f"claim span hash does not match proof: {node.claim_id}")
        return cls(
            candidate_sha256=candidate_sha256,
            proof_sha256=proof_sha256,
            proof_utf8_sha256=sha256_text(encoded.decode("utf-8")),
            nodes=nodes,
        )

    def validate_against_proof(
        self,
        proof: str,
        *,
        evidence_ids: frozenset[str] = frozenset(),
        rule_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Recompute byte spans and reject graph metadata that was only model-reported."""

        encoded = proof.encode("utf-8")
        if self.proof_sha256 != sha256_text(proof):
            raise ValueError("claim graph proof hash does not match frozen proof")
        if self.proof_utf8_sha256 != sha256_text(proof):
            raise ValueError("claim graph UTF-8 hash does not match frozen proof")
        for node in self.nodes:
            if node.byte_end > len(encoded):
                raise ValueError(f"claim span exceeds frozen proof: {node.claim_id}")
            try:
                span = encoded[node.byte_start : node.byte_end].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"claim span splits UTF-8 codepoint: {node.claim_id}") from error
            if span != node.claim_text:
                raise ValueError(f"claim text does not match byte span: {node.claim_id}")
            if (
                hashlib.sha256(encoded[node.byte_start : node.byte_end]).hexdigest()
                != node.span_sha256
            ):
                raise ValueError(f"claim span hash does not match proof: {node.claim_id}")
            unknown_evidence = set(node.evidence_ids) - evidence_ids
            if unknown_evidence:
                raise ValueError(
                    f"claim {node.claim_id} references unknown evidence: "
                    f"{sorted(unknown_evidence)}"
                )
            unknown_rules = set(node.rule_ids) - rule_ids
            if unknown_rules:
                raise ValueError(
                    f"claim {node.claim_id} references unknown rules: {sorted(unknown_rules)}"
                )


class BundleVerificationResult(StrictModel):
    schema_version: Literal[1] = 1
    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    decision: Literal["PASS", "NON_PASS", "UNKNOWN"]
    signature_status: Literal["unsigned", "valid", "invalid", "unavailable"]
    checked_artifacts: tuple[str, ...] = ()


class EvidenceGate(StrictModel):
    gate_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]+$")]
    dimension: Literal["architecture", "security", "mathematics", "maturity"]
    required: bool = True
    status: Literal["passed", "failed", "blocked", "unrun", "unknown"]
    command: Annotated[str, Field(min_length=1)]
    utc_date: Annotated[str, Field(min_length=1)]
    commit_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]
    environment: Annotated[str, Field(min_length=1)]
    result: Annotated[str, Field(min_length=1)]
    artifact_path: Annotated[str, Field(min_length=1)]
    artifact_sha256: Sha256 | None = None
    limitation: str | None = None
    references: tuple[str, ...] = ()


class StableEvidence(StrictModel):
    schema_version: Literal[1] = 1
    commit_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]
    generated_at: Annotated[str, Field(min_length=1)]
    gates: tuple[EvidenceGate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_gates(self) -> Self:
        ids = [gate.gate_id for gate in self.gates]
        if len(ids) != len(set(ids)):
            raise ValueError("stable evidence gate IDs must be unique")
        return self

    def eligible_for_10(self, dimension: str) -> bool:
        gates = tuple(
            gate for gate in self.gates if gate.dimension == dimension and gate.required
        )
        return bool(gates) and all(gate.status == "passed" for gate in gates)


def stable_evidence_sha256(evidence: StableEvidence) -> str:
    return canonical_sha256(evidence)
