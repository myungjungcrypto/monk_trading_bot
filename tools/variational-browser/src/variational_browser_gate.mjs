#!/usr/bin/env node

import dotenv from "dotenv";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { chromium } from "playwright";

const ROOT = path.resolve(new URL("../../../", import.meta.url).pathname);
const TOOL_DIR = path.join(ROOT, "tools", "variational-browser");
dotenv.config({ path: path.join(ROOT, "backend", ".env") });
dotenv.config({ path: path.join(TOOL_DIR, ".env"), override: true });

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
  constructor({ token, chatId, allowedUserIds, timeoutMs, bestEffortTimeoutMs = 3000, prefix = "vb" }) {
    this.token = token;
    this.chatId = chatId;
    this.allowedUserIds = new Set(allowedUserIds.map(String));
    this.timeoutMs = timeoutMs;
    this.bestEffortTimeoutMs = bestEffortTimeoutMs;
    this.prefix = prefix;
    this.offset = 0;
    this.pending = new Map();
    this.running = false;
    this.pollAbortController = null;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.pollLoop().catch((error) => {
      console.error("[telegram] polling stopped:", error);
      this.running = false;
    });
  }

  stop() {
    this.running = false;
    if (this.pollAbortController) {
      this.pollAbortController.abort();
      this.pollAbortController = null;
    }
  }

  async sendMessage(text, inlineKeyboard = undefined, options = {}) {
    const payload = {
      chat_id: this.chatId,
      text,
      disable_web_page_preview: true,
    };
    if (inlineKeyboard) {
      payload.reply_markup = { inline_keyboard: inlineKeyboard };
    }
    const result = await this.call("sendMessage", payload, options);
    return result.result;
  }

  async sendPhoto(filePath, caption = "", inlineKeyboard = undefined, options = {}) {
    const form = new FormData();
    form.set("chat_id", this.chatId);
    if (caption) form.set("caption", caption);
    const data = await fs.promises.readFile(filePath);
    form.set("photo", new Blob([data], { type: "image/png" }), path.basename(filePath));
    if (inlineKeyboard) {
      form.set("reply_markup", JSON.stringify({ inline_keyboard: inlineKeyboard }));
    }

    const result = await this.callMultipart("sendPhoto", form, options);
    return result.result;
  }

  async trySendMessage(text, inlineKeyboard = undefined, label = "sendMessage") {
    try {
      return await this.sendMessage(text, inlineKeyboard, { timeoutMs: this.bestEffortTimeoutMs });
    } catch (error) {
      console.warn(`[telegram] ${label} failed: ${error.message}`);
      return null;
    }
  }

  async trySendPhoto(filePath, caption = "", inlineKeyboard = undefined, label = "sendPhoto") {
    try {
      return await this.sendPhoto(filePath, caption, inlineKeyboard, { timeoutMs: this.bestEffortTimeoutMs });
    } catch (error) {
      console.warn(`[telegram] ${label} failed: ${error.message}`);
      return null;
    }
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
        }, { abortable: true });
        for (const update of result.result || []) {
          this.offset = Math.max(this.offset, update.update_id + 1);
          await this.handleUpdate(update);
        }
      } catch (error) {
        if (!this.running) return;
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

  async call(method, payload, { abortable = false, timeoutMs = 0 } = {}) {
    const url = `https://api.telegram.org/bot${this.token}/${method}`;
    let controller = null;
    let timeout = null;
    if (abortable || timeoutMs > 0) {
      controller = new AbortController();
      if (abortable) {
        this.pollAbortController = controller;
      }
      if (timeoutMs > 0) {
        timeout = setTimeout(() => controller.abort(), timeoutMs);
      }
    }
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller?.signal,
      });
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        throw new Error(`${method} failed: ${response.status} ${JSON.stringify(data).slice(0, 500)}`);
      }
      return data;
    } finally {
      if (timeout) {
        clearTimeout(timeout);
      }
      if (controller && this.pollAbortController === controller) {
        this.pollAbortController = null;
      }
    }
  }

  async callMultipart(method, form, { timeoutMs = 0 } = {}) {
    const url = `https://api.telegram.org/bot${this.token}/${method}`;
    const controller = timeoutMs > 0 ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const response = await fetch(url, { method: "POST", body: form, signal: controller?.signal });
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        throw new Error(`${method} failed: ${response.status} ${JSON.stringify(data).slice(0, 500)}`);
      }
      return data;
    } finally {
      if (timeout) {
        clearTimeout(timeout);
      }
    }
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
      bestEffortTimeoutMs: config.telegramBestEffortTimeoutMs,
    });
    this.context = null;
    this.page = null;
    this.killSwitchLogged = false;
  }

  async start({ pollTelegram = true } = {}) {
    if (pollTelegram) {
      this.telegram.start();
    }
    await ensureDir(this.config.profileDir);
    await ensureDir(this.config.screenshotDir);
    await ensureDir(this.config.requestDir);

    this.context = await chromium.launchPersistentContext(this.config.profileDir, {
      headless: this.config.headless,
      viewport: this.config.viewport,
      args: ["--disable-dev-shm-usage", "--no-sandbox"],
      permissions: ["clipboard-read", "clipboard-write"],
    });
    this.page = this.context.pages()[0] || await this.context.newPage();
    this.page.setDefaultTimeout(this.config.actionTimeoutMs);
  }

  async stop() {
    this.telegram.stop();
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

  async connectWallet() {
    await this.page.goto(this.config.url, { waitUntil: "domcontentloaded" });
    await this.page.waitForTimeout(this.config.previewDelayMs);
    const initialState = await this.waitForInitialWalletState();
    if (initialState.stage === "ready") {
      const screenshotPath = await this.captureScreenshot(`walletconnect-ready-${Date.now()}`);
      await this.telegram.sendPhoto(
        screenshotPath,
        "[Variational Browser] wallet already ready; skipped new WalletConnect URI",
      );
      return { status: "ready" };
    }
    if (initialState.stage === "auth_required") {
      await this.telegram.sendMessage("[Variational Browser] wallet session already present; triggering authenticate instead of creating a new URI");
      return this.authenticateCurrentPage({ alreadyLoaded: true });
    }

    await this.clickFirstAvailable(this.config.connectWalletSelectors, "connect wallet");
    await this.page.waitForTimeout(1000);
    await this.clickFirstAvailable(this.config.walletConnectSelectors, "walletconnect");
    await this.page.waitForTimeout(2000);

    const screenshotPath = await this.captureScreenshot(`walletconnect-${Date.now()}`);
    const uri = await this.extractWalletConnectUri();
    if (uri) {
      const uriPath = path.join(this.config.runtimeDir, "walletconnect_uri.txt");
      await fs.promises.writeFile(uriPath, `${uri}\n`, { mode: 0o600 });
      await this.telegram.sendPhoto(
        screenshotPath,
        [
          "[Variational Browser] WalletConnect URI found",
          `uri_file: ${uriPath}`,
          "",
          "Run:",
          `cd ${path.join(ROOT, "tools", "variational-wallet")}`,
          `npm start -- --uri '${uri}'`,
        ].join("\n").slice(0, 1024),
      );
      console.log(uri);
      await this.telegram.sendMessage([
        "[Variational Browser] waiting for WalletConnect approval",
        `timeout_sec: ${Math.round(this.config.connectWaitMs / 1000)}`,
        "Keep this browser process running, then run the variational-wallet command in another SSH terminal.",
        "After the session is approved, this process will click the authenticate/login button if it appears.",
      ].join("\n"));
      const connected = await this.waitForWalletReady();
      const afterPath = await this.captureScreenshot(`walletconnect-after-${Date.now()}`);
      await this.telegram.sendPhoto(
        afterPath,
        connected
          ? "[Variational Browser] wallet appears ready"
          : "[Variational Browser] wallet is not ready after waiting",
      );
      return { status: connected ? "ready" : "uri_found_not_ready", uri, uriPath };
    }

    await this.telegram.sendPhoto(
      screenshotPath,
      [
        "[Variational Browser] WalletConnect modal opened, but wc URI was not found in DOM.",
        "If the modal has a Copy Link button, send the screenshot so we can add its selector.",
      ].join("\n"),
    );
    return { status: "uri_not_found", screenshotPath };
  }

  async authenticateCurrentPage({ alreadyLoaded = false } = {}) {
    if (!alreadyLoaded) {
      await this.page.goto(this.config.url, { waitUntil: "domcontentloaded" });
      await this.page.waitForTimeout(this.config.previewDelayMs);
    }
    const beforePath = await this.captureScreenshot(`authenticate-before-${Date.now()}`);
    const selector = await this.clickFirstAvailableOptional(this.config.authenticateSelectors, "authenticate", 5000);
    if (!selector) {
      await this.telegram.sendPhoto(
        beforePath,
        [
          "[Variational Browser] authenticate button not found",
          `url: ${this.page.url()}`,
          `selectors: ${this.config.authenticateSelectors.join(", ")}`,
        ].join("\n").slice(0, 1024),
      );
      return { status: "not_found" };
    }

    await this.telegram.sendPhoto(
      beforePath,
      [
        "[Variational Browser] authenticate button clicked",
        `selector: ${selector}`,
        "Approve the WalletConnect SIGN REQUEST from variational-wallet.",
      ].join("\n"),
    );
    await this.page.waitForTimeout(this.config.authenticateWaitMs);
    const afterPath = await this.captureScreenshot(`authenticate-after-${Date.now()}`);
    const walletState = await this.assessWalletState();
    await this.telegram.sendPhoto(
      afterPath,
      [
        "[Variational Browser] authenticate result",
        `stage: ${walletState.stage}`,
        `connect_wallet_visible: ${walletState.connectWalletVisible}`,
        `authenticate_visible: ${walletState.authenticateVisible}`,
        `wallet_prompt_visible: ${walletState.walletPromptVisible}`,
      ].join("\n"),
    );
    return { status: walletState.stage, selector };
  }

  async statusCurrentPage() {
    await this.page.goto(this.config.url, { waitUntil: "domcontentloaded" });
    await this.page.waitForTimeout(this.config.previewDelayMs);
    const walletState = await this.assessWalletState();
    const screenshotPath = await this.captureScreenshot(`status-${Date.now()}`);
    await this.telegram.sendPhoto(
      screenshotPath,
      [
        "[Variational Browser] WALLET STATUS",
        `url: ${this.page.url()}`,
        `stage: ${walletState.stage}`,
        `ready: ${walletState.stage === "ready"}`,
        `connect_wallet_visible: ${walletState.connectWalletVisible}`,
        `authenticate_visible: ${walletState.authenticateVisible}`,
        `wallet_prompt_visible: ${walletState.walletPromptVisible}`,
      ].join("\n"),
    );
    return { status: walletState.stage, screenshotPath };
  }

  async processRequestFile(filePath) {
    const processingPath = `${filePath}.processing`;
    await fs.promises.rename(filePath, processingPath);
    const raw = await fs.promises.readFile(processingPath, "utf8");
    try {
      const request = JSON.parse(raw);
      request.id ||= path.basename(filePath, path.extname(filePath));
      const result = await this.processRequest(request);
      await this.archiveRequestFile(processingPath, raw, result.status, filePath);
      return result;
    } catch (error) {
      const status = String(error?.message || "").startsWith("request expired:")
        ? "expired"
        : "failed";
      await this.archiveRequestFile(processingPath, raw, status, filePath);
      error.archivedStatus = status;
      throw error;
    }
  }

  async archiveRequestFile(filePath, raw, status, originalPath = filePath) {
    const donePath = `${originalPath}.${status}.done`;
    await fs.promises.rename(filePath, donePath).catch(async () => {
      await fs.promises.writeFile(donePath, raw);
      await fs.promises.unlink(filePath).catch(() => {});
    });
    return donePath;
  }

  async watchRequests() {
    console.log("[Variational Browser] request watcher started");
    console.log(`dir: ${this.config.requestDir}`);
    console.log(`dry_run: ${this.config.dryRun}`);
    console.log(`auto_click_open: ${this.config.autoClickOpen}`);
    console.log(`auto_click_open_max_size_usd: ${this.config.autoClickOpenMaxSizeUsd}`);
    console.log(`auto_click_reduce_only: ${this.config.autoClickReduceOnly}`);

    await this.telegram.trySendMessage([
      "[Variational Browser] request watcher started",
      `dir: ${this.config.requestDir}`,
      `dry_run: ${this.config.dryRun}`,
      `auto_click_open: ${this.config.autoClickOpen}`,
      `auto_click_open_max_size_usd: ${this.config.autoClickOpenMaxSizeUsd}`,
      `auto_click_reduce_only: ${this.config.autoClickReduceOnly}`,
    ].join("\n"), undefined, "request watcher startup status");

    while (true) {
      const killSwitch = await this.readKillSwitch();
      if (killSwitch.active) {
        if (!this.killSwitchLogged) {
          this.killSwitchLogged = true;
          console.warn(`[Variational Browser] kill switch active; request processing paused: ${killSwitch.reason || ""}`);
        }
        await sleep(this.config.watchIntervalMs);
        continue;
      }
      this.killSwitchLogged = false;

      const files = (await fs.promises.readdir(this.config.requestDir))
        .filter((name) => name.endsWith(".json"))
        .sort();
      for (const name of files) {
        const filePath = path.join(this.config.requestDir, name);
        try {
          await this.processRequestFile(filePath);
        } catch (error) {
          await this.telegram.trySendMessage(
            `[Variational Browser] request failed\nfile: ${name}\n${error.stack || error.message}`,
            undefined,
            "request failure notice",
          );
        }
      }
      await sleep(this.config.watchIntervalMs);
    }
  }

  async processRequest(request) {
    this.validateRequest(request);
    if (Array.isArray(request.variationalBatch)) {
      return this.processBatchRequest(request);
    }

    const started = Date.now();
    console.log(`[Variational Browser] processing request: ${request.id || "(no id)"}`);

    if (request.url) {
      console.log(`[Variational Browser] opening: ${request.url}`);
      await this.page.goto(request.url, { waitUntil: "domcontentloaded" });
    }
    await this.runSteps(request.beforeSteps || request.steps || []);
    await this.page.waitForTimeout(Number(request.previewDelayMs ?? this.config.previewDelayMs));

    console.log("[Variational Browser] checking wallet state...");
    const walletState = await this.waitForRequestWalletReady();
    console.log(`[Variational Browser] wallet stage: ${walletState.stage}`);
    if (walletState.stage !== "ready") {
      const notReadyPath = await this.captureScreenshot(`${request.id || "request"}-wallet-not-ready`);
      await this.telegram.trySendPhoto(
        notReadyPath,
        [
          "[Variational Browser] request blocked: wallet not ready",
          `id: ${request.id || ""}`,
          `stage: ${walletState.stage}`,
          `connect_wallet_visible: ${walletState.connectWalletVisible}`,
          `authenticate_visible: ${walletState.authenticateVisible}`,
          `wallet_prompt_visible: ${walletState.walletPromptVisible}`,
          "Run --status / --authenticate / --connect-wallet before retrying this request.",
        ].join("\n").slice(0, 1024),
        undefined,
        "wallet not ready screenshot",
      );
      return { status: `wallet_${walletState.stage}` };
    }

    if (request.variationalOrder) {
      console.log("[Variational Browser] setting up order panel...");
      await this.setupVariationalOrder(request.variationalOrder);
      await this.page.waitForTimeout(Number(request.previewDelayMs ?? this.config.previewDelayMs));
      console.log("[Variational Browser] rechecking wallet state after order setup...");
      const postSetupState = await this.waitForRequestWalletReady();
      console.log(`[Variational Browser] wallet stage after setup: ${postSetupState.stage}`);
      if (postSetupState.stage !== "ready") {
        const notReadyPath = await this.captureScreenshot(`${request.id || "request"}-wallet-not-ready-after-setup`);
        await this.telegram.trySendPhoto(
          notReadyPath,
          [
            "[Variational Browser] request blocked after order setup: wallet not ready",
            `id: ${request.id || ""}`,
            `stage: ${postSetupState.stage}`,
            `connect_wallet_visible: ${postSetupState.connectWalletVisible}`,
            `authenticate_visible: ${postSetupState.authenticateVisible}`,
            `wallet_prompt_visible: ${postSetupState.walletPromptVisible}`,
          ].join("\n").slice(0, 1024),
          undefined,
          "wallet not ready after setup screenshot",
        );
        return { status: `wallet_${postSetupState.stage}` };
      }
    }

    console.log("[Variational Browser] capturing approval screenshot...");
    const screenshotPath = await this.captureScreenshot(request.id || `request-${started}`);
    const confirmCandidates = await this.getConfirmCandidates(request.variationalOrder).catch((error) => {
      console.warn("[Variational Browser] confirm candidate scan failed:", error.message);
      return [];
    });
    const body = this.buildApprovalBody(request, screenshotPath, confirmCandidates);
    const autoReduceOnly = this.shouldAutoClickReduceOnly(request);
    const dryRun = request.dryRun ?? this.config.dryRun;

    if (autoReduceOnly) {
      await this.telegram.trySendPhoto(
        screenshotPath,
        this.buildAutoReduceOnlyCaption(request, confirmCandidates),
        undefined,
        "auto reduce-only preview",
      );
      if (dryRun) {
        await this.telegram.trySendMessage(
          `[Variational Browser] reduce-only dry-run, click skipped\nid: ${request.id}`,
          undefined,
          "auto reduce-only dry-run notice",
        );
        console.log("[Variational Browser] reduce-only dry-run, click skipped");
        return { status: "dryrun" };
      }

      console.log("[Variational Browser] auto-clicking reduce-only close request...");
      await this.clickConfirm(request, confirmCandidates);
      await this.page.waitForTimeout(Number(request.afterClickDelayMs ?? this.config.afterClickDelayMs));
      const afterPath = await this.captureScreenshot(`${request.id || "request"}-after`);
      await this.telegram.trySendPhoto(
        afterPath,
        `[Variational Browser] reduce-only clicked\nid: ${request.id}`,
        undefined,
        "auto reduce-only clicked screenshot",
      );
      console.log("[Variational Browser] reduce-only final click completed");
      return { status: "clicked" };
    }

    console.log("[Variational Browser] sending Telegram approval request...");
    const decision = await this.telegram.requestApproval({
      title: "[Variational Browser] ORDER CLICK REQUEST",
      body,
      screenshotPath,
      approveLabel: "Click",
      rejectLabel: "Reject",
      timeoutMs: Number(request.approvalTimeoutMs ?? this.config.approvalTimeoutMs),
    });

    if (!decision.approved) {
      await this.telegram.trySendMessage(
        `[Variational Browser] rejected\nid: ${request.id}\nreason: ${decision.reason}`,
        undefined,
        "manual reject notice",
      );
      console.log(`[Variational Browser] request rejected: ${decision.reason}`);
      return { status: "rejected", reason: decision.reason };
    }

    if (dryRun) {
      await this.telegram.trySendMessage(
        `[Variational Browser] dry-run approved, click skipped\nid: ${request.id}`,
        undefined,
        "manual dry-run notice",
      );
      console.log("[Variational Browser] dry-run approved, click skipped");
      return { status: "dryrun" };
    }

    console.log("[Variational Browser] clicking final confirm control...");
    await this.clickConfirm(request, confirmCandidates);
    await this.page.waitForTimeout(Number(request.afterClickDelayMs ?? this.config.afterClickDelayMs));
    const afterPath = await this.captureScreenshot(`${request.id || "request"}-after`);
    await this.telegram.trySendPhoto(
      afterPath,
      `[Variational Browser] clicked\nid: ${request.id}`,
      undefined,
      "manual clicked screenshot",
    );
    console.log("[Variational Browser] final click completed");
    return { status: "clicked" };
  }

  async processBatchRequest(request) {
    const legs = request.variationalBatch.filter((leg) => leg && typeof leg === "object");
    if (!legs.length) {
      throw new Error("variationalBatch must contain at least one leg request");
    }

    const autoReduceOnly = legs.every((leg) => this.shouldAutoClickReduceOnly(leg));
    const autoOpen = autoReduceOnly ? false : this.shouldAutoClickOpenBatch(request, legs);
    const dryRun = request.dryRun ?? this.config.dryRun;
    const clickedLegs = [];
    console.log(`[Variational Browser] processing batch request: ${request.id || "(no id)"} legs=${legs.length}`);

    if (!autoReduceOnly) {
      const previews = [];
      if (autoOpen) {
        await this.telegram.trySendMessage(
          this.buildAutoBatchOpenCaption(request, legs, previews),
          undefined,
          "auto batch open notice",
        );
      } else {
        for (const leg of legs) {
          const preview = await this.prepareRequestPreview(leg, `${request.id || "batch"}-${leg.variationalOrder?.symbol || "leg"}-preview`);
          previews.push({ leg, ...preview });
          const caption = this.buildBatchPreviewCaption(leg, preview.confirmCandidates);
          await this.telegram.sendPhoto(preview.screenshotPath, caption);
        }

        const decision = await this.telegram.requestApproval({
          title: "[Variational Browser] PAIR ORDER CLICK REQUEST",
          body: this.buildBatchApprovalBody(request, legs, previews),
          approveLabel: "Click Pair",
          rejectLabel: "Reject",
          timeoutMs: Number(request.approvalTimeoutMs ?? this.config.approvalTimeoutMs),
        });
        if (!decision.approved) {
          await this.telegram.trySendMessage(
            `[Variational Browser] batch rejected\nid: ${request.id}\nreason: ${decision.reason}`,
            undefined,
            "batch reject notice",
          );
          console.log(`[Variational Browser] batch rejected: ${decision.reason}`);
          return { status: "rejected", reason: decision.reason };
        }
      }
    } else {
      await this.telegram.trySendMessage(
        this.buildAutoBatchReduceOnlyCaption(request, legs),
        undefined,
        "auto batch reduce-only notice",
      );
    }

    if (dryRun) {
      await this.telegram.trySendMessage(
        `[Variational Browser] batch dry-run, clicks skipped\nid: ${request.id}`,
        undefined,
        "batch dry-run notice",
      );
      console.log("[Variational Browser] batch dry-run, clicks skipped");
      return { status: "dryrun" };
    }

    if (autoReduceOnly) {
      const result = await this.clickAutoReduceOnlyBatch(legs, request);
      if (result.failures.length) {
        const noPositionFailures = result.failures.filter(({ error }) => error?.code === "NO_POSITION_FOR_REDUCE_ONLY");
        const blockingFailures = result.failures.filter(({ error }) => error?.code !== "NO_POSITION_FOR_REDUCE_ONLY");
        if (!blockingFailures.length) {
          await this.telegram.trySendMessage([
            "[Variational Browser] batch reduce-only found no open position",
            `id: ${request.id}`,
            `clicked_legs: ${result.successes.map((leg) => leg.variationalOrder?.symbol).join(",") || "none"}`,
            "no_position_legs:",
            ...noPositionFailures.map(({ leg }) => `- ${leg.variationalOrder?.symbol || "leg"}`),
          ].join("\n"), undefined, "batch reduce-only no-position notice");
          return { status: result.successes.length ? "clicked" : "external_closed" };
        }
        await this.telegram.trySendMessage([
          "[Variational Browser] batch reduce-only partially failed",
          `id: ${request.id}`,
          `clicked_legs: ${result.successes.map((leg) => leg.variationalOrder?.symbol).join(",") || "none"}`,
          "failed_legs:",
          ...blockingFailures.map(({ leg, error }) => `- ${leg.variationalOrder?.symbol || "leg"}: ${error.message}`),
        ].join("\n"), undefined, "batch reduce-only partial failure notice");
        return {
          status: result.successes.length ? "partial_failed" : "failed",
          reason: blockingFailures.map(({ leg, error }) => `${leg.variationalOrder?.symbol || "leg"}=${error.message}`).join("; "),
        };
      }

      await this.telegram.trySendMessage(
        `[Variational Browser] batch clicked\nid: ${request.id}\nlegs: ${legs.length}`,
        undefined,
        "batch reduce-only clicked notice",
      );
      return { status: "clicked" };
    }

    try {
      for (const leg of legs) {
        await this.clickPreparedRequest(leg, {
          screenshotPrefix: `${request.id || "batch"}-${leg.variationalOrder?.symbol || "leg"}`,
          clickedCaption: "clicked",
        });
        clickedLegs.push(leg);
      }
    } catch (error) {
      if (!autoReduceOnly && clickedLegs.length) {
        await this.telegram.trySendMessage([
          "[Variational Browser] batch partially clicked; attempting rollback",
          `id: ${request.id}`,
          `clicked_legs: ${clickedLegs.map((leg) => leg.variationalOrder?.symbol).join(",")}`,
          `error: ${error.message}`,
        ].join("\n"), undefined, "batch rollback notice");
        const rollbackOk = await this.rollbackClickedOpenLegs(clickedLegs, request.id);
        return { status: rollbackOk ? "rolledback" : "partial_failed", reason: error.message };
      }
      throw error;
    }

    await this.telegram.trySendMessage(
      `[Variational Browser] batch clicked\nid: ${request.id}\nlegs: ${legs.length}`,
      undefined,
      "batch clicked notice",
    );
    return { status: "clicked" };
  }

  async clickAutoReduceOnlyBatch(legs, request) {
    const attempts = Math.max(1, Number(this.config.reduceOnlyBatchRetryAttempts || 1));
    const delayMs = Math.max(0, Number(this.config.reduceOnlyBatchRetryDelayMs || 0));
    const successes = [];
    let pending = [...legs];
    let failures = [];
    const terminalFailures = [];

    for (let attempt = 1; attempt <= attempts && pending.length; attempt += 1) {
      const nextPending = [];
      failures = [];

      for (const leg of pending) {
        try {
          if (attempt > 1) {
            await this.telegram.trySendMessage([
              "[Variational Browser] retrying reduce-only leg",
              `batch_id: ${request.id || ""}`,
              `attempt: ${attempt}/${attempts}`,
              `leg: ${leg.variationalOrder?.symbol || "leg"}`,
            ].join("\n"), undefined, "reduce-only retry notice");
          }
          await this.clickPreparedRequest(leg, {
            screenshotPrefix: `${request.id || "batch"}-${leg.variationalOrder?.symbol || "leg"}-attempt${attempt}`,
            clickedCaption: "reduce-only clicked",
          });
          successes.push(leg);
        } catch (error) {
          if (error?.code === "NO_POSITION_FOR_REDUCE_ONLY") {
            terminalFailures.push({ leg, error });
          } else {
            failures.push({ leg, error });
            nextPending.push(leg);
          }
          const failurePath = await this.captureScreenshot(
            `${request.id || "batch"}-${leg.variationalOrder?.symbol || "leg"}-attempt${attempt}-failed`,
          ).catch(() => "");
          if (failurePath) {
            await this.telegram.trySendPhoto(
              failurePath,
              [
                "[Variational Browser] reduce-only leg failed screenshot",
                `batch_id: ${request.id || ""}`,
                `attempt: ${attempt}/${attempts}`,
                `leg: ${leg.variationalOrder?.symbol || "leg"}`,
                `error: ${error.message}`,
              ].join("\n").slice(0, 1024),
              undefined,
              "reduce-only leg failure screenshot",
            );
          }
          await this.telegram.trySendMessage([
            "[Variational Browser] reduce-only leg failed",
            `batch_id: ${request.id || ""}`,
            `attempt: ${attempt}/${attempts}`,
            `leg: ${leg.variationalOrder?.symbol || "leg"}`,
            `error: ${error.message}`,
          ].join("\n"), undefined, "reduce-only leg failure notice");
        }
      }

      pending = nextPending;
      if (pending.length && attempt < attempts && delayMs > 0) {
        await sleep(delayMs);
      }
    }

    return { successes, failures: [...terminalFailures, ...failures] };
  }

  async prepareRequestPreview(request, screenshotId) {
    await this.prepareRequestPanel(request);
    const screenshotPath = await this.captureScreenshot(screenshotId);
    const confirmCandidates = await this.getConfirmCandidates(request.variationalOrder).catch((error) => {
      console.warn("[Variational Browser] confirm candidate scan failed:", error.message);
      return [];
    });
    return { screenshotPath, confirmCandidates };
  }

  async prepareRequestPanel(request) {
    if (request.url) {
      console.log(`[Variational Browser] opening: ${request.url}`);
      await this.page.goto(request.url, { waitUntil: "domcontentloaded" });
    }
    await this.runSteps(request.beforeSteps || request.steps || []);
    await this.page.waitForTimeout(Number(request.previewDelayMs ?? this.config.previewDelayMs));

    console.log("[Variational Browser] checking wallet state...");
    const walletState = await this.waitForRequestWalletReady();
    console.log(`[Variational Browser] wallet stage: ${walletState.stage}`);
    if (walletState.stage !== "ready") {
      throw new Error(`wallet not ready: ${walletState.stage}`);
    }

    if (request.variationalOrder) {
      console.log("[Variational Browser] setting up order panel...");
      await this.setupVariationalOrder(request.variationalOrder);
      await this.page.waitForTimeout(Number(request.previewDelayMs ?? this.config.previewDelayMs));
      console.log("[Variational Browser] rechecking wallet state after order setup...");
      const postSetupState = await this.waitForRequestWalletReady();
      console.log(`[Variational Browser] wallet stage after setup: ${postSetupState.stage}`);
      if (postSetupState.stage !== "ready") {
        throw new Error(`wallet not ready after order setup: ${postSetupState.stage}`);
      }
    }
  }

  async clickPreparedRequest(request, { screenshotPrefix, clickedCaption }) {
    const prepared = await this.prepareRequestPreview(request, `${screenshotPrefix}-before-click`);
    const legSymbol = request.variationalOrder?.symbol || "";
    const orderAction = String(request.variationalOrder?.action || "").toLowerCase();
    let clickPhase = "prepared";
    console.log(`[Variational Browser] prepared click: id=${request.id} leg=${legSymbol}`);
    await this.telegram.trySendPhoto(
      prepared.screenshotPath,
      `[Variational Browser] prepared click\nid: ${request.id}\nleg: ${legSymbol}`,
      undefined,
      "prepared click screenshot",
    );

    try {
      if (request.variationalOrder?.reduceOnly === true) {
        clickPhase = "pre-confirm reduce-only guard";
        try {
          await this.assertReduceOnlyChecked("before confirm click");
        } catch (error) {
          if (orderAction === "close" && await this.hasNoOpenPositionForSymbol(legSymbol)) {
            throw noPositionForReduceOnlyError(legSymbol, error.message);
          }
          throw error;
        }
      }

      clickPhase = "confirm click";
      const clickedTarget = await this.clickConfirm(request, prepared.confirmCandidates);
      console.log(`[Variational Browser] click submitted: id=${request.id} leg=${legSymbol} target=${clickedTarget}`);

      clickPhase = "after-click wait";
      await this.page.waitForTimeout(Number(request.afterClickDelayMs ?? this.config.afterClickDelayMs));
      const afterPath = await this.captureScreenshot(`${screenshotPrefix}-after`);
      await this.telegram.trySendPhoto(
        afterPath,
        `[Variational Browser] ${clickedCaption}\nid: ${request.id}\nleg: ${legSymbol}`,
        undefined,
        "clicked screenshot",
      );
    } catch (error) {
      const failurePath = await this.captureScreenshot(`${screenshotPrefix}-${clickPhase.replace(/\W+/g, "-")}-failed`).catch(() => "");
      const message = [
        "[Variational Browser] prepared click failed",
        `id: ${request.id}`,
        `leg: ${legSymbol}`,
        `phase: ${clickPhase}`,
        `error: ${error.message}`,
      ].join("\n");
      if (failurePath) {
        await this.telegram.trySendPhoto(
          failurePath,
          message.slice(0, 1024),
          undefined,
          "prepared click failure screenshot",
        );
      } else {
        await this.telegram.trySendMessage(message, undefined, "prepared click failure notice");
      }
      if (error?.code === "NO_POSITION_FOR_REDUCE_ONLY") {
        throw error;
      }
      throw new Error(`${clickPhase}: ${error.message}`);
    }
  }

  async rollbackClickedOpenLegs(clickedLegs, batchId = "") {
    let ok = true;
    for (const leg of [...clickedLegs].reverse()) {
      const rollback = this.buildRollbackCloseRequest(leg, batchId);
      try {
        await this.clickPreparedRequest(rollback, {
          screenshotPrefix: `${batchId || "batch"}-${rollback.variationalOrder.symbol}-rollback`,
          clickedCaption: "rollback reduce-only clicked",
        });
      } catch (error) {
        ok = false;
        await this.telegram.trySendMessage([
          "[Variational Browser] rollback failed",
          `batch_id: ${batchId}`,
          `leg: ${rollback.variationalOrder.symbol}`,
          `error: ${error.message}`,
        ].join("\n"), undefined, "rollback failure notice");
      }
    }
    return ok;
  }

  buildRollbackCloseRequest(leg, batchId = "") {
    const order = leg.variationalOrder || {};
    const closeSide = String(order.side || "").toUpperCase() === "BUY" ? "SELL" : "BUY";
    return {
      ...leg,
      id: `${batchId || leg.id || "batch"}-${order.symbol || "leg"}-rollback`,
      summary: [
        "Rollback reduce-only close after partial batch entry",
        leg.summary || "",
      ].filter(Boolean).join("\n"),
      variationalOrder: {
        ...order,
        side: closeSide,
        action: "close",
        reduceOnly: true,
      },
      signal: {
        ...(leg.signal || {}),
        action: "close",
      },
    };
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
    const dryRun = request.dryRun ?? this.config.dryRun;
    if (!dryRun && isUnsafeConfirmSelector(selector)) {
      throw new Error(`Unsafe confirmSelector for live click: ${selector}`);
    }
  }

  async readKillSwitch() {
    try {
      const raw = await fs.promises.readFile(this.config.killSwitchPath, "utf8");
      const data = JSON.parse(raw);
      if (data && typeof data === "object") {
        return {
          active: Boolean(data.active),
          reason: data.reason || "",
          updatedAt: data.updated_at || "",
          updatedBy: data.updated_by || "",
        };
      }
    } catch (error) {
      if (error.code !== "ENOENT") {
        console.warn(`[Variational Browser] failed to read kill switch: ${error.message}`);
      }
    }
    return { active: false, reason: "", updatedAt: "", updatedBy: "" };
  }

  async assertKillSwitchClear(context = "") {
    const state = await this.readKillSwitch();
    if (!state.active) return;

    const suffix = context ? ` during ${context}` : "";
    throw new Error(
      `Emergency kill switch active${suffix}; refusing Variational browser click` +
      (state.reason ? ` (${state.reason})` : ""),
    );
  }

  shouldAutoClickReduceOnly(request) {
    if (!this.config.autoClickReduceOnly) return false;
    if (request.autoClickReduceOnly === false) return false;

    const order = request.variationalOrder || {};
    const action = String(order.action || request.signal?.action || "").toLowerCase();
    return action === "close" && order.reduceOnly === true;
  }

  shouldAutoClickOpenBatch(request, legs) {
    if (!this.config.autoClickOpen) return false;
    if (request.autoClickOpen === false) return false;

    const orders = legs.map((leg) => leg.variationalOrder || {});
    const symbols = new Set(orders.map((order) => String(order.symbol || "").toUpperCase()));
    if (legs.length !== 2 || !symbols.has("BTC") || !symbols.has("ETH")) {
      throw new Error("auto open requires exactly one BTC leg and one ETH leg");
    }

    for (const leg of legs) {
      const order = leg.variationalOrder || {};
      const action = String(order.action || leg.signal?.action || request.signal?.action || "open").toLowerCase();
      if (action !== "open" || order.reduceOnly === true) {
        throw new Error("auto open only supports non-reduce-only open legs");
      }
    }

    const sizeUsd = this.batchSizeUsd(request, legs);
    const maxSizeUsd = Number(this.config.autoClickOpenMaxSizeUsd || 0);
    if (maxSizeUsd > 0) {
      if (!Number.isFinite(sizeUsd) || sizeUsd <= 0) {
        throw new Error("auto open requires size_usd_per_leg when max size guard is enabled");
      }
      if (sizeUsd > maxSizeUsd) {
        throw new Error(`auto open size exceeds guard: ${sizeUsd} > ${maxSizeUsd}`);
      }
    }
    return true;
  }

  batchSizeUsd(request, legs) {
    const candidates = [
      request.signal?.size_usd_per_leg,
      request.signal?.sizeUsdPerLeg,
      ...legs.map((leg) => leg.signal?.size_usd_per_leg),
      ...legs.map((leg) => leg.signal?.sizeUsdPerLeg),
    ];
    for (const candidate of candidates) {
      const value = Number(candidate);
      if (Number.isFinite(value) && value > 0) return value;
    }
    return NaN;
  }

  async setupVariationalOrder(order) {
    const symbol = String(order.symbol || "").toUpperCase();
    const side = String(order.side || "").toUpperCase();
    const quantity = String(order.quantity ?? "").trim();
    const orderType = String(order.orderType || "market").toLowerCase();
    const reduceOnly = Boolean(order.reduceOnly);
    const action = String(order.action || "").toLowerCase();

    if (!["BTC", "ETH"].includes(symbol)) {
      throw new Error(`unsupported Variational order symbol: ${order.symbol}`);
    }
    if (!["BUY", "SELL"].includes(side)) {
      throw new Error(`unsupported Variational order side: ${order.side}`);
    }
    if (!quantity || Number(quantity) <= 0) {
      throw new Error(`invalid Variational order quantity: ${order.quantity}`);
    }
    console.log(`[Variational Browser] order target: ${symbol} ${side} ${quantity} reduce_only=${reduceOnly}`);

    const targetPath = `/perpetual/${symbol}`;
    if (!this.page.url().includes(targetPath)) {
      console.log(`[Variational Browser] switching symbol page: ${targetPath}`);
      await this.page.goto(`${this.config.variationalBaseUrl}${targetPath}`, { waitUntil: "domcontentloaded" });
      await this.page.waitForTimeout(Number(this.config.previewDelayMs));
    }

    if (orderType === "market") {
      console.log("[Variational Browser] selecting market tab...");
      await this.clickFirstAvailableOptional(this.config.orderMarketSelectors, "market tab", 2000);
    }

    console.log(`[Variational Browser] selecting ${side} side...`);
    const clickedSide = await this.clickOrderSide(side);
    await this.page.waitForTimeout(300);

    let reduceOnlySelector = "";
    if (reduceOnly) {
      console.log("[Variational Browser] enabling reduce only...");
      try {
        reduceOnlySelector = await this.enableReduceOnly();
        await this.page.waitForTimeout(300);
        await this.assertReduceOnlyChecked("after enable");
      } catch (error) {
        if (action === "close" && await this.hasNoOpenPositionForSymbol(symbol)) {
          throw noPositionForReduceOnlyError(symbol, error.message);
        }
        throw error;
      }
    }

    console.log("[Variational Browser] filling size input...");
    const filledSelector = await this.fillOrderSize(quantity);
    await this.page.waitForTimeout(this.config.orderSetupDelayMs);
    if (reduceOnly) {
      try {
        await this.assertReduceOnlyChecked("after size input");
      } catch (error) {
        if (action === "close" && await this.hasNoOpenPositionForSymbol(symbol)) {
          throw noPositionForReduceOnlyError(symbol, error.message);
        }
        throw error;
      }
    }
    console.log(`[Variational Browser] order panel set: side_selector=${clickedSide} reduce_only_selector=${reduceOnlySelector || "none"} size_selector=${filledSelector}`);

    return { symbol, side, quantity, clickedSide, reduceOnlySelector, filledSelector };
  }

  async clickOrderSide(side) {
    const selectors = side === "BUY" ? this.config.orderBuySelectors : this.config.orderSellSelectors;
    const selector = await this.clickFirstAvailableOptional(selectors, `${side.toLowerCase()} side`, 2500);
    if (selector) return selector;

    if (!this.config.orderSideFallbackEnabled) {
      throw new Error(`Could not find ${side.toLowerCase()} side. Tried: ${selectors.join(", ")}`);
    }

    const point = side === "BUY" ? this.config.orderBuyFallbackPoint : this.config.orderSellFallbackPoint;
    const viewport = this.page.viewportSize() || this.config.viewport;
    const x = Math.round(viewport.width * point.x);
    const y = Math.round(viewport.height * point.y);
    console.log(`[Variational Browser] ${side} selector not found; clicking fallback point x=${x} y=${y}`);
    await this.page.mouse.click(x, y);
    return `fallback:${point.x},${point.y}`;
  }

  async enableReduceOnly() {
    const selector = await this.checkFirstAvailableOptional(
      this.config.reduceOnlySelectors,
      "reduce only",
      this.config.reduceOnlyTimeoutMs,
    );
    if (selector) return selector;

    if (!this.config.reduceOnlyFallbackEnabled) {
      throw new Error(`Could not enable reduce only. Tried: ${this.config.reduceOnlySelectors.join(", ")}`);
    }

    const viewport = this.page.viewportSize() || this.config.viewport;
    const point = this.config.reduceOnlyFallbackPoint;
    const x = Math.round(viewport.width * point.x);
    const y = Math.round(viewport.height * point.y);
    console.log(`[Variational Browser] reduce only selector not found; clicking fallback point x=${x} y=${y}`);
    await this.page.mouse.click(x, y);
    return `fallback:${point.x},${point.y}`;
  }

  async hasNoOpenPositionForSymbol(symbol) {
    return this.page.evaluate((asset) => {
      const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
      const text = document.body?.innerText || "";
      const lines = text.split(/\n+/).map(normalize).filter(Boolean);
      if (/Positions\s*\([1-9]\d*\)/i.test(text)) return false;
      if (lines.some((line) => /^No positions$/i.test(line))) return true;

      const symbolRe = new RegExp(`\\b${asset}\\s*[-/]?\\s*PERP\\b`, "i");
      if (symbolRe.test(text)) return false;

      const currentIdx = lines.findIndex((line) => /^Current Position$/i.test(line));
      if (currentIdx >= 0) {
        const value = lines[currentIdx + 1] || "";
        if (/^-+$/.test(value)) return true;
        if (new RegExp(`^0(?:\\.0+)?\\s*${asset}?$`, "i").test(value)) return true;
        if (/[1-9]/.test(value)) return false;
      }

      return false;
    }, symbol).catch(() => false);
  }

  async assertReduceOnlyChecked(context = "") {
    if (!this.config.requireReduceOnlyChecked) return;

    const state = await this.readReduceOnlyControlState();
    const suffix = context ? ` (${context})` : "";
    if (!state.found) {
      throw new Error(`Reduce Only checkbox state could not be verified${suffix}; refusing live reduce-only click`);
    }
    if (state.disabled) {
      throw new Error(`Reduce Only checkbox is disabled${suffix}; refusing live reduce-only click`);
    }
    if (state.checked !== true) {
      throw new Error(
        `Reduce Only checkbox is not checked${suffix}; refusing live reduce-only click ` +
        `(source=${state.source || "unknown"}, checked=${state.checked})`,
      );
    }
  }

  async readReduceOnlyControlState() {
    return this.page.evaluate(() => {
      const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
      const visible = (node) => {
        if (!node || !(node instanceof Element)) return false;
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width > 0
          && rect.height > 0
          && style.visibility !== "hidden"
          && style.display !== "none"
          && Number(style.opacity || "1") > 0.01;
      };
      const rectInfo = (node) => {
        const rect = node.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          top: rect.top,
          bottom: rect.bottom,
          x: rect.left + rect.width / 2,
          y: rect.top + rect.height / 2,
          width: rect.width,
          height: rect.height,
        };
      };
      const ownText = (node) => normalize(
        Array.from(node.childNodes || [])
          .filter((child) => child.nodeType === Node.TEXT_NODE)
          .map((child) => child.textContent || "")
          .join(" "),
      );
      const checkedValue = (node) => {
        const tag = String(node.tagName || "").toLowerCase();
        const type = String(node.getAttribute("type") || "").toLowerCase();
        if (tag === "input" && type === "checkbox") return Boolean(node.checked);
        const aria = node.getAttribute("aria-checked");
        if (aria === "true") return true;
        if (aria === "false") return false;
        const state = String(node.getAttribute("data-state") || "").toLowerCase();
        if (["checked", "on", "true"].includes(state)) return true;
        if (["unchecked", "off", "false"].includes(state)) return false;
        const className = String(node.className || "").toLowerCase();
        if (/\bchecked\b/.test(className)) return true;
        return null;
      };
      const disabledValue = (node) => Boolean(
        node.disabled
        || node.getAttribute("aria-disabled") === "true"
        || node.getAttribute("data-disabled") === "true"
        || node.closest?.('[aria-disabled="true"],[data-disabled="true"],[disabled]'),
      );
      const describe = (node, source) => ({
        found: true,
        checked: checkedValue(node),
        disabled: disabledValue(node),
        source,
      });

      const all = Array.from(document.querySelectorAll("body *"));
      const labels = all
        .filter(visible)
        .filter((node) => {
          const text = ownText(node) || normalize(node.getAttribute("aria-label") || node.textContent);
          return /^Reduce Only$/i.test(text) && node.getBoundingClientRect().width < 260;
        });

      for (const label of labels) {
        if (label instanceof HTMLLabelElement && label.control) {
          return describe(label.control, "label.control");
        }
        const embedded = label.querySelector?.('input[type="checkbox"],[role="checkbox"],[aria-checked],[data-state]');
        if (embedded) return describe(embedded, "label.embedded");
      }

      const controls = all
        .filter((node) => (
          node.matches?.('input[type="checkbox"],[role="checkbox"],[aria-checked],[data-state]')
        ))
        .filter((node) => visible(node) || (node.tagName || "").toLowerCase() === "input")
        .map((node) => ({ node, rect: rectInfo(node) }))
        .filter((item) => item.rect.width <= 80 && item.rect.height <= 80);

      let best = null;
      for (const label of labels) {
        const labelRect = rectInfo(label);
        for (const control of controls) {
          const dy = Math.abs(control.rect.y - labelRect.y);
          const dx = labelRect.left - control.rect.right;
          if (dy > 28 || dx < -16 || dx > 120) continue;
          const score = dy + Math.max(dx, 0) / 8;
          if (!best || score < best.score) {
            best = { ...control, score };
          }
        }
      }
      if (best) return describe(best.node, "geometry");

      return { found: false, checked: null, disabled: false, source: "" };
    });
  }

  async fillOrderSize(quantity) {
    const selector = await this.fillFirstAvailableOptional(
      this.config.orderSizeInputSelectors,
      quantity,
      "size input",
      this.config.orderInputTimeoutMs,
    );
    if (selector) return selector;

    if (!this.config.orderSizeFallbackEnabled) {
      throw new Error(`Could not fill size input. Tried: ${this.config.orderSizeInputSelectors.join(", ")}`);
    }

    const viewport = this.page.viewportSize() || this.config.viewport;
    const point = this.config.orderSizeFallbackPoint;
    const x = Math.round(viewport.width * point.x);
    const y = Math.round(viewport.height * point.y);
    console.log(`[Variational Browser] size input selector not found; typing via fallback point x=${x} y=${y}`);
    await this.page.mouse.click(x, y);
    const focus = await this.focusedEditableInfo();
    if (!focus.editable) {
      await this.clearPageSelection();
      throw new Error(
        `Size fallback point did not focus an editable input (active=${focus.tag || "none"} role=${focus.role || ""}). ` +
        "Refusing to press Ctrl+A because it would select the page instead of the size field.",
      );
    }
    await this.page.keyboard.press("Control+A");
    await this.page.keyboard.type(String(quantity));
    return `fallback:${point.x},${point.y}:${focus.tag}`;
  }

  async focusedEditableInfo() {
    return this.page.evaluate(() => {
      const element = document.activeElement;
      if (!element) return { editable: false, tag: "" };
      const tag = String(element.tagName || "").toLowerCase();
      const role = element.getAttribute("role") || "";
      const type = element.getAttribute("type") || "";
      const editable = Boolean(
        element.isContentEditable
        || tag === "input"
        || tag === "textarea"
        || role === "spinbutton"
        || role === "textbox",
      );
      return { editable, tag, role, type };
    });
  }

  async clearPageSelection() {
    await this.page.evaluate(() => {
      const selection = window.getSelection?.();
      if (selection) selection.removeAllRanges();
    }).catch(() => {});
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

  async getConfirmCandidates(order = undefined) {
    const side = String(order?.side || "").toUpperCase();
    const symbol = String(order?.symbol || "").toUpperCase();
    const viewport = this.page.viewportSize() || this.config.viewport;
    const minX = viewport.width * this.config.confirmCandidateMinXRatio;
    const minY = viewport.height * this.config.confirmCandidateMinYRatio;
    const maxY = viewport.height * this.config.confirmCandidateMaxYRatio;
    const candidates = await this.page.locator("button").evaluateAll((buttons, params) => {
      const { minX, minY, maxY, side, symbol } = params;
      return buttons.map((button, index) => {
        const rect = button.getBoundingClientRect();
        const text = (button.innerText || button.textContent || "").replace(/\s+/g, " ").trim();
        const style = window.getComputedStyle(button);
        const ariaDisabled = button.getAttribute("aria-disabled") === "true";
        const disabled = Boolean(button.disabled || ariaDisabled);
        const visible = rect.width > 1
          && rect.height > 1
          && style.visibility !== "hidden"
          && style.display !== "none"
          && Number(style.opacity || "1") > 0.01;
        let score = 0;
        const upper = text.toUpperCase();
        if (visible) score += 10;
        if (!disabled) score += 10;
        if (rect.x >= minX && rect.y >= minY && rect.y <= maxY) score += 25;
        if (side && upper.includes(side)) score += 20;
        if (symbol && upper.includes(symbol)) score += 8;
        if (/ORDER|PLACE|SUBMIT|LONG|SHORT|BUY|SELL/i.test(text)) score += 8;
        if (/CONNECT|AUTHENTICATE|TRANSFER|MARKET|LIMIT|CROSS|PRO|TP\/SL|50X/i.test(text)) score -= 20;
        if (/ENTER SIZE/i.test(text)) score -= 40;
        if (!text) score -= 100;
        if (disabled) score -= 60;
        return {
          index,
          text,
          disabled,
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
          score,
        };
      });
    }, { minX, minY, maxY, side, symbol });

    return candidates
      .filter((candidate) => candidate.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, this.config.confirmCandidateLimit);
  }

  async clickConfirm(request, confirmCandidates = []) {
    await this.assertKillSwitchClear("confirm click");

    const selector = String(request.confirmSelector || this.config.confirmSelector || "auto").trim();
    if (selector && selector.toLowerCase() !== "auto") {
      if (isUnsafeConfirmSelector(selector)) {
        throw new Error(`Unsafe confirmSelector for live click: ${selector}`);
      }
      const locator = this.page.locator(selector).first();
      await locator.waitFor({ state: "visible" });
      await locator.click();
      return `selector:${selector}`;
    }

    const candidate =
      confirmCandidates.find((item) => this.isUsableConfirmCandidate(item, request.variationalOrder))
      || (await this.getConfirmCandidates(request.variationalOrder))
        .find((item) => this.isUsableConfirmCandidate(item, request.variationalOrder));
    if (candidate) {
      console.log(`[Variational Browser] auto confirm candidate: #${candidate.index} "${candidate.text}" score=${candidate.score}`);
      await this.page.locator("button").nth(candidate.index).click();
      return `button:${candidate.index}`;
    }

    if (!this.config.confirmFallbackEnabled) {
      throw new Error("No enabled confirm button candidate found. Keep dry-run on and inspect the approval screenshot.");
    }

    const viewport = this.page.viewportSize() || this.config.viewport;
    const point = this.config.confirmFallbackPoint;
    const x = Math.round(viewport.width * point.x);
    const y = Math.round(viewport.height * point.y);
    console.log(`[Variational Browser] confirm candidate not found; clicking fallback point x=${x} y=${y}`);
    await this.page.mouse.click(x, y);
    return `fallback:${point.x},${point.y}`;
  }

  isUsableConfirmCandidate(candidate, order = undefined) {
    if (!candidate || candidate.disabled) return false;
    const text = String(candidate.text || "").replace(/\s+/g, " ").trim();
    if (!text) return false;
    if (!order) return true;

    const upper = text.toUpperCase();
    if (/CONNECT|AUTHENTICATE|TRANSFER|MARKET|LIMIT|CROSS|PRO|TP\/SL|50X|ENTER SIZE/i.test(text)) {
      return false;
    }

    const side = String(order.side || "").toUpperCase();
    const symbol = String(order.symbol || "").toUpperCase();
    const sideOk = side
      ? upper.includes(side) || (side === "BUY" && upper.includes("LONG")) || (side === "SELL" && upper.includes("SHORT"))
      : /BUY|SELL|LONG|SHORT|ORDER|PLACE|SUBMIT/i.test(text);
    const symbolOk = symbol ? upper.includes(symbol) : true;
    return sideOk && symbolOk;
  }

  async waitForWalletReady() {
    const deadline = Date.now() + this.config.connectWaitMs;
    let authenticateClicked = false;
    while (Date.now() < deadline) {
      await this.page.waitForTimeout(2000);
      const walletState = await this.assessWalletState();
      if (walletState.stage === "ready") {
        await this.page.waitForTimeout(this.config.connectedStableMs);
        return (await this.assessWalletState()).stage === "ready";
      }
      if (!authenticateClicked && walletState.stage === "auth_required") {
        const selector = await this.clickFirstAvailableOptional(this.config.authenticateSelectors, "authenticate", 1000);
        if (selector) {
          authenticateClicked = true;
          await this.telegram.sendMessage([
            "[Variational Browser] authenticate/login button clicked",
            `selector: ${selector}`,
            "Approve the WalletConnect SIGN REQUEST from variational-wallet, then keep both processes running.",
          ].join("\n"));
          await this.page.waitForTimeout(this.config.authenticateWaitMs);
        }
      }
    }
    return false;
  }

  async waitForInitialWalletState() {
    const deadline = Date.now() + this.config.stateSettleMs;
    let state = await this.assessWalletState();
    while (state.stage === "disconnected" && Date.now() < deadline) {
      await this.page.waitForTimeout(1000);
      state = await this.assessWalletState();
    }
    return state;
  }

  async waitForRequestWalletReady() {
    const deadline = Date.now() + this.config.requestWalletReadyWaitMs;
    let state = await this.assessWalletState();
    while (state.stage !== "ready" && Date.now() < deadline) {
      await this.page.waitForTimeout(1000);
      state = await this.assessWalletState();
    }
    if (state.stage === "ready") {
      await this.page.waitForTimeout(this.config.connectedStableMs);
      return this.assessWalletState();
    }
    return state;
  }

  async assessWalletState() {
    const connectWalletVisible = await this.hasVisibleConnectWallet();
    const authenticateVisible = await this.hasVisibleAuthenticate();
    const walletPromptVisible = await this.hasVisibleWalletPrompt();
    let stage = "ready";
    if (connectWalletVisible) {
      stage = "disconnected";
    } else if (authenticateVisible || walletPromptVisible) {
      stage = "auth_required";
    }
    return { stage, connectWalletVisible, authenticateVisible, walletPromptVisible };
  }

  async hasVisibleConnectWallet() {
    for (const selector of this.config.connectWalletSelectors) {
      const locators = await this.page.locator(selector).all();
      for (const locator of locators) {
        try {
          if (await locator.isVisible()) return true;
        } catch {
          // Ignore stale locators.
        }
      }
    }
    return false;
  }

  async hasVisibleAuthenticate() {
    return this.hasVisibleBySelectors(this.config.authenticateSelectors);
  }

  async hasVisibleWalletPrompt() {
    return this.hasVisibleBySelectors(this.config.walletPromptSelectors);
  }

  async hasVisibleBySelectors(selectors) {
    for (const selector of selectors) {
      const locators = await this.page.locator(selector).all();
      for (const locator of locators) {
        try {
          if (await locator.isVisible()) return true;
        } catch {
          // Ignore stale locators.
        }
      }
    }
    return false;
  }

  async clickFirstAvailable(selectors, label) {
    for (const selector of selectors) {
      const locator = this.page.locator(selector).first();
      try {
        await locator.waitFor({ state: "visible", timeout: 5000 });
        await locator.click();
        return selector;
      } catch {
        // Try next selector.
      }
    }
    throw new Error(`Could not find ${label}. Tried: ${selectors.join(", ")}`);
  }

  async clickFirstAvailableOptional(selectors, label, timeoutMs = 3000) {
    for (const selector of selectors) {
      const locator = this.page.locator(selector).first();
      try {
        await locator.waitFor({ state: "visible", timeout: timeoutMs });
        await locator.click();
        return selector;
      } catch {
        // Try next selector.
      }
    }
    return "";
  }

  async fillFirstAvailable(selectors, value, label, timeoutMs = 3000) {
    for (const selector of selectors) {
      const locator = this.page.locator(selector).first();
      try {
        await locator.waitFor({ state: "visible", timeout: timeoutMs });
        await locator.fill(String(value), { timeout: timeoutMs });
        return selector;
      } catch {
        try {
          await locator.waitFor({ state: "visible", timeout: timeoutMs });
          await locator.click();
          await this.page.keyboard.press("Control+A");
          await this.page.keyboard.type(String(value));
          return selector;
        } catch {
          // Try next selector.
        }
      }
    }
    throw new Error(`Could not fill ${label}. Tried: ${selectors.join(", ")}`);
  }

  async fillFirstAvailableOptional(selectors, value, label, timeoutMs = 3000) {
    for (const selector of selectors) {
      const locator = this.page.locator(selector).first();
      try {
        await locator.waitFor({ state: "visible", timeout: timeoutMs });
        await locator.fill(String(value), { timeout: timeoutMs });
        return selector;
      } catch {
        try {
          await locator.waitFor({ state: "visible", timeout: timeoutMs });
          await locator.click();
          await this.page.keyboard.press("Control+A");
          await this.page.keyboard.type(String(value));
          return selector;
        } catch {
          // Try next selector.
        }
      }
    }
    console.log(`[Variational Browser] ${label} selector not found. Tried: ${selectors.join(", ")}`);
    return "";
  }

  async checkFirstAvailableOptional(selectors, label, timeoutMs = 3000) {
    for (const selector of selectors) {
      const locator = this.page.locator(selector).first();
      try {
        await locator.waitFor({ state: "visible", timeout: timeoutMs });
        const tagName = await locator.evaluate((node) => node.tagName.toLowerCase()).catch(() => "");
        const inputType = await locator.getAttribute("type").catch(() => "");
        const role = await locator.getAttribute("role").catch(() => "");
        if (tagName === "input" && inputType === "checkbox") {
          await locator.setChecked(true, { force: true, timeout: timeoutMs });
        } else if (role === "checkbox") {
          const checked = await locator.getAttribute("aria-checked").catch(() => "");
          if (checked !== "true") await locator.click();
        } else {
          await locator.click();
        }
        return selector;
      } catch {
        // Try next selector.
      }
    }
    console.log(`[Variational Browser] ${label} checkbox selector not found. Tried: ${selectors.join(", ")}`);
    return "";
  }

  async extractWalletConnectUri() {
    if (this.config.walletConnectUriSelector) {
      const locator = this.page.locator(this.config.walletConnectUriSelector).first();
      try {
        await locator.waitFor({ state: "visible", timeout: 3000 });
        const value = await locator.inputValue().catch(() => "");
        const text = value || await locator.textContent().catch(() => "");
        const href = await locator.getAttribute("href").catch(() => "");
        const uri = findWalletConnectUri(`${value}\n${text}\n${href}`);
        if (uri) return uri;
      } catch {
        // Continue with generic extraction.
      }
    }

    const hrefs = await this.page.locator('a[href^="wc:"]').evaluateAll((nodes) => nodes.map((node) => node.href));
    for (const href of hrefs) {
      const uri = findWalletConnectUri(href);
      if (uri) return uri;
    }

    const text = await this.page.locator("body").textContent().catch(() => "");
    const textUri = findWalletConnectUri(text || "");
    if (textUri) return textUri;

    const html = await this.page.content();
    const htmlUri = findWalletConnectUri(html);
    if (htmlUri) return htmlUri;

    return this.copyAndReadWalletConnectUri();
  }

  async copyAndReadWalletConnectUri() {
    for (const selector of this.config.walletConnectCopySelectors) {
      const locator = this.page.locator(selector).first();
      try {
        await locator.waitFor({ state: "visible", timeout: 3000 });
        await locator.click();
        await this.page.waitForTimeout(500);
        const clipboardText = await this.page.evaluate(async () => {
          try {
            return await navigator.clipboard.readText();
          } catch {
            return "";
          }
        });
        const uri = findWalletConnectUri(clipboardText);
        if (uri) return uri;
      } catch {
        // Try next copy selector.
      }
    }
    return "";
  }

  async captureScreenshot(id) {
    await this.clearPageSelection();
    const safeId = String(id).replace(/[^a-zA-Z0-9_.-]/g, "_");
    const filePath = path.join(this.config.screenshotDir, `${new Date().toISOString().replace(/[:.]/g, "-")}_${safeId}.png`);
    await this.page.screenshot({ path: filePath, fullPage: true });
    return filePath;
  }

  buildApprovalBody(request, screenshotPath, confirmCandidates = []) {
    const lines = [
      `id: ${request.id || ""}`,
      `url: ${this.page.url()}`,
      `dry_run: ${request.dryRun ?? this.config.dryRun}`,
      `confirm_selector: ${request.confirmSelector || this.config.confirmSelector}`,
      "",
      request.summary || "No summary provided.",
    ];
    if (confirmCandidates.length) {
      lines.push(
        "",
        "confirm_button_candidates:",
        ...confirmCandidates.slice(0, 3).map((candidate) => (
          `#${candidate.index} score=${candidate.score} disabled=${candidate.disabled} text="${candidate.text}" box=${candidate.x},${candidate.y},${candidate.width}x${candidate.height}`
        )),
      );
    } else {
      lines.push("", "confirm_button_candidates: none");
    }
    if (request.signal) {
      lines.push("", "signal:", JSON.stringify(request.signal, null, 2).slice(0, 900));
    }
    if (request.variationalOrder) {
      lines.push("", "variational_order:", JSON.stringify(request.variationalOrder, null, 2).slice(0, 800));
    }
    lines.push("", `screenshot: ${screenshotPath}`);
    return lines.join("\n").slice(0, 3500);
  }

  buildAutoReduceOnlyCaption(request, confirmCandidates = []) {
    const order = request.variationalOrder || {};
    const lines = [
      "[Variational Browser] AUTO REDUCE-ONLY CLICK",
      `id: ${request.id || ""}`,
      `url: ${this.page.url()}`,
      `dry_run: ${request.dryRun ?? this.config.dryRun}`,
      `leg: ${order.symbol || ""} ${order.side || ""}`,
      `quantity: ${order.quantity || ""}`,
      `reduce_only: ${order.reduceOnly === true}`,
      "",
      request.summary || "No summary provided.",
    ];
    if (confirmCandidates.length) {
      const candidate = confirmCandidates.find((item) => !item.disabled) || confirmCandidates[0];
      lines.push(
        "",
        "confirm_button_candidate:",
        `#${candidate.index} score=${candidate.score} disabled=${candidate.disabled} text="${candidate.text}" box=${candidate.x},${candidate.y},${candidate.width}x${candidate.height}`,
      );
    } else {
      lines.push("", "confirm_button_candidates: none");
    }
    return lines.join("\n").slice(0, 1024);
  }

  buildBatchPreviewCaption(request, confirmCandidates = []) {
    const order = request.variationalOrder || {};
    const lines = [
      "[Variational Browser] PAIR LEG PREVIEW",
      `id: ${request.id || ""}`,
      `leg: ${order.symbol || ""} ${order.side || ""}`,
      `quantity: ${order.quantity || ""}`,
      `action: ${order.action || ""}`,
      `reduce_only: ${order.reduceOnly === true}`,
    ];
    if (confirmCandidates.length) {
      const candidate = confirmCandidates.find((item) => !item.disabled) || confirmCandidates[0];
      lines.push(
        "",
        "confirm_button_candidate:",
        `#${candidate.index} score=${candidate.score} disabled=${candidate.disabled} text="${candidate.text}"`,
      );
    }
    return lines.join("\n").slice(0, 1024);
  }

  buildBatchApprovalBody(request, legs, previews = []) {
    const lines = [
      `id: ${request.id || ""}`,
      `dry_run: ${request.dryRun ?? this.config.dryRun}`,
      "",
      request.summary || "No summary provided.",
      "",
      "legs:",
    ];
    for (const leg of legs) {
      const order = leg.variationalOrder || {};
      lines.push(`- ${order.symbol || ""} ${order.side || ""} qty=${order.quantity || ""} reduce_only=${order.reduceOnly === true}`);
    }
    if (previews.length) {
      lines.push("", "preview_screenshots_sent: true");
    }
    if (request.signal) {
      lines.push("", "signal:", JSON.stringify(request.signal, null, 2).slice(0, 1000));
    }
    return lines.join("\n").slice(0, 3500);
  }

  buildAutoBatchOpenCaption(request, legs, previews = []) {
    const sizeUsd = this.batchSizeUsd(request, legs);
    const lines = [
      "[Variational Browser] AUTO PAIR OPEN",
      `id: ${request.id || ""}`,
      `dry_run: ${request.dryRun ?? this.config.dryRun}`,
      `max_size_usd_per_leg: ${this.config.autoClickOpenMaxSizeUsd}`,
      Number.isFinite(sizeUsd) ? `size_usd_per_leg: ${sizeUsd}` : "size_usd_per_leg: unknown",
      "",
      request.summary || "No summary provided.",
      "",
      "legs:",
    ];
    for (const leg of legs) {
      const order = leg.variationalOrder || {};
      lines.push(`- ${order.symbol || ""} ${order.side || ""} qty=${order.quantity || ""} reduce_only=${order.reduceOnly === true}`);
    }
    if (previews.length) {
      lines.push("", "preview_screenshots_sent: true");
    }
    return lines.join("\n").slice(0, 3500);
  }

  buildAutoBatchReduceOnlyCaption(request, legs) {
    const lines = [
      "[Variational Browser] AUTO PAIR REDUCE-ONLY",
      `id: ${request.id || ""}`,
      `dry_run: ${request.dryRun ?? this.config.dryRun}`,
      "",
      request.summary || "No summary provided.",
      "",
      "legs:",
    ];
    for (const leg of legs) {
      const order = leg.variationalOrder || {};
      lines.push(`- ${order.symbol || ""} ${order.side || ""} qty=${order.quantity || ""} reduce_only=${order.reduceOnly === true}`);
    }
    return lines.join("\n").slice(0, 3500);
  }
}

function loadConfig() {
  const runtimeDir = path.join(TOOL_DIR, "runtime");
  const url = env("VARIATIONAL_BROWSER_URL", "https://omni.variational.io");
  return {
    url,
    variationalBaseUrl: normalizeBaseUrl(env("VARIATIONAL_BROWSER_BASE_URL", url)),
    profileDir: path.resolve(ROOT, env("VARIATIONAL_BROWSER_PROFILE_DIR", path.join("tools", "variational-browser", "runtime", "profile"))),
    requestDir: path.resolve(ROOT, env("VARIATIONAL_BROWSER_REQUEST_DIR", path.join("tools", "variational-browser", "runtime", "requests"))),
    screenshotDir: path.resolve(ROOT, env("VARIATIONAL_BROWSER_SCREENSHOT_DIR", path.join("tools", "variational-browser", "runtime", "screenshots"))),
    killSwitchPath: path.resolve(ROOT, env("VARIATIONAL_BROWSER_KILL_SWITCH_PATH", path.join("tools", "variational-browser", "runtime", "kill_switch.json"))),
    confirmSelector: env("VARIATIONAL_BROWSER_CONFIRM_SELECTOR", "auto"),
    headless: envBool("VARIATIONAL_BROWSER_HEADLESS", true),
    dryRun: envBool("VARIATIONAL_BROWSER_DRY_RUN", true),
    autoClickReduceOnly: envBool("VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY", true),
    autoClickOpen: envBool("VARIATIONAL_BROWSER_AUTO_CLICK_OPEN", false),
    autoClickOpenMaxSizeUsd: Number(env("VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD", "100")),
    approvalTimeoutMs: Number(env("VARIATIONAL_BROWSER_APPROVAL_TIMEOUT_SEC", "45")) * 1000,
    actionTimeoutMs: Number(env("VARIATIONAL_BROWSER_ACTION_TIMEOUT_SEC", "15000")),
    previewDelayMs: Number(env("VARIATIONAL_BROWSER_PREVIEW_DELAY_MS", "1000")),
    afterClickDelayMs: Number(env("VARIATIONAL_BROWSER_AFTER_CLICK_DELAY_MS", "3000")),
    reduceOnlyBatchRetryAttempts: Number(env("VARIATIONAL_BROWSER_REDUCE_ONLY_BATCH_RETRY_ATTEMPTS", "3")),
    reduceOnlyBatchRetryDelayMs: Number(env("VARIATIONAL_BROWSER_REDUCE_ONLY_BATCH_RETRY_DELAY_MS", "5000")),
    connectWaitMs: Number(env("VARIATIONAL_BROWSER_CONNECT_WAIT_SEC", "300")) * 1000,
    connectedStableMs: Number(env("VARIATIONAL_BROWSER_CONNECTED_STABLE_MS", "3000")),
    authenticateWaitMs: Number(env("VARIATIONAL_BROWSER_AUTHENTICATE_WAIT_SEC", "15")) * 1000,
    stateSettleMs: Number(env("VARIATIONAL_BROWSER_STATE_SETTLE_SEC", "8")) * 1000,
    requestWalletReadyWaitMs: Number(env("VARIATIONAL_BROWSER_REQUEST_WALLET_READY_WAIT_SEC", "15")) * 1000,
    watchIntervalMs: Number(env("VARIATIONAL_BROWSER_WATCH_INTERVAL_MS", "1000")),
    maxRequestAgeSec: Number(env("VARIATIONAL_BROWSER_MAX_REQUEST_AGE_SEC", "60")),
    viewport: parseViewport(env("VARIATIONAL_BROWSER_VIEWPORT", "1440x1200")),
    connectWalletSelectors: envList("VARIATIONAL_BROWSER_CONNECT_WALLET_SELECTORS", [
      'button:has-text("Connect Wallet")',
      'text="Connect Wallet"',
    ]),
    walletConnectSelectors: envList("VARIATIONAL_BROWSER_WALLETCONNECT_SELECTORS", [
      'text=/WalletConnect/i',
      'button:has-text("WalletConnect")',
      '[data-testid*="walletconnect" i]',
      '[aria-label*="WalletConnect" i]',
    ]),
    walletConnectCopySelectors: envList("VARIATIONAL_BROWSER_WC_COPY_SELECTORS", [
      'button:has-text("Copy link")',
      'text=/Copy link/i',
      'button:has-text("Copy")',
      '[aria-label*="Copy" i]',
      '[title*="Copy" i]',
    ]),
    authenticateSelectors: envList("VARIATIONAL_BROWSER_AUTHENTICATE_SELECTORS", [
      'button:has-text("Authenticate")',
      'text=/Authenticate/i',
      'button:has-text("Sign In")',
      'text=/Sign In/i',
      'button:has-text("Log In")',
      'text=/Log In/i',
      'button:has-text("Continue")',
    ]),
    walletPromptSelectors: envList("VARIATIONAL_BROWSER_WALLET_PROMPT_SELECTORS", [
      'text=/Connect your wallet to see your positions/i',
      'text=/Authenticate/i',
    ]),
    orderMarketSelectors: envList("VARIATIONAL_BROWSER_MARKET_TAB_SELECTORS", [
      'button:has-text("Market")',
      '[role="tab"]:has-text("Market")',
      'text=/^Market$/',
    ]),
    orderBuySelectors: envList("VARIATIONAL_BROWSER_BUY_SELECTORS", [
      'button:has-text("Buy")',
      '[role="button"]:has-text("Buy")',
      'text=/^Buy/i',
    ]),
    orderSellSelectors: envList("VARIATIONAL_BROWSER_SELL_SELECTORS", [
      'button:has-text("Sell")',
      '[role="button"]:has-text("Sell")',
      'text=/^Sell/i',
    ]),
    orderSizeInputSelectors: envList("VARIATIONAL_BROWSER_SIZE_INPUT_SELECTORS", [
      'input[placeholder*="Size" i]',
      'input[name*="size" i]',
      'input[aria-label*="Size" i]',
      'input[type="number"]',
      '[role="spinbutton"]',
      '[role="textbox"]',
      '[contenteditable="true"]',
      'input',
    ]),
    reduceOnlySelectors: envList("VARIATIONAL_BROWSER_REDUCE_ONLY_SELECTORS", [
      'label:has-text("Reduce Only")',
      '[role="checkbox"]:has-text("Reduce Only")',
      'text=/^Reduce Only$/i',
      'input[type="checkbox"]',
    ]),
    orderInputTimeoutMs: Number(env("VARIATIONAL_BROWSER_ORDER_INPUT_TIMEOUT_MS", "3000")),
    orderSetupDelayMs: Number(env("VARIATIONAL_BROWSER_ORDER_SETUP_DELAY_MS", "1200")),
    orderSideFallbackEnabled: envBool("VARIATIONAL_BROWSER_SIDE_FALLBACK_ENABLED", true),
    orderBuyFallbackPoint: parsePoint(env("VARIATIONAL_BROWSER_BUY_FALLBACK_POINT", "0.82,0.186")),
    orderSellFallbackPoint: parsePoint(env("VARIATIONAL_BROWSER_SELL_FALLBACK_POINT", "0.94,0.186")),
    orderSizeFallbackEnabled: envBool("VARIATIONAL_BROWSER_SIZE_FALLBACK_ENABLED", true),
    orderSizeFallbackPoint: parsePoint(env("VARIATIONAL_BROWSER_SIZE_FALLBACK_POINT", "0.948,0.266")),
    reduceOnlyTimeoutMs: Number(env("VARIATIONAL_BROWSER_REDUCE_ONLY_TIMEOUT_MS", "2000")),
    reduceOnlyFallbackEnabled: envBool("VARIATIONAL_BROWSER_REDUCE_ONLY_FALLBACK_ENABLED", false),
    reduceOnlyFallbackPoint: parsePoint(env("VARIATIONAL_BROWSER_REDUCE_ONLY_FALLBACK_POINT", "0.768,0.340")),
    requireReduceOnlyChecked: envBool("VARIATIONAL_BROWSER_REQUIRE_REDUCE_ONLY_CHECKED", true),
    confirmCandidateMinXRatio: Number(env("VARIATIONAL_BROWSER_CONFIRM_CANDIDATE_MIN_X_RATIO", "0.70")),
    confirmCandidateMinYRatio: Number(env("VARIATIONAL_BROWSER_CONFIRM_CANDIDATE_MIN_Y_RATIO", "0.30")),
    confirmCandidateMaxYRatio: Number(env("VARIATIONAL_BROWSER_CONFIRM_CANDIDATE_MAX_Y_RATIO", "0.60")),
    confirmCandidateLimit: Number(env("VARIATIONAL_BROWSER_CONFIRM_CANDIDATE_LIMIT", "5")),
    confirmFallbackEnabled: envBool("VARIATIONAL_BROWSER_CONFIRM_FALLBACK_ENABLED", false),
    confirmFallbackPoint: parsePoint(env("VARIATIONAL_BROWSER_CONFIRM_FALLBACK_POINT", "0.844,0.431")),
    walletConnectUriSelector: env("VARIATIONAL_BROWSER_WC_URI_SELECTOR"),
    telegramToken: requireEnv("VARIATIONAL_BROWSER_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    telegramChatId: requireEnv("VARIATIONAL_BROWSER_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"),
    telegramAllowedUserIds: envList("VARIATIONAL_BROWSER_TELEGRAM_ALLOWED_USER_IDS", envList("TELEGRAM_ALLOWED_USER_IDS", [])),
    telegramBestEffortTimeoutMs: Number(env("VARIATIONAL_BROWSER_TELEGRAM_BEST_EFFORT_TIMEOUT_SEC", "3")) * 1000,
    runtimeDir,
  };
}

function isUnsafeConfirmSelector(selector) {
  const normalized = String(selector || "").trim().toLowerCase();
  return ["", "body", "html", "*", "main", "#root", "div"].includes(normalized);
}

function parsePoint(value) {
  const [x, y] = String(value).split(/[x,]/i).map((part) => Number(part.trim()));
  return {
    x: Number.isFinite(x) && x > 0 && x < 1 ? x : 0.82,
    y: Number.isFinite(y) && y > 0 && y < 1 ? y : 0.186,
  };
}

function normalizeBaseUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.origin;
  } catch {
    return "https://omni.variational.io";
  }
}

function parseViewport(value) {
  const [width, height] = String(value).toLowerCase().split("x").map((part) => Number(part.trim()));
  return {
    width: Number.isFinite(width) && width > 0 ? width : 1440,
    height: Number.isFinite(height) && height > 0 ? height : 1200,
  };
}

function findWalletConnectUri(text) {
  const match = String(text || "").match(/wc:[^\s"'<>\\]+/);
  if (!match) return "";
  return match[0].replace(/&amp;/g, "&");
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
    connectWallet: args.includes("--connect-wallet"),
    authenticate: args.includes("--authenticate"),
    status: args.includes("--status"),
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

function noPositionForReduceOnlyError(symbol, cause = "") {
  const suffix = cause ? `; reduce-only unavailable: ${cause}` : "";
  const error = new Error(`No open ${symbol} position found while preparing reduce-only close${suffix}`);
  error.code = "NO_POSITION_FOR_REDUCE_ONLY";
  return error;
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
  const pollTelegram = args.approveClick || Boolean(args.request) || args.daemon;
  await gate.start({ pollTelegram });

  try {
    if (args.open) {
      await gate.openOnly(args.url || config.url);
    } else if (args.connectWallet) {
      await gate.connectWallet();
    } else if (args.authenticate) {
      await gate.authenticateCurrentPage();
    } else if (args.status) {
      await gate.statusCurrentPage();
    } else if (args.request) {
      await gate.processRequestFile(resolveRequestPath(args.request));
    } else if (args.approveClick) {
      await gate.approveCurrentPage();
    } else if (args.daemon) {
      await gate.watchRequests();
    } else {
      console.log("Usage:");
      console.log("  npm start -- --open");
      console.log("  npm start -- --connect-wallet");
      console.log("  npm start -- --authenticate");
      console.log("  npm start -- --status");
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

function resolveRequestPath(value) {
  if (path.isAbsolute(value)) return value;

  const fromCwd = path.resolve(process.cwd(), value);
  if (fs.existsSync(fromCwd)) return fromCwd;

  const fromRoot = path.resolve(ROOT, value);
  if (fs.existsSync(fromRoot)) return fromRoot;

  return fromCwd;
}

run().catch((error) => {
  console.error("[variational-browser] fatal:", error);
  process.exit(1);
});
