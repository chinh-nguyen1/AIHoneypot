import React from "react";

function StateMessage({ children, error = false }) {
  return (
    <p
      role={error ? "alert" : "status"}
      style={{ padding: "20px", textAlign: "center", color: error ? "#ff8a80" : "#aaa" }}
    >
      {children}
    </p>
  );
}

export function getEventRowKey(log) {
  return log.id;
}

export default function LiveLogTable({
  logs,
  loading,
  refreshing,
  error,
}) {
  const hasLogs = Array.isArray(logs);
  const rows = hasLogs ? logs : [];

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
      {error && (
        <StateMessage error>
          Không thể cập nhật log: {error}
          {rows.length > 0 && " Đang hiển thị dữ liệu lần tải thành công trước đó."}
        </StateMessage>
      )}
      {!error && refreshing && (
        <StateMessage>Đang cập nhật danh sách sự kiện...</StateMessage>
      )}
      {loading && !hasLogs && (
        <StateMessage>Đang tải sự kiện...</StateMessage>
      )}
      {!loading && !error && !hasLogs && (
        <StateMessage error>Danh sách sự kiện không khả dụng.</StateMessage>
      )}
      {!loading && !error && hasLogs && rows.length === 0 && (
        <StateMessage>Chưa có sự kiện được lưu trong hệ thống.</StateMessage>
      )}
      {rows.length > 0 && (
        <div style={{ overflowX: "auto" }}>
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
                <th style={{ padding: "10px" }}>Event ID</th>
                <th style={{ padding: "10px" }}>IP nguồn</th>
                <th style={{ padding: "10px" }}>Phương thức</th>
                <th style={{ padding: "10px" }}>URI</th>
                <th style={{ padding: "10px" }}>Phân loại AI</th>
                <th style={{ padding: "10px" }}>Confidence</th>
                <th style={{ padding: "10px" }}>Risk Score</th>
                <th style={{ padding: "10px" }}>Thời gian</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((log) => (
                <tr
                  key={getEventRowKey(log)}
                  style={{ borderBottom: "1px solid #2a2a3d" }}
                >
                  <td style={{ padding: "10px", fontFamily: "monospace" }}>
                    {log.id}
                  </td>
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
                            : "#ddd",
                      fontWeight: "bold",
                    }}
                  >
                    {log.classification_status === "UNCLASSIFIED"
                      ? "Chưa phân loại"
                      : log.attack_type || "Phân loại không khả dụng"}
                  </td>
                  <td style={{ padding: "10px" }}>
                    {log.confidence_score === null
                      ? "—"
                      : `${(log.confidence_score * 100).toFixed(1)}%`}
                  </td>
                  <td style={{ padding: "10px", fontWeight: "bold" }}>
                    {log.risk_score === null ? "—" : `${log.risk_score}/10`}
                  </td>
                  <td style={{ padding: "10px", color: "#888" }}>
                    {log.created_at}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
