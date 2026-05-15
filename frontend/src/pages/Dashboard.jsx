import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  getBotStatus,
  getTrades,
  getTradeSummary,
  getPnlHistory,
  startBot,
  stopBot,
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

  // 초기 데이터 로드
  useEffect(() => {
    const load = async () => {
      try {
        const [statusRes, tradesRes, summaryRes, pnlRes] = await Promise.all([
          getBotStatus(),
          getTrades(20),
          getTradeSummary(),
          getPnlHistory(100),
        ]);
        setStatus(statusRes.data);
        setTrades(tradesRes.data);
        setSummary(summaryRes.data);
        setPnlData(pnlRes.data);
      } catch {
        // 401은 interceptor에서 처리
      }
    };
    load();
  }, []);

  // WebSocket 실시간 데이터
  useEffect(() => {
    wsRef.current = createDashboardWs((msg) => {
      if (msg.type === "status") {
        setStatus(msg.data);
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
      const res = await getBotStatus();
      setStatus(res.data);
    } catch (err) {
      alert(err.response?.data?.detail || "Start failed");
    }
    setLoading(false);
  };

  const handleStop = async () => {
    setLoading(true);
    try {
      await stopBot();
      const res = await getBotStatus();
      setStatus(res.data);
    } catch (err) {
      alert(err.response?.data?.detail || "Stop failed");
    }
    setLoading(false);
  };

  const handleLogout = () => {
    localStorage.removeItem("token");
    navigate("/login");
  };

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
                opacity: status?.running ? 0.5 : 1,
              }}
              onClick={() => handleStart(mode)}
              disabled={loading || status?.running}
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
        </div>
      </div>

      {/* Stats */}
      <TradeLog summary={summary} />

      {/* Charts + Signal */}
      <div style={styles.grid2}>
        <PNLChart data={pnlData} />
        <SignalMonitor status={status} />
      </div>

      <SpreadChart data={spreadData} />

      {/* Trades Table */}
      <PositionTable trades={trades} />
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
  grid2: {
    display: "grid",
    gridTemplateColumns: "2fr 1fr",
    gap: "16px",
  },
};
