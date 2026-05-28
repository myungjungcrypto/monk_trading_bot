import fs from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import dotenv from "dotenv";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../..");
const TOOL_DIR = path.join(ROOT, "tools", "variational-browser");

dotenv.config({ path: path.join(ROOT, "backend", ".env") });
dotenv.config({ path: path.join(TOOL_DIR, ".env"), override: true });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const env = (name, fallback = "") => {
  const value = process.env[name];
  return value === undefined || value === "" ? fallback : value;
};
const envBool = (name, fallback = false) => {
  const value = process.env[name];
  if (value === undefined || value === "") return fallback;
  return !["0", "false", "no", "off"].includes(String(value).trim().toLowerCase());
};
const envList = (name) => {
  const value = process.env[name];
  if (!value) return [];
  return value.split(",").map((part) => part.trim()).filter(Boolean);
};

function commandExists(command) {
  const result = spawnSync("sh", ["-lc", `command -v ${command}`], {
    stdio: "ignore",
  });
  return result.status === 0;
}

async function main() {
  const chromeBin = env("VARIATIONAL_CHROME_BIN", env("VARIATIONAL_BROWSER_EXECUTABLE_PATH", "/usr/bin/google-chrome-stable"));
  const profileDir = path.resolve(ROOT, env("VARIATIONAL_BROWSER_PROFILE_DIR", path.join("tools", "variational-browser", "runtime", "profile")));
  const port = env("VARIATIONAL_CHROME_REMOTE_DEBUGGING_PORT", "9222");
  const address = env("VARIATIONAL_CHROME_REMOTE_DEBUGGING_ADDRESS", "127.0.0.1");
  const windowSize = env("VARIATIONAL_CHROME_WINDOW_SIZE", env("VARIATIONAL_BROWSER_VIEWPORT", "1440x1200").replace("x", ","));
  const url = env("VARIATIONAL_CHROME_URL", `${env("VARIATIONAL_BROWSER_BASE_URL", env("VARIATIONAL_BROWSER_URL", "https://omni.variational.io")).replace(/\/$/, "")}/perpetual/BTC`);
  const headless = envBool("VARIATIONAL_CHROME_HEADLESS", false);
  const autoXvfb = envBool("VARIATIONAL_CHROME_AUTO_XVFB", true);
  const xvfbScreen = env("VARIATIONAL_CHROME_XVFB_SCREEN", "1440x1200x24");

  if (!fs.existsSync(chromeBin)) {
    throw new Error(`Chrome binary not found: ${chromeBin}`);
  }

  fs.mkdirSync(profileDir, { recursive: true });

  const childEnv = { ...process.env };
  let xvfb = null;
  if (!headless && !childEnv.DISPLAY) {
    if (!autoXvfb) {
      throw new Error("DISPLAY is empty. Start an X server/noVNC session, or set VARIATIONAL_CHROME_HEADLESS=true.");
    }
    if (!commandExists("Xvfb")) {
      throw new Error("DISPLAY is empty and Xvfb is not installed. Install xorg-x11-server-Xvfb or set VARIATIONAL_CHROME_HEADLESS=true.");
    }
    const display = env("VARIATIONAL_CHROME_DISPLAY", ":99");
    childEnv.DISPLAY = display;
    console.log(`[variational-chrome] starting Xvfb display=${display} screen=${xvfbScreen}`);
    xvfb = spawn("Xvfb", [display, "-screen", "0", xvfbScreen, "-ac"], {
      stdio: "inherit",
      env: childEnv,
    });
    await sleep(1000);
  }

  const args = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    `--remote-debugging-address=${address}`,
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    `--window-size=${windowSize}`,
    ...envList("VARIATIONAL_CHROME_EXTRA_ARGS"),
  ];
  if (headless) {
    args.push("--headless=new");
  }
  args.push(url);

  console.log(`[variational-chrome] launching ${chromeBin}`);
  console.log(`[variational-chrome] CDP endpoint: http://${address}:${port}`);
  console.log(`[variational-chrome] profile: ${profileDir}`);
  console.log(`[variational-chrome] headless: ${headless}`);
  console.log(`[variational-chrome] DISPLAY: ${childEnv.DISPLAY || "(none)"}`);

  const chrome = spawn(chromeBin, args, {
    stdio: "inherit",
    env: childEnv,
  });

  const shutdown = (signal) => {
    console.log(`[variational-chrome] received ${signal}, stopping Chrome`);
    chrome.kill(signal);
    if (xvfb) xvfb.kill(signal);
  };
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));

  chrome.on("exit", (code, signal) => {
    if (xvfb) xvfb.kill("SIGTERM");
    console.log(`[variational-chrome] Chrome exited code=${code} signal=${signal || ""}`);
    process.exit(code ?? (signal ? 1 : 0));
  });
}

main().catch((error) => {
  console.error("[variational-chrome] fatal:", error);
  process.exit(1);
});
