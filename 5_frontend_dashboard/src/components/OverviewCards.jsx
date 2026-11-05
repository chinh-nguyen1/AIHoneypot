import React from "react";

const cardStyle = {
  flex: 1,
  minWidth: "150px",
  background: "#1e1e2f",
  color: "#fff",
  padding: "20px",
  borderRadius: "8px",
};

function StatusMessage({ children, error = false }) {
  return (
    <p role={error ? "alert" : "status"} style={{ color: error ? "#ff8a80" : "#aaa" }}>
      {children}
    </p>
  );
}

export default function OverviewCards({
  stats,
  loading,
  refreshing,
  error,
}) {
  if (!stats && loading) {
    return <StatusMessage>Đang tải thống kê...</StatusMessage>;
  }
  if (!stats && error) {
    return <StatusMessage error>Không thể tải thống kê: {error}</StatusMessage>;
  }
  if (!stats) {
    return <StatusMessage>Chưa có dữ liệu thống kê.</StatusMessage>;
  }

  const cards = [
    ["Tổng số sự kiện", stats.total_logs, "#4caf50"],
    ["Tấn công SQL Injection", stats.sqli_count, "#f44336"],
    ["Tấn công XSS", stats.xss_count, "#ff9800"],
    ["IP duy nhất", stats.unique_ips, "#00bcd4"],
    ["Chưa phân loại", stats.unclassified_count, "#b0bec5"],
  ];

  return (
    <section aria-label="Thống kê tổng quan">
      {error && (
        <StatusMessage error>
          Không thể cập nhật thống kê: {error} Đang hiển thị dữ liệu lần tải
          thành công trước đó.
        </StatusMessage>
      )}
      {!error && refreshing && (
        <StatusMessage>Đang cập nhật thống kê...</StatusMessage>
      )}
      {stats.total_logs === 0 && (
        <StatusMessage>Chưa có sự kiện nào được lưu.</StatusMessage>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "20px", marginBottom: "20px" }}>
        {cards.map(([label, count, color]) => (
          <div key={label} style={cardStyle}>
            <h3>{label}</h3>
            <p style={{ fontSize: "28px", fontWeight: "bold", color }}>
              {count}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}
