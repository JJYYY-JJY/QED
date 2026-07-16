"""Immutable, content-addressed inputs for one research run."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import StringConstraints, model_validator

from qed.schemas import StrictModel, canonical_sha256

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TrimmedStr = Annotated[str, StringConstraints(strip_whitespace=True)]


class RunInput(StrictModel):
    schema_version: Literal[1] = 1
    problem: NonEmptyStr
    prove_guidance: TrimmedStr = ""
    verification_rules: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def reject_duplicate_rules(self) -> Self:
        if len(set(self.verification_rules)) != len(self.verification_rules):
            raise ValueError("verification_rules must be unique")
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)

