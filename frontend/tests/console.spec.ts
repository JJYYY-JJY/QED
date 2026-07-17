import { expect, test, type Page } from "@playwright/test";

import { completedSnapshot, runRecord } from "../src/test/fixtures";

async function mockCompletedRun(page: Page): Promise<void> {
  await page.route("**/api/v1/capabilities", async (route) => {
    await route.fulfill({
      json: {
        schema_version: 1,
        api_version: "v1",
        default_model: "gpt-5.6-sol",
        commands: ["start", "cancel", "resume"],
        event_transport: "sse",
        authentication_required: false,
      },
    });
  });
  await page.route("**/api/v1/runs?limit=100", async (route) => {
    await route.fulfill({ json: { schema_version: 1, items: [runRecord], total: 1, offset: 0, limit: 100 } });
  });
  await page.route(`**/api/v1/runs/${runRecord.id}/snapshot`, async (route) => {
    await route.fulfill({ json: completedSnapshot });
  });
}

test.beforeEach(async ({ page }) => {
  await mockCompletedRun(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Candidate 1" })).toBeVisible();
});

test("keeps proof, reports, and evidence in one auditable workspace", async ({ page }, testInfo) => {
  await expect(page.getByText("PASS", { exact: true })).toBeVisible();
  await expect(page.getByText("3 independent reports", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /Citation/ }).click();
  await expect(page.getByRole("heading", { name: "Citation report" })).toBeVisible();
  await expect(page.getByText("codex-thread-3", { exact: true })).toBeVisible();
  if (testInfo.project.name.includes("mobile")) {
    await page.getByLabel("Research inspector").getByRole("button", { name: "Close inspector" }).click();
  }

  await page.getByRole("tab", { name: "Evidence" }).click();
  await expect(page.getByRole("heading", { name: "Evidence ledger" })).toBeVisible();
  await expect(page.getByText("Classical group result", { exact: true })).toBeVisible();
});

test("uses navigation and inspector drawers on a narrow viewport", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.includes("mobile"), "Mobile-only structural check");

  await expect(page.getByRole("listitem").filter({ hasText: "Complete" })).toBeInViewport();
  await page.getByRole("button", { name: "Open run navigation" }).click();
  await expect(page.getByLabel("Research runs")).toHaveClass(/is-open/);
  await expect(page.getByRole("button", { name: "Close navigation" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("Research runs")).not.toHaveClass(/is-open/);
  await expect(page.getByRole("button", { name: "Open run navigation" })).toBeFocused();

  await page.getByRole("button", { name: /Detailed/ }).click();
  await expect(page.getByLabel("Research inspector")).toHaveClass(/is-open/);
  await expect(page.getByRole("heading", { name: "Detailed report" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Close inspector" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("Research inspector")).not.toHaveClass(/is-open/);
  await expect(page.getByRole("button", { name: /Detailed/ })).toBeFocused();

  const overflows = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(overflows).toBe(false);
});

test("contains long unbroken proof text on a narrow viewport", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.includes("mobile"), "Mobile-only overflow check");
  const longSnapshot = {
    ...completedSnapshot,
    candidates: completedSnapshot.candidates.map((candidate, index) => index === 0
      ? { ...candidate, candidate: { ...candidate.candidate, proof: "x".repeat(1_000) } }
      : candidate),
  };
  await page.unroute(`**/api/v1/runs/${runRecord.id}/snapshot`);
  await page.route(`**/api/v1/runs/${runRecord.id}/snapshot`, async (route) => {
    await route.fulfill({ json: longSnapshot });
  });
  await page.reload();
  await expect(page.getByRole("heading", { name: "Candidate 1" })).toBeVisible();

  const widths = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(widths.scroll).toBe(widths.client);
});
