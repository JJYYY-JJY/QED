"""Immutable, content-addressed inputs for one research run."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from qed.schemas import NonEmptyStr, StrictModel, canonical_sha256

ProblemText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=131_072),
]
GuidanceText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=65_536),
]
VerificationRule = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8_192),
]


class FrozenVerificationRule(StrictModel):
    """Application-assigned identity for one rule in a frozen run input."""

    id: NonEmptyStr
    text: VerificationRule


class RunInput(StrictModel):
    schema_version: Literal[1] = 1
    problem: ProblemText
    prove_guidance: GuidanceText = ""
    verification_rules: tuple[VerificationRule, ...] = Field(
        default=(),
        max_length=64,
    )

    @model_validator(mode="after")
    def reject_duplicate_rules(self) -> Self:
        if len(set(self.verification_rules)) != len(self.verification_rules):
            raise ValueError("verification_rules must be unique")
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def frozen_verification_rules(self) -> tuple[FrozenVerificationRule, ...]:
        return tuple(
            FrozenVerificationRule(
                id=(
                    f"rule-{position:03d}-"
                    f"{canonical_sha256({'position': position, 'text': text})[:16]}"
                ),
                text=text,
            )
            for position, text in enumerate(self.verification_rules, start=1)
        )
