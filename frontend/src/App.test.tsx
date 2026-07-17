import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { completedSnapshot, runRecord } from "./test/fixtures";

function requestUrl(input: string | URL | Request): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(message: string, status = 503): Response {
  return new Response(JSON.stringify({ error: { code: "unavailable", message } }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("QED research console", () => {
  it("puts a sealed proof and independent checks at the center of a completed run", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = requestUrl(input);
      if (url.endsWith("/api/v1/capabilities")) {
        return Promise.resolve(
          jsonResponse({
            schema_version: 1,
            api_version: "v1",
            default_model: "gpt-5.6-sol",
            commands: ["start", "cancel", "resume"],
            event_transport: "sse",
            authentication_required: false,
          }),
        );
      }
      if (url.endsWith("/api/v1/runs?limit=100")) {
        return Promise.resolve(
          jsonResponse({ schema_version: 1, items: [runRecord], total: 1, offset: 0, limit: 100 }),
        );
      }
      if (url.endsWith(`/api/v1/runs/${runRecord.id}/snapshot`)) {
        return Promise.resolve(jsonResponse(completedSnapshot));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Candidate 1" })).toBeInTheDocument();
    expect(screen.getByText(/Every finite subgroup of/)).toBeInTheDocument();
    expect(screen.getByText("PASS")).toBeInTheDocument();
    expect(screen.getByText("3 independent reports")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Structural/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Detailed/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Citation/ })).toBeInTheDocument();
  });

  it("exposes the evidence ledger and event audit trail without leaving the run", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = requestUrl(input);
      if (url.endsWith("/api/v1/capabilities")) {
        return Promise.resolve(jsonResponse({ default_model: "gpt-5.6-sol", authentication_required: false }));
      }
      if (url.endsWith("/api/v1/runs?limit=100")) {
        return Promise.resolve(jsonResponse({ items: [runRecord], total: 1, offset: 0, limit: 100 }));
      }
      return Promise.resolve(jsonResponse(completedSnapshot));
    });

    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Candidate 1" });

    await user.click(screen.getByRole("tab", { name: "Evidence" }));
    expect(screen.getByRole("heading", { name: "Evidence ledger" })).toBeInTheDocument();
    expect(screen.getByText("Classical group result")).toBeInTheDocument();
    expect(screen.getAllByText(/1f2e3d/).length).toBeGreaterThan(0);

    await user.click(screen.getByRole("tab", { name: "Activity" }));
    const timeline = screen.getByLabelText("Run event timeline");
    expect(within(timeline).getByText("Candidate sealed")).toBeInTheDocument();
    expect(within(timeline).getByText("#8")).toBeInTheDocument();
  });

  it("creates and starts a run from a problem-first editor", async () => {
    const calls: string[] = [];
    let submittedBody: Record<string, unknown> | null = null;
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = requestUrl(input);
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url.endsWith("/api/v1/capabilities")) {
        return Promise.resolve(jsonResponse({ default_model: "gpt-5.6-sol", authentication_required: false }));
      }
      if (url.endsWith("/api/v1/runs?limit=100")) {
        return Promise.resolve(jsonResponse({ items: [], total: 0, offset: 0, limit: 100 }));
      }
      if (url.endsWith("/api/v1/runs") && init?.method === "POST") {
        if (typeof init.body !== "string") throw new Error("Expected a JSON request body.");
        submittedBody = JSON.parse(init.body) as Record<string, unknown>;
        return Promise.resolve(jsonResponse({ ...runRecord, status: "created", stage: "intake" }));
      }
      if (url.includes("/commands/start")) {
        return Promise.resolve(jsonResponse({ accepted: true, command: "start", status: "created" }));
      }
      if (url.endsWith("/snapshot")) {
        return Promise.resolve(jsonResponse({ ...completedSnapshot, candidates: [], verifications: [] }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "New research run" }));
    await user.type(screen.getByLabelText("Problem statement"), "Prove that every finite field has cyclic multiplicative group.");
    await user.type(screen.getByLabelText(/^Verification rules/), "Check every use of finiteness.\nVerify all citations.");
    await user.click(screen.getByText("Runtime and budgets"));
    await user.clear(screen.getByLabelText("Effort"));
    await user.type(screen.getByLabelText("Effort"), "max");
    await user.selectOptions(screen.getByLabelText("Runtime backend"), "exec");
    expect(screen.queryByRole("option", { name: "Workspace write" })).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("Concurrent runs"));
    expect(screen.getByLabelText("Concurrent runs")).toHaveValue(null);
    await user.type(screen.getByLabelText("Concurrent runs"), "2");
    await user.click(screen.getByRole("button", { name: "Create and start" }));

    await waitFor(() => {
      expect(calls.some((call) => call === "POST /api/v1/runs")).toBe(true);
      expect(calls.some((call) => call.includes("/commands/start"))).toBe(true);
    });
    expect(submittedBody).toMatchObject({
      schema_version: 1,
      run_input: {
        verification_rules: ["Check every use of finiteness.", "Verify all citations."],
      },
      config: {
        model: "gpt-5.6-sol",
        effort: "max",
        backend: "exec",
        parallelism: { runs: 2, proof_candidates: 4, verifiers: 2, proactive_multi_agent: true },
        budgets: {
          run_seconds: 7200,
          stage_seconds: 1800,
          max_tokens: 250000,
          proof_attempts: 8,
          plan_revisions: 2,
          strategy_rewrites: 2,
          turn_retries: 2,
        },
        search: {
          enabled: true,
          allowed_roles: ["literature", "citation"],
          max_queries_per_stage: 20,
        },
        sandbox: {
          literature: "read-only",
          planner: "read-only",
          prover: "read-only",
          verifier: "read-only",
          adjudicator: "read-only",
          approval: "never",
        },
      },
    });
    expect(consoleError).not.toHaveBeenCalled();
  });

  it("keeps initialization failures separate from empty state and retries them", async () => {
    let capabilityAttempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = requestUrl(input);
      if (url.endsWith("/api/v1/capabilities")) {
        capabilityAttempts += 1;
        if (capabilityAttempts === 1) return Promise.resolve(errorResponse("Capabilities are unavailable."));
        return Promise.resolve(jsonResponse({ default_model: "gpt-5.6-sol", authentication_required: false }));
      }
      if (url.endsWith("/api/v1/runs?limit=100")) {
        return Promise.resolve(jsonResponse({ items: [runRecord], total: 1, offset: 0, limit: 100 }));
      }
      if (url.endsWith(`/api/v1/runs/${runRecord.id}/snapshot`)) {
        return Promise.resolve(jsonResponse(completedSnapshot));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Research console unavailable" })).toBeInTheDocument();
    expect(screen.queryByText("Begin with a mathematical problem")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry loading console" }));
    expect(await screen.findByRole("heading", { name: "Candidate 1" })).toBeInTheDocument();
  });

  it("offers retry when a selected run snapshot fails", async () => {
    let snapshotAttempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = requestUrl(input);
      if (url.endsWith("/api/v1/capabilities")) {
        return Promise.resolve(jsonResponse({ default_model: "gpt-5.6-sol", authentication_required: false }));
      }
      if (url.endsWith("/api/v1/runs?limit=100")) {
        return Promise.resolve(jsonResponse({ items: [runRecord], total: 1, offset: 0, limit: 100 }));
      }
      if (url.endsWith(`/api/v1/runs/${runRecord.id}/snapshot`)) {
        snapshotAttempts += 1;
        if (snapshotAttempts === 1) return Promise.resolve(errorResponse("Snapshot is unavailable."));
        return Promise.resolve(jsonResponse(completedSnapshot));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Run snapshot unavailable" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry loading run" }));
    expect(await screen.findByRole("heading", { name: "Candidate 1" })).toBeInTheDocument();
  });

  it("requires confirmation before cancelling an active run", async () => {
    const runningSnapshot = {
      ...completedSnapshot,
      run: { ...completedSnapshot.run, status: "running" as const, stage: "proving" as const, resumable: false },
    };
    let cancelCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = requestUrl(input);
      if (url.endsWith("/api/v1/capabilities")) {
        return Promise.resolve(jsonResponse({ default_model: "gpt-5.6-sol", authentication_required: false }));
      }
      if (url.endsWith("/api/v1/runs?limit=100")) {
        return Promise.resolve(jsonResponse({ items: [runningSnapshot.run], total: 1, offset: 0, limit: 100 }));
      }
      if (url.endsWith(`/api/v1/runs/${runRecord.id}/snapshot`)) {
        return Promise.resolve(jsonResponse(runningSnapshot));
      }
      if (url.endsWith(`/api/v1/runs/${runRecord.id}/events`)) {
        return Promise.resolve(new Response("", { status: 200, headers: { "Content-Type": "text/event-stream" } }));
      }
      if (url.endsWith(`/api/v1/runs/${runRecord.id}/commands/cancel`) && init?.method === "POST") {
        cancelCalls += 1;
        return Promise.resolve(jsonResponse({ accepted: true, status: "running" }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Candidate 1" });

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(cancelCalls).toBe(0);
    expect(screen.getByText(/stop this attempt/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm cancel" }));
    await waitFor(() => expect(cancelCalls).toBe(1));
  });

  it("supports arrow-key navigation across workspace tabs", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = requestUrl(input);
      if (url.endsWith("/api/v1/capabilities")) {
        return Promise.resolve(jsonResponse({ default_model: "gpt-5.6-sol", authentication_required: false }));
      }
      if (url.endsWith("/api/v1/runs?limit=100")) {
        return Promise.resolve(jsonResponse({ items: [runRecord], total: 1, offset: 0, limit: 100 }));
      }
      return Promise.resolve(jsonResponse(completedSnapshot));
    });

    const user = userEvent.setup();
    render(<App />);
    const proofs = await screen.findByRole("tab", { name: "Proofs" });
    proofs.focus();
    await user.keyboard("{ArrowRight}");

    const evidence = screen.getByRole("tab", { name: "Evidence" });
    expect(evidence).toHaveFocus();
    expect(evidence).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Evidence");
  });
});
