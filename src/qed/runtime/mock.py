from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterable, Mapping

from pydantic import JsonValue

from qed.schemas import sha256_text

from .models import (
    CapabilityRequest,
    RunRequest,
    RuntimeBackend,
    RuntimeCapabilities,
    RuntimeEvent,
    ThreadStarted,
    TokenUsage,
    TokenUsageUpdated,
    TurnCompleted,
    TurnRef,
    TurnStarted,
)


class MockRuntime:
    def __init__(
        self,
        events: Iterable[RuntimeEvent] = (),
        *,
        capabilities: RuntimeCapabilities | None = None,
        responses: Mapping[
            str,
            JsonValue | Callable[[RunRequest], JsonValue],
        ]
        | None = None,
    ) -> None:
        self._events = tuple(events)
        self._capabilities = capabilities
        self._responses = dict(responses) if responses is not None else None
        self._turn_count = 0
        self.requests: list[RunRequest] = []
        self.probes: list[CapabilityRequest] = []
        self.interruptions: list[TurnRef] = []

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        self.probes.append(request)
        if self._capabilities is None:
            raise RuntimeError("MockRuntime has no configured capabilities")
        return self._capabilities

    def runtime_resolution(
        self,
        capabilities: RuntimeCapabilities,
        *,
        requested_effort: str,
        config_sha256: str,
        prompt_sha256: str,
        schema_sha256: str,
        requested_backend: object | None = None,
    ) -> dict[str, object]:
        """Expose an explicit fixture provenance shape; never used by production construction."""

        del config_sha256, prompt_sha256, schema_sha256, requested_backend
        return {
            "schema_version": 1,
            "model_provider": "OpenAI",
            "model": capabilities.model,
            "model_version": "fixture-model-version",
            "backend": "fixture",
            "codex_runtime_version": "fixture-runtime/1",
            "codex_cli_version": "fixture-cli/1",
            "sdk_version": "fixture-sdk/1",
            "app_server_version": "fixture-app-server/1",
            "requested_effort": requested_effort,
            "selected_effort": capabilities.selected_effort,
            "model_catalog_sha256": sha256_text("fixture-model-catalog"),
            "config_sha256": sha256_text("fixture-config"),
            "prompt_sha256": sha256_text("fixture-prompts"),
            "schema_sha256": sha256_text("fixture-schemas"),
            "executable_sha256": sha256_text("fixture-executable"),
            "capability_response_sha256": sha256_text("fixture-capability-response"),
            "protocol_version": "qed-fixture-protocol-v1",
        }

    def preflight(self, request: RunRequest) -> None:
        del request

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        if self._responses is not None:
            title = request.output_schema.get("title")
            if not isinstance(title, str) or title not in self._responses:
                raise RuntimeError(f"MockRuntime has no response for schema: {title!r}")
            configured_response = self._responses[title]
            response = (
                configured_response(request)
                if callable(configured_response)
                else configured_response
            )
            self._turn_count += 1
            thread_id = f"mock-{title.lower()}-{self._turn_count}"
            turn = TurnRef(
                thread_id=thread_id,
                turn_id=f"turn-{self._turn_count}",
                backend=RuntimeBackend.MOCK,
            )
            yield ThreadStarted(thread_id=thread_id, backend=RuntimeBackend.MOCK)
            yield TurnStarted(turn=turn)
            yield TokenUsageUpdated(
                thread_id=thread_id,
                turn_id=turn.turn_id,
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
            yield TurnCompleted(
                turn=turn,
                status="completed",
                output=json.dumps(
                    response,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return
        for event in self._events:
            yield event

    async def interrupt(self, turn: TurnRef) -> None:
        self.interruptions.append(turn)

    async def close(self) -> None:
        return None
