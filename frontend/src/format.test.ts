import { expect, it } from "vitest";

import { totalTokens } from "./format";
import type { RunEvent } from "./types";

function usageEvent(
  seq: number,
  threadId: string,
  turnId: string,
  inputTokens: number,
  outputTokens: number,
): RunEvent {
  return {
    schema_version: 1,
    run_id: "run-1",
    seq,
    event_type: "runtime.token_usage",
    stage: "proving",
    payload: {
      thread_id: threadId,
      turn_id: turnId,
      usage: { input_tokens: inputTokens, output_tokens: outputTokens },
    },
    payload_sha256: "0".repeat(64),
    created_at: "2026-07-16T12:00:00Z",
  };
}

it("keeps equal turn identifiers distinct across runtime threads", () => {
  expect(
    totalTokens([
      usageEvent(1, "thread-a", "turn-1", 10, 5),
      usageEvent(2, "thread-b", "turn-1", 20, 7),
      usageEvent(3, "thread-a", "turn-1", 12, 6),
    ]),
  ).toBe(45);
});
