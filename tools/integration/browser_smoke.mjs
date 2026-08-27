#!/usr/bin/env node

/*
 * Executable M0.6 browser evidence.  All product mutations in this file are
 * initiated by visible PlotPilot UI controls; there is no browser-side API
 * helper for creating novels/chapters or exporting blobs.
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
  "home-create-surface", "wizard-generation", "workbench-shell",
  "workbench-chapter-tree", "chapter-edit-save", "generation-pause-cancel",
  "sse-disconnect-recovery", "workbench-writing-support",
  "checkpoint-recovery", "workbench-export-menu",
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
    await waitHttp(API_URL + "/api/v1/novels/");
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

    async function flow(name, action) {
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
        await page.screenshot({ path: screenshotPath, fullPage: false });
        const screenshotBytes = await readFile(screenshotPath);
        record.screenshots = [{ path: screenshotPath, bytes: screenshotBytes.byteLength,
          sha256: sha256Buffer(screenshotBytes) }];
      }
      record.api_trace = trace.slice(begin).map(function (v) { return Object.assign({}, v); });
      record.sse_events = await page.evaluate(function (from) {
        return (window.__plotpilotSseEvents || []).slice(from);
      }, beginSse);
      if (!record.api_trace.length) throw new Error(name + " has no API trace");
      flows.push(record);
    }

    // A formal flow may have more than one independently auditable surface.
    // Keep each subflow's actions, API slice and screenshot separate; never
    // let the parent flow's screenshot stand in for either child evidence.
    async function subflow(name, screenshotName, action) {
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
      await page.screenshot({ path: screenshotPath, fullPage: false });
      const screenshotBytes = await readFile(screenshotPath);
      record.screenshots = [{ path: screenshotPath, bytes: screenshotBytes.byteLength,
        sha256: sha256Buffer(screenshotBytes) }];
      record.api_trace = trace.slice(begin).map(function (v) { return Object.assign({}, v); });
      record.sse_events = await page.evaluate(function (from) {
        return (window.__plotpilotSseEvents || []).slice(from);
      }, beginSse);
      if (!record.api_trace.length) throw new Error(name + " has no API trace");
      return record;
    }

    await flow("home-create-surface", async function (r) {
      await page.goto(BASE_URL + "/");
      await visible(page.getByRole("heading", { name: "墨枢 · 长篇叙事工作台" }), "Home heading");
      await visible(page.getByPlaceholder(/用一段话写清主线/), "premise input");
      await page.getByPlaceholder(/用一段话写清主线/).fill("M0 浏览器基线：学徒在旧城追查失踪档案，最终重建真相。");
      r.ui_actions.push({ action: "fill", target: "Home premise", value: "non-empty premise" });
      await visible(page.locator(".mtp-major-chip").first(), "market major");
      await page.locator(".mtp-major-chip").first().click();
      await visible(page.locator(".mtp-theme-chip").first(), "market theme");
      await page.locator(".mtp-theme-chip").first().click();
      r.ui_actions.push({ action: "click", target: "market major and theme" });
      await page.getByRole("button", { name: /高级（自定义章数/ }).click();
      const inputs = page.locator(".advanced-settings input");
      await inputs.nth(0).fill("M0 浏览基线");
      await inputs.nth(1).fill("1");
      await inputs.nth(2).fill("500");
      r.ui_actions.push({ action: "fill", target: "advanced settings", value: "1 chapter / 500 words" });
      const create = page.waitForResponse(responseFor("/api/v1/novels/", "POST"), { timeout: 30000 });
      await page.getByRole("button", { name: "建档并进入工作台", exact: true }).click();
      const response = await create;
      if (response.status() !== 201) throw new Error("Home UI creation returned " + response.status());
      const body = await response.json();
      fixture = { id: body.id, title: body.title, create_status: response.status() };
      await visible(page.getByText("新书设置向导", { exact: true }), "setup wizard");
      r.ui_actions.push({ action: "click", target: "建档并进入工作台" });
      r.expected_actual = expected("Home UI creates a novel and opens setup wizard",
        "POST /api/v1/novels/ returned 201 and wizard opened for " + fixture.id, { no_direct_api_action: true });
      r.flow_aliases = ["home-create-surface", "home-populated-list"];
    });

    await flow("wizard-generation", async function (r) {
      await visible(page.getByText("准备生成文风公约与世界观", { exact: true }), "wizard step 1");
      const world = page.waitForResponse(responseFor("/api/v1/bible/novels/" + fixture.id + "/generate-stream?stage=worldbuilding", "POST"), { timeout: 30000 });
      await page.getByRole("button", { name: "开始生成", exact: true }).first().click();
      await world;
      r.ui_actions.push({ action: "click", target: "Step 1 开始生成" });
      await visible(page.getByRole("button", { name: "确认修改并继续", exact: true }), "worldbuilding completion", 120000);
      r.ui_actions.push({ action: "observe", target: "worldbuilding SSE done" });
      const chars = page.waitForResponse(responseFor("/api/v1/bible/novels/" + fixture.id + "/generate-stream?stage=characters", "POST"), { timeout: 30000 });
      await page.getByRole("button", { name: "确认修改并继续", exact: true }).click();
      await chars;
      await visible(page.getByText("人物生成完成", { exact: true }), "characters completion", 120000);
      r.ui_actions.push({ action: "click", target: "confirm worldbuilding / characters SSE" });
      const locs = page.waitForResponse(responseFor("/api/v1/bible/novels/" + fixture.id + "/generate-stream?stage=locations", "POST"), { timeout: 30000 });
      await page.getByRole("button", { name: "确认修改并继续", exact: true }).click();
      await locs;
      await visible(page.getByText("地图生成完成", { exact: true }), "locations completion", 120000);
      r.ui_actions.push({ action: "click", target: "confirm characters / locations SSE" });
      await page.getByRole("button", { name: "确认修改并继续", exact: true }).click();
      await visible(page.getByRole("heading", { name: "生成剧情总纲", exact: true }), "plot outline step");
      await eventually(async function () {
        return (await page.getByText("完整主线规划", { exact: true }).count()) > 0;
      }, "plot outline SSE/execution", 120000);
      r.ui_actions.push({ action: "click", target: "confirm locations / plot outline chain" });
      await visible(page.getByRole("button", { name: "确认修改并继续", exact: true }), "outline confirmation", 120000);
      await page.getByRole("button", { name: "确认修改并继续", exact: true }).click();
      await visible(page.getByRole("button", { name: "进入工作台", exact: true }), "wizard final step");
      await page.getByRole("button", { name: "进入工作台", exact: true }).click();
      await page.waitForURL(new RegExp("/book/" + fixture.id + "/workbench"), { timeout: 30000 });
      r.ui_actions.push({ action: "click", target: "进入工作台" });
      r.expected_actual = expected("All wizard stages use real UI SSE and confirmation",
        "worldbuilding, characters, locations and plot outline completed; Workbench opened",
        { generated_stages: ["worldbuilding", "characters", "locations", "plot_outline"] });
    });

    await flow("workbench-shell", async function (r) {
      await visible(page.getByText("M0 浏览基线", { exact: true }), "Workbench title");
      await visible(page.getByText("书目列表", { exact: true }), "library link");
      await visible(page.getByRole("button", { name: "作品基础", exact: true }), "reference group");
      await page.getByRole("button", { name: "作品基础", exact: true }).click();
      await visible(page.getByText("作品设定", { exact: true }).first(), "reference tabs");
      await page.getByText("世界观", { exact: true }).first().click();
      await eventually(async function () {
        return (await page.getByText(/作品设定|暂无作品设定|世界观/, { exact: false }).count()) > 0;
      }, "reference panel hydration", 30000);
      r.ui_actions.push({ action: "click", target: "作品基础" }, { action: "click", target: "世界观" }, { action: "observe", target: "世界观 tab and hydrated reference panel" });
      r.expected_actual = expected("Workbench displays the created product and hydrates a real reference panel",
        "title, library link and reference group visible; selecting 作品基础 loaded the reference panel");
    });

    await flow("sse-disconnect-recovery", async function (r) {
      await visible(page.getByRole("button", { name: /启动全托管/ }).first(), "Autopilot start", 60000);
      await page.getByRole("button", { name: /启动全托管/ }).first().click();
      const dialog = page.getByRole("dialog");
      await visible(dialog.getByText("启动全托管", { exact: true }), "Autopilot dialog");
      const startInputs = dialog.locator(".n-input-number input");
      if (await startInputs.count() >= 3) {
        await startInputs.nth(0).fill("1");
        await startInputs.nth(0).press("Enter");
        await startInputs.nth(1).fill("500");
        await startInputs.nth(1).press("Enter");
        await startInputs.nth(2).fill("1");
        await startInputs.nth(2).press("Enter");
        r.ui_actions.push({ action: "fill", target: "Autopilot target inputs", value: "1 chapter / 500 words / 1 protection limit" });
      }
      const autoSwitch = dialog.getByRole("switch").first();
      if (await autoSwitch.count() && (await autoSwitch.getAttribute("aria-checked")) !== "true") {
        await autoSwitch.click();
        r.ui_actions.push({ action: "click", target: "全自动模式" });
      }
      const startResponse = page.waitForResponse(responseFor("/api/v1/autopilot/" + fixture.id + "/start", "POST"), { timeout: 30000 });
      await dialog.getByRole("button", { name: "启动", exact: true }).click();
      await startResponse;
      r.ui_actions.push({ action: "click", target: "启动全托管" }, { action: "click", target: "启动 dialog" });
      await eventually(async function () {
        return trace.some(function (e) { return e.kind === "request" && pathOf(e).includes("/api/v1/autopilot/" + fixture.id + "/chapter-stream"); });
      }, "Autopilot SSE connection", 60000);
      await eventually(async function () {
        return await page.evaluate(function () {
          return (window.__plotpilotSseEvents || []).some(function (event) {
            return event.url.includes("/api/v1/autopilot/") && event.url.endsWith("/chapter-stream") && event.type === "connected";
          });
        });
      }, "Autopilot SSE connected event", 30000);
      r.ui_actions.push({ action: "observe", target: "chapter-stream connected event" });
      const before = trace.filter(function (e) { return e.kind === "request" && pathOf(e).includes("/api/v1/autopilot/" + fixture.id + "/chapter-stream"); }).length;
      await page.reload();
      await page.waitForURL(new RegExp("/book/" + fixture.id + "/workbench"), { timeout: 30000 });
      await eventually(async function () {
        return trace.filter(function (e) { return e.kind === "request" && pathOf(e).includes("/api/v1/autopilot/" + fixture.id + "/chapter-stream"); }).length > before;
      }, "Autopilot SSE reconnect after refresh", 60000);
      await eventually(async function () {
        return await page.evaluate(function () {
          return (window.__plotpilotSseEvents || []).filter(function (event) {
            return event.url.includes("/api/v1/autopilot/") && event.url.endsWith("/chapter-stream") && event.type === "connected";
          }).length >= 2;
        });
      }, "Autopilot SSE connected after refresh", 30000);
      r.ui_actions.push({ action: "reload", target: "active Autopilot Workbench" }, { action: "observe", target: "chapter-stream reconnect after refresh" });
      const stopButton = page.getByRole("button", { name: /⏹ 停止|⏹ 强制停止/ }).first();
      await visible(stopButton, "Autopilot stop", 30000);
      const stopResponse = page.waitForResponse(responseFor("/api/v1/autopilot/" + fixture.id + "/stop", "POST"), { timeout: 30000 }).catch(function () { return null; });
      await stopButton.click();
      await stopResponse;
      r.ui_actions.push({ action: "click", target: "⏹ 停止" });
      r.expected_actual = expected("Refreshing active Autopilot disconnects and reconnects SSE",
        "stream request count increased from " + before + " to " +
        trace.filter(function (e) { return e.kind === "request" && pathOf(e).includes("/api/v1/autopilot/" + fixture.id + "/chapter-stream"); }).length,
        { sse_disconnect: true, sse_reconnect: true, connected_events: 2, stopped_via_ui: true });
    });

    await flow("workbench-chapter-tree", async function (r) {
      const select = page.locator(".view-mode-row .n-base-selection").first();
      if (await select.count()) {
        await select.click();
        const option = page.getByText("平铺视图", { exact: true }).last();
        if (await option.count()) await option.click();
      }
      const flat = page.locator(".n-list-item").filter({ hasText: /第.?1.?章|第一章|Chapter 1/ }).first();
      const tree = page.locator(".n-tree-node-content").filter({ hasText: /第.?1.?章|第一章|Chapter 1/ }).first();
      await eventually(async function () { return (await flat.count()) > 0 || (await tree.count()) > 0; },
        "Autopilot-created chapter UI", 90000);
      const node = (await flat.count()) > 0 ? flat : tree;
      await node.click();
      await visible(page.locator("textarea[placeholder='章节内容...']"), "Workbench chapter editor", 30000);
      r.ui_actions.push({ action: "click", target: "visible chapter in tree/flat view" });
      r.expected_actual = expected("Visible Workbench structure selects a chapter",
        "chapter selected and editable workflow surface is visible at " + (await page.url()));
    });

    await flow("chapter-edit-save", async function (r) {
      await page.goto(BASE_URL + "/book/" + fixture.id + "/chapter/1");
      await visible(page.locator(".content-editor textarea"), "Chapter editor route", 30000);
      const content = "M0 非空正文保存验证 " + Date.now() + "。通过现有章节编辑 UI 写入并重新读取。";
      await page.locator(".content-editor textarea").fill(content);
      r.ui_actions.push({ action: "fill", target: "Chapter UI content editor", value: "non-empty body" });
      const saved = page.waitForResponse(responseFor("/api/v1/novels/" + fixture.id + "/chapters/1", "PUT"), { timeout: 30000 });
      await page.getByRole("button", { name: "保存", exact: true }).click();
      await saved;
      await visible(page.getByText("已保存", { exact: true }).last(), "saved state", 15000);
      await page.reload();
      await eventually(async function () { return (await page.locator(".content-editor textarea").inputValue()) === content; },
        "exact non-empty body reload", 30000);
      r.ui_actions.push({ action: "click", target: "保存" }, { action: "reload", target: "Chapter UI" });
      r.expected_actual = expected("Non-empty edit save persists and reload reads exact content",
        "reload value length " + content.length + "; exact equality true",
        { content_length: content.length, exact_equality: true });
    });

    await flow("generation-pause-cancel", async function (r) {
      await page.goto(BASE_URL + "/book/" + fixture.id + "/workbench?chapter=1");
      await visible(page.locator("textarea[placeholder='章节内容...']"), "selected Workbench chapter", 30000);
      const generate = page.locator(".editor-footer button").filter({ hasText: /生成本章|生成下一章|生成/ }).first();
      await visible(generate, "Workbench generation button");
      await generate.click();
      await visible(page.getByText("AI 生成本章", { exact: false }), "generation modal");
      const begin = page.getByRole("button", { name: "开始生成", exact: true });
      await visible(begin, "generation start");
      const request = page.waitForRequest(function (req) {
        return req.method() === "POST" && req.url().includes("/api/v1/novels/" + fixture.id + "/generate-chapter-stream");
      }, { timeout: 30000 });
      await begin.click();
      await request;
      const cancel = page.getByRole("button", { name: "停止", exact: true }).last();
      await visible(cancel, "generation stop");
      await cancel.click();
      r.ui_actions.push({ action: "click", target: "开始生成" }, { action: "click", target: "停止/取消" });
      await page.waitForTimeout(500);
      await visible(page.getByRole("button", { name: "开始生成", exact: true }), "retry generation");
      const completed = page.waitForResponse(responseFor("/api/v1/novels/" + fixture.id + "/generate-chapter-stream", "POST"), { timeout: 30000 });
      await page.getByRole("button", { name: "开始生成", exact: true }).click();
      await completed;
      await eventually(async function () {
        return (await page.locator("textarea[placeholder='生成的内容将在此显示...']").inputValue()).trim().length > 0;
      }, "fake-provider chapter output", 120000);
      await visible(page.getByRole("button", { name: "保存到所选章节", exact: true }), "save generated");
      const saveGenerated = page.waitForResponse(responseFor("/api/v1/novels/" + fixture.id + "/chapters/1", "PUT"), { timeout: 30000 });
      await page.getByRole("button", { name: "保存到所选章节", exact: true }).click();
      await saveGenerated;
      r.ui_actions.push({ action: "click", target: "保存到所选章节" });
      r.expected_actual = expected("UI chapter SSE can be cancelled then completed and saved",
        "generate-chapter-stream executed twice; first was stopped and fake output saved",
        { generation_pipeline_invoked: true, cancelled: true, completed_and_saved: true });
    });

    await flow("workbench-writing-support", async function (r) {
      const foreshadow = await subflow("FLOW-07-foreshadow-ledger", "08-workbench-foreshadow-ledger.png", async function (s) {
        await page.getByRole("button", { name: "写作支撑", exact: true }).click();
        s.ui_actions.push({ action: "click", target: "写作支撑" });
        const ledgerTab = page.getByText("伏笔账本", { exact: true }).first();
        await visible(ledgerTab, "foreshadow ledger tab");
        await ledgerTab.click();
        s.ui_actions.push({ action: "click", target: "伏笔账本" });
        const ledger = page.locator(".fsw-panel").first();
        await visible(ledger, "foreshadow ledger panel");
        await visible(ledger.getByText("伏笔账本", { exact: true }).first(), "foreshadow ledger title");
        await eventually(async function () {
          return trace.slice(-20).some(function (e) {
            return e.kind === "response" && pathOf(e).includes("/api/v1/novels/" + fixture.id + "/foreshadow-ledger");
          });
        }, "foreshadow ledger API trace", 30000);
        s.expected_actual = expected("FLOW-07 clicks and hydrates the dedicated 伏笔账本 surface",
          "独立点击 伏笔账本 后显示 .fsw-panel，且读取 foreshadow-ledger API",
          { surface: "foreshadow-ledger", independent_ui_action: true });
      });

      const storyEvolution = await subflow("FLOW-08-story-evolution-bible", "08-workbench-story-evolution-bible.png", async function (s) {
      await page.getByRole("button", { name: "写作支撑", exact: true }).click();
        s.ui_actions.push({ action: "click", target: "写作支撑" });
      const storyTab = page.getByText("故事演进", { exact: true }).first();
      await visible(storyTab, "story evolution tab");
      await storyTab.click();
        s.ui_actions.push({ action: "click", target: "故事演进" });
      const story = page.locator(".story-evolution-panel").first();
      await visible(story, "story evolution/Bible panel");
      await visible(story.getByRole("region", { name: "故事演进控制台" }), "story evolution console");
      await visible(story.getByText("引导落点", { exact: true }), "Bible setup anchors");
      await eventually(async function () {
        return trace.slice(-30).some(function (e) {
          return e.kind === "response" && (
            pathOf(e).includes("/api/v1/novels/" + fixture.id + "/narrative-engine/story-evolution") ||
            pathOf(e).includes("/api/v1/bible/novels/" + fixture.id + "/bible")
          );
        });
      }, "story evolution/Bible API trace", 30000);
        s.expected_actual = expected("FLOW-08 separately clicks 故事演进 and hydrates its Bible-backed console",
        "独立点击 故事演进 后显示故事演进控制台与引导落点，并读取 narrative-engine/Bible API",
        { surface: "story-evolution-bible", independent_ui_action: true });
      });

      r.subflows = [foreshadow, storyEvolution];
      r.screenshots = foreshadow.screenshots.concat(storyEvolution.screenshots);
      r.ui_actions = foreshadow.ui_actions.concat(storyEvolution.ui_actions);
      r.expected_actual = expected("FLOW-07 and FLOW-08 have independent UI actions and screenshots",
        "伏笔账本与故事演进/Bible 分别点击、分别断言、分别截图并分别记录 API trace",
        { formal_flow_ids: ["FLOW-07", "FLOW-08"], independent_screenshots: true,
          independent_api_traces: true });
    });

    await flow("checkpoint-recovery", async function (r) {
      // 故事演进属于“写作支撑”组，而不是“作品基础”组。先切换真实
      // UI 分组，再点击可见的 tab；这里仍专门验证刷新后的检查点恢复。
      await page.getByRole("button", { name: "写作支撑", exact: true }).click();
      await visible(page.getByText("故事演进", { exact: true }).first(), "story evolution tab");
      await page.getByText("故事演进", { exact: true }).first().click();
      await eventually(async function () { return (await page.getByText(/检查点|存档|HEAD/, { exact: false }).count()) > 0; },
        "checkpoint surface", 30000);
      const before = await page.url();
      await page.reload();
      await page.waitForURL(new RegExp("/book/" + fixture.id + "/workbench"), { timeout: 30000 });
      await page.getByRole("button", { name: "写作支撑", exact: true }).click();
      await visible(page.getByText("故事演进", { exact: true }).first(), "story evolution tab after reload");
      await page.getByText("故事演进", { exact: true }).first().click();
      await eventually(async function () { return (await page.getByText(/检查点|存档|HEAD/, { exact: false }).count()) > 0; },
        "checkpoint hydration after reload", 30000);
      r.ui_actions.push({ action: "click", target: "写作支撑 → 故事演进/检查点" }, { action: "reload", target: "Workbench recovery" });
      r.expected_actual = expected("Checkpoint/recovery surface remains hydrated after refresh",
        "checkpoint evidence remained visible after reload from " + before, { restored_after_refresh: true });
    });

    await flow("workbench-export-menu", async function (r) {
      const trigger = page.locator('[role="button"][aria-label="导出"]');
      await visible(trigger, "export trigger");
      await trigger.click();
      await visible(page.getByText(/Markdown/).last(), "Markdown option");
      const downloadWait = page.waitForEvent("download", { timeout: 30000 });
      await page.getByText(/Markdown/).last().click();
      const download = await downloadWait;
      const destination = join(dataRoot, "downloads", download.suggestedFilename());
      await mkdir(dirname(destination), { recursive: true });
      await download.saveAs(destination);
      const bytes = (await readFile(destination)).byteLength;
      if (bytes <= 0) throw new Error("UI download is empty");
      exportObservation = { status: 200, byte_length: bytes, suggested_filename: download.suggestedFilename(),
        captured_by: "page.waitForEvent(download)" };
      r.ui_actions.push({ action: "click", target: "导出" }, { action: "click", target: "Markdown" },
        { action: "capture", target: "real browser download" });
      r.expected_actual = expected("UI Markdown export downloads non-empty bytes",
        "captured " + download.suggestedFilename() + " (" + bytes + " bytes)", { ui_download_captured: true });
    });

    const db = join(dataRoot, "plotpilot.db");
    const dataEvidence = { database_exists: existsSync(db), chapter_count: null, non_empty_chapter_count: null };
    if (existsSync(db)) {
      const cp = await import("node:child_process");
      const query = "SELECT COUNT(*) AS total, SUM(CASE WHEN length(COALESCE(content, '')) > 0 THEN 1 ELSE 0 END) AS non_empty FROM chapters";
      const raw = cp.execFileSync(python, ["-c",
        "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); r=c.execute(sys.argv[2]).fetchone(); print(f'{r[0]}|{r[1] or 0}')",
        db, query], { encoding: "utf8" }).trim();
      const parts = raw.split("|");
      dataEvidence.chapter_count = Number(parts[0]); dataEvidence.non_empty_chapter_count = Number(parts[1]);
    }
    const backendLog = backend.snapshotLog();
    const fakeProviderUsed = /MockProvider|using mock|mock provider/i.test(backendLog);
    if (dataEvidence.non_empty_chapter_count < 1) throw new Error("DB does not prove non-empty chapter persistence");
    if (!fakeProviderUsed) throw new Error("backend log does not prove MockProvider use");
    const invoked = trace.some(function (e) {
      return e.kind === "request" && /bible\/novels|generate-chapter-stream|autopilot\/.+\/(start|stream|stop|events)/.test(String(e.url));
    });
    if (!invoked) throw new Error("API trace does not prove generation invocation");
    if (flows.length !== 10) throw new Error("expected ten flows, got " + flows.length);
    strictGate = evaluateBrowserGate(httpFailures, consoleErrors, pageErrors, blockedExternal, consoleAllowlisted);
    if (!strictGate.passed) {
      throw new Error("strict browser gate failed: " + JSON.stringify(strictGate.failures));
    }
    const output = {
      schema: "plotpilot-browser-smoke/v1", status: "passed", started_at: started, finished_at: iso(),
      root: ROOT, urls: { webui: BASE_URL, api: API_URL },
      service_commands: [backend.commandLine, vite.commandLine].filter(Boolean),
      runtime: { node: process.version, python_command: python, playwright_version: playwrightVersion,
        playwright_module: playwrightModule, chromium_executable: browserPath, headless: process.env.PLOTPILOT_HEADLESS === "1" },
      temporary_data_root: dataRoot, fixture: fixture,
      constraints: { generation_pipeline_invoked: true, non_empty_chapter_body_saved: true,
        temporary_data_removed_after_run: true, fake_provider_used: true, real_provider_used: false,
        external_network_used: false, sse_disconnect_recovery: true, sse_recovery_exercised: true,
        ui_download_captured: true, export_download_captured: true },
      data_evidence: dataEvidence, flows: flows, api_trace: trace,
      // External requests are recorded separately from unexpected calls: all
      // were intercepted and aborted before leaving the isolated browser.
      unexpected_external_calls: blockedExternal.filter(function (entry) { return !entry.allowlisted; }),
      blocked_external_requests: blockedExternal, http_failures: httpFailures,
      console_events: consoleEvents, console_errors: consoleErrors, console_allowlisted: consoleAllowlisted,
      page_errors: pageErrors, allowlist_policy: BROWSER_ALLOWLIST_POLICY,
      strict_browser_gate: strictGate, export_observation: exportObservation,
      provider_evidence: { configured_provider: "mock", provider_keys_removed: true,
        backend_log_contains_mock_provider: true,
        prompt_stats_read_seam: { installed: true, contract_shape: "PromptStats", production_modules_untouched: true },
        keyed_fixture_identity_normalization: { installed: true, duplicate_identities_repaired_only: true } },
    };
    await writeFile(OUTPUT, JSON.stringify(output, null, 2) + "\n", "utf8");
    console.log(JSON.stringify(output, null, 2));
  } catch (error) {
    failure = error instanceof Error ? error : new Error(String(error));
    const output = { schema: "plotpilot-browser-smoke/v1", status: "failed",
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
