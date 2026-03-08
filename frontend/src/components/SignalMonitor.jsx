export default function SignalMonitor({ status }) {
  if (!status) return null;

  const zscore = status.zscore_current || 0;
  const spread = status.spread_current || 0;

  // Z-score 게이지 (-4 ~ +4 범위)
  const pct = Math.min(Math.max((zscore + 4) / 8, 0), 1) * 100;
  const barColor =
    Math.abs(zscore) >= 2.5
      ? "#ef4444"
      : Math.abs(zscore) >= 1.5
        ? "#f59e0b"
        : "#22c55e";

  return (
    <div style={styles.card}>
      <h3 style={styles.title}>Signal Monitor</h3>

      <div style={styles.row}>
        <span style={styles.label}>Z-Score (5m)</span>
        <span style={{ ...styles.value, color: barColor }}>
          {zscore.toFixed(3)}
        </span>
      </div>

      <div style={styles.barBg}>
        <div
          style={{
            ...styles.barFill,
            width: `${pct}%`,
            background: barColor,
          }}
        />
        <div style={{ ...styles.barCenter }} />
      </div>

      <div style={styles.row}>
        <span style={styles.label}>Spread</span>
        <span style={styles.value}>{(spread * 100).toFixed(4)}%</span>
      </div>

      <div style={styles.row}>
        <span style={styles.label}>Bot Running</span>
        <span
          style={{
            ...styles.value,
            color: status.running ? "#22c55e" : "#888",
          }}
        >
          {status.running ? "ACTIVE" : "STOPPED"}
        </span>
      </div>

      {status.mode && (
        <div style={styles.row}>
          <span style={styles.label}>Mode</span>
          <span style={styles.value}>{status.mode.toUpperCase()}</span>
        </div>
      )}
    </div>
  );
}

const styles = {
  card: {
    background: "#1a1d29",
    borderRadius: "12px",
    padding: "20px",
    display: "flex",
    flexDirection: "column",
    gap: "12px",
  },
  title: {
    color: "#fff",
    margin: 0,
    fontSize: "16px",
  },
  row: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
  },
  label: {
    color: "#888",
    fontSize: "13px",
  },
  value: {
    color: "#fff",
    fontSize: "15px",
    fontWeight: "600",
  },
  barBg: {
    position: "relative",
    height: "8px",
    background: "#0f1117",
    borderRadius: "4px",
    overflow: "hidden",
  },
  barFill: {
    height: "100%",
    borderRadius: "4px",
    transition: "width 0.3s",
  },
  barCenter: {
    position: "absolute",
    left: "50%",
    top: 0,
    width: "2px",
    height: "100%",
    background: "#555",
  },
};
