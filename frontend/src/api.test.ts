import { afterEach, expect, it, vi } from "vitest";

import { api } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
});

it("keeps browser requests same-origin and never sends authorization credentials", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ default_model: "gpt-5.6-sol", authentication_required: false }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );

  await api.capabilities();

  expect(fetchMock).toHaveBeenCalledOnce();
  const [url, init] = fetchMock.mock.calls[0];
  expect(typeof url === "string" ? url : url instanceof URL ? url.toString() : url.url).toBe("/api/v1/capabilities");
  expect(new Headers(init?.headers).has("Authorization")).toBe(false);
});
