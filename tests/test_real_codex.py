from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from qed.config import BudgetPolicy, ParallelismPolicy, QEDConfig, SearchPolicy
from qed.decision import candidate_decision_sha256
from qed.inputs import RunInput
from qed.runtime import (
    CapabilityRequest,
    FreshThread,
    RunRequest,
    RuntimeBackend,
    RuntimePreference,
    SandboxMode,
    ThreadStarted,
    TurnCompleted,
    WebSearchMode,
    WorkRole,
    create_codex_runtime,
)
from qed.schemas import (
    Manifest,
    canonical_json,
    canonical_sha256,
    sha256_text,
    verification_report_sha256,
)
from qed.store import RunStage, RunStatus, RunStore
from qed.workflow import ResearchWorkflow

MODEL = "gpt-5.6-sol"
EFFORT = "low"


class SmokeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    ok: Literal[True]


def _required_dedicated_roots() -> tuple[Path, Path]:
    if os.environ.get("QED_RUN_REAL_CODEX") != "1":
        pytest.skip("set QED_RUN_REAL_CODEX=1 to permit a real Codex call")

    values: dict[str, Path] = {}
    for name in ("QED_REAL_CODEX_DATA_ROOT", "QED_REAL_CODEX_HOME"):
        raw = os.environ.get(name)
        if raw is None:
            pytest.skip(f"set {name} to an existing absolute dedicated directory")
        path = Path(raw)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            pytest.skip(f"{name} must be an existing absolute non-symlink directory")
        values[name] = path.resolve(strict=True)

    data_root = values["QED_REAL_CODEX_DATA_ROOT"]
    codex_home = values["QED_REAL_CODEX_HOME"]
    if codex_home != (data_root / "codex-home").resolve(strict=False):
        pytest.skip("QED_REAL_CODEX_HOME must be <QED_REAL_CODEX_DATA_ROOT>/codex-home")

    personal_codex_home = (Path.home() / ".codex").resolve(strict=False)
    if codex_home == personal_codex_home or codex_home.is_relative_to(personal_codex_home):
        pytest.skip("the smoke test refuses to use the personal ~/.codex tree")
    return data_root, codex_home


def _initialize_empty_git_workspace(data_root: Path) -> tempfile.TemporaryDirectory[str]:
    git_path = shutil.which("git")
    if git_path is None:
        pytest.skip("git is required for an isolated real-Codex workspace")
    git = Path(git_path).resolve(strict=True)
    workspace = tempfile.TemporaryDirectory(prefix=".real-codex-", dir=data_root)
    template = tempfile.TemporaryDirectory(prefix=".git-template-", dir=data_root)
    try:
        subprocess.run(  # noqa: S603 - resolved executable and fixed argv
            (
                str(git),
                "init",
                "--quiet",
                f"--template={template.name}",
                "--initial-branch=qed",
                workspace.name,
            ),
            check=True,
            capture_output=True,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "LC_ALL": "C",
            },
        )
    except BaseException:
        workspace.cleanup()
        raise
    finally:
        template.cleanup()
    return workspace


@pytest.mark.real_codex
@pytest.mark.parametrize(
    ("preference", "expected_backend"),
    (
        (RuntimePreference.SDK, RuntimeBackend.SDK),
        (RuntimePreference.APP_SERVER, RuntimeBackend.APP_SERVER),
    ),
)
async def test_authenticated_runtime_completes_schema_constrained_offline_turn(
    preference: RuntimePreference,
    expected_backend: RuntimeBackend,
) -> None:
    data_root, codex_home = _required_dedicated_roots()
    workspace = _initialize_empty_git_workspace(data_root)
    try:
        runtime = create_codex_runtime(codex_home)
        try:
            capabilities = await runtime.probe(CapabilityRequest(model=MODEL, effort=EFFORT))
            assert capabilities.model == MODEL
            assert capabilities.selected_effort == EFFORT
            assert EFFORT in capabilities.advertised_efforts

            request = RunRequest(
                model=MODEL,
                effort=capabilities.selected_effort,
                prompt=(
                    "Return schema_version 1 and ok true as the schema-constrained "
                    "final response. Do not perform any other work."
                ),
                output_schema=SmokeOutput.model_json_schema(),
                thread=FreshThread(),
                role=WorkRole.VERIFIER,
                sandbox=SandboxMode.READ_ONLY,
                web_search=WebSearchMode.DISABLED,
                runtime=preference,
                cwd=Path(workspace.name),
            )
            events = [event async for event in runtime.stream(request)]
        finally:
            await runtime.close()
    finally:
        workspace.cleanup()

    threads = [event for event in events if isinstance(event, ThreadStarted)]
    terminals = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(threads) == 1
    assert threads[0].backend is expected_backend
    assert len(terminals) == 1
    assert terminals[0].status == "completed"
    assert terminals[0].turn.thread_id == threads[0].thread_id
    assert terminals[0].parse_output_as(SmokeOutput) == SmokeOutput(
        schema_version=1,
        ok=True,
    )


@pytest.mark.real_codex
async def test_authenticated_exec_runtime_is_separately_opted_in() -> None:
    if os.environ.get("QED_RUN_REAL_CODEX_EXEC") != "1":
        pytest.skip("set QED_RUN_REAL_CODEX_EXEC=1 to permit the exec fallback smoke test")
    data_root, codex_home = _required_dedicated_roots()
    workspace = _initialize_empty_git_workspace(data_root)
    try:
        runtime = create_codex_runtime(codex_home)
        try:
            capabilities = await runtime.probe(CapabilityRequest(model=MODEL, effort=EFFORT))
            request = RunRequest(
                model=MODEL,
                effort=capabilities.selected_effort,
                prompt="Return schema_version 1 and ok true. Do no other work.",
                output_schema=SmokeOutput.model_json_schema(),
                thread=FreshThread(),
                role=WorkRole.VERIFIER,
                sandbox=SandboxMode.READ_ONLY,
                web_search=WebSearchMode.DISABLED,
                runtime=RuntimePreference.EXEC,
                cwd=Path(workspace.name),
            )
            events = [event async for event in runtime.stream(request)]
        finally:
            await runtime.close()
    finally:
        workspace.cleanup()

    threads = [event for event in events if isinstance(event, ThreadStarted)]
    terminals = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(threads) == 1
    assert threads[0].backend is RuntimeBackend.EXEC
    assert len(terminals) == 1
    assert terminals[0].status == "completed"
    assert terminals[0].parse_output_as(SmokeOutput).ok


def _recompute_export_hashes(
    store: RunStore,
    *,
    export_root: Path,
    run_id: str,
) -> Manifest:
    snapshot = store.snapshot(run_id)
    manifest_artifact = next(
        artifact for artifact in snapshot.artifacts if artifact.kind == "manifest"
    )
    assert manifest_artifact.relative_path is not None
    manifest_path = export_root / manifest_artifact.relative_path
    manifest_text = manifest_path.read_text()
    manifest = Manifest.model_validate_json(manifest_text)
    assert manifest_text == f"{canonical_json(manifest)}\n"
    assert manifest_artifact.sha256 == sha256_text(manifest_text)

    files = {
        artifact.kind: export_root / artifact.relative_path
        for artifact in snapshot.artifacts
        if artifact.relative_path is not None
    }
    manifest_artifacts = {artifact.kind: artifact for artifact in manifest.artifacts}
    assert sha256_text(files["proof"].read_text()) == manifest_artifacts["proof"].sha256
    assert (
        sha256_text(files["report"].read_text())
        == manifest_artifacts["verification_report"].sha256
    )

    assert manifest.candidate_records == tuple(
        type(record)(id=item.id, sha256=item.candidate_sha256)
        for record, item in zip(
            manifest.candidate_records,
            sorted(snapshot.candidates, key=lambda item: (item.attempt, item.id)),
            strict=True,
        )
    )
    expected_report_hashes = {
        item.id: verification_report_sha256(item.report)
        for item in snapshot.verifications
    }
    assert {
        record.id: record.sha256 for record in manifest.verification_records
    } == expected_report_hashes
    assert {
        record.id: record.sha256 for record in manifest.evidence_records
    } == {item.id: canonical_sha256(item) for item in snapshot.evidence}
    assert {
        record.id: record.sha256 for record in manifest.plan_records
    } == {item.id: canonical_sha256(item) for item in snapshot.plans}
    assert {
        record.id: record.sha256 for record in manifest.adjudication_records
    } == {item.id: canonical_sha256(item) for item in snapshot.adjudications}
    assert {
        record.id: record.sha256 for record in manifest.decision_records
    } == {
        item.candidate_id: candidate_decision_sha256(item)
        for item in snapshot.decisions
    }

    selected_events = tuple(
        event for event in snapshot.events if event.seq <= manifest.last_event_seq
    )
    digest = hashlib.sha256()
    for event in selected_events:
        digest.update(canonical_json(event).encode())
        digest.update(b"\n")
    assert digest.hexdigest() == manifest.event_chain_sha256
    assert tuple(event.seq for event in selected_events) == tuple(
        range(manifest.first_event_seq or 1, (manifest.last_event_seq or 0) + 1)
    )
    return manifest


@pytest.mark.real_codex
async def test_authenticated_runtime_completes_full_research_lifecycle() -> None:
    if os.environ.get("QED_RUN_REAL_CODEX_LIFECYCLE") != "1":
        pytest.skip(
            "set QED_RUN_REAL_CODEX_LIFECYCLE=1 to permit the multi-turn lifecycle test"
        )
    data_root, codex_home = _required_dedicated_roots()
    backend = os.environ.get("QED_REAL_CODEX_LIFECYCLE_BACKEND", "sdk")
    if backend not in {"sdk", "app-server"}:
        pytest.skip("QED_REAL_CODEX_LIFECYCLE_BACKEND must be sdk or app-server")
    run_root = tempfile.TemporaryDirectory(prefix=".real-lifecycle-", dir=data_root)
    runtime = create_codex_runtime(codex_home)
    store = RunStore(Path(run_root.name) / "qed.sqlite3")
    export_root = Path(run_root.name) / "exports"
    workflow = ResearchWorkflow(
        store,
        runtime,
        runtime_version=runtime.runtime_version,
        export_root=export_root,
    )
    run_id = f"real-{uuid4().hex}"
    try:
        config = QEDConfig(
            model=MODEL,
            effort=EFFORT,
            backend=backend,
            parallelism=ParallelismPolicy(
                runs=1,
                proof_candidates=2,
                verifiers=3,
                proactive_multi_agent=False,
            ),
            budgets=BudgetPolicy(
                run_seconds=3600,
                stage_seconds=1200,
                max_tokens=200_000,
                proof_attempts=2,
                plan_revisions=0,
                strategy_rewrites=0,
                turn_retries=1,
            ),
            search=SearchPolicy(enabled=False),
        )
        workflow.create_run(
            RunInput(
                problem=(
                    "Prove that for every integer n, if n is even then n squared is even."
                ),
                verification_rules=(
                    "Check that the proof explicitly uses the definition of an even integer.",
                ),
            ),
            config,
            run_id=run_id,
        )
        completed = await workflow.execute(run_id)
        assert completed.status is RunStatus.COMPLETED
        assert completed.stage is RunStage.COMPLETE

        snapshot = store.snapshot(run_id)
        assert len(snapshot.candidates) >= 2
        assert {report.kind for report in snapshot.verifications} >= {
            "structural",
            "detailed",
            "citation",
        }
        assert snapshot.adjudications[-1].outcome == "accept"
        assert snapshot.decisions[-1].passed
        external_ids = {
            thread.id: thread.external_thread_id for thread in snapshot.threads
        }
        assert all(external_ids.values())
        for candidate in snapshot.candidates:
            prover_external = external_ids[candidate.thread_id]
            verifier_externals = {
                external_ids[report.thread_id]
                for report in snapshot.verifications
                if report.candidate_id == candidate.id
            }
            assert prover_external not in verifier_externals
            assert len(verifier_externals) == len(
                [
                    report
                    for report in snapshot.verifications
                    if report.candidate_id == candidate.id
                ]
            )

        manifest = _recompute_export_hashes(
            store,
            export_root=export_root,
            run_id=run_id,
        )
        assert manifest.code_verdict == "PASS"
        assert manifest.run_status == "running"
        assert manifest.run_stage == "export"
        assert manifest.publication_phase == "export_intent"
    finally:
        await workflow.close()
        await runtime.close()
        store.close()
        run_root.cleanup()
