import axios from "axios";

const API_BASE = import.meta.env.VITE_API_URL || "";

const api = axios.create({
  baseURL: API_BASE || window.location.origin,
});

// JWT 토큰 자동 첨부
api.interceptors.request.use((config) => {
  const token = localStorage.getItem("token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// 401 시 로그인 페이지로 리다이렉트
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem("token");
      window.location.href = "/login";
    }
    return Promise.reject(err);
  }
);

// Auth
export const login = (username, password) =>
  api.post("/api/auth/login", new URLSearchParams({ username, password }));

export const getMe = () => api.get("/api/auth/me");

// Bot
export const getBotStatus = () => api.get("/api/bot/status");
export const startBot = (
  trading_mode = "swing",
  paper_trading = true,
  execution_mode = null,
  primary_exchange = null,
) => api.post("/api/bot/start", {
  trading_mode,
  paper_trading,
  execution_mode,
  primary_exchange,
});
export const stopBot = () => api.post("/api/bot/stop");
export const setKillSwitch = (active = true, reason = "") =>
  api.post("/api/bot/kill-switch", { active, reason });
export const manualCloseVariationalTrade = (
  trade_id = null,
  reason = "MANUAL_CLOSE",
  force = false,
) => api.post("/api/bot/manual-close", { trade_id, reason, force });
export const forceFlattenVariational = (reason = "FORCE_FLATTEN", dry_run = null) =>
  api.post("/api/bot/force-flatten-variational", { reason, dry_run });
export const reconcileExternalClose = (trade_id = null, reason = "EXTERNAL_MANUAL_CLOSE") =>
  api.post("/api/bot/reconcile-external-close", { trade_id, reason });
export const testTelegram = () => api.post("/api/bot/test-telegram");

// Config
export const getConfigs = () => api.get("/api/config/");
export const getConfig = (key) => api.get(`/api/config/${key}`);
export const upsertConfig = (config_key, config_val) =>
  api.put("/api/config/", { config_key, config_val });

// Trades
export const getTrades = (limit = 50) =>
  api.get("/api/trades/", { params: { limit } });
export const getTradeSummary = () => api.get("/api/trades/summary");

// PNL
export const getPnlHistory = (limit = 100) =>
  api.get("/api/pnl/", { params: { limit } });

// Health
export const getHealth = () => api.get("/api/health");

// Dashboard WebSocket
export const createDashboardWs = (onMessage) => {
  const base = API_BASE || window.location.origin;
  const wsUrl = base.replace(/^http/, "ws") + "/ws/dashboard";
  const ws = new WebSocket(wsUrl);
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onclose = () => setTimeout(() => createDashboardWs(onMessage), 3000);
  return ws;
};

export default api;
