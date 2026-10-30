import React from "react";

export default function OverviewCards({ stats }) {
  return (
    <div style={{ display: "flex", gap: "20px", marginBottom: "20px" }}>
      <div
        style={{
          flex: 1,
          background: "#1e1e2f",
          color: "#fff",
          padding: "20px",
          borderRadius: "8px",
        }}
      >
        <h3>Tổng số Request</h3>
        <p style={{ fontSize: "28px", fontWeight: "bold", color: "#4caf50" }}>
          {stats?.total_logs || 0}
        </p>
      </div>
      <div
        style={{
          flex: 1,
          background: "#1e1e2f",
          color: "#fff",
          padding: "20px",
          borderRadius: "8px",
        }}
      >
        <h3>Tấn công SQL Injection</h3>
        <p style={{ fontSize: "28px", fontWeight: "bold", color: "#f44336" }}>
          {stats?.sqli_count || 0}
        </p>
      </div>
      <div
        style={{
          flex: 1,
          background: "#1e1e2f",
          color: "#fff",
          padding: "20px",
          borderRadius: "8px",
        }}
      >
        <h3>Tấn công XSS</h3>
        <p style={{ fontSize: "28px", fontWeight: "bold", color: "#ff9800" }}>
          {stats?.xss_count || 0}
        </p>
      </div>
      <div
        style={{
          flex: 1,
          background: "#1e1e2f",
          color: "#fff",
          padding: "20px",
          borderRadius: "8px",
        }}
      >
        <h3>IP Độc hại</h3>
        <p style={{ fontSize: "28px", fontWeight: "bold", color: "#00bcd4" }}>
          {stats?.unique_ips || 0}
        </p>
      </div>
    </div>
  );
}
