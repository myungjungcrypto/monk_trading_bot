import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

export default function PNLChart({ data }) {
  if (!data || data.length === 0) {
    return <div style={styles.empty}>No PNL data yet</div>;
  }

  const formatted = data.map((d) => ({
    ...d,
    time: d.snapshot_at ? new Date(d.snapshot_at).toLocaleTimeString() : "",
  }));

  return (
    <div style={styles.card}>
      <h3 style={styles.title}>Cumulative PNL</h3>
      <ResponsiveContainer width="100%" height={250}>
        <LineChart data={formatted}>
          <CartesianGrid strokeDasharray="3 3" stroke="#333" />
          <XAxis dataKey="time" stroke="#666" fontSize={11} />
          <YAxis stroke="#666" fontSize={11} />
          <Tooltip
            contentStyle={{ background: "#1a1d29", border: "1px solid #333" }}
          />
          <Line
            type="monotone"
            dataKey="cumulative_pnl"
            stroke="#22c55e"
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
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
  empty: {
    background: "#1a1d29",
    borderRadius: "12px",
    padding: "40px",
    color: "#666",
    textAlign: "center",
  },
};
