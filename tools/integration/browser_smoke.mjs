#!/usr/bin/env node

/**
 * M0.6 browser smoke and parity evidence collector.
 *
 * This is intentionally a browser-only, temporary-data run.  It reuses the
 * existing PlotPilot Home/Workbench surface, creates one empty fixture novel
 * and one empty chapter, and never calls a generation route or writes a
 * non-empty chapter body.  The script owns the two local development servers
 * so that the exact browser boundary is visible in the resulting evidence.
 */

import { existsSync } from "node:fs";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, normalize, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawn } from "node:child_process";

const ROOT = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const OUTPUT = resolve(
  process.env.PLOTPILOT_BROWSER_SMOKE_OUTPUT ??
    join(ROOT, "docs", "deliveries", "PPA-00", "evidence", "browser-smoke.json"),
);
const SCREENSHOT_DIR = resolve(
  process.env.PLOTPILOT_PARITY_SCREENSHOT_DIR ??
    join(ROOT, "docs", "deliveries", "PPA-00", "parity", "screenshots"),
);
const BASE_URL = (process.env.PLOTPILOT_WEBUI_URL ?? "http://127.0.0.1:3000").replace(/\/$/, "");
const API_URL = (process.env.PLOTPILOT_API_URL ?? "http://127.0.0.1:8005").replace(/\/$/, "");

const PYTHON_CANDIDATES = [
  process.env.PLOTPILOT_PYTHON,
  "C:\\Users\\Administrator\\Desktop\\写作资料汇总\\PlotPilot-update-v4.6.0\\.venv\\Scripts\\python.exe",
  "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
  "python",
].filter(Boolean);
const PLAYWRIGHT_CANDIDATES = [
  process.env.PLOTPILOT_PLAYWRIGHT_MODULE,
  join(ROOT, "node_modules", "playwright", "index.mjs"),
  join(ROOT, "frontend", "node_modules", "playwright", "index.mjs"),
  "C:\\Users\\Administrator\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright\\index.mjs",
].filter(Boolean);
const BROWSER_CANDIDATES = [
  process.env.PLOTPILOT_BROWSER_EXECUTABLE,
  "C:\\Users\\Administrator\\.agent-browser\\browsers\\chrome-152.0.7977.54\\chrome.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].filter(Boolean);

const FLOW_NAMES = [
  "home-empty",
  "home-create-surface",
  "home-advanced-surface",
  "home-populated-list",
  "workbench-shell",
  "workbench-chapter-tree",
  "workbench-writing-support",
  "workbench-reference-panel",
  "workbench-export-menu",
  "return-home",
];

function nowIso() {
  return new Date().toISOString();
}

function commandLabel(command, args) {
  return [command, ...args].join(" ");
}

function pickExisting(candidates, label) {
  const value = candidates.find((candidate) => candidate === "python" || existsSync(candidate));
  if (!value) throw new Error(`${label} not found; set the corresponding PLOTPILOT_* environment variable`);
  return value;
}

async function importPlaywright() {
  const modulePath = pickExisting(PLAYWRIGHT_CANDIDATES, "Playwright module");
  const imported = await import(pathToFileURL(normalize(modulePath)).href);
  let version = "unknown";
  try {
    const packageJson = JSON.parse(
      await readFile(join(dirname(modulePath), "package.json"), "utf8"),
    );
    version = packageJson.version ?? version;
  } catch {
    // The package version is supplemental evidence; browser execution is the gate.
  }
  return { ...imported, modulePath, version };
}

function startProcess(command, args, env, logPath, cwd = ROOT) {
  const logChunks = [];
  const child = spawn(command, args, {
    cwd,
    env,
    windowsHide: true,
    shell: process.platform === "win32" && /\.cmd$/i.test(command),
    stdio: ["ignore", "pipe", "pipe"],
  });
  const append = (chunk) => {
    const text = chunk.toString();
    logChunks.push(text);
    if (logChunks.join("").length > 200_000) logChunks.splice(0, 1);
  };
  child.stdout.on("data", append);
  child.stderr.on("data", append);
  child.logPath = logPath;
  child.commandLine = commandLabel(command, args);
  child.snapshotLog = () => logChunks.join("");
  return child;
}

async function waitWithTimeout(promise, timeoutMs) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((resolvePromise) => {
        timer = setTimeout(() => resolvePromise(undefined), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function stopProcess(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === "win32" && child.pid) {
    // `spawn()` may leave a direct Vite/Python descendant alive when only the
    // parent receives SIGTERM.  Kill this exact service tree, never a name or
    // port match, so the smoke run cannot leak a server into the next run.
    const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore",
    });
    await waitWithTimeout(
      new Promise((resolvePromise) => killer.once("exit", resolvePromise)),
      5_000,
    );
  } else {
    child.kill();
    await waitWithTimeout(
      new Promise((resolvePromise) => child.once("exit", resolvePromise)),
      2_500,
    );
  }
}

async function waitForHttp(url, timeoutMs = 45_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "unknown";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.status >= 200 && response.status < 500) return response;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
  }
  throw new Error(`Timed out waiting for ${url}: ${lastError}`);
}

async function jsonFetch(page, path, init = {}) {
  return page.evaluate(
    async ({ path: requestPath, init: requestInit }) => {
      const response = await fetch(requestPath, {
        ...requestInit,
        headers: { "Content-Type": "application/json", ...(requestInit.headers ?? {}) },
      });
      const text = await response.text();
      let body = null;
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        body = text;
      }
      return {
        status: response.status,
        headers: Object.fromEntries(response.headers.entries()),
        body,
      };
    },
    { path, init: { ...init, body: init.body ?? undefined } },
  );
}

async function assertVisible(page, locator, label) {
  await locator.waitFor({ state: "visible", timeout: 15_000 });
  if (!(await locator.isVisible())) throw new Error(`${label} is not visible`);
}

async function screenshot(page, name) {
  const path = join(SCREENSHOT_DIR, `${String(FLOW_NAMES.indexOf(name) + 1).padStart(2, "0")}-${name}.png`);
  await page.screenshot({ path, fullPage: false });
  return path;
}

function apiEntriesSince(apiTrace, startIndex) {
  return apiTrace.slice(startIndex).map((entry) => ({ ...entry }));
}

async function collectExportObservation(page, novelId) {
  return page.evaluate(async (id) => {
    const response = await fetch(`/api/v1/export/novel/${encodeURIComponent(id)}?format=markdown`);
    const bytes = new Uint8Array(await response.arrayBuffer());
    const contentDisposition = response.headers.get("content-disposition") ?? "";
    const charset = /charset=([^;]+)/i.exec(response.headers.get("content-type") ?? "")?.[1] ?? null;
    const decoded = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    return {
      status: response.status,
      content_type: response.headers.get("content-type"),
      content_disposition: contentDisposition,
      charset,
      byte_length: bytes.byteLength,
      utf8_bom: bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf,
      filename_encoded: contentDisposition.match(/filename=([^;]+)/i)?.[1] ?? null,
      filename_utf8: contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1] ?? null,
      sequence: decoded.split("\n").slice(0, 12),
      chapter_body_saved: false,
    };
  }, novelId);
}

async function main() {
  const startedAt = nowIso();
  await mkdir(dirname(OUTPUT), { recursive: true });
  await mkdir(SCREENSHOT_DIR, { recursive: true });
  const dataRoot = await mkdtemp(join(tmpdir(), "plotpilot-m0-browser-"));
  const serviceLogDir = join(dataRoot, "service-logs");
  await mkdir(serviceLogDir, { recursive: true });

  const python = pickExisting(PYTHON_CANDIDATES, "Python 3.12 runtime");
  const backendArgs = ["-m", "uvicorn", "interfaces.main:app", "--host", "127.0.0.1", "--port", "8005", "--log-level", "warning"];
  const viteCommand = process.execPath;
  const viteArgs = [join(ROOT, "frontend", "node_modules", "vite", "bin", "vite.js"), "--host", "127.0.0.1", "--port", "3000", "--strictPort"];
  const serviceEnv = {
    ...process.env,
    PLOTPILOT_PROD_DATA_DIR: dataRoot,
    DISABLE_AUTO_DAEMON: "1",
    VECTOR_STORE_ENABLED: "false",
    CORS_ORIGINS: BASE_URL,
    LOG_FILE: join(dataRoot, "plotpilot.log"),
    PYTHONIOENCODING: "utf-8",
  };
  const backend = startProcess(python, backendArgs, serviceEnv, join(serviceLogDir, "backend.log"));
  const vite = startProcess(viteCommand, viteArgs, serviceEnv, join(serviceLogDir, "vite.log"), join(ROOT, "frontend"));

  let browser;
  let context;
  let page;
  const apiTrace = [];
  const consoleErrors = [];
  const pageErrors = [];
  const flows = [];
  let fixture;
  let exportObservation;
  let failure;

  try {
    await waitForHttp(`${API_URL}/api/v1/novels/`);
    await waitForHttp(`${BASE_URL}/`);
    const { chromium, version: playwrightVersion, modulePath } = await importPlaywright();
    const browserExecutable = pickExisting(BROWSER_CANDIDATES, "Chromium executable");
    browser = await chromium.launch({
      headless: process.env.PLOTPILOT_HEADLESS === "1",
      executablePath: browserExecutable,
      args: ["--disable-extensions", "--no-first-run", "--disable-default-apps"],
    });
    context = await browser.newContext({
      viewport: { width: 1440, height: 1000 },
      acceptDownloads: false,
    });
    page = await context.newPage();
    page.on("request", (request) => {
      if (!request.url().includes("/api/")) return;
      apiTrace.push({
        kind: "request",
        at: nowIso(),
        method: request.method(),
        url: request.url(),
        resource_type: request.resourceType(),
        has_post_data: Boolean(request.postData()),
      });
    });
    page.on("response", (response) => {
      if (!response.url().includes("/api/")) return;
      apiTrace.push({
        kind: "response",
        at: nowIso(),
        method: response.request().method(),
        url: response.url(),
        status: response.status(),
        content_type: response.headers()["content-type"] ?? null,
      });
    });
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) {
        consoleErrors.push({ type: message.type(), text: message.text(), at: nowIso() });
      }
    });
    page.on("pageerror", (error) => pageErrors.push({ message: error.message, at: nowIso() }));

    async function flow(name, action) {
      const start = apiTrace.length;
      const record = { id: name, started_at: nowIso() };
      await action(record);
      await page.waitForTimeout(250);
      record.finished_at = nowIso();
      record.screenshot = await screenshot(page, name);
      record.api_trace = apiEntriesSince(apiTrace, start);
      flows.push(record);
    }

    await flow("home-empty", async (record) => {
      await page.goto(`${BASE_URL}/`);
      await assertVisible(page, page.getByRole("heading", { name: "墨枢 · 长篇叙事工作台" }), "Home heading");
      await assertVisible(page, page.getByText("还没有书目", { exact: true }), "Home empty state");
      record.url = await page.url();
      record.assertions = ["Home heading is visible", "empty library state is visible"];
    });

    await flow("home-create-surface", async (record) => {
      await assertVisible(page, page.getByPlaceholder(/用一段话写清主线/), "Home premise input");
      await page.getByPlaceholder(/用一段话写清主线/).click();
      record.url = await page.url();
      record.assertions = ["premise input is focusable", "create card remains on Home route"];
    });

    await flow("home-advanced-surface", async (record) => {
      await page.getByRole("button", { name: /高级/ }).click();
      await assertVisible(page, page.getByText("章节数", { exact: true }), "advanced chapter count field");
      await assertVisible(page, page.getByText("每章字数", { exact: true }), "advanced word count field");
      record.url = await page.url();
      record.assertions = ["advanced settings toggle opens", "custom chapter count and word count are visible"];
    });

    const fixtureId = `m0-browser-${Date.now()}`;
    const fixtureTitle = "M0 浏览基线";
    const createResponse = await jsonFetch(page, "/api/v1/novels/", {
      method: "POST",
      body: JSON.stringify({
        novel_id: fixtureId,
        title: fixtureTitle,
        author: "P0",
        target_chapters: 1,
        target_words_per_chapter: 500,
        premise: "M0 临时浏览器基线；不生成正文。",
        genre: "玄幻",
        world_preset: "东方奇幻",
        story_structure: "三幕结构",
        pacing_control: "稳步推进",
        writing_style: "简洁叙事",
        special_requirements: "仅用于 M0 浏览验证",
        length_tier: null,
      }),
    });
    if (createResponse.status !== 201) throw new Error(`fixture novel create failed: ${createResponse.status}`);
    fixture = { id: fixtureId, title: fixtureTitle, create_response: createResponse.status };

    await flow("home-populated-list", async (record) => {
      await page.reload();
      await assertVisible(page, page.getByText(fixtureTitle, { exact: true }), "fixture book card");
      await assertVisible(page, page.getByText("1 本", { exact: true }), "Home book count");
      record.url = await page.url();
      record.assertions = ["created fixture is rendered as a book card", "book count is 1"];
    });

    const chapterResponse = await jsonFetch(page, `/api/v1/novels/${encodeURIComponent(fixtureId)}/chapters/1/ensure`, {
      method: "POST",
      body: JSON.stringify({ title: "M0 空白章" }),
    });
    if (![200, 201].includes(chapterResponse.status)) throw new Error(`empty chapter ensure failed: ${chapterResponse.status}`);
    fixture.chapter_ensure_response = chapterResponse.status;

    await flow("workbench-shell", async (record) => {
      await page.goto(`${BASE_URL}/book/${encodeURIComponent(fixtureId)}/workbench`);
      await assertVisible(page, page.getByText(fixtureTitle, { exact: true }), "Workbench title");
      await assertVisible(page, page.getByText("书目列表", { exact: true }), "Workbench back link");
      await assertVisible(page, page.getByText("作品基础", { exact: true }), "Workbench reference group");
      record.url = await page.url();
      record.assertions = ["Workbench route loads", "existing product title is retained", "reference group is visible"];
    });

    await flow("workbench-chapter-tree", async (record) => {
      const chapterNode = page.locator(".n-tree-node-content").filter({ hasText: "M0 空白章" }).first();
      await assertVisible(page, chapterNode, "empty chapter tree node");
      await chapterNode.click();
      await assertVisible(page, page.getByPlaceholder("章节内容..."), "empty chapter editor");
      record.url = await page.url();
      record.assertions = ["empty chapter is selectable", "chapter editor is visible", "editor remains empty"];
      record.chapter_body_length = await page.getByPlaceholder("章节内容...").inputValue();
    });

    await flow("workbench-writing-support", async (record) => {
      await page.getByRole("button", { name: "写作支撑", exact: true }).click();
      await assertVisible(page, page.getByText("叙事简报", { exact: true }).first(), "writing support tabs");
      await page.getByText("叙事简报", { exact: true }).first().click();
      record.url = await page.url();
      record.assertions = ["writing support group switches without generation", "narrative brief tab is available"];
    });

    await flow("workbench-reference-panel", async (record) => {
      await page.getByRole("button", { name: "作品基础", exact: true }).click();
      await assertVisible(page, page.getByText("世界观", { exact: true }).first(), "reference tabs");
      await page.getByText("世界观", { exact: true }).first().click();
      record.url = await page.url();
      record.assertions = ["reference group switches", "worldbuilding tab is available"];
    });

    await flow("workbench-export-menu", async (record) => {
      await page.locator('[role="button"][aria-label="导出"]').click();
      await assertVisible(page, page.getByText("EPUB (电子书)", { exact: false }).first(), "export menu");
      await assertVisible(page, page.getByText(/Markdown/).first(), "Markdown export option");
      exportObservation = await collectExportObservation(page, fixtureId);
      record.url = await page.url();
      record.assertions = ["export menu exposes EPUB/PDF/DOCX/Markdown", "markdown export metadata is observable", "no download is triggered"];
      record.export_observation = exportObservation;
      await page.keyboard.press("Escape");
    });

    await flow("return-home", async (record) => {
      await page.getByText("书目列表", { exact: true }).click();
      await page.waitForURL(`${BASE_URL}/`, { timeout: 15_000 });
      await assertVisible(page, page.getByRole("heading", { name: "墨枢 · 长篇叙事工作台" }), "Home after return");
      record.url = await page.url();
      record.assertions = ["Workbench back action returns to Home", "Home surface remains available"];
    });

    const forbiddenGenerationCalls = apiTrace.filter((entry) => {
      const method = String(entry.method ?? "").toUpperCase();
      if (!["POST", "PUT", "PATCH"].includes(method)) return false;
      const path = String(entry.url ?? "").toLowerCase();
      return /generate|autopilot\/(?:start|resume|stop)|ai-invocation|chapter-stream|planning/.test(path);
    });
    if (forbiddenGenerationCalls.length) {
      throw new Error(`generation pipeline was invoked: ${JSON.stringify(forbiddenGenerationCalls)}`);
    }

    const dataDb = join(dataRoot, "plotpilot.db");
    let dataEvidence = { database_exists: existsSync(dataDb), non_empty_chapter_count: null, chapter_count: null };
    if (existsSync(dataDb)) {
      const { execFileSync } = await import("node:child_process");
      const query = "SELECT COUNT(*) AS total, SUM(CASE WHEN length(COALESCE(content, '')) > 0 THEN 1 ELSE 0 END) AS non_empty FROM chapters";
      try {
        const result = execFileSync(python, ["-c", `import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); r=c.execute(sys.argv[2]).fetchone(); print(f'{r[0]}|{r[1] or 0}')`, dataDb, query], { encoding: "utf8" }).trim();
        const [total, nonEmpty] = result.split("|").map(Number);
        dataEvidence = { database_exists: true, chapter_count: total, non_empty_chapter_count: nonEmpty };
      } catch (error) {
        dataEvidence.sqlite_query_error = error instanceof Error ? error.message : String(error);
      }
    }
    if (dataEvidence.non_empty_chapter_count !== null && dataEvidence.non_empty_chapter_count !== 0) {
      throw new Error(`temporary smoke data contains non-empty chapter content: ${dataEvidence.non_empty_chapter_count}`);
    }

    const output = {
      schema: "plotpilot-browser-smoke/v1",
      status: "passed",
      started_at: startedAt,
      finished_at: nowIso(),
      root: ROOT,
      urls: { webui: BASE_URL, api: API_URL },
      service_commands: [
        commandLabel(python, backendArgs),
        commandLabel(viteCommand, viteArgs),
      ],
      runtime: {
        node: process.version,
        python_command: python,
        playwright_version: playwrightVersion,
        playwright_module: modulePath,
        chromium_executable: browserExecutable,
        headless: process.env.PLOTPILOT_HEADLESS === "1",
      },
      temporary_data_root: dataRoot,
      fixture,
      constraints: {
        generation_pipeline_invoked: false,
        non_empty_chapter_body_saved: false,
        temporary_data_removed_after_run: true,
      },
      data_evidence: dataEvidence,
      flows,
      api_trace: apiTrace,
      forbidden_generation_calls: forbiddenGenerationCalls,
      console_errors: consoleErrors,
      page_errors: pageErrors,
      export_observation: exportObservation,
    };
    await writeFile(OUTPUT, `${JSON.stringify(output, null, 2)}\n`, "utf8");
    console.log(JSON.stringify(output, null, 2));
  } catch (error) {
    failure = error instanceof Error ? error : new Error(String(error));
    const failedOutput = {
      schema: "plotpilot-browser-smoke/v1",
      status: "failed",
      started_at: startedAt,
      finished_at: nowIso(),
      root: ROOT,
      urls: { webui: BASE_URL, api: API_URL },
      service_commands: [commandLabel(python, backendArgs), commandLabel(viteCommand, viteArgs)],
      fixture,
      flows,
      api_trace: apiTrace,
      console_errors: consoleErrors,
      page_errors: pageErrors,
      error: failure.message,
      service_logs: { backend: backend.snapshotLog(), vite: vite.snapshotLog() },
    };
    await writeFile(OUTPUT, `${JSON.stringify(failedOutput, null, 2)}\n`, "utf8");
    console.error(JSON.stringify(failedOutput, null, 2));
  } finally {
    if (page) await waitWithTimeout(page.close().catch(() => {}), 5_000);
    if (context) await waitWithTimeout(context.close().catch(() => {}), 5_000);
    if (browser) await waitWithTimeout(browser.close().catch(() => {}), 5_000);
    await stopProcess(vite);
    await stopProcess(backend);
    await waitWithTimeout(rm(dataRoot, { recursive: true, force: true }), 5_000);
  }

  if (failure) throw failure;
}

main().catch((error) => {
  console.error(`[browser-smoke] ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
  process.exitCode = 1;
});
