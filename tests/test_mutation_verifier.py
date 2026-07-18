from __future__ import annotations

import pytest

from qed.decision import CandidateIntegrityError, decide_candidate
from tests.test_decision import candidate


def test_mutated_candidate_is_rejected_before_verdict_computation() -> None:
    original = candidate()
    mutated = original.model_copy(update={"proof": f"{original.proof}\nUnverified addition."})

    with pytest.raises(CandidateIntegrityError, match="candidate proof hash"):
        decide_candidate(
            mutated,
            (),
            prover_external_thread_id="codex-writer-thread",
        )
