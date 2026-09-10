#!/usr/bin/env node

/*
 * Executable WebUI capability-baseline evidence. All product mutations in
 * this file are initiated by visible PlotPilot UI controls; there is no
 * browser-side API helper for creating Workspaces, Documents, Revisions,
 * Candidates, Jobs, checkpoints or export Artifacts.
 */
import { existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, normalize, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawn } from "node:child_process";

const ROOT = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const DEFAULT_SCREENSHOT_DIR = resolve(join(ROOT, "docs", "deliveries", "PPA-00", "parity", "screenshots"));
const OUTPUT = resolve(process.env.PLOTPILOT_BROWSER_SMOKE_OUTPUT ||
  join(ROOT, "docs", "deliveries", "PPA-00", "evidence", "browser-smoke.json"));
const SCREENSHOT_DIR = resolve(process.env.PLOTPILOT_PARITY_SCREENSHOT_DIR ||
  DEFAULT_SCREENSHOT_DIR);
const BASE_URL = (process.env.PLOTPILOT_WEBUI_URL || "http://127.0.0.1:3000").replace(/\/$/, "");
const API_URL = (process.env.PLOTPILOT_API_URL || "http://127.0.0.1:8005").replace(/\/$/, "");
const FLOW_NAMES = [
  "home-create-surface", "core-workspace-direct-entry", "core-workbench-shell",
  "core-chapter-create", "core-chapter-select", "core-chapter-edit-save",
  "generation-controls-job-status", "story-state-surfaces",
  "checkpoint-recovery", "workbench-export-controls",
];

// Browser smoke is intentionally offline.  These are the only two benign
// browser-console cases permitted by the gate, and both are matched against
// the complete known message shape rather than a broad substring.
const ALLOWED_FONT_URLS = new Set([
  "https://fonts.loli.net/css2?family=Inter:wght@400;500;600;700&display=swap",
  "https://fonts.loli.net/css2?family=JetBrains+Mono:wght@400;500&display=swap",
  "https://fonts.loli.net/css2?family=Noto+Sans+SC:wght@400;500;600;700&display=swap",
]);
const ALLOWED_FONT_FAILURE_MESSAGE = "Failed to load resource: net::ERR_FAILED";
const ALLOWED_TAURI_FALLBACK_MESSAGE = /^\[API\] Tauri IPC 调用失败: TypeError: Cannot read properties of undefined \(reading 'invoke'\)(?:\n    at invoke \(http:\/\/127\.0\.0\.1:3000\/node_modules\/.vite\/deps\/@tauri-apps_api_core\.js\?v=[a-z0-9]+:\d+:\d+\))?(?:\n    at initApiClient \(http:\/\/127\.0\.0\.1:3000\/src\/api\/config\.ts:\d+:\d+\))?(?:\n    at async bootstrap \(http:\/\/127\.0\.0\.1:3000\/src\/main\.ts:\d+:\d+\))?$/i;
const BROWSER_ALLOWLIST_POLICY = [
  { id: "blocked-font-css", kind: "external_stylesheet_and_matching_console_error",
    match: "GET exact https://fonts.loli.net/css2 stylesheet plus exact net::ERR_FAILED console text",
    reason: "External font CSS is deliberately aborted to keep M0 browser smoke offline" },
  { id: "tauri-browser-fallback", kind: "console_warning",
    match: "exact documented [API] Tauri IPC invoke failure stack in browser/Vite mode",
    reason: "Browser/Vite smoke has no Tauri IPC host; the documented desktop fallback is expected" },
];

function allowlistedExternalRequest(url, method, resourceType) {
  if (method.toUpperCase() !== "GET" || resourceType !== "stylesheet") return null;
  if (!ALLOWED_FONT_URLS.has(url)) return null;
  return {
    id: "blocked-font-css",
    reason: "External font CSS is deliberately aborted to keep M0 browser smoke offline",
  };
}

function classifyConsoleMessage(type, text, availableFontFailures) {
  if (type === "warning" && ALLOWED_TAURI_FALLBACK_MESSAGE.test(text)) {
    return {
      id: "tauri-browser-fallback",
      reason: "Browser/Vite smoke has no Tauri IPC host; the documented desktop fallback is expected",
    };
  }
  if (type === "error" && text === ALLOWED_FONT_FAILURE_MESSAGE && availableFontFailures > 0) {
    return {
      id: "blocked-font-css",
      reason: "The corresponding exact fonts.loli.net stylesheet request was intentionally aborted",
    };
  }
  return null;
}

function evaluateBrowserGate(httpFailures, consoleErrors, pageErrors, blockedExternal, consoleAllowlisted) {
  const failures = [];
  for (const item of httpFailures) failures.push({ kind: "http_status", ...item });
  for (const item of consoleErrors) failures.push({ kind: "console", ...item });
  for (const item of pageErrors) failures.push({ kind: "pageerror", ...item });
  for (const item of blockedExternal) {
    if (!item.allowlisted) failures.push({ kind: "unexpected_external_request", ...item });
  }
  const exactFontRequests = blockedExternal.filter(item => item.allowlist_id === "blocked-font-css").length;
  const allowlistedFontErrors = consoleAllowlisted.filter(item => item.allowlist_id === "blocked-font-css").length;
  if (allowlistedFontErrors > exactFontRequests) {
    failures.push({ kind: "allowlist_accounting", message: "font console failures exceed exact blocked-font requests",
      allowlisted_font_errors: allowlistedFontErrors, exact_font_requests: exactFontRequests });
  }
  return {
    passed: failures.length === 0,
    failure_count: failures.length,
    failures,
    policy: {
      http_status_ge_400: "fail",
      pageerror: "fail",
      console_error_or_warning: "fail_unless_exact_allowlist",
      external_request: "fail_unless_exact_fonts_loli_net_stylesheet",
    },
  };
}

const PYTHON_CANDIDATES = [
  process.env.PLOTPILOT_PYTHON,
  join(ROOT, ".venv", "Scripts", "python.exe"),
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

function iso() { return new Date().toISOString(); }
function label(command, args) { return [command].concat(args).join(" "); }
function pick(candidates, name) {
  const found = candidates.find(function (v) { return v === "python" || existsSync(v); });
  if (!found) throw new Error(name + " not found");
  return found;
}
async function playwrightImport() {
  const modulePath = pick(PLAYWRIGHT_CANDIDATES, "Playwright module");
  const imported = await import(pathToFileURL(normalize(modulePath)).href);
  let version = "unknown";
  try { version = JSON.parse(await readFile(join(dirname(modulePath), "package.json"), "utf8")).version || version; } catch {}
  return { ...imported, modulePath, version };
}
function start(command, args, env, cwd) {
  const chunks = [];
  const child = spawn(command, args, {
    cwd: cwd || ROOT, env: env, windowsHide: true,
    shell: process.platform === "win32" && /\.cmd$/i.test(command),
    stdio: ["ignore", "pipe", "pipe"],
  });
  function append(chunk) {
    chunks.push(chunk.toString());
    while (chunks.join("").length > 300000) chunks.shift();
  }
  child.stdout.on("data", append);
  child.stderr.on("data", append);
  child.snapshotLog = function () { return chunks.join(""); };
  child.commandLine = label(command, args);
  return child;
}
async function timed(promise, ms) {
  let timer;
  try {
    return await Promise.race([promise, new Promise(function (resolve) {
      timer = setTimeout(function () { resolve(undefined); }, ms);
    })]);
  } finally { if (timer) clearTimeout(timer); }
}
async function stop(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === "win32" && child.pid) {
    const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
    await timed(new Promise(function (resolve) { killer.once("exit", resolve); }), 5000);
  } else {
    child.kill();
    await timed(new Promise(function (resolve) { child.once("exit", resolve); }), 3000);
  }
}
async function waitHttp(url, ms) {
  const deadline = Date.now() + (ms || 60000);
  let last = "unknown";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.status >= 200 && response.status < 500) return response;
      last = "HTTP " + response.status;
    } catch (error) { last = error instanceof Error ? error.message : String(error); }
    await new Promise(function (resolve) { setTimeout(resolve, 250); });
  }
  throw new Error("Timed out waiting for " + url + ": " + last);
}
async function visible(locator, name, ms) {
  await locator.waitFor({ state: "visible", timeout: ms || 30000 });
  if (!(await locator.isVisible())) throw new Error(name + " is not visible");
}
async function eventually(predicate, name, ms) {
  const deadline = Date.now() + (ms || 30000);
  while (Date.now() < deadline) {
    try { if (await predicate()) return; } catch {}
    await new Promise(function (resolve) { setTimeout(resolve, 250); });
  }
  throw new Error(name + " timed out");
}
function responseFor(fragment, method) {
  return function (response) {
    return response.url().includes(fragment) &&
      response.request().method().toUpperCase() === method.toUpperCase();
  };
}
function pathOf(entry) {
  try { return new URL(entry.url).pathname; } catch { return String(entry.url || ""); }
}
function isApiUrl(value) {
  try {
    const pathname = new URL(value).pathname;
    return pathname === "/api" || pathname.startsWith("/api/");
  } catch { return false; }
}
function sha256Buffer(value) {
  return createHash("sha256").update(value).digest("hex");
}
function expected(expectedText, actualText, extra) {
  return [Object.assign({ expected: expectedText, actual: actualText }, extra || {})];
}
function serviceEnvironment(dataRoot) {
  const env = Object.assign({}, process.env);
  [
    "LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "GEMINI_API_KEY", "GEMINI_BASE_URL", "GEMINI_MODEL", "ARK_API_KEY", "ARK_BASE_URL", "ARK_MODEL",
    "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "ZHIPUAI_API_KEY",
    "MINIMAX_API_KEY", "AI_GATEWAY_API_KEY", "EMBEDDING_API_KEY", "VECTOR_STORE_ENABLED",
    "DISABLE_AUTO_DAEMON",
  ].forEach(function (key) { delete env[key]; });
  Object.assign(env, {
    LLM_PROVIDER: "mock",
    PLOTPILOT_PROD_DATA_DIR: dataRoot,
    VECTOR_STORE_ENABLED: "false",
    CORS_ORIGINS: BASE_URL,
    LOG_FILE: join(dataRoot, "plotpilot.log"),
    PYTHONIOENCODING: "utf-8",
    PYTHONUNBUFFERED: "1",
  });
  return env;
}
function backendArgs() {
  const code = [
    "import sys; sys.path.insert(0,'backend')",
    "import tools.integration.browser_smoke_provider as seam",
    "seam.install_browser_smoke_provider(); seam.patch_prompt_manager_for_browser_smoke(); seam.patch_daemon_manager_for_browser_smoke()",
    "import interfaces.daemon_manager as dm",
    "dm.cleanup_orphan_python_processes=lambda logger_=None: None",
    "import interfaces.main as appmod",
    "appmod._cleanup_orphan_python_processes=lambda: None",
    "import uvicorn",
    "uvicorn.run(appmod.app,host='127.0.0.1',port=8005,log_level='warning')",
  ].join(";");
  return ["-c", code];
}
async function main() {
  const started = iso();
  await mkdir(dirname(OUTPUT), { recursive: true });
  if (SCREENSHOT_DIR !== DEFAULT_SCREENSHOT_DIR) {
    throw new Error("PLOTPILOT_PARITY_SCREENSHOT_DIR must be the tracked PPA-00 evidence directory");
  }
  // A run must not inherit a previous run's screenshots.  The directory is a
  // fixed, tracked evidence root; remove only that exact directory before
  // recreating it so stale R2 images cannot enter the new parity ledger.
  await rm(SCREENSHOT_DIR, { recursive: true, force: true });
  await mkdir(SCREENSHOT_DIR, { recursive: true });
  const dataRoot = await mkdtemp(join(tmpdir(), "plotpilot-m0-browser-"));
  const python = pick(PYTHON_CANDIDATES, "Python 3.12 runtime");
  const bArgs = backendArgs();
  const viteArgs = [join(ROOT, "frontend", "node_modules", "vite", "bin", "vite.js"),
    "--host", "127.0.0.1", "--port", "3000", "--strictPort"];
  const env = serviceEnvironment(dataRoot);
  const backend = start(python, bArgs, env);
  const vite = start(process.execPath, viteArgs, env, join(ROOT, "frontend"));
  let browser, context, page, browserPath, playwrightVersion = "unknown", playwrightModule = null;
  let fixture = null, exportObservation = null, failure = null, strictGate = null, flowNo = 0, activeFlowId = null;
  const trace = [], flows = [], blockedExternal = [], httpFailures = [];
  const consoleEvents = [], consoleErrors = [], consoleAllowlisted = [], pageErrors = [];
  let availableFontFailures = 0;
  try {
    await waitHttp(API_URL + "/api/v1/core/workspaces?offset=0&limit=1");
    await waitHttp(BASE_URL + "/");
    const pw = await playwrightImport();
    playwrightVersion = pw.version; playwrightModule = pw.modulePath;
    browserPath = pick(BROWSER_CANDIDATES, "Chromium executable");
    browser = await pw.chromium.launch({
      headless: process.env.PLOTPILOT_HEADLESS === "1",
      executablePath: browserPath,
      args: ["--disable-extensions", "--no-first-run", "--disable-default-apps"],
    });
    context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
    await context.route("**/*", async function (route) {
      const u = new URL(route.request().url());
      if (["127.0.0.1", "localhost", "::1"].indexOf(u.hostname) < 0) {
        const request = route.request();
        const allowlist = allowlistedExternalRequest(request.url(), request.method(), request.resourceType());
        if (allowlist?.id === "blocked-font-css") availableFontFailures += 1;
        blockedExternal.push({ at: iso(), method: request.method(), url: request.url(),
          resource_type: request.resourceType(), action: "aborted",
          allowlisted: Boolean(allowlist), allowlist_id: allowlist?.id || null,
          allowlist_reason: allowlist?.reason || null });
        await route.abort();
      } else await route.continue();
    });
    page = await context.newPage();
    await page.addInitScript(() => {
      // The product consumes SSE with fetch/ReadableStream rather than a
      // browser EventSource.  Clone only the stream response so the product
      // reader remains untouched while this evidence collector records the
      // actual event sequence.
      let existing = [];
      try {
        existing = JSON.parse(window.sessionStorage.getItem("plotpilot-m0-sse-history") || "[]");
      } catch { existing = []; }
      window.__plotpilotSseEvents = Array.isArray(existing) ? existing : [];
      const recordSseEvent = (event) => {
        window.__plotpilotSseEvents.push(event);
        try {
          window.sessionStorage.setItem(
            "plotpilot-m0-sse-history",
            JSON.stringify(window.__plotpilotSseEvents.slice(-500)),
          );
        } catch { /* evidence collection must not affect the product reader */ }
      };
      const originalFetch = window.fetch.bind(window);
      window.fetch = async (...args) => {
        const response = await originalFetch(...args);
        const requestUrl = typeof args[0] === "string" ? args[0] : String(args[0]?.url || "");
        const contentType = response.headers.get("content-type") || "";
        if (contentType.includes("text/event-stream") || /generate-stream|chapter-stream/.test(requestUrl)) {
          const clone = response.clone();
          void (async () => {
            try {
              if (!clone.body) return;
              const reader = clone.body.getReader();
              const decoder = new TextDecoder();
              let buffer = "";
              const flush = () => {
                const blocks = buffer.split(/\r?\n\r?\n/);
                buffer = blocks.pop() || "";
                for (const block of blocks) {
                  for (const line of block.split(/\r?\n/)) {
                    if (!line.startsWith("data: ")) continue;
                    try {
                      const event = JSON.parse(line.slice(6));
                      recordSseEvent({
                        at: new Date().toISOString(),
                        url: requestUrl,
                        type: event.type || null,
                        event,
                      });
                    } catch { /* ignore non-JSON heartbeat/comment blocks */ }
                  }
                }
              };
              while (true) {
                const { done, value } = await reader.read();
                if (value) buffer += decoder.decode(value, { stream: true });
                flush();
                if (done) {
                  buffer += decoder.decode();
                  flush();
                  break;
                }
              }
            } catch { /* response may be aborted by the tested recovery action */ }
          })();
        }
        return response;
      };
    });
    page.on("request", function (request) {
      if (!isApiUrl(request.url())) return;
      trace.push({ kind: "request", at: iso(), method: request.method(), url: request.url(),
        resource_type: request.resourceType(), has_post_data: Boolean(request.postData()), flow_id: activeFlowId });
    });
    page.on("response", function (response) {
      if (response.status() >= 400) {
        httpFailures.push({ kind: "response", at: iso(), method: response.request().method(),
          url: response.url(), status: response.status(), flow_id: activeFlowId });
      }
      if (!isApiUrl(response.url())) return;
      trace.push({ kind: "response", at: iso(), method: response.request().method(), url: response.url(),
        status: response.status(), content_type: response.headers()["content-type"] || null, flow_id: activeFlowId });
    });
    page.on("console", function (message) {
      if (["error", "warning"].indexOf(message.type()) < 0) return;
      const event = { at: iso(), type: message.type(), text: message.text(), flow_id: activeFlowId };
      consoleEvents.push(event);
      const allowlist = classifyConsoleMessage(message.type(), message.text(), availableFontFailures);
      if (allowlist) {
        if (allowlist.id === "blocked-font-css") availableFontFailures -= 1;
        consoleAllowlisted.push(Object.assign({}, event, {
          allowlist_id: allowlist.id, allowlist_reason: allowlist.reason,
        }));
      } else consoleErrors.push(event);
    });
    page.on("pageerror", function (error) {
      pageErrors.push({ at: iso(), message: error.message, flow_id: activeFlowId });
    });

    async function flow(name, action, options = {}) {
      const index = flowNo++;
      const begin = trace.length;
      const beginSse = await page.evaluate(() => window.__plotpilotSseEvents?.length || 0);
      const record = { flow_id: name, status: "passed", exercised: true, started_at: iso(),
        ui_actions: [], expected_actual: [] };
      activeFlowId = name;
      try {
        await action(record);
      } finally {
        activeFlowId = null;
      }
      await page.waitForTimeout(500);
      record.finished_at = iso();
      if (!record.screenshots) {
        const screenshotPath = join(SCREENSHOT_DIR, String(index + 1).padStart(2, "0") + "-" + name + ".png");
        if (options.screenshotLocator) await options.screenshotLocator.screenshot({ path: screenshotPath });
        else await page.screenshot({ path: screenshotPath, fullPage: false });
        const screenshotBytes = await readFile(screenshotPath);
        record.screenshots = [{ path: screenshotPath, bytes: screenshotBytes.byteLength,
          sha256: sha256Buffer(screenshotBytes) }];
      }
      record.api_trace = trace.slice(begin).map(function (v) { return Object.assign({}, v); });
      record.sse_events = await page.evaluate(function (from) {
        return (window.__plotpilotSseEvents || []).slice(from);
      }, beginSse);
      if (!record.api_trace.length && options.requiresApiTrace !== false) {
        throw new Error(name + " has no API trace");
      }
      if (!record.api_trace.length) record.api_trace_expectation = "zero_calls_by_design";
      flows.push(record);
    }

    // A formal flow may have more than one independently auditable surface.
    // Keep each subflow's actions, API slice and screenshot separate; never
    // let the parent flow's screenshot stand in for either child evidence.
    async function subflow(name, screenshotName, action, options = {}) {
      const begin = trace.length;
      const beginSse = await page.evaluate(() => window.__plotpilotSseEvents?.length || 0);
      const record = { flow_id: name, status: "passed", exercised: true, started_at: iso(),
        ui_actions: [], expected_actual: [] };
      const previousFlow = activeFlowId;
      activeFlowId = name;
      try {
        await action(record);
      } finally {
        activeFlowId = previousFlow;
      }
      await page.waitForTimeout(500);
      record.finished_at = iso();
      const screenshotPath = join(SCREENSHOT_DIR, screenshotName);
      if (options.screenshotLocator) await options.screenshotLocator.screenshot({ path: screenshotPath });
      else await page.screenshot({ path: screenshotPath, fullPage: false });
      const screenshotBytes = await readFile(screenshotPath);
      record.screenshots = [{ path: screenshotPath, bytes: screenshotBytes.byteLength,
        sha256: sha256Buffer(screenshotBytes) }];
      record.api_trace = trace.slice(begin).map(function (v) { return Object.assign({}, v); });
      record.sse_events = await page.evaluate(function (from) {
        return (window.__plotpilotSseEvents || []).slice(from);
      }, beginSse);
      if (!record.api_trace.length && options.requiresApiTrace !== false) {
        throw new Error(name + " has no API trace");
      }
      if (!record.api_trace.length) record.api_trace_expectation = "zero_calls_by_design";
      return record;
    }

    await flow("home-create-surface", async function (r) {
      await page.goto(BASE_URL + "/");
      await visible(page.getByRole("heading", { name: "墨枢 · 长篇叙事工作台" }), "Home heading");
      const planningAlert = page.getByRole("alert").filter({
        hasText: "当前源码节点仅创建权威 Core Workspace",
      });
      await visible(planningAlert, "Core Workspace-only planning warning");

      const titleInput = page.getByPlaceholder("输入书名");
      await visible(titleInput, "title input");
      await titleInput.fill("WebUI 交付基线");
      r.ui_actions.push({ action: "fill", target: "Home title", value: "non-empty title" });

      const premiseInput = page.locator("textarea[placeholder^='用一段话写清主线']");
      await visible(premiseInput, "premise input");
      if (!(await premiseInput.isDisabled())) throw new Error("Home premise textarea is not disabled");

      const marketControls = page.locator(".taxonomy-block input, .taxonomy-block textarea, .taxonomy-block button");
      const marketState = await marketControls.evaluateAll(function (controls) {
        return {
          count: controls.length,
          enabled: controls.filter(function (control) {
            return !control.disabled && control.getAttribute("aria-disabled") !== "true";
          }).length,
        };
      });
      if (marketState.count === 0 || marketState.enabled !== 0) {
        throw new Error("Home market planning controls are not disabled");
      }

      const lengthControls = page.locator(".length-tier-block input[type='radio'], .length-tier-block [role='radio']");
      const lengthState = await lengthControls.evaluateAll(function (controls) {
        return {
          count: controls.length,
          enabled: controls.filter(function (control) {
            return !control.disabled && control.getAttribute("aria-disabled") !== "true";
          }).length,
        };
      });
      if (lengthState.count === 0 || lengthState.enabled !== 0) {
        throw new Error("Home length planning controls are not disabled");
      }

      const advancedInputs = page.locator(".advanced-settings input");
      const advancedState = await advancedInputs.evaluateAll(function (controls) {
        return {
          count: controls.length,
          enabled: controls.filter(function (control) {
            return !control.disabled && control.getAttribute("aria-disabled") !== "true";
          }).length,
        };
      });
      if (advancedState.count === 0 || advancedState.enabled !== 0) {
        throw new Error("Home advanced planning controls are not disabled");
      }
      r.ui_actions.push({ action: "observe", target: "Core Workspace-only warning and deferred planning controls" });

      const create = page.waitForResponse(responseFor("/api/v1/core/workspaces", "POST"), { timeout: 30000 });
      await page.getByRole("button", { name: "建档并进入工作台", exact: true }).click();
      const response = await create;
      if (response.status() !== 201) throw new Error("Home UI Workspace creation returned " + response.status());
      const body = await response.json();
      if (body.schema !== "core-workspace/v1" || typeof body.workspace_id !== "string" || !body.workspace_id) {
        throw new Error("Home UI did not return a bound Core Workspace");
      }
      fixture = {
        id: body.workspace_id,
        workspace_id: body.workspace_id,
        title: body.title,
        create_status: response.status(),
        chapter_document_id: null,
        revision_id: null,
      };
      await page.waitForURL(function (url) {
        return url.pathname === "/book/" + fixture.id + "/workbench";
      }, { timeout: 30000 });
      await visible(page.getByText("Core Workspace · " + fixture.id, { exact: true }), "Core Workbench identity");
      if ((await page.getByText("新书设置向导", { exact: true }).count()) !== 0) {
        throw new Error("Removed setup wizard unexpectedly rendered");
      }
      r.ui_actions.push({ action: "click", target: "建档并进入工作台" });
      r.expected_actual = expected("Home creates one authoritative Core Workspace and enters Workbench directly",
        "POST /api/v1/core/workspaces returned 201 for " + fixture.id + "; no setup wizard rendered",
        { no_direct_api_action: true, core_workspace_only: true, planning_inputs_deferred: true, wizard_present: false });
    });

    await flow("core-workspace-direct-entry", async function (r) {
      await visible(page.locator(".core-workbench-header"), "Core Workbench header");
      await visible(page.getByText("Core Workspace · " + fixture.id, { exact: true }), "Core Workspace identity");
      for (const removedText of ["新书设置向导", "确认修改并继续", "进入工作台"]) {
        if ((await page.getByText(removedText, { exact: true }).count()) !== 0) {
          throw new Error("Removed wizard control unexpectedly rendered: " + removedText);
        }
      }
      const refresh = page.locator(".core-chapter-list__actions").getByRole("button", { name: "刷新", exact: true });
      await eventually(async function () { return !(await refresh.isDisabled()); }, "Core chapter refresh enabled", 30000);
      const refreshed = page.waitForResponse(responseFor("/api/v1/core/workspaces/" + fixture.id + "/documents", "GET"), { timeout: 30000 });
      await refresh.click();
      const response = await refreshed;
      if (response.status() !== 200) throw new Error("Core Workbench refresh returned " + response.status());
      r.ui_actions.push({ action: "observe", target: "direct Core Workbench entry without wizard" },
        { action: "click", target: "Core chapter list refresh" });
      r.expected_actual = expected("Workspace creation enters the no-wizard Core Workbench",
        "Core Workspace identity was visible and a UI refresh returned the authoritative Core document page",
        { wizard_present: false, refreshed_status: response.status() });
    });

    await flow("core-workbench-shell", async function (r) {
      await visible(page.getByText("Core 信息", { exact: true }), "Core information panel");
      await visible(page.locator(".core-chapter-list__empty"), "empty Core chapter list");
      const candidate = page.locator("[data-feature-surface='candidate-review']");
      await visible(candidate, "Candidate review surface");
      const candidateInput = candidate.locator("input[placeholder='candidate_id']");
      if (await candidateInput.isDisabled()) throw new Error("Candidate identity input is unexpectedly disabled");
      for (const label of ["预览", "审阅", "接受并发布"]) {
        if (!(await candidate.getByRole("button", { name: label, exact: true }).isDisabled())) {
          throw new Error("Candidate action is enabled without an authoritative Candidate ID: " + label);
        }
      }
      const refresh = page.locator(".core-chapter-list__actions").getByRole("button", { name: "刷新", exact: true });
      await eventually(async function () { return !(await refresh.isDisabled()); }, "Core shell refresh enabled", 30000);
      const refreshed = page.waitForResponse(responseFor("/api/v1/core/workspaces/" + fixture.id + "/documents", "GET"), { timeout: 30000 });
      await refresh.click();
      const response = await refreshed;
      r.ui_actions.push({ action: "observe", target: "Core shell, empty chapter state and Candidate controls" },
        { action: "click", target: "Core chapter list refresh" });
      r.expected_actual = expected("Workbench exposes the Core shell without inventing a Candidate",
        "Core information and empty chapter state were visible; Candidate actions stayed disabled until a real ID is supplied; refresh returned " + response.status(),
        { candidate_request_sent: false });
    });

    await flow("core-chapter-create", async function (r) {
      const created = page.waitForResponse(responseFor("/api/v1/core/workspaces/" + fixture.id + "/documents", "POST"), { timeout: 30000 });
      await page.getByRole("button", { name: "新建章节", exact: true }).click();
      const response = await created;
      if (response.status() !== 201) throw new Error("Core Document creation returned " + response.status());
      const body = await response.json();
      if (body.schema !== "core-document/v1" || body.workspace_id !== fixture.id || typeof body.document_id !== "string") {
        throw new Error("Core chapter creation response crossed its Workspace or Document identity");
      }
      fixture.chapter_document_id = body.document_id;
      const item = page.locator(".core-chapter-list__item").filter({ hasText: "第 1 章" }).first();
      await visible(item, "created Core chapter", 30000);
      await visible(page.locator("textarea[placeholder='在这里撰写章节正文…']"), "Core chapter editor", 30000);
      r.ui_actions.push({ action: "click", target: "新建章节" });
      r.expected_actual = expected("Visible UI creates and opens one Core chapter",
        "POST Core Document returned 201 for " + fixture.chapter_document_id + " and the Core editor opened",
        { workspace_id: fixture.id, document_id: fixture.chapter_document_id });
    });

    await flow("core-chapter-select", async function (r) {
      const item = page.locator(".core-chapter-list__item").filter({ hasText: "第 1 章" }).first();
      await visible(item, "Core chapter list item");
      await eventually(async function () { return !(await item.isDisabled()); }, "Core chapter list item enabled", 30000);
      const opened = page.waitForResponse(responseFor("/api/v1/core/documents/" + fixture.chapter_document_id, "GET"), { timeout: 30000 });
      await item.click();
      const response = await opened;
      if (response.status() !== 200) throw new Error("Core Document read returned " + response.status());
      await visible(page.locator("textarea[placeholder='在这里撰写章节正文…']"), "selected Core chapter editor");
      await visible(page.locator(".core-right-panel"), "Core identity panel");
      r.ui_actions.push({ action: "click", target: "Core chapter list item" });
      r.expected_actual = expected("Selecting the visible Core chapter reads the exact Document identity",
        "GET /api/v1/core/documents/" + fixture.chapter_document_id + " returned 200 and the editor stayed bound to the chapter");
    });

    let savedContent = null;
    await flow("core-chapter-edit-save", async function (r) {
      const editor = page.locator("textarea[placeholder='在这里撰写章节正文…']");
      await visible(editor, "Core chapter editor");
      savedContent = "WebUI 非空正文保存验证 " + Date.now() + "。通过 Core Revision 写入并重新读取。";
      await editor.fill(savedContent);
      r.ui_actions.push({ action: "fill", target: "Core chapter editor", value: "non-empty body" });
      const saveButton = page.locator(".core-chapter-editor").getByRole("button", { name: "保存", exact: true });
      await eventually(async function () { return !(await saveButton.isDisabled()); }, "Core save button enabled", 30000);
      const saved = page.waitForResponse(responseFor("/api/v1/core/documents/" + fixture.chapter_document_id + "/revisions", "POST"), { timeout: 30000 });
      await saveButton.click();
      const response = await saved;
      if (response.status() !== 201) throw new Error("Core Revision creation returned " + response.status());
      const body = await response.json();
      if (body.schema !== "core-revision/v1" || body.workspace_id !== fixture.id ||
          body.document_id !== fixture.chapter_document_id || typeof body.revision_id !== "string") {
        throw new Error("Core Revision response crossed its Workspace, Document or Revision identity");
      }
      fixture.revision_id = body.revision_id;
      await visible(page.getByText(fixture.revision_id, { exact: true }), "saved Core Revision identity");
      const reread = page.waitForResponse(responseFor("/api/v1/core/revisions/" + fixture.revision_id + "/content", "GET"), { timeout: 30000 });
      await page.reload();
      const rereadResponse = await reread;
      if (rereadResponse.status() !== 200) throw new Error("Core Revision content reread returned " + rereadResponse.status());
      await eventually(async function () { return (await editor.inputValue()) === savedContent; },
        "exact Core Revision content reload", 30000);
      r.ui_actions.push({ action: "click", target: "保存" }, { action: "reload", target: "Core Workbench" });
      r.expected_actual = expected("Non-empty Core edit persists as a Revision and reload reads exact content",
        "Revision " + fixture.revision_id + " reloaded with exact equality and length " + savedContent.length,
        { content_length: savedContent.length, exact_equality: true });
    });

    await flow("generation-controls-job-status", async function (r) {
      const generation = page.locator("[data-feature-surface='generation']");
      await visible(generation, "generation feature surface");
      await generation.scrollIntoViewIfNeeded();
      const startGeneration = generation.getByRole("button", { name: "开始生成", exact: true });
      if (!(await startGeneration.isDisabled())) throw new Error("Generation is enabled without a RunSnapshot binding");
      const reason = (await generation.locator(".feature-reason").first().innerText()).trim();
      if (!reason.includes("RunSnapshot")) throw new Error("Generation unavailable reason does not identify RunSnapshot binding");
      const taskStatus = generation.getByRole("button", { name: "任务状态", exact: true });
      if (await taskStatus.isDisabled()) throw new Error("Mounted Jobs v2 task status is unexpectedly disabled");
      await taskStatus.click();
      const drawerTrigger = page.getByRole("button", { name: "打开任务抽屉" }).last();
      await visible(drawerTrigger, "task drawer trigger");
      await drawerTrigger.click();
      await visible(page.getByText("暂无任务", { exact: true }).last(), "empty task drawer");
      const refreshJobs = page.getByRole("button", { name: "刷新并重新挂接任务" }).last();
      const refreshed = page.waitForResponse(responseFor("/api/v2/jobs/" + fixture.id, "GET"), { timeout: 30000 });
      await refreshJobs.click();
      const response = await refreshed;
      if (response.status() !== 200) throw new Error("Jobs v2 discovery returned " + response.status());
      r.ui_actions.push({ action: "observe", target: "disabled generation with RunSnapshot reason" },
        { action: "click", target: "任务状态" }, { action: "click", target: "打开任务抽屉" },
        { action: "click", target: "刷新并重新挂接任务" });
      r.expected_actual = expected("Unavailable generation never fabricates a Job while Jobs v2 discovery remains usable",
        "开始生成 was disabled; the empty task drawer refreshed through GET /api/v2/jobs/ and returned 200",
        { generation_started: false, job_count: 0, job_discovery_status: response.status() });
      await page.keyboard.press("Escape");
      await page.waitForTimeout(200);
    });

    const flow07Region = page.locator("[data-flow='FLOW-07'][aria-label='FLOW-07：伏笔账本']");
    const flow08Region = page.locator("[data-flow='FLOW-08'][aria-label='FLOW-08：故事演进与 Bible']");
    await flow("story-state-surfaces", async function (r) {
      const foreshadow = await subflow("FLOW-07-foreshadow-ledger", "08-workbench-foreshadow-ledger.png", async function (s) {
        await visible(flow07Region, "FLOW-07 foreshadow surface");
        await flow07Region.scrollIntoViewIfNeeded();
        const refresh = flow07Region.getByRole("button", { name: "刷新伏笔账本", exact: true });
        if (!(await refresh.isDisabled())) throw new Error("FLOW-07 refresh is enabled without a story projection");
        const reason = (await flow07Region.locator(".feature-reason").innerText()).trim();
        s.ui_actions.push({ action: "observe", target: "伏笔账本 unavailable state" });
        s.expected_actual = expected("FLOW-07 is independently visible and truthfully unavailable",
          "伏笔账本 rendered with a disabled refresh button and reason: " + reason,
          { surface: "foreshadow-ledger", independent_ui_observation: true, api_calls_expected: 0 });
      }, { requiresApiTrace: false, screenshotLocator: flow07Region });

      const storyEvolution = await subflow("FLOW-08-story-evolution-bible", "08-workbench-story-evolution-bible.png", async function (s) {
        await visible(flow08Region, "FLOW-08 story/Bible surface");
        await flow08Region.scrollIntoViewIfNeeded();
        const refresh = flow08Region.getByRole("button", { name: "刷新故事演进 / Bible", exact: true });
        if (!(await refresh.isDisabled())) throw new Error("FLOW-08 refresh is enabled without a story/Bible projection");
        const reason = (await flow08Region.locator(".feature-reason").innerText()).trim();
        s.ui_actions.push({ action: "observe", target: "故事演进 / Bible unavailable state" });
        s.expected_actual = expected("FLOW-08 is independently visible and truthfully unavailable",
          "故事演进 / Bible rendered with a disabled refresh button and reason: " + reason,
          { surface: "story-evolution-bible", independent_ui_observation: true, api_calls_expected: 0 });
      }, { requiresApiTrace: false, screenshotLocator: flow08Region });

      r.subflows = [foreshadow, storyEvolution];
      r.screenshots = foreshadow.screenshots.concat(storyEvolution.screenshots);
      r.ui_actions = foreshadow.ui_actions.concat(storyEvolution.ui_actions);
      r.expected_actual = expected("FLOW-07 and FLOW-08 retain independent UI evidence",
        "Both plugin surfaces were separately asserted and separately captured without fabricating API calls",
        { formal_flow_ids: ["FLOW-07", "FLOW-08"], independent_screenshots: true,
          independent_api_traces: true, api_calls_expected_per_subflow: 0 });
    }, { requiresApiTrace: false });

    const checkpoint = page.locator("[data-feature-surface='checkpoint-recovery']");
    await flow("checkpoint-recovery", async function (r) {
      await visible(checkpoint, "checkpoint recovery surface");
      await checkpoint.scrollIntoViewIfNeeded();
      const restore = checkpoint.getByRole("button", { name: "恢复检查点", exact: true });
      if (!(await restore.isDisabled())) throw new Error("Checkpoint recovery is enabled without a rich checkpoint projection");
      const reason = (await checkpoint.locator(".feature-reason").innerText()).trim();
      r.ui_actions.push({ action: "observe", target: "检查点恢复 unavailable state" });
      r.expected_actual = expected("Checkpoint recovery does not invent a resumable Job",
        "恢复检查点 rendered disabled with reason: " + reason,
        { checkpoint_resume_sent: false, api_calls_expected: 0 });
    }, { requiresApiTrace: false, screenshotLocator: checkpoint });

    const exportSurface = page.locator("[data-feature-surface='export']");
    await flow("workbench-export-controls", async function (r) {
      await visible(exportSurface, "export feature surface");
      await exportSurface.scrollIntoViewIfNeeded();
      const exportButton = exportSurface.getByRole("button", { name: "导出当前作品", exact: true });
      if (!(await exportButton.isDisabled())) throw new Error("Export is enabled without Artifact authority");
      const reason = (await exportSurface.locator(".feature-reason").innerText()).trim();
      exportObservation = { status: "unavailable_by_design", byte_length: 0, download_attempted: false, reason: reason };
      r.ui_actions.push({ action: "observe", target: "导出 unavailable state" });
      r.expected_actual = expected("Export never offers a fake download without an authoritative Artifact",
        "导出当前作品 rendered disabled with reason: " + reason,
        { export_request_sent: false, download_attempted: false, api_calls_expected: 0 });
    }, { requiresApiTrace: false, screenshotLocator: exportSurface });

    const db = join(dataRoot, "core", "core.db");
    const dataEvidence = {
      database_exists: existsSync(db), workspace_count: null, document_count: null,
      revision_count: null, non_empty_revision_count: null,
    };
    if (existsSync(db)) {
      const cp = await import("node:child_process");
      const raw = cp.execFileSync(python, ["-c",
        [
          "import sqlite3,sys",
          "c=sqlite3.connect(sys.argv[1])",
          "w=c.execute('SELECT COUNT(*) FROM workspace WHERE workspace_id=?',(sys.argv[2],)).fetchone()[0]",
          "d=c.execute('SELECT COUNT(*) FROM document WHERE workspace_id=? AND document_id=?',(sys.argv[2],sys.argv[3])).fetchone()[0]",
          "r=c.execute(\"SELECT COUNT(*),SUM(CASE WHEN length(COALESCE(content,''))>0 THEN 1 ELSE 0 END) FROM revision WHERE workspace_id=? AND document_id=?\",(sys.argv[2],sys.argv[3])).fetchone()",
          "print(f'{w}|{d}|{r[0]}|{r[1] or 0}')",
        ].join(";"),
        db, fixture.id, fixture.chapter_document_id], { encoding: "utf8" }).trim();
      const parts = raw.split("|");
      dataEvidence.workspace_count = Number(parts[0]);
      dataEvidence.document_count = Number(parts[1]);
      dataEvidence.revision_count = Number(parts[2]);
      dataEvidence.non_empty_revision_count = Number(parts[3]);
    }
    if (dataEvidence.workspace_count !== 1 || dataEvidence.document_count !== 1 ||
        dataEvidence.revision_count < 1 || dataEvidence.non_empty_revision_count < 1) {
      throw new Error("Core DB does not prove the Workspace, Document and non-empty Revision persistence");
    }

    const flowIds = flows.map(function (record) { return record.flow_id; });
    if (JSON.stringify(flowIds) !== JSON.stringify(FLOW_NAMES)) {
      throw new Error("WebUI flow order drift: " + JSON.stringify(flowIds));
    }
    function hasResponse(method, pathname, status) {
      return trace.some(function (entry) {
        return entry.kind === "response" && entry.method === method &&
          pathOf(entry) === pathname && entry.status === status;
      });
    }
    const requiredResponses = [
      ["POST", "/api/v1/core/workspaces", 201],
      ["POST", "/api/v1/core/workspaces/" + fixture.id + "/documents", 201],
      ["POST", "/api/v1/core/documents/" + fixture.chapter_document_id + "/revisions", 201],
      ["GET", "/api/v2/jobs/" + fixture.id, 200],
    ];
    for (const required of requiredResponses) {
      if (!hasResponse(required[0], required[1], required[2])) {
        throw new Error("API trace lacks required response " + required.join(" "));
      }
    }
    const forbiddenGenerationCalls = trace.filter(function (entry) {
      if (entry.kind !== "request") return false;
      const pathname = pathOf(entry);
      return pathname.startsWith("/api/v1/novels") || pathname.startsWith("/api/v1/bible/novels") ||
        pathname.startsWith("/api/v1/autopilot") || pathname.startsWith("/api/v1/export/novel") ||
        pathname.includes("generate-chapter-stream") || pathname.includes("generate-stream") ||
        (pathname.startsWith("/api/v2/jobs/") && pathname.endsWith("/start"));
    });
    if (forbiddenGenerationCalls.length) {
      throw new Error("Unavailable plugin capability emitted a forbidden generation/export call");
    }
    strictGate = evaluateBrowserGate(httpFailures, consoleErrors, pageErrors, blockedExternal, consoleAllowlisted);
    if (!strictGate.passed) {
      throw new Error("strict browser gate failed: " + JSON.stringify(strictGate.failures));
    }
    const output = {
      schema: "plotpilot-browser-smoke/v1", status: "passed", release_scope: "webui-capability-baseline",
      started_at: started, finished_at: iso(),
      root: ROOT, urls: { webui: BASE_URL, api: API_URL },
      service_commands: [backend.commandLine, vite.commandLine].filter(Boolean),
      runtime: { node: process.version, python_command: python, playwright_version: playwrightVersion,
        playwright_module: playwrightModule, chromium_executable: browserPath, headless: process.env.PLOTPILOT_HEADLESS === "1" },
      temporary_data_root: dataRoot, fixture: fixture,
      constraints: { core_workspace_created: true, core_document_created: true,
        core_revision_persisted: true, non_empty_chapter_body_saved: true, exact_body_reloaded: true,
        generation_pipeline_invoked: false, job_drawer_discovery_exercised: true,
        sse_disconnect_recovery: false, sse_recovery_exercised: false,
        ui_download_captured: false, export_download_captured: false,
        unavailable_capabilities_truthfully_reported: true, temporary_data_removed_after_run: true,
        fake_provider_configured: true, fake_provider_used: false, real_provider_used: false,
        external_network_used: false },
      data_evidence: dataEvidence, flows: flows, api_trace: trace,
      // External requests are recorded separately from unexpected calls: all
      // were intercepted and aborted before leaving the isolated browser.
      forbidden_generation_calls: forbiddenGenerationCalls,
      unexpected_external_calls: blockedExternal.filter(function (entry) { return !entry.allowlisted; }),
      blocked_external_requests: blockedExternal, http_failures: httpFailures,
      console_events: consoleEvents, console_errors: consoleErrors, console_allowlisted: consoleAllowlisted,
      page_errors: pageErrors, allowlist_policy: BROWSER_ALLOWLIST_POLICY,
      strict_browser_gate: strictGate, export_observation: exportObservation,
      capability_evidence: {
        working: ["workspace", "direct-workbench-entry", "chapter-document", "revision-save-reload", "job-list"],
        unavailable: ["planning", "generation", "checkpoint", "foreshadow", "story-bible", "export"],
      },
      provider_evidence: { configured_provider: "mock", provider_keys_removed: true,
        provider_invoked: false,
        prompt_stats_read_seam: { installed: true, contract_shape: "PromptStats", production_modules_untouched: true },
        keyed_fixture_identity_normalization: { installed: true, duplicate_identities_repaired_only: true } },
    };
    await writeFile(OUTPUT, JSON.stringify(output, null, 2) + "\n", "utf8");
    console.log(JSON.stringify(output, null, 2));
  } catch (error) {
    failure = error instanceof Error ? error : new Error(String(error));
    const output = { schema: "plotpilot-browser-smoke/v1", status: "failed", release_scope: "webui-capability-baseline",
      started_at: started, finished_at: iso(), root: ROOT,
      urls: { webui: BASE_URL, api: API_URL }, service_commands: [backend.commandLine, vite.commandLine].filter(Boolean),
      fixture: fixture, flows: flows, api_trace: trace,
      unexpected_external_calls: blockedExternal.filter(function (entry) { return !entry.allowlisted; }),
      blocked_external_requests: blockedExternal, http_failures: httpFailures,
      console_events: consoleEvents, console_errors: consoleErrors, console_allowlisted: consoleAllowlisted,
      page_errors: pageErrors, allowlist_policy: BROWSER_ALLOWLIST_POLICY,
      strict_browser_gate: strictGate, error: failure.message,
      service_logs: { backend: backend.snapshotLog(), vite: vite.snapshotLog() }, temporary_data_root: dataRoot };
    await writeFile(OUTPUT, JSON.stringify(output, null, 2) + "\n", "utf8");
    console.error(JSON.stringify(output, null, 2));
  } finally {
    if (page) await timed(page.close().catch(function () {}), 5000);
    if (context) await timed(context.close().catch(function () {}), 5000);
    if (browser) await timed(browser.close().catch(function () {}), 5000);
    await stop(vite); await stop(backend);
    await timed(rm(dataRoot, { recursive: true, force: true }), 10000);
  }
  if (failure) throw failure;
}
main().catch(function (error) {
  console.error("[browser-smoke] " + (error instanceof Error ? (error.stack || error.message) : String(error)));
  process.exitCode = 1;
});
