import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { login } from "../api/client";

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await login(username, password);
      localStorage.setItem("token", res.data.access_token);
      navigate("/");
    } catch (err) {
      setError(err.response?.data?.detail || "Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={styles.container}>
      <form onSubmit={handleSubmit} style={styles.form}>
        <h1 style={styles.title}>Monk Trading Bot</h1>
        <p style={styles.subtitle}>BTC/ETH Pair Trading</p>

        {error && <div style={styles.error}>{error}</div>}

        <input
          type="text"
          placeholder="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          style={styles.input}
          autoFocus
        />
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          style={styles.input}
        />
        <button type="submit" style={styles.button} disabled={loading}>
          {loading ? "Logging in..." : "Login"}
        </button>
      </form>
    </div>
  );
}

const styles = {
  container: {
    display: "flex",
    justifyContent: "center",
    alignItems: "center",
    minHeight: "100vh",
    background: "#0f1117",
  },
  form: {
    background: "#1a1d29",
    padding: "40px",
    borderRadius: "12px",
    width: "360px",
    display: "flex",
    flexDirection: "column",
    gap: "16px",
  },
  title: {
    color: "#fff",
    margin: 0,
    textAlign: "center",
    fontSize: "24px",
  },
  subtitle: {
    color: "#888",
    margin: 0,
    textAlign: "center",
    fontSize: "14px",
  },
  input: {
    padding: "12px",
    borderRadius: "8px",
    border: "1px solid #333",
    background: "#0f1117",
    color: "#fff",
    fontSize: "14px",
    outline: "none",
  },
  button: {
    padding: "12px",
    borderRadius: "8px",
    border: "none",
    background: "#3b82f6",
    color: "#fff",
    fontSize: "16px",
    cursor: "pointer",
    fontWeight: "600",
  },
  error: {
    color: "#ef4444",
    fontSize: "14px",
    textAlign: "center",
    padding: "8px",
    background: "#1f1215",
    borderRadius: "6px",
  },
};
