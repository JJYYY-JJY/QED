from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from qed.api import create_app
from qed.config import QEDConfig
from qed.inputs import RunInput
from qed.runtime import MockRuntime, RuntimeCapabilities
from qed.service import ApplicationService
from qed.service_settings import ServiceSettings
from qed.store import RunStore
from qed.workflow import ResearchWorkflow


def _service(tmp_path: Path) -> ApplicationService:
    runtime = MockRuntime(
        capabilities=RuntimeCapabilities(
            model="gpt-5.6-sol",
            advertised_efforts=("high",),
            default_effort="high",
            selected_effort="high",
            multi_agent=False,
            proactive_multi_agent=False,
        )
    )
    store = RunStore(tmp_path / "qed.sqlite3")
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    return ApplicationService(store=store, workflow=workflow, runtime=runtime)


def test_run_can_be_created_and_read_through_typed_api(tmp_path: Path) -> None:
    service = _service(tmp_path)
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={
                "run_id": "run-api-1",
                "run_input": {
                    "problem": "Prove that there are infinitely many primes.",
                    "verification_rules": ["Check the contradiction explicitly."],
                },
                "config": {
                    "search": {
                        "enabled": True,
                        "allowed_roles": ["literature", "citation"],
                        "max_queries_per_stage": 20,
                    }
                },
            },
        )
        listed = client.get("/api/v1/runs")
        status = client.get("/api/v1/runs/run-api-1")
        snapshot = client.get("/api/v1/runs/run-api-1/snapshot")

    assert response.status_code == 201
    assert response.json()["id"] == "run-api-1"
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == ["run-api-1"]
    assert status.json()["status"] == "created"
    assert snapshot.json()["run_input"]["problem"].startswith("Prove that")
    assert snapshot.json()["run_input"]["verification_rules"] == [
        "Check the contradiction explicitly."
    ]


def test_app_factory_defaults_to_managed_mock_service(tmp_path: Path) -> None:
    app = create_app(settings=ServiceSettings(data_root=tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs",
            json={"run_id": "run-managed", "run_input": {"problem": "Prove P."}},
        )

    assert created.status_code == 201
    assert (tmp_path / "qed.sqlite3").is_file()


def test_capabilities_describe_versioned_commands_and_sse(tmp_path: Path) -> None:
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=_service(tmp_path),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "api_version": "v1",
        "default_model": "gpt-5.6-sol",
        "commands": ["start", "cancel", "resume"],
        "event_transport": "sse",
        "authentication_required": False,
    }


def test_run_collection_is_bounded_filterable_and_paginated(tmp_path: Path) -> None:
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=_service(tmp_path),
    )

    with TestClient(app) as client:
        for number in range(3):
            client.post(
                "/api/v1/runs",
                json={
                    "run_id": f"run-page-{number}",
                    "run_input": {"problem": f"Prove P_{number}."},
                },
            )
        response = client.get("/api/v1/runs?status=created&offset=1&limit=1")

    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert response.json()["offset"] == 1
    assert response.json()["limit"] == 1
    assert [item["id"] for item in response.json()["items"]] == ["run-page-1"]


def test_bearer_auth_is_enforced_without_exposing_the_secret(tmp_path: Path) -> None:
    token = "s" * 32
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path, auth_token=token),
        service=_service(tmp_path),
    )

    with TestClient(app) as client:
        missing = client.get("/api/v1/runs")
        wrong = client.get(
            "/api/v1/runs",
            headers={"Authorization": f"Bearer {'x' * 32}"},
        )
        accepted = client.get(
            "/api/v1/runs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code == 200
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json()["error"]["code"] == "authentication_required"
    assert token not in missing.text + wrong.text + accepted.text


def test_run_creation_rejects_declared_and_streamed_oversized_bodies_before_auth(
    tmp_path: Path,
) -> None:
    token = "s" * 32
    service = _service(tmp_path)
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path, auth_token=token),
        service=service,
    )
    oversized_body = json.dumps(
        {
            "run_id": "run-oversized",
            "run_input": {"problem": "P" * (1024 * 1024)},
        }
    )

    with TestClient(app) as client:
        declared_response = client.post(
            "/api/v1/runs",
            content=oversized_body,
            headers={"Content-Type": "application/json"},
        )
        assert service.list_runs() == ()

    async def post_streamed_body() -> list[Message]:
        received: list[Message] = [
            {"type": "http.request", "body": b"x" * 600_000, "more_body": True},
            {"type": "http.request", "body": b"y" * 600_000, "more_body": False},
        ]
        sent: list[Message] = []

        async def receive() -> Message:
            return received.pop(0)

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/runs",
            "raw_path": b"/api/v1/runs",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "state": {},
        }
        await app(scope, receive, send)
        return sent

    streamed_messages = asyncio.run(post_streamed_body())
    streamed_start = next(
        message for message in streamed_messages if message["type"] == "http.response.start"
    )
    streamed_body = b"".join(
        message.get("body", b"")
        for message in streamed_messages
        if message["type"] == "http.response.body"
    )

    assert declared_response.status_code == 413
    assert declared_response.json()["error"]["code"] == "request_too_large"
    assert streamed_start["status"] == 413
    assert json.loads(streamed_body)["error"]["code"] == "request_too_large"


@pytest.mark.parametrize(
    "run_input",
    (
        {"problem": "P" * 131_073},
        {"problem": "Prove P.", "prove_guidance": "G" * 65_537},
        {
            "problem": "Prove P.",
            "verification_rules": ["R" * 8_193],
        },
        {
            "problem": "Prove P.",
            "verification_rules": [f"Check condition {index}." for index in range(65)],
        },
    ),
)
def test_run_creation_rejects_inputs_beyond_finite_schema_bounds(
    tmp_path: Path,
    run_input: dict[str, object],
) -> None:
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=_service(tmp_path),
    )

    with TestClient(app) as client:
        response = client.post("/api/v1/runs", json={"run_input": run_input})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_api_maps_not_found_and_validation_errors_to_safe_envelopes(tmp_path: Path) -> None:
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=_service(tmp_path),
    )

    with TestClient(app) as client:
        missing = client.get("/api/v1/runs/does-not-exist")
        invalid = client.post(
            "/api/v1/runs",
            json={"run_id": "bad/id", "run_input": {"problem": ""}, "config": {}},
        )
        invalid_command = client.post(
            "/api/v1/runs/does-not-exist/commands/start",
            json={"idempotency_key": "bad/key"},
        )

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "run_not_found"
    assert "does-not-exist" not in missing.text
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert invalid.json()["error"]["message"] == "Request validation failed."
    assert invalid.json()["error"]["diagnostic_id"].startswith("diag-")

    colon = client.post(
        "/api/v1/runs",
        json={"run_id": "bad:run", "run_input": {"problem": "Prove P."}},
    )
    assert colon.status_code == 422
    assert invalid_command.status_code == 422
    assert invalid_command.json()["error"]["code"] == "invalid_request"


def test_unexpected_server_errors_do_not_expose_internal_details(tmp_path: Path) -> None:
    service = _service(tmp_path)
    asyncio.run(service.close())
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=service,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/runs")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "closed database" not in response.text.lower()


def test_unknown_api_routes_use_the_stable_error_envelope(tmp_path: Path) -> None:
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=_service(tmp_path),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/not-a-route")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "route_not_found"


def test_start_command_returns_typed_acknowledgement(tmp_path: Path) -> None:
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=_service(tmp_path),
    )

    with TestClient(app) as client:
        client.post(
            "/api/v1/runs",
            json={"run_id": "run-start", "run_input": {"problem": "Prove P."}},
        )
        response = client.post(
            "/api/v1/runs/run-start/commands/start",
            json={"idempotency_key": "start-1"},
        )

    assert response.status_code == 202
    assert response.json() == {
        "schema_version": 1,
        "run_id": "run-start",
        "command": "start",
        "idempotency_key": "start-1",
        "accepted": True,
        "status": "created",
    }


def test_missing_run_commands_do_not_retain_process_locks(tmp_path: Path) -> None:
    service = _service(tmp_path)
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=service,
    )

    with TestClient(app) as client:
        responses = tuple(
            client.post(
                f"/api/v1/runs/missing-{command}/commands/{command}",
                json={"idempotency_key": f"{command}-missing"},
            )
            for command in ("start", "cancel", "resume")
        )

        assert [response.status_code for response in responses] == [404, 404, 404]
        assert service._run_locks == {}


def test_cancel_and_resume_commands_use_typed_idempotent_receipts(tmp_path: Path) -> None:
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=_service(tmp_path),
    )

    with TestClient(app) as client:
        client.post(
            "/api/v1/runs",
            json={"run_id": "run-lifecycle", "run_input": {"problem": "Prove P."}},
        )
        cancelled = client.post(
            "/api/v1/runs/run-lifecycle/commands/cancel",
            json={"idempotency_key": "cancel-1"},
        )
        resumed = client.post(
            "/api/v1/runs/run-lifecycle/commands/resume",
            json={"idempotency_key": "resume-1"},
        )

    assert cancelled.status_code == 202
    assert cancelled.json()["command"] == "cancel"
    assert cancelled.json()["status"] == "cancelled"
    assert resumed.status_code == 202
    assert resumed.json()["command"] == "resume"
    assert resumed.json()["status"] == "cancelled"


def test_sse_replays_after_last_event_id_and_closes_for_terminal_run(
    tmp_path: Path,
) -> None:
    runtime = MockRuntime(
        capabilities=RuntimeCapabilities(
            model="gpt-5.6-sol",
            advertised_efforts=("high",),
            default_effort="high",
            selected_effort="high",
            multi_agent=False,
            proactive_multi_agent=False,
        )
    )
    store = RunStore(tmp_path / "qed.sqlite3")
    service = ApplicationService(
        store=store,
        workflow=ResearchWorkflow(store, runtime, runtime_version="test-runtime"),
        runtime=runtime,
    )
    service.create_run(
        run_input=RunInput(problem="Prove P."),
        config=QEDConfig(),
        run_id="run-events",
    )
    replayed = store.list_events("run-events")[-1]
    store.request_cancel("run-events")
    store.acknowledge_cancel("run-events")
    app = create_app(
        settings=ServiceSettings(data_root=tmp_path),
        service=service,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/runs/run-events/events",
            headers={"Accept": "text/event-stream", "Last-Event-ID": str(replayed.seq - 1)},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f"id: {replayed.seq}\n" in response.text
    assert f"event: {replayed.event_type}\n" in response.text
    assert '"sequence":' in response.text
    assert "run.cancelled" in response.text
