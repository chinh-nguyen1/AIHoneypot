import React from "react";

export default function LiveLogTable({ logs }) {
  return (
    <div
      style={{
        background: "#1e1e2f",
        padding: "20px",
        borderRadius: "8px",
        color: "#fff",
      }}
    >
      <h3 style={{ marginTop: 0 }}>📋 Luồng Log Tấn Công Thời Gian Thực</h3>
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          textAlign: "left",
          fontSize: "14px",
        }}
      >
        <thead>
          <tr style={{ borderBottom: "2px solid #333", color: "#aaa" }}>
            <th style={{ padding: "10px" }}>IP Hacker</th>
            <th style={{ padding: "10px" }}>Phương thức</th>
            <th style={{ padding: "10px" }}>URI</th>
            <th style={{ padding: "10px" }}>Phân loại AI</th>
            <th style={{ padding: "10px" }}>Risk Score</th>
            <th style={{ padding: "10px" }}>Thời gian</th>
          </tr>
        </thead>
        <tbody>
          {logs && logs.length > 0 ? (
            logs.map((log, index) => (
              <tr key={index} style={{ borderBottom: "1px solid #2a2a3d" }}>
                <td style={{ padding: "10px", fontFamily: "monospace" }}>
                  {log.client_ip}
                </td>
                <td style={{ padding: "10px" }}>{log.http_method}</td>
                <td style={{ padding: "10px" }}>{log.request_uri}</td>
                <td
                  style={{
                    padding: "10px",
                    color:
                      log.attack_type === "SQLi"
                        ? "#f44336"
                        : log.attack_type === "XSS"
                          ? "#ff9800"
                          : "#4caf50",
                    fontWeight: "bold",
                  }}
                >
                  {log.attack_type}
                </td>
                <td style={{ padding: "10px", fontWeight: "bold" }}>
                  {log.risk_score}/10
                </td>
                <td style={{ padding: "10px", color: "#888" }}>
                  {log.created_at}
                </td>
              </tr>
            ))
          ) : (
            <tr>
              <td
                colSpan="6"
                style={{ padding: "20px", textAlign: "center", color: "#666" }}
              >
                Đang chờ nhận log từ Web bẫy...
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
