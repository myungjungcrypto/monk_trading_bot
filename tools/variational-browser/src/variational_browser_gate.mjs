#!/usr/bin/env node

import dotenv from "dotenv";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { chromium } from "playwright";

const ROOT = path.resolve(new URL("../../../", import.meta.url).pathname);
const TOOL_DIR = path.join(ROOT, "tools", "variational-browser");
dotenv.config({ path: path.join(ROOT, "backend", ".env") });
dotenv.config({ path: path.join(TOOL_DIR, ".env") });

function env(name, fallback = "") {
  return process.env[name]?.trim() || fallback;
}

function envBool(name, fallback = false) {
  const value = env(name);
  if (!value) return fallback;
  return ["1", "true", "yes", "on"].includes(value.toLowerCase());
}

function envList(name, fallback = []) {
  const value = env(name);
  if (!value) return fallback;
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function requireEnv(name, fallbackName = "") {
  const value = env(name) || (fallbackName ? env(fallbackName) : "");
  if (!value) {
    throw new Error(`Missing required env: ${name}${fallbackName ? ` or ${fallbackName}` : ""}`);
  }
  return value;
}

class TelegramApprovalClient {
  constructor({ token, chatId, allowedUserIds, timeoutMs, prefix = "vb" }) {
    this.token = token;
    this.chatId = chatId;
    this.allowedUserIds = new Set(allowedUserIds.map(String));
    this.timeoutMs = timeoutMs;
    this.prefix = prefix;
    this.offset = 0;
    this.pending = new Map();
    this.running = false;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.pollLoop().catch((error) => {
      console.error("[telegram] polling stopped:", error);
      this.running = false;
    });
  }

  async sendMessage(text, inlineKeyboard = undefined) {
    const payload = {
      chat_id: this.chatId,
      text,
      disable_web_page_preview: true,
    };
    if (inlineKeyboard) {
      payload.reply_markup = { inline_keyboard: inlineKeyboard };
    }
    const result = await this.call("sendMessage", payload);
    return result.result;
  }

  async sendPhoto(filePath, caption = "", inlineKeyboard = undefined) {
    const form = new FormData();
    form.set("chat_id", this.chatId);
    if (caption) form.set("caption", caption);
    const data = await fs.promises.readFile(filePath);
    form.set("photo", new Blob([data], { type: "image/png" }), path.basename(filePath));
    if (inlineKeyboard) {
      form.set("reply_markup", JSON.stringify({ inline_keyboard: inlineKeyboard }));
    }

    const result = await this.callMultipart("sendPhoto", form);
    return result.result;
  }

  async requestApproval({ title, body, screenshotPath = "", approveLabel = "Approve", rejectLabel = "Reject", timeoutMs }) {
    const id = randomId();
    const expiresAt = Date.now() + (timeoutMs ?? this.timeoutMs);
    const caption = [
      title,
      "",
      body,
      "",
      `id: ${id}`,
      `expires: ${new Date(expiresAt).toISOString()}`,
    ].join("\n").slice(0, 1024);
    const keyboard = [
      [
        { text: approveLabel, callback_data: `${this.prefix}:${id}:approve` },
        { text: rejectLabel, callback_data: `${this.prefix}:${id}:reject` },
      ],
      [{ text: "Kill Switch", callback_data: `${this.prefix}:${id}:kill` }],
    ];

    if (screenshotPath) {
      await this.sendPhoto(screenshotPath, caption, keyboard);
    } else {
      await this.sendMessage(caption, keyboard);
    }

    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve({ approved: false, reason: "timeout", id });
      }, timeoutMs ?? this.timeoutMs);
      this.pending.set(id, { resolve, timer, expiresAt });
    });
  }

  async pollLoop() {
    while (this.running) {
      try {
        const result = await this.call("getUpdates", {
          offset: this.offset,
          timeout: 25,
          allowed_updates: ["callback_query"],
        });
        for (const update of result.result || []) {
          this.offset = Math.max(this.offset, update.update_id + 1);
          await this.handleUpdate(update);
        }
      } catch (error) {
        console.warn("[telegram] poll error:", error.message);
        await sleep(3000);
      }
    }
  }

  async handleUpdate(update) {
    const query = update.callback_query;
    if (!query?.data?.startsWith(`${this.prefix}:`)) return;

    const fromId = String(query.from?.id || "");
    if (this.allowedUserIds.size && !this.allowedUserIds.has(fromId)) {
      await this.answerCallback(query.id, "Not allowed");
      return;
    }

    const [, id, action] = query.data.split(":");
    const pending = this.pending.get(id);
    if (!pending) {
      await this.answerCallback(query.id, "Expired or already handled");
      return;
    }

    clearTimeout(pending.timer);
    this.pending.delete(id);
    await this.answerCallback(query.id, action);

    if (action === "kill") {
      pending.resolve({ approved: false, reason: "kill", id });
      await this.sendMessage("[Variational Browser] KILL SWITCH requested. Exiting.");
      process.exit(2);
    }

    pending.resolve({ approved: action === "approve", reason: action, id });
  }

  async answerCallback(callbackQueryId, text) {
    await this.call("answerCallbackQuery", {
      callback_query_id: callbackQueryId,
      text,
      show_alert: false,
    });
  }

  async call(method, payload) {
    const url = `https://api.telegram.org/bot${this.token}/${method}`;
    const response = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
      throw new Error(`${method} failed: ${response.status} ${JSON.stringify(data).slice(0, 500)}`);
    }
    return data;
  }

  async callMultipart(method, form) {
    const url = `https://api.telegram.org/bot${this.token}/${method}`;
    const response = await fetch(url, { method: "POST", body: form });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
      throw new Error(`${method} failed: ${response.status} ${JSON.stringify(data).slice(0, 500)}`);
    }
    return data;
  }
}

class VariationalBrowserGate {
  constructor(config) {
    this.config = config;
    this.telegram = new TelegramApprovalClient({
      token: config.telegramToken,
      chatId: config.telegramChatId,
      allowedUserIds: config.telegramAllowedUserIds,
      timeoutMs: config.approvalTimeoutMs,
    });
    this.context = null;
    this.page = null;
  }

  async start() {
    this.telegram.start();
    await ensureDir(this.config.profileDir);
    await ensureDir(this.config.screenshotDir);
    await ensureDir(this.config.requestDir);

    this.context = await chromium.launchPersistentContext(this.config.profileDir, {
      headless: this.config.headless,
      viewport: this.config.viewport,
      args: ["--disable-dev-shm-usage", "--no-sandbox"],
    });
    this.page = this.context.pages()[0] || await this.context.newPage();
    this.page.setDefaultTimeout(this.config.actionTimeoutMs);
  }

  async stop() {
    if (this.context) {
      await this.context.close();
    }
  }

  async openOnly(url = this.config.url) {
    await this.page.goto(url, { waitUntil: "domcontentloaded" });
    await this.telegram.sendMessage([
      "[Variational Browser] opened",
      `url: ${url}`,
      `headless: ${this.config.headless}`,
      `profile: ${this.config.profileDir}`,
    ].join("\n"));
    await new Promise(() => {});
  }

  async approveCurrentPage() {
    const request = {
      id: `manual-${Date.now()}`,
      url: this.config.url,
      summary: "Manual approval for current Variational page",
      confirmSelector: this.config.confirmSelector,
      dryRun: this.config.dryRun,
    };
    return this.processRequest(request);
  }

  async processRequestFile(filePath) {
    const raw = await fs.promises.readFile(filePath, "utf8");
    const request = JSON.parse(raw);
    request.id ||= path.basename(filePath, path.extname(filePath));
    const result = await this.processRequest(request);
    const donePath = `${filePath}.${result.status}.done`;
    await fs.promises.rename(filePath, donePath).catch(async () => {
      await fs.promises.writeFile(donePath, raw);
      await fs.promises.unlink(filePath).catch(() => {});
    });
    return result;
  }

  async watchRequests() {
    await this.telegram.sendMessage([
      "[Variational Browser] request watcher started",
      `dir: ${this.config.requestDir}`,
      `dry_run: ${this.config.dryRun}`,
    ].join("\n"));

    while (true) {
      const files = (await fs.promises.readdir(this.config.requestDir))
        .filter((name) => name.endsWith(".json"))
        .sort();
      for (const name of files) {
        const filePath = path.join(this.config.requestDir, name);
        try {
          await this.processRequestFile(filePath);
        } catch (error) {
          await this.telegram.sendMessage(`[Variational Browser] request failed\nfile: ${name}\n${error.stack || error.message}`);
        }
      }
      await sleep(this.config.watchIntervalMs);
    }
  }

  async processRequest(request) {
    this.validateRequest(request);
    const started = Date.now();

    if (request.url) {
      await this.page.goto(request.url, { waitUntil: "domcontentloaded" });
    }
    await this.runSteps(request.beforeSteps || request.steps || []);
    await this.page.waitForTimeout(Number(request.previewDelayMs ?? this.config.previewDelayMs));

    const screenshotPath = await this.captureScreenshot(request.id || `request-${started}`);
    const body = this.buildApprovalBody(request, screenshotPath);
    const decision = await this.telegram.requestApproval({
      title: "[Variational Browser] ORDER CLICK REQUEST",
      body,
      screenshotPath,
      approveLabel: "Click",
      rejectLabel: "Reject",
      timeoutMs: Number(request.approvalTimeoutMs ?? this.config.approvalTimeoutMs),
    });

    if (!decision.approved) {
      await this.telegram.sendMessage(`[Variational Browser] rejected\nid: ${request.id}\nreason: ${decision.reason}`);
      return { status: "rejected", reason: decision.reason };
    }

    if (request.dryRun ?? this.config.dryRun) {
      await this.telegram.sendMessage(`[Variational Browser] dry-run approved, click skipped\nid: ${request.id}`);
      return { status: "dryrun" };
    }

    await this.clickConfirm(request.confirmSelector || this.config.confirmSelector);
    await this.page.waitForTimeout(Number(request.afterClickDelayMs ?? this.config.afterClickDelayMs));
    const afterPath = await this.captureScreenshot(`${request.id || "request"}-after`);
    await this.telegram.sendPhoto(afterPath, `[Variational Browser] clicked\nid: ${request.id}`);
    return { status: "clicked" };
  }

  validateRequest(request) {
    if (!request || typeof request !== "object") {
      throw new Error("request must be an object");
    }
    const createdAt = request.createdAt ? Date.parse(request.createdAt) : 0;
    const maxAgeSec = Number(request.maxAgeSec ?? this.config.maxRequestAgeSec);
    if (createdAt && maxAgeSec > 0 && Date.now() - createdAt > maxAgeSec * 1000) {
      throw new Error(`request expired: ${request.createdAt}`);
    }
    const selector = request.confirmSelector || this.config.confirmSelector;
    if (!selector) {
      throw new Error("confirmSelector is required");
    }
  }

  async runSteps(steps) {
    for (const step of steps) {
      if (!step || typeof step !== "object") continue;
      if (step.type === "goto") {
        await this.page.goto(step.url, { waitUntil: step.waitUntil || "domcontentloaded" });
      } else if (step.type === "click") {
        await this.page.locator(step.selector).click();
      } else if (step.type === "fill") {
        await this.page.locator(step.selector).fill(String(step.value ?? ""));
      } else if (step.type === "press") {
        await this.page.locator(step.selector).press(step.key);
      } else if (step.type === "wait") {
        if (step.selector) {
          await this.page.locator(step.selector).waitFor({ state: step.state || "visible" });
        } else {
          await this.page.waitForTimeout(Number(step.ms || 1000));
        }
      } else {
        throw new Error(`unknown step type: ${step.type}`);
      }
    }
  }

  async clickConfirm(selector) {
    const locator = this.page.locator(selector);
    await locator.waitFor({ state: "visible" });
    await locator.click();
  }

  async captureScreenshot(id) {
    const safeId = String(id).replace(/[^a-zA-Z0-9_.-]/g, "_");
    const filePath = path.join(this.config.screenshotDir, `${new Date().toISOString().replace(/[:.]/g, "-")}_${safeId}.png`);
    await this.page.screenshot({ path: filePath, fullPage: true });
    return filePath;
  }

  buildApprovalBody(request, screenshotPath) {
    const lines = [
      `id: ${request.id || ""}`,
      `url: ${this.page.url()}`,
      `dry_run: ${request.dryRun ?? this.config.dryRun}`,
      `confirm_selector: ${request.confirmSelector || this.config.confirmSelector}`,
      "",
      request.summary || "No summary provided.",
    ];
    if (request.signal) {
      lines.push("", "signal:", JSON.stringify(request.signal, null, 2).slice(0, 1200));
    }
    lines.push("", `screenshot: ${screenshotPath}`);
    return lines.join("\n").slice(0, 3500);
  }
}

function loadConfig() {
  const runtimeDir = path.join(TOOL_DIR, "runtime");
  return {
    url: env("VARIATIONAL_BROWSER_URL", "https://app.variational.io"),
    profileDir: path.resolve(ROOT, env("VARIATIONAL_BROWSER_PROFILE_DIR", path.join("tools", "variational-browser", "runtime", "profile"))),
    requestDir: path.resolve(ROOT, env("VARIATIONAL_BROWSER_REQUEST_DIR", path.join("tools", "variational-browser", "runtime", "requests"))),
    screenshotDir: path.resolve(ROOT, env("VARIATIONAL_BROWSER_SCREENSHOT_DIR", path.join("tools", "variational-browser", "runtime", "screenshots"))),
    confirmSelector: env("VARIATIONAL_BROWSER_CONFIRM_SELECTOR"),
    headless: envBool("VARIATIONAL_BROWSER_HEADLESS", true),
    dryRun: envBool("VARIATIONAL_BROWSER_DRY_RUN", true),
    approvalTimeoutMs: Number(env("VARIATIONAL_BROWSER_APPROVAL_TIMEOUT_SEC", "45")) * 1000,
    actionTimeoutMs: Number(env("VARIATIONAL_BROWSER_ACTION_TIMEOUT_SEC", "15000")),
    previewDelayMs: Number(env("VARIATIONAL_BROWSER_PREVIEW_DELAY_MS", "1000")),
    afterClickDelayMs: Number(env("VARIATIONAL_BROWSER_AFTER_CLICK_DELAY_MS", "3000")),
    watchIntervalMs: Number(env("VARIATIONAL_BROWSER_WATCH_INTERVAL_MS", "1000")),
    maxRequestAgeSec: Number(env("VARIATIONAL_BROWSER_MAX_REQUEST_AGE_SEC", "60")),
    viewport: parseViewport(env("VARIATIONAL_BROWSER_VIEWPORT", "1440x1200")),
    telegramToken: requireEnv("VARIATIONAL_BROWSER_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    telegramChatId: requireEnv("VARIATIONAL_BROWSER_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"),
    telegramAllowedUserIds: envList("VARIATIONAL_BROWSER_TELEGRAM_ALLOWED_USER_IDS", envList("TELEGRAM_ALLOWED_USER_IDS", [])),
    runtimeDir,
  };
}

function parseViewport(value) {
  const [width, height] = String(value).toLowerCase().split("x").map((part) => Number(part.trim()));
  return {
    width: Number.isFinite(width) && width > 0 ? width : 1440,
    height: Number.isFinite(height) && height > 0 ? height : 1200,
  };
}

function parseArgs() {
  const args = process.argv.slice(2);
  const get = (flag) => {
    const idx = args.indexOf(flag);
    return idx >= 0 ? args[idx + 1] : "";
  };
  return {
    open: args.includes("--open"),
    daemon: args.includes("--daemon"),
    approveClick: args.includes("--approve-click"),
    request: get("--request"),
    selector: get("--selector"),
    url: get("--url"),
  };
}

async function ensureDir(dir) {
  await fs.promises.mkdir(dir, { recursive: true });
}

function randomId() {
  return Math.random().toString(36).slice(2, 10);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function run() {
  const config = loadConfig();
  const args = parseArgs();
  if (args.selector) {
    config.confirmSelector = args.selector;
  }
  if (args.url) {
    config.url = args.url;
  }

  const gate = new VariationalBrowserGate(config);
  await gate.start();

  try {
    if (args.open) {
      await gate.openOnly(args.url || config.url);
    } else if (args.request) {
      await gate.processRequestFile(path.resolve(ROOT, args.request));
    } else if (args.approveClick) {
      await gate.approveCurrentPage();
    } else if (args.daemon) {
      await gate.watchRequests();
    } else {
      console.log("Usage:");
      console.log("  npm start -- --open");
      console.log("  npm start -- --approve-click --selector 'button:has-text(\"Submit\")'");
      console.log("  npm start -- --request runtime/requests/order.json");
      console.log("  npm start -- --daemon");
    }
  } finally {
    if (!args.open && !args.daemon) {
      await gate.stop();
    }
  }
}

run().catch((error) => {
  console.error("[variational-browser] fatal:", error);
  process.exit(1);
});

