export default function TradeLog({ summary }) {
  if (!summary) return null;

  const cards = [
    { label: "Total Trades", value: summary.total_trades, color: "#fff" },
    { label: "Open", value: summary.open_trades ?? 0, color: "#f59e0b" },
    {
      label: "Total PNL",
      value: `$${summary.total_pnl_usd?.toFixed(2)}`,
      color: (summary.total_pnl_usd || 0) >= 0 ? "#22c55e" : "#ef4444",
    },
    { label: "Wins", value: summary.wins, color: "#22c55e" },
    { label: "Losses", value: summary.losses, color: "#ef4444" },
    {
      label: "Win Rate",
      value: `${summary.win_rate}%`,
      color: "#3b82f6",
    },
  ];

  return (
    <div style={styles.grid}>
      {cards.map((c) => (
        <div key={c.label} style={styles.card}>
          <div style={styles.label}>{c.label}</div>
          <div style={{ ...styles.value, color: c.color }}>{c.value}</div>
        </div>
      ))}
    </div>
  );
}

const styles = {
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
    gap: "12px",
  },
  card: {
    background: "#1a1d29",
    borderRadius: "12px",
    padding: "16px",
    textAlign: "center",
  },
  label: {
    color: "#888",
    fontSize: "12px",
    marginBottom: "6px",
  },
  value: {
    fontSize: "22px",
    fontWeight: "700",
  },
};
