import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getConfigs, upsertConfig } from "../api/client";

const MODE_DEFAULTS = {
  scalp: {
    signal: {
      z_window_5m: 30,
      entry_zscore: 1.5,
      max_zscore: 3.0,
      divergence_threshold_pct: 0.3,
      divergence_lookback: 3,
      peak_revert_ratio: 0.95,
    },
    exit: {
      take_profit_pct: 0.4,
      stop_loss_pct: -1.5,
      zscore_revert_threshold: 0.3,
      min_hold_minutes: 5,
      max_hold_hours: 2,
      zscore_exit_min_pnl_pct: 0.05,
    },
  },
  swing: {
    signal: {
      z_window_5m: 50,
      entry_zscore: 2.0,
      max_zscore: 3.5,
      divergence_threshold_pct: 1.5,
      divergence_lookback: 12,
      peak_revert_ratio: 0.9,
    },
    exit: {
      take_profit_pct: 0.8,
      stop_loss_pct: -3.0,
      zscore_revert_threshold: 1.5,
      max_hold_hours: 12,
      min_hold_minutes: 120,
      zscore_exit_min_pnl_pct: 0.05,
    },
  },
  position: {
    signal: {
      z_window_5m: 100,
      entry_zscore: 2.5,
      max_zscore: 4.0,
      divergence_threshold_pct: 2.0,
      divergence_lookback: 24,
      peak_revert_ratio: 0.85,
    },
    exit: {
      take_profit_pct: 2.0,
      stop_loss_pct: -5.0,
      zscore_revert_threshold: 1.5,
      min_hold_minutes: 240,
      max_hold_hours: 48,
      zscore_exit_min_pnl_pct: 0.05,
    },
  },
};

const EXCHANGE_DEFAULTS = {
  pacifica: {
    enabled: true,
    position_size_usd: 500,
    leverage: 3,
    taker_fee_bps: 2,
    slippage_bps: 1,
  },
  extended: {
    enabled: true,
    position_size_usd: 500,
    leverage: 3,
    taker_fee_bps: 2,
    slippage_bps: 1,
  },
  lighter: {
    enabled: true,
    position_size_usd: 500,
    leverage: 3,
    taker_fee_bps: 0,
    slippage_bps: 1,
  },
  backpack: {
    enabled: true,
    position_size_usd: 500,
    leverage: 3,
    taker_fee_bps: 6,
    slippage_bps: 1,
  },
};

const mergeExchangeDefaults = (saved = {}) =>
  Object.fromEntries(
    Object.entries(EXCHANGE_DEFAULTS).map(([name, defaults]) => [
      name,
      { ...defaults, ...(saved[name] || {}) },
    ])
  );

export default function Settings() {
  const [mode, setMode] = useState("swing");
  const [execution, setExecution] = useState({
    mode: "alert_only",
    primary_exchange: "lighter",
  });
  const [signal, setSignal] = useState(MODE_DEFAULTS.swing.signal);
  const [exit, setExit] = useState(MODE_DEFAULTS.swing.exit);
  const [exchanges, setExchanges] = useState(mergeExchangeDefaults());
  const [risk, setRisk] = useState({
    max_open_trades: 3,
    daily_loss_limit_usd: -200,
  });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const navigate = useNavigate();

  // 서버에서 설정 로드
  useEffect(() => {
    const load = async () => {
      try {
        const res = await getConfigs();
        const configs = {};
        res.data.forEach((c) => (configs[c.config_key] = c.config_val));
        if (configs.mode) setMode(configs.mode.value || "swing");
        if (configs.execution) setExecution(configs.execution);
        if (configs.signal) setSignal(configs.signal);
        if (configs.exit) setExit(configs.exit);
        if (configs.exchanges) setExchanges(mergeExchangeDefaults(configs.exchanges));
        if (configs.risk) setRisk(configs.risk);
      } catch {
        // 첫 실행 시 설정 없음
      }
    };
    load();
  }, []);

  // 모드 변경 시 기본값 적용
  const handleModeChange = (newMode) => {
    setMode(newMode);
    setSignal(MODE_DEFAULTS[newMode].signal);
    setExit(MODE_DEFAULTS[newMode].exit);
  };

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    try {
      await Promise.all([
        upsertConfig("mode", { value: mode }),
        upsertConfig("execution", execution),
        upsertConfig("signal", signal),
        upsertConfig("exit", exit),
        upsertConfig("exchanges", exchanges),
        upsertConfig("risk", risk),
      ]);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      alert("Save failed: " + (err.response?.data?.detail || err.message));
    }
    setSaving(false);
  };

  const numField = (label, obj, key, setter) => (
    <div style={styles.field} key={key}>
      <label style={styles.label}>{label}</label>
      <input
        type="number"
        step="any"
        value={obj[key]}
        onChange={(e) => setter({ ...obj, [key]: parseFloat(e.target.value) || 0 })}
        style={styles.input}
      />
    </div>
  );

  return (
    <div style={styles.container}>
      <header style={styles.header}>
        <h1 style={styles.title}>Settings</h1>
        <button style={styles.backBtn} onClick={() => navigate("/")}>
          Back to Dashboard
        </button>
      </header>

      {/* Mode Selection */}
      <div style={styles.card}>
        <h3 style={styles.cardTitle}>Trading Mode</h3>
        <div style={styles.modeGroup}>
          {["scalp", "swing", "position"].map((m) => (
            <button
              key={m}
              style={{
                ...styles.modeBtn,
                background: mode === m ? "#3b82f6" : "#0f1117",
                border: mode === m ? "1px solid #3b82f6" : "1px solid #333",
              }}
              onClick={() => handleModeChange(m)}
            >
              {m.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {/* Execution */}
      <div style={styles.card}>
        <h3 style={styles.cardTitle}>Execution</h3>
        <div style={styles.fieldGrid}>
          <div style={styles.field}>
            <label style={styles.label}>Mode</label>
            <select
              value={execution.mode}
              onChange={(e) => setExecution({ ...execution, mode: e.target.value })}
              style={styles.input}
            >
              <option value="alert_only">Alert Only</option>
              <option value="paper">Paper</option>
              <option value="live">Live</option>
            </select>
          </div>
          <div style={styles.field}>
            <label style={styles.label}>Primary Exchange</label>
            <select
              value={execution.primary_exchange}
              onChange={(e) => setExecution({ ...execution, primary_exchange: e.target.value })}
              style={styles.input}
            >
              {Object.keys(exchanges).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Signal Parameters */}
      <div style={styles.card}>
        <h3 style={styles.cardTitle}>Signal Parameters</h3>
        <div style={styles.fieldGrid}>
          {numField("Z-Score Window (5m)", signal, "z_window_5m", setSignal)}
          {numField("Entry Z-Score", signal, "entry_zscore", setSignal)}
          {numField("Max Z-Score", signal, "max_zscore", setSignal)}
          {numField("Divergence Threshold %", signal, "divergence_threshold_pct", setSignal)}
          {numField("Divergence Lookback (5m bars)", signal, "divergence_lookback", setSignal)}
          {numField("Peak Revert Ratio", signal, "peak_revert_ratio", setSignal)}
        </div>
      </div>

      {/* Exit Parameters */}
      <div style={styles.card}>
        <h3 style={styles.cardTitle}>Exit Parameters</h3>
        <div style={styles.fieldGrid}>
          {numField("Take Profit %", exit, "take_profit_pct", setExit)}
          {numField("Stop Loss %", exit, "stop_loss_pct", setExit)}
          {numField("Z-Score Revert Threshold", exit, "zscore_revert_threshold", setExit)}
          {numField("Min Hold Minutes", exit, "min_hold_minutes", setExit)}
          {numField("Max Hold Hours (0=off)", exit, "max_hold_hours", setExit)}
          {numField("Z-Score Exit Min PnL %", exit, "zscore_exit_min_pnl_pct", setExit)}
        </div>
      </div>

      {/* Exchange Settings */}
      <div style={styles.card}>
        <h3 style={styles.cardTitle}>Exchanges</h3>
        {Object.entries(exchanges).map(([name, cfg]) => (
          <div key={name} style={styles.exchangeRow}>
            <label style={styles.checkLabel}>
              <input
                type="checkbox"
                checked={cfg.enabled}
                onChange={(e) =>
                  setExchanges({
                    ...exchanges,
                    [name]: { ...cfg, enabled: e.target.checked },
                  })
                }
              />
              <span style={styles.exchangeName}>{name}</span>
            </label>
            <div style={styles.exchangeFields}>
              <label style={styles.miniLabel}>
                Size $
                <input
                  type="number"
                  value={cfg.position_size_usd}
                  onChange={(e) =>
                    setExchanges({
                      ...exchanges,
                      [name]: { ...cfg, position_size_usd: parseFloat(e.target.value) || 0 },
                    })
                  }
                  style={styles.miniInput}
                />
              </label>
              <label style={styles.miniLabel}>
                Leverage
                <input
                  type="number"
                  value={cfg.leverage}
                  onChange={(e) =>
                    setExchanges({
                      ...exchanges,
                      [name]: { ...cfg, leverage: parseInt(e.target.value) || 1 },
                    })
                  }
                  style={styles.miniInput}
                />
              </label>
              <label style={styles.miniLabel}>
                Fee bp
                <input
                  type="number"
                  step="any"
                  value={cfg.taker_fee_bps}
                  onChange={(e) =>
                    setExchanges({
                      ...exchanges,
                      [name]: { ...cfg, taker_fee_bps: parseFloat(e.target.value) || 0 },
                    })
                  }
                  style={styles.miniInput}
                />
              </label>
              <label style={styles.miniLabel}>
                Slip bp
                <input
                  type="number"
                  step="any"
                  value={cfg.slippage_bps}
                  onChange={(e) =>
                    setExchanges({
                      ...exchanges,
                      [name]: { ...cfg, slippage_bps: parseFloat(e.target.value) || 0 },
                    })
                  }
                  style={styles.miniInput}
                />
              </label>
            </div>
          </div>
        ))}
      </div>

      {/* Risk */}
      <div style={styles.card}>
        <h3 style={styles.cardTitle}>Risk Management</h3>
        <div style={styles.fieldGrid}>
          {numField("Max Open Trades", risk, "max_open_trades", setRisk)}
          {numField("Daily Loss Limit $", risk, "daily_loss_limit_usd", setRisk)}
        </div>
      </div>

      {/* Save */}
      <button
        style={styles.saveBtn}
        onClick={handleSave}
        disabled={saving}
      >
        {saving ? "Saving..." : saved ? "Saved!" : "Save Settings"}
      </button>
    </div>
  );
}

const styles = {
  container: {
    maxWidth: "800px",
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
  },
  title: { color: "#fff", margin: 0, fontSize: "20px" },
  backBtn: {
    padding: "8px 16px",
    background: "#1a1d29",
    color: "#ddd",
    border: "1px solid #333",
    borderRadius: "8px",
    cursor: "pointer",
    fontSize: "13px",
  },
  card: {
    background: "#1a1d29",
    borderRadius: "8px",
    padding: "20px",
  },
  cardTitle: {
    color: "#fff",
    margin: "0 0 16px 0",
    fontSize: "15px",
  },
  modeGroup: {
    display: "flex",
    gap: "8px",
  },
  modeBtn: {
    flex: 1,
    padding: "10px",
    color: "#fff",
    borderRadius: "8px",
    cursor: "pointer",
    fontWeight: "600",
    fontSize: "14px",
  },
  fieldGrid: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: "12px",
  },
  field: {
    display: "flex",
    flexDirection: "column",
    gap: "4px",
  },
  label: {
    color: "#888",
    fontSize: "12px",
  },
  input: {
    padding: "8px 12px",
    borderRadius: "6px",
    border: "1px solid #333",
    background: "#0f1117",
    color: "#fff",
    fontSize: "14px",
    outline: "none",
  },
  exchangeRow: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    padding: "8px 0",
    borderBottom: "1px solid #222",
    gap: "12px",
    flexWrap: "wrap",
  },
  checkLabel: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    color: "#ddd",
  },
  exchangeName: {
    fontSize: "14px",
    textTransform: "capitalize",
  },
  exchangeFields: {
    display: "flex",
    gap: "12px",
    flexWrap: "wrap",
    justifyContent: "flex-end",
  },
  miniLabel: {
    color: "#888",
    fontSize: "12px",
    display: "flex",
    alignItems: "center",
    gap: "6px",
  },
  miniInput: {
    width: "70px",
    padding: "4px 8px",
    borderRadius: "4px",
    border: "1px solid #333",
    background: "#0f1117",
    color: "#fff",
    fontSize: "13px",
    outline: "none",
  },
  saveBtn: {
    padding: "14px",
    background: "#3b82f6",
    color: "#fff",
    border: "none",
    borderRadius: "8px",
    cursor: "pointer",
    fontWeight: "700",
    fontSize: "16px",
  },
};
