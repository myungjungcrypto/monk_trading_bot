#!/usr/bin/env node

import { Core } from "@walletconnect/core";
import { formatJsonRpcError, formatJsonRpcResult } from "@walletconnect/jsonrpc-utils";
import { buildApprovedNamespaces, getSdkError } from "@walletconnect/utils";
import { WalletKit } from "@reown/walletkit";
import dotenv from "dotenv";
import dns from "node:dns";
import { ethers } from "ethers";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import readline from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";

const ROOT = path.resolve(new URL("../../../", import.meta.url).pathname);
dotenv.config({ path: path.join(ROOT, "backend", ".env") });
dotenv.config({ path: path.join(ROOT, "tools", "variational-wallet", ".env") });

const DEFAULT_METHODS = [
  "eth_accounts",
  "eth_requestAccounts",
  "personal_sign",
  "eth_sign",
  "eth_signTypedData",
  "eth_signTypedData_v3",
  "eth_signTypedData_v4",
  "eth_signTransaction",
  "eth_sendTransaction",
  "wallet_switchEthereumChain",
];

const DEFAULT_EVENTS = ["accountsChanged", "chainChanged", "message", "disconnect", "connect"];
const DEFAULT_CHAINS = ["eip155:42161"];
const DEFAULT_ARBITRUM_RPC = "https://arb1.arbitrum.io/rpc";

dns.setDefaultResultOrder("ipv4first");

function env(name, fallback = "") {
  return process.env[name]?.trim() || fallback;
}

function envBool(name, fallback = false) {
  const value = env(name);
  if (!value) return fallback;
  return ["1", "true", "yes", "on"].includes(value.toLowerCase());
}

function envList(name, fallback) {
  const value = env(name);
  if (!value) return fallback;
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function requireEnv(name) {
  const value = env(name);
  if (!value) {
    throw new Error(`Missing required env: ${name}`);
  }
  return value;
}

function resolveRootPath(value) {
  if (!value) return "";
  return path.isAbsolute(value) ? value : path.resolve(ROOT, value);
}

class TelegramApprovalClient {
  constructor({ token, chatId, allowedUserIds, timeoutMs, httpTimeoutMs, httpRetries }) {
    this.token = token;
    this.chatId = chatId;
    this.allowedUserIds = new Set(allowedUserIds.map(String));
    this.timeoutMs = timeoutMs;
    this.httpTimeoutMs = httpTimeoutMs;
    this.httpRetries = httpRetries;
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

  async requestApproval({ title, body, approveLabel = "Approve", rejectLabel = "Reject", timeoutMs }) {
    const id = randomId();
    const expiresAt = Date.now() + (timeoutMs ?? this.timeoutMs);
    const text = [
      title,
      "",
      body,
      "",
      `id: ${id}`,
      `expires: ${new Date(expiresAt).toISOString()}`,
    ].join("\n");

    await this.sendMessage(text, [
      [
        { text: approveLabel, callback_data: `wc:${id}:approve` },
        { text: rejectLabel, callback_data: `wc:${id}:reject` },
      ],
      [{ text: "Kill Switch", callback_data: `wc:${id}:kill` }],
    ]);

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
    if (!query?.data?.startsWith("wc:")) return;

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
      await this.sendMessage("[Variational Wallet] KILL SWITCH requested. Exiting.");
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
    const longPollTimeoutMs = method === "getUpdates"
      ? Number(payload?.timeout || 0) * 1000 + 10000
      : 0;
    const timeoutMs = Math.max(this.httpTimeoutMs, longPollTimeoutMs);
    let lastError = null;
    for (let attempt = 1; attempt <= this.httpRetries + 1; attempt += 1) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
          signal: controller.signal,
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) {
          throw new Error(`${method} failed: ${response.status} ${JSON.stringify(data).slice(0, 500)}`);
        }
        return data;
      } catch (error) {
        lastError = error;
        if (attempt > this.httpRetries) break;
        console.warn(`[telegram] ${method} failed; retrying ${attempt}/${this.httpRetries}: ${error.message}`);
        await sleep(Math.min(1000 * attempt, 5000));
      } finally {
        clearTimeout(timer);
      }
    }
    throw lastError;
  }
}

class VariationalWalletBot {
  constructor(config) {
    this.config = config;
    this.provider = new ethers.JsonRpcProvider(config.rpcUrl);
    this.wallet = new ethers.Wallet(config.privateKey, this.provider);
    this.telegram = new TelegramApprovalClient({
      token: config.telegramToken,
      chatId: config.telegramChatId,
      allowedUserIds: config.telegramAllowedUserIds,
      timeoutMs: config.approvalTimeoutMs,
      httpTimeoutMs: config.telegramHttpTimeoutMs,
      httpRetries: config.telegramHttpRetries,
    });
    this.walletKit = null;
    this.seenPairingUris = new Set();
    this.pairingUriFileTimer = null;
  }

  async start(pairingUri) {
    this.telegram.start();
    const address = await this.wallet.getAddress();
    const core = new Core({
      projectId: this.config.projectId,
      customStoragePrefix: this.config.storagePrefix,
    });

    this.walletKit = await WalletKit.init({
      core,
      metadata: {
        name: "Monk Variational Telegram Wallet",
        description: "Telegram approval wallet for a small Variational automation account",
        url: "https://github.com/myungjungcrypto/monk_trading_bot",
        icons: [],
      },
    });

    this.registerHandlers(address);
    const startLines = [
      "[Variational Wallet] started",
      `address: ${address}`,
      `chains: ${this.config.chains.join(", ")}`,
      `dry_run: ${this.config.dryRun}`,
      `send_tx_enabled: ${this.config.allowSendTransaction}`,
    ];
    if (this.config.pairingUriFileEnabled && this.config.pairingUriFile) {
      startLines.push(`pairing_uri_file: ${this.config.pairingUriFile}`);
      startLines.push(`pairing_uri_poll_sec: ${Math.round(this.config.pairingUriPollMs / 1000)}`);
    }
    await this.telegram.sendMessage(startLines.join("\n"));

    if (pairingUri) {
      this.rememberPairingUri(pairingUri);
      await this.pair(pairingUri);
    }
    this.startPairingUriFileWatcher();

    console.log("[wallet] running. Press Ctrl+C to stop.");
    await new Promise(() => {});
  }

  async pair(uri, { source = "manual" } = {}) {
    const normalized = String(uri || "").trim();
    if (!normalized.startsWith("wc:")) {
      throw new Error(`invalid WalletConnect URI from ${source}`);
    }
    console.log(`[wallet] pairing (${source})...`);
    await this.walletKit.pair({ uri: normalized });
  }

  rememberPairingUri(uri) {
    const normalized = String(uri || "").trim();
    if (!normalized) return;
    this.seenPairingUris.add(normalized);
    if (this.seenPairingUris.size > 20) {
      const [oldest] = this.seenPairingUris;
      this.seenPairingUris.delete(oldest);
    }
  }

  startPairingUriFileWatcher() {
    if (!this.config.pairingUriFileEnabled || !this.config.pairingUriFile || this.pairingUriFileTimer) {
      return;
    }

    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const raw = await fs.promises.readFile(this.config.pairingUriFile, "utf8").catch((error) => {
          if (error.code === "ENOENT") return "";
          throw error;
        });
        const uri = raw.trim();
        if (!uri || !uri.startsWith("wc:") || this.seenPairingUris.has(uri)) {
          return;
        }

        this.rememberPairingUri(uri);
        const message = [
          "[Variational Wallet] pairing URI detected",
          `file: ${this.config.pairingUriFile}`,
        ].join("\n");
        await this.telegram.sendMessage(message).catch((error) => {
          console.warn("[telegram] pairing URI notice failed:", error.message);
        });
        await this.pair(uri, { source: "uri_file" });
      } catch (error) {
        console.warn("[wallet] pairing URI file failed:", error.message);
        await this.telegram.sendMessage([
          "[Variational Wallet] pairing URI failed",
          `file: ${this.config.pairingUriFile}`,
          `reason: ${error.message}`,
        ].join("\n")).catch(() => {});
      } finally {
        polling = false;
      }
    };

    this.pairingUriFileTimer = setInterval(() => {
      poll().catch((error) => console.warn("[wallet] pairing URI poll failed:", error.message));
    }, this.config.pairingUriPollMs);
    this.pairingUriFileTimer.unref?.();
    poll().catch((error) => console.warn("[wallet] pairing URI initial poll failed:", error.message));
  }

  registerHandlers(address) {
    this.walletKit.on("session_proposal", async (proposal) => {
      await this.handleSessionProposal(proposal, address);
    });

    this.walletKit.on("session_request", async (event) => {
      await this.handleSessionRequest(event, address);
    });

    this.walletKit.on("session_delete", async (event) => {
      await this.telegram.sendMessage(`[Variational Wallet] session deleted\n${JSON.stringify(event).slice(0, 1000)}`);
    });
  }

  async handleSessionProposal(proposal, address) {
    const peer = proposal.params?.proposer?.metadata || {};
    const body = [
      "Session proposal",
      `peer: ${peer.name || "unknown"}`,
      `url: ${peer.url || "unknown"}`,
      `description: ${peer.description || ""}`,
      `wallet: ${address}`,
      "",
      "required namespaces:",
      JSON.stringify(proposal.params?.requiredNamespaces || {}, null, 2).slice(0, 2500),
      "",
      "Approve this WalletConnect session?",
    ].join("\n");

    const decision = await this.telegram.requestApproval({
      title: "[Variational Wallet] SESSION REQUEST",
      body,
      approveLabel: "Approve Session",
      rejectLabel: "Reject",
      timeoutMs: this.config.sessionApprovalTimeoutMs,
    });

    if (!decision.approved) {
      await this.walletKit.rejectSession({
        id: proposal.id,
        reason: getSdkError("USER_REJECTED"),
      });
      return;
    }

    try {
      const approvedNamespaces = buildApprovedNamespaces({
        proposal: proposal.params,
        supportedNamespaces: {
          eip155: {
            chains: this.config.chains,
            methods: this.config.methods,
            events: this.config.events,
            accounts: this.config.chains.map((chain) => `${chain}:${address}`),
          },
        },
      });
      const session = await this.walletKit.approveSession({
        id: proposal.id,
        namespaces: approvedNamespaces,
      });
      await this.telegram.sendMessage(`[Variational Wallet] session approved\ntopic: ${session.topic}`);
    } catch (error) {
      await this.walletKit.rejectSession({
        id: proposal.id,
        reason: getSdkError("USER_REJECTED_METHODS"),
      });
      await this.telegram.sendMessage(`[Variational Wallet] session approval failed\n${error.message}`);
    }
  }

  async handleSessionRequest(event, address) {
    const { topic, params, id } = event;
    const { request, chainId } = params;
    const method = request.method;

    try {
      this.assertChainAllowed(chainId);
      const result = await this.resolveRequest({ method, params: request.params || [], chainId, address, id });
      await this.walletKit.respondSessionRequest({
        topic,
        response: formatJsonRpcResult(id, result),
      });
      await this.telegram.sendMessage(`[Variational Wallet] request completed\nmethod: ${method}\nid: ${id}`);
    } catch (error) {
      await this.walletKit.respondSessionRequest({
        topic,
        response: formatJsonRpcError(id, error.message || String(error)),
      });
      await this.telegram.sendMessage(`[Variational Wallet] request rejected\nmethod: ${method}\nid: ${id}\nreason: ${error.message}`);
    }
  }

  async resolveRequest({ method, params, chainId, address, id }) {
    if (method === "eth_accounts" || method === "eth_requestAccounts") {
      return [address];
    }
    if (method === "wallet_switchEthereumChain") {
      return null;
    }

    const summary = buildRequestSummary({ method, params, chainId, address, id });
    const decision = await this.telegram.requestApproval({
      title: "[Variational Wallet] SIGN REQUEST",
      body: [
        summary,
        "",
        `dry_run: ${this.config.dryRun}`,
        "Approve signing this request?",
      ].join("\n"),
      approveLabel: "Sign",
      rejectLabel: "Reject",
      timeoutMs: this.config.approvalTimeoutMs,
    });
    if (!decision.approved) {
      throw new Error(`user rejected: ${decision.reason}`);
    }
    if (this.config.dryRun) {
      throw new Error("dry run enabled; set VARIATIONAL_WC_DRY_RUN=false to sign");
    }

    if (method === "personal_sign") {
      return this.signPersonalMessage(params, address);
    }
    if (method === "eth_sign") {
      return this.signEthMessage(params, address);
    }
    if (method === "eth_signTypedData" || method === "eth_signTypedData_v3" || method === "eth_signTypedData_v4") {
      return this.signTypedData(params, address);
    }
    if (method === "eth_signTransaction") {
      const tx = await this.prepareTransaction(params[0], chainId, address);
      return this.wallet.signTransaction(tx);
    }
    if (method === "eth_sendTransaction") {
      if (!this.config.allowSendTransaction) {
        throw new Error("eth_sendTransaction disabled; set VARIATIONAL_WC_ALLOW_SEND_TRANSACTION=true");
      }
      const tx = await this.prepareTransaction(params[0], chainId, address);
      const sent = await this.wallet.sendTransaction(tx);
      return sent.hash;
    }

    throw new Error(`unsupported method: ${method}`);
  }

  signPersonalMessage(params, address) {
    const [message, signer] = params;
    this.assertAddressMatches(signer, address);
    return this.wallet.signMessage(messageToBytesOrString(message));
  }

  signEthMessage(params, address) {
    const [signer, message] = params;
    this.assertAddressMatches(signer, address);
    return this.wallet.signMessage(messageToBytesOrString(message));
  }

  signTypedData(params, address) {
    const [signer, typedDataRaw] = params;
    this.assertAddressMatches(signer, address);
    const typedData = typeof typedDataRaw === "string" ? JSON.parse(typedDataRaw) : typedDataRaw;
    const { domain, message, primaryType } = typedData;
    const types = { ...typedData.types };
    delete types.EIP712Domain;
    if (primaryType && !types[primaryType]) {
      throw new Error(`typed data primaryType not found: ${primaryType}`);
    }
    return this.wallet.signTypedData(domain || {}, types, message);
  }

  async prepareTransaction(rawTx, chainId, address) {
    if (!rawTx || typeof rawTx !== "object") {
      throw new Error("transaction params missing");
    }
    this.assertAddressMatches(rawTx.from, address);
    const tx = { ...rawTx };
    delete tx.from;
    tx.chainId = chainIdToNumber(chainId);
    if (tx.value && BigInt(tx.value) > this.config.maxNativeValueWei) {
      throw new Error(`native value exceeds limit: ${tx.value}`);
    }
    return tx;
  }

  assertChainAllowed(chainId) {
    if (!this.config.chains.includes(chainId)) {
      throw new Error(`chain not allowed: ${chainId}`);
    }
  }

  assertAddressMatches(candidate, address) {
    if (!candidate) return;
    if (ethers.getAddress(candidate) !== ethers.getAddress(address)) {
      throw new Error(`address mismatch: ${candidate}`);
    }
  }
}

function buildRequestSummary({ method, params, chainId, address, id }) {
  const lines = [
    `method: ${method}`,
    `id: ${id}`,
    `chain: ${chainId}`,
    `wallet: ${address}`,
  ];

  if (method === "personal_sign") {
    lines.push("message:", previewMessage(params[0]));
  } else if (method === "eth_sign") {
    lines.push(`signer: ${params[0]}`, "message:", previewMessage(params[1]));
  } else if (method.startsWith("eth_signTypedData")) {
    const typed = typeof params[1] === "string" ? safeJsonParse(params[1]) : params[1];
    lines.push(`signer: ${params[0]}`);
    lines.push(`domain: ${JSON.stringify(typed?.domain || {}).slice(0, 900)}`);
    lines.push(`primaryType: ${typed?.primaryType || ""}`);
    lines.push(`message: ${JSON.stringify(typed?.message || {}).slice(0, 1500)}`);
  } else if (method === "eth_signTransaction" || method === "eth_sendTransaction") {
    lines.push(`tx: ${JSON.stringify(params[0] || {}, null, 2).slice(0, 2200)}`);
  } else {
    lines.push(`params: ${JSON.stringify(params).slice(0, 2200)}`);
  }
  return lines.join("\n");
}

function previewMessage(value) {
  if (typeof value !== "string") {
    return JSON.stringify(value).slice(0, 1200);
  }
  if (ethers.isHexString(value)) {
    try {
      return ethers.toUtf8String(value).slice(0, 1200);
    } catch {
      return `${value.slice(0, 1200)}${value.length > 1200 ? "..." : ""}`;
    }
  }
  return value.slice(0, 1200);
}

function messageToBytesOrString(value) {
  if (typeof value === "string" && ethers.isHexString(value)) {
    return ethers.getBytes(value);
  }
  return String(value);
}

function safeJsonParse(value) {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function chainIdToNumber(chainId) {
  const [, raw] = String(chainId).split(":");
  return Number(raw);
}

function randomId() {
  return Math.random().toString(36).slice(2, 10);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function loadConfig() {
  const privateKeyFile = env("VARIATIONAL_WALLET_PRIVATE_KEY_FILE");
  const privateKey = privateKeyFile
    ? fs.readFileSync(resolveRootPath(privateKeyFile), "utf8").trim()
    : requireEnv("VARIATIONAL_WALLET_PRIVATE_KEY");
  const pairingUriPollSec = Number(env("VARIATIONAL_WC_PAIRING_URI_POLL_SEC", "2"));

  return {
    projectId: requireEnv("WALLETCONNECT_PROJECT_ID"),
    privateKey,
    telegramToken: requireEnv("TELEGRAM_BOT_TOKEN"),
    telegramChatId: requireEnv("TELEGRAM_CHAT_ID"),
    telegramAllowedUserIds: envList("TELEGRAM_ALLOWED_USER_IDS", []),
    approvalTimeoutMs: Number(env("VARIATIONAL_WC_APPROVAL_TIMEOUT_SEC", "45")) * 1000,
    telegramHttpTimeoutMs: Number(env("VARIATIONAL_WC_TELEGRAM_HTTP_TIMEOUT_SEC", "20")) * 1000,
    telegramHttpRetries: Number(env("VARIATIONAL_WC_TELEGRAM_HTTP_RETRIES", "3")),
    sessionApprovalTimeoutMs: Number(env("VARIATIONAL_WC_SESSION_APPROVAL_TIMEOUT_SEC", "120")) * 1000,
    chains: envList("VARIATIONAL_WC_CHAINS", DEFAULT_CHAINS),
    methods: envList("VARIATIONAL_WC_METHODS", DEFAULT_METHODS),
    events: envList("VARIATIONAL_WC_EVENTS", DEFAULT_EVENTS),
    rpcUrl: env("ARBITRUM_RPC_URL", DEFAULT_ARBITRUM_RPC),
    dryRun: envBool("VARIATIONAL_WC_DRY_RUN", true),
    allowSendTransaction: envBool("VARIATIONAL_WC_ALLOW_SEND_TRANSACTION", false),
    maxNativeValueWei: BigInt(env("VARIATIONAL_WC_MAX_NATIVE_VALUE_WEI", "0")),
    storagePrefix: env("VARIATIONAL_WC_STORAGE_PREFIX", "monk-variational-wallet"),
    pairingUriFileEnabled: envBool("VARIATIONAL_WC_PAIRING_URI_FILE_ENABLED", true),
    pairingUriFile: resolveRootPath(env(
      "VARIATIONAL_WC_PAIRING_URI_FILE",
      path.join("tools", "variational-browser", "runtime", "walletconnect_uri.txt"),
    )),
    pairingUriPollMs: Math.max(500, (Number.isFinite(pairingUriPollSec) ? pairingUriPollSec : 2) * 1000),
  };
}

async function readPairingUriFromStdin() {
  const rl = readline.createInterface({ input, output });
  const uri = await rl.question("Paste WalletConnect URI from Variational: ");
  rl.close();
  return uri.trim();
}

async function run() {
  const config = loadConfig();
  const args = process.argv.slice(2);
  let pairingUri = "";
  const uriIndex = args.indexOf("--uri");
  if (uriIndex >= 0) {
    pairingUri = args[uriIndex + 1] || "";
  }
  if (!pairingUri && env("WALLETCONNECT_PAIRING_URI")) {
    pairingUri = env("WALLETCONNECT_PAIRING_URI");
  }
  if (!pairingUri && args.includes("--pair")) {
    pairingUri = await readPairingUriFromStdin();
  }

  const bot = new VariationalWalletBot(config);
  await bot.start(pairingUri);
}

run().catch((error) => {
  console.error("[wallet] fatal:", error);
  process.exit(1);
});
