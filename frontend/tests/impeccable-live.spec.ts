import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import { createServer, type RequestListener, type Server } from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

type LiveServerInfo = {
  port: number;
  token: string;
};

type RunningLiveServer = {
  child: ChildProcess;
  info: LiveServerInfo;
};

const testsDir = path.dirname(fileURLToPath(import.meta.url));
const liveServerPath = path.resolve(
  testsDir,
  "..",
  "..",
  ".agents",
  "skills",
  "impeccable",
  "scripts",
  "live-server.mjs",
);

function isLiveServerInfo(value: unknown): value is LiveServerInfo {
  return typeof value === "object"
    && value !== null
    && "port" in value
    && Number.isInteger(value.port)
    && "token" in value
    && typeof value.token === "string";
}

async function reservePort(): Promise<number> {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (address === null || typeof address === "string") {
        server.close();
        reject(new Error("Failed to reserve a loopback port"));
        return;
      }
      server.close((error) => {
        if (error) reject(error);
        else resolve(address.port);
      });
    });
  });
}

async function startHttpServer(handler: RequestListener): Promise<Server> {
  const server = createServer(handler);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return server;
}

function serverPort(server: Server): number {
  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("HTTP server has no loopback port");
  }
  return address.port;
}

async function closeHttpServer(server: Server): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    server.close((error) => {
      if (error) reject(error);
      else resolve();
    });
  });
}

async function startLiveServer(root: string): Promise<RunningLiveServer> {
  const port = await reservePort();
  let stdout = "";
  let stderr = "";
  const child = spawn(process.execPath, [liveServerPath, `--port=${port}`], {
    cwd: root,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout?.setEncoding("utf-8");
  child.stderr?.setEncoding("utf-8");
  child.stdout?.on("data", (chunk: string) => {
    stdout += chunk;
  });
  child.stderr?.on("data", (chunk: string) => {
    stderr += chunk;
  });

  const infoPath = path.join(root, ".impeccable", "live", "server.json");
  const deadline = Date.now() + 8_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Live server exited early (${child.exitCode}): ${stderr || stdout}`);
    }
    try {
      const parsed: unknown = JSON.parse(fs.readFileSync(infoPath, "utf-8"));
      if (isLiveServerInfo(parsed)) return { child, info: parsed };
    } catch {
      // The server info file is written after the listener is ready.
    }
    await new Promise((resolve) => {
      setTimeout(resolve, 25);
    });
  }
  child.kill("SIGKILL");
  throw new Error(`Timed out waiting for live server: ${stderr || stdout}`);
}

async function stopLiveServer(server: RunningLiveServer): Promise<void> {
  try {
    await fetch(
      `http://127.0.0.1:${server.info.port}/stop?token=${encodeURIComponent(server.info.token)}`,
    );
  } catch {
    server.child.kill("SIGTERM");
  }
  if (server.child.exitCode === null) {
    await Promise.race([
      new Promise<void>((resolve) => {
        server.child.once("exit", () => {
          resolve();
        });
      }),
      new Promise<void>((resolve) => {
        setTimeout(resolve, 1_000);
      }),
    ]);
  }
  if (server.child.exitCode === null) server.child.kill("SIGKILL");
}

test("design sidecar previews cannot activate loopback links", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name.includes("mobile"), "Desktop interaction security check");

  const root = fs.mkdtempSync(path.join(os.tmpdir(), "impeccable-live-e2e-"));
  const navigationHits: string[] = [];
  let sink: Server | null = null;
  let preview: Server | null = null;
  let live: RunningLiveServer | null = null;

  try {
    sink = await startHttpServer((request, response) => {
      navigationHits.push(request.url ?? "");
      response.writeHead(200, { "Content-Type": "text/html" });
      response.end("<p>unexpected navigation</p>");
    });
    const sinkUrl = `http://127.0.0.1:${serverPort(sink)}/hit`;
    fs.mkdirSync(path.join(root, ".impeccable"), { recursive: true });
    fs.writeFileSync(
      path.join(root, ".impeccable", "design.json"),
      JSON.stringify({
        schemaVersion: 2,
        components: [
          {
            kind: "button",
            name: "Hostile keyboard preview",
            html: `<a href="${sinkUrl}" style="display:block;width:100vw;height:100vh">Open</a>`,
          },
          {
            kind: "button",
            name: "Hostile pointer preview",
            html: `<a href="${sinkUrl}" style="display:block;width:100vw;height:100vh">Open</a>`,
          },
        ],
      }),
    );

    live = await startLiveServer(root);
    const livePort = live.info.port;
    preview = await startHttpServer((_request, response) => {
      response.writeHead(200, { "Content-Type": "text/html" });
      response.end(
        `<!doctype html><html><body><main>Preview</main><script crossorigin="anonymous" src="http://localhost:${livePort}/live.js"></script></body></html>`,
      );
    });
    const previewOrigin = `http://127.0.0.1:${serverPort(preview)}`;
    const authorization = await fetch(
      `http://127.0.0.1:${live.info.port}/authorize-browser`,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${live.info.token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({ origin: previewOrigin }),
      },
    );
    expect(authorization.status).toBe(200);

    await page.goto(previewOrigin);
    await page.locator("#impeccable-live-design-toggle").click();
    const frames = page.locator("#impeccable-live-design-host iframe");
    await expect(frames).toHaveCount(2);
    await expect(frames.first()).toBeVisible();

    const close = page.locator("#impeccable-live-design-host .panel-close");
    await close.focus();
    for (let index = 0; index < 3 && navigationHits.length === 0; index += 1) {
      await page.keyboard.press("Tab");
      await page.keyboard.press("Enter");
      await page.waitForTimeout(50);
    }
    const keyboardNavigationHits = navigationHits.splice(0);

    const pointerFrame = frames.nth(1);
    const box = await pointerFrame.boundingBox();
    if (box === null) throw new Error("Pointer preview has no bounding box");
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await page.waitForTimeout(250);
    const pointerNavigationHits = navigationHits.splice(0);

    const attributes = await frames.evaluateAll((elements) => elements.map((element) => {
      const frame = element as HTMLIFrameElement;
      return {
        inert: frame.inert,
        tabIndex: frame.tabIndex,
        pointerEvents: frame.style.pointerEvents,
      };
    }));
    expect(keyboardNavigationHits).toEqual([]);
    expect(pointerNavigationHits).toEqual([]);
    expect(attributes).toEqual([
      { inert: true, tabIndex: -1, pointerEvents: "none" },
      { inert: true, tabIndex: -1, pointerEvents: "none" },
    ]);
  } finally {
    if (preview) await closeHttpServer(preview);
    if (live) await stopLiveServer(live);
    if (sink) await closeHttpServer(sink);
    fs.rmSync(root, { force: true, recursive: true });
  }
});
