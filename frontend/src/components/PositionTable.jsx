export default function PositionTable({ trades, onReconcileExternalClose }) {
  if (!trades || trades.length === 0) {
    return <div style={styles.empty}>No trades yet</div>;
  }

  return (
    <div style={styles.card}>
      <h3 style={styles.title}>Recent Trades</h3>
      <div style={styles.tableWrap}>
        <table style={styles.table}>
          <thead>
            <tr>
              <th style={styles.th}>ID</th>
              <th style={styles.th}>Exchange</th>
              <th style={styles.th}>Direction</th>
              <th style={styles.th}>Size</th>
              <th style={styles.th}>Z-Score</th>
              <th style={styles.th}>PNL</th>
              <th style={styles.th}>Costs</th>
              <th style={styles.th}>Exit</th>
              <th style={styles.th}>Time</th>
              <th style={styles.th}>Action</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => {
              const isOpen = !t.closed_at;
              const canReconcile = isOpen && t.exchange === "variational_browser";
              return (
                <tr key={t.id}>
                  <td style={styles.td}>{t.id}</td>
                  <td style={styles.td}>{t.exchange}</td>
                  <td style={styles.td}>
                    <span
                      style={{
                        color: t.direction?.includes("LONG_BTC")
                          ? "#22c55e"
                          : "#ef4444",
                      }}
                    >
                      {t.direction}
                    </span>
                  </td>
                  <td style={styles.td}>${t.size_usd}</td>
                  <td style={styles.td}>{t.zscore_entry?.toFixed(2)}</td>
                  <td
                    style={{
                      ...styles.td,
                      color: isOpen
                        ? "#f59e0b"
                        : (t.net_pnl_usd || 0) >= 0
                          ? "#22c55e"
                          : "#ef4444",
                    }}
                  >
                    {isOpen ? "OPEN" : `$${(t.net_pnl_usd || 0).toFixed(2)}`}
                  </td>
                  <td style={styles.td}>${(t.fees_usd || 0).toFixed(2)}</td>
                  <td style={styles.td}>{isOpen ? "OPEN" : t.exit_reason || "-"}</td>
                  <td style={styles.td}>
                    {t.opened_at
                      ? new Date(t.opened_at).toLocaleString()
                      : "-"}
                  </td>
                  <td style={styles.td}>
                    {canReconcile ? (
                      <button
                        style={styles.reconcileBtn}
                        onClick={() => onReconcileExternalClose?.(t.id)}
                      >
                        Clear External
                      </button>
                    ) : "-"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const styles = {
  card: {
    background: "#1a1d29",
    borderRadius: "12px",
    padding: "20px",
  },
  title: {
    color: "#fff",
    margin: "0 0 16px 0",
    fontSize: "16px",
  },
  tableWrap: {
    overflowX: "auto",
  },
  table: {
    width: "100%",
    borderCollapse: "collapse",
    fontSize: "13px",
  },
  th: {
    color: "#888",
    textAlign: "left",
    padding: "8px 12px",
    borderBottom: "1px solid #333",
    whiteSpace: "nowrap",
  },
  td: {
    color: "#ddd",
    padding: "8px 12px",
    borderBottom: "1px solid #1f2233",
    whiteSpace: "nowrap",
  },
  reconcileBtn: {
    padding: "6px 10px",
    background: "#27272a",
    color: "#fca5a5",
    border: "1px solid #7f1d1d",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "12px",
    fontWeight: 600,
  },
  empty: {
    background: "#1a1d29",
    borderRadius: "12px",
    padding: "40px",
    color: "#666",
    textAlign: "center",
  },
};
