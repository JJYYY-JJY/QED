"""Pure runtime-event and concurrency helpers for stage orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import JsonValue

from qed.runtime import ItemCompleted, RuntimePreference, TurnRef
from qed.schemas import (
    WebSearchObservation,
    canonical_sha256,
    sha256_text,
)
from qed.state_machine import ThreadRole


def runtime_preference(value: str) -> RuntimePreference:
    if value == "app-server":
        return RuntimePreference.APP_SERVER
    return RuntimePreference(value)


def make_local_thread_id(run_id: str, role: ThreadRole, external_id: str) -> str:
    identity: dict[str, JsonValue] = {
        "run_id": run_id,
        "role": role.value,
        "external_thread_id": external_id,
    }
    return f"thread-{canonical_sha256(identity)[:24]}"


def counts_as_search_query(event: ItemCompleted) -> bool:
    if event.item_type not in {"webSearch", "web_search", "web_search_call"}:
        return False
    action = event.payload.get("action")
    if not isinstance(action, dict):
        return True
    action_type = action.get("type")
    if action_type in {"openPage", "open_page", "findInPage", "find_in_page"}:
        return False
    return action_type == "search" or action_type is None


def web_search_observation(
    event: ItemCompleted,
    *,
    run_id: str,
    local_thread_id: str,
    turn: TurnRef,
    captured_at: datetime,
) -> WebSearchObservation | None:
    """Materialize the URL identity exposed by a native web-search action."""

    if event.item_type not in {"webSearch", "web_search", "web_search_call"}:
        return None
    action = event.payload.get("action")
    if not isinstance(action, dict):
        return None
    action_types = {
        "openPage": "open_page",
        "open_page": "open_page",
        "findInPage": "find_in_page",
        "find_in_page": "find_in_page",
    }
    runtime_action_type = action.get("type")
    if not isinstance(runtime_action_type, str):
        return None
    action_type = action_types.get(runtime_action_type)
    uri = action.get("url")
    if action_type is None or not isinstance(uri, str):
        return None
    payload = cast(dict[str, JsonValue], event.payload)
    identity: dict[str, JsonValue] = {
        "run_id": run_id,
        "backend": turn.backend.value,
        "external_thread_id": turn.thread_id,
        "turn_id": turn.turn_id,
        "item_id": event.item_id,
        "action_type": action_type,
        "uri": uri,
        "payload_sha256": canonical_sha256(payload),
    }
    return WebSearchObservation(
        id=f"observation-{canonical_sha256(identity)[:24]}",
        run_id=run_id,
        backend=turn.backend.value,
        local_thread_id=local_thread_id,
        external_thread_id=turn.thread_id,
        turn_id=turn.turn_id,
        item_id=event.item_id,
        action_type=cast(Literal["open_page", "find_in_page"], action_type),
        uri=uri,
        uri_sha256=sha256_text(uri),
        payload=payload,
        payload_sha256=canonical_sha256(payload),
        captured_at=captured_at,
    )


async def gather_strict[GatherT](
    coroutines: tuple[Coroutine[Any, Any, GatherT], ...],
) -> tuple[GatherT, ...]:
    """Cancel and drain every sibling when one concurrent operation fails."""

    tasks = tuple(asyncio.create_task(coroutine) for coroutine in coroutines)
    try:
        return tuple(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
