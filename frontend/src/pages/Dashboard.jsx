import { useCallback, useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  getBotStatus,
  getTrades,
  getTradeSummary,
  getPnlHistory,
  startBot,
  stopBot,
  setKillSwitch,
  manualCloseVariationalTrade,
  forceFlattenVariational,
  reconcileExternalClose,
  createDashboardWs,
} from "../api/client";
import PNLChart from "../components/PNLChart";
import PositionTable from "../components/PositionTable";
import TradeLog from "../components/TradeLog";
import SignalMonitor from "../components/SignalMonitor";
import SpreadChart from "../components/SpreadChart";

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [trades, setTrades] = useState([]);
  const [summary, setSummary] = useState(null);
  const [pnlData, setPnlData] = useState([]);
  const [spreadData, setSpreadData] = useState([]);
  const [loading, setLoading] = useState(false);
  const wsRef = useRef(null);
  const navigate = useNavigate();

  const loadRuntimeData = useCallback(async () => {
    try {
      const [statusRes, tradesRes, summaryRes, pnlRes] = await Promise.all([
        getBotStatus(),
        getTrades(50),
        getTradeSummary(),
        getPnlHistory(200),
      ]);
      setStatus(statusRes.data);
      setTrades(tradesRes.data);
      setSummary(summaryRes.data);
      setPnlData(pnlRes.data);
    } catch {
      // 401은 interceptor에서 처리
    }
  }, []);

  // 초기 데이터 로드 + DB 기반 거래/PNL 주기 갱신
  useEffect(() => {
    const initialId = setTimeout(loadRuntimeData, 0);
    const refreshId = setInterval(loadRuntimeData, 15000);
    return () => {
      clearTimeout(initialId);
      clearInterval(refreshId);
    };
  }, [loadRuntimeData]);

  // WebSocket 실시간 데이터
  useEffect(() => {
    wsRef.current = createDashboardWs((msg) => {
      if (msg.type === "status") {
        setStatus((prev) => ({
          ...msg.data,
          kill_switch: msg.data.kill_switch ?? prev?.kill_switch,
        }));
      }
      if (msg.type === "spread") {
        setSpreadData((prev) => [...prev.slice(-200), msg.data]);
      }
    });
    return () => wsRef.current?.close();
  }, []);

  const handleStart = async (mode) => {
    setLoading(true);
    try {
      await startBot(mode);
      await loadRuntimeData();
    } catch (err) {
      alert(err.response?.data?.detail || "Start failed");
    }
    setLoading(false);
  };

  const handleStop = async () => {
    setLoading(true);
    try {
      await stopBot();
      await loadRuntimeData();
    } catch (err) {
      alert(err.response?.data?.detail || "Stop failed");
    }
    setLoading(false);
  };

  const handleKillSwitch = async () => {
    const ok = window.confirm(
      "Emergency stop will stop the bot and block Variational browser clicks. Continue?"
    );
    if (!ok) return;
    setLoading(true);
    try {
      await setKillSwitch(true, "Dashboard emergency stop");
      await loadRuntimeData();
    } catch (err) {
      alert(err.response?.data?.detail || "Emergency stop failed");
    }
    setLoading(false);
  };

  const handleClearKillSwitch = async () => {
    const ok = window.confirm("Clear the emergency kill switch? The bot will not start automatically.");
    if (!ok) return;
    setLoading(true);
    try {
      await setKillSwitch(false, "Dashboard reset");
      await loadRuntimeData();
    } catch (err) {
      alert(err.response?.data?.detail || "Reset failed");
    }
    setLoading(false);
  };

  const handleReconcileExternalClose = async (tradeId) => {
    const ok = window.confirm(
      `Only use this after the real Variational position for DB trade #${tradeId} is already closed. Clear the dashboard/DB position?`
    );
    if (!ok) return;
    setLoading(true);
    try {
      await reconcileExternalClose(tradeId, "EXTERNAL_MANUAL_CLOSE");
      await loadRuntimeData();
    } catch (err) {
      alert(err.response?.data?.detail || "Reconcile failed");
    }
    setLoading(false);
  };

  const handleManualClose = async (tradeId) => {
    const ok = window.confirm(
      `This will queue a real reduce-only Variational close for DB trade #${tradeId}. Continue?`
    );
    if (!ok) return;
    setLoading(true);
    try {
      await manualCloseVariationalTrade(tradeId, "MANUAL_DASHBOARD_CLOSE", true);
      await loadRuntimeData();
    } catch (err) {
      alert(err.response?.data?.detail || "Manual close failed");
    }
    setLoading(false);
  };

  const handleForceFlatten = async () => {
    const ok = window.confirm(
      "This will queue a real Variational Close All request from the live Positions table. Use only when the pair close path is stuck. Continue?"
    );
    if (!ok) return;
    setLoading(true);
    try {
      await forceFlattenVariational("DASHBOARD_FORCE_FLATTEN", false);
      await loadRuntimeData();
    } catch (err) {
      alert(err.response?.data?.detail || "Force flatten failed");
    }
    setLoading(false);
  };

  const handleLogout = () => {
    localStorage.removeItem("token");
    navigate("/login");
  };

  const killSwitchActive = Boolean(status?.kill_switch?.active);

  return (
    <div style={styles.container}>
      {/* Header */}
      <header style={styles.header}>
        <h1 style={styles.logo}>Monk Trading Bot</h1>
        <div style={styles.headerRight}>
          <button
            style={styles.navBtn}
            onClick={() => navigate("/settings")}
          >
            Settings
          </button>
          <button style={styles.logoutBtn} onClick={handleLogout}>
            Logout
          </button>
        </div>
      </header>

      {/* Bot Controls */}
      <div style={styles.controls}>
        <span style={styles.statusLabel}>
          Status:{" "}
          <span
            style={{
              color: status?.running ? "#22c55e" : "#888",
              fontWeight: 700,
            }}
          >
            {status?.running ? "RUNNING" : "STOPPED"}
          </span>
        </span>

        <div style={styles.btnGroup}>
          {["scalp", "swing", "position"].map((mode) => (
            <button
              key={mode}
              style={{
                ...styles.startBtn,
                opacity: status?.running || killSwitchActive ? 0.5 : 1,
              }}
              onClick={() => handleStart(mode)}
              disabled={loading || status?.running || killSwitchActive}
            >
              Start {mode}
            </button>
          ))}
          <button
            style={styles.stopBtn}
            onClick={handleStop}
            disabled={loading || !status?.running}
          >
            Stop
          </button>
          <button
            style={styles.killBtn}
            onClick={handleKillSwitch}
            disabled={loading || killSwitchActive}
          >
            Kill Switch
          </button>
          <button
            style={{
              ...styles.resetKillBtn,
              opacity: killSwitchActive ? 1 : 0.45,
            }}
            onClick={handleClearKillSwitch}
            disabled={loading || !killSwitchActive}
          >
            Reset Kill
          </button>
          <button
            style={styles.flattenBtn}
            onClick={handleForceFlatten}
            disabled={loading}
          >
            Force Flatten
          </button>
        </div>
      </div>

      {killSwitchActive && (
        <div style={styles.killBanner}>
          <strong>Emergency Kill Switch Active</strong>
          <span>
            Bot start and normal Variational requests are blocked. Force Flatten is still allowed for emergency close-all.
            {status?.kill_switch?.reason ? ` Reason: ${status.kill_switch.reason}` : ""}
          </span>
        </div>
      )}

      {/* Stats */}
      <TradeLog summary={summary} />

      {/* Charts + Signal */}
      <div style={styles.grid2}>
        <PNLChart data={pnlData} />
        <SignalMonitor status={status} />
      </div>

      <SpreadChart data={spreadData} />

      {/* Trades Table */}
      <PositionTable
        trades={trades}
        onManualClose={handleManualClose}
        onReconcileExternalClose={handleReconcileExternalClose}
      />
    </div>
  );
}

const styles = {
  container: {
    maxWidth: "1200px",
    margin: "0 auto",
    padding: "20px",
    display: "flex",
    flexDirection: "column",
    gap: "16px",
    minHeight: "100vh",
    background: "#0f1117",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    padding: "8px 0",
  },
  logo: {
    color: "#fff",
    fontSize: "20px",
    margin: 0,
  },
  headerRight: {
    display: "flex",
    gap: "8px",
  },
  navBtn: {
    padding: "8px 16px",
    background: "#1a1d29",
    color: "#ddd",
    border: "1px solid #333",
    borderRadius: "8px",
    cursor: "pointer",
    fontSize: "13px",
  },
  logoutBtn: {
    padding: "8px 16px",
    background: "transparent",
    color: "#888",
    border: "1px solid #333",
    borderRadius: "8px",
    cursor: "pointer",
    fontSize: "13px",
  },
  controls: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    background: "#1a1d29",
    padding: "16px 20px",
    borderRadius: "12px",
    flexWrap: "wrap",
    gap: "12px",
  },
  statusLabel: {
    color: "#ddd",
    fontSize: "15px",
  },
  btnGroup: {
    display: "flex",
    gap: "8px",
    flexWrap: "wrap",
  },
  startBtn: {
    padding: "8px 16px",
    background: "#22c55e",
    color: "#fff",
    border: "none",
    borderRadius: "8px",
    cursor: "pointer",
    fontWeight: "600",
    fontSize: "13px",
  },
  stopBtn: {
    padding: "8px 16px",
    background: "#ef4444",
    color: "#fff",
    border: "none",
    borderRadius: "8px",
    cursor: "pointer",
    fontWeight: "600",
    fontSize: "13px",
  },
  killBtn: {
    padding: "8px 16px",
    background: "#991b1b",
    color: "#fff",
    border: "1px solid #ef4444",
    borderRadius: "8px",
    cursor: "pointer",
    fontWeight: "700",
    fontSize: "13px",
  },
  resetKillBtn: {
    padding: "8px 16px",
    background: "#27272a",
    color: "#fca5a5",
    border: "1px solid #7f1d1d",
    borderRadius: "8px",
    cursor: "pointer",
    fontWeight: "600",
    fontSize: "13px",
  },
  flattenBtn: {
    padding: "8px 16px",
    background: "#7c2d12",
    color: "#fed7aa",
    border: "1px solid #ea580c",
    borderRadius: "8px",
    cursor: "pointer",
    fontWeight: "700",
    fontSize: "13px",
  },
  killBanner: {
    display: "flex",
    flexDirection: "column",
    gap: "4px",
    padding: "12px 16px",
    background: "#2a1216",
    color: "#fecaca",
    border: "1px solid #7f1d1d",
    borderRadius: "8px",
    fontSize: "13px",
  },
  grid2: {
    display: "grid",
    gridTemplateColumns: "2fr 1fr",
    gap: "16px",
  },
};
