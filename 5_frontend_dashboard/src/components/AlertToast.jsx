import React, { useEffect } from "react";

export default function AlertToast({ alertData, onClose }) {
  useEffect(() => {
    if (alertData) {
      const timer = setTimeout(() => {
        onClose();
      }, 6000);
      return () => clearTimeout(timer);
    }
  }, [alertData, onClose]);

  if (!alertData) return null;

  return (
    <div
      style={{
        position: "fixed",
        bottom: "30px",
        right: "30px",
        background: "#dc3545",
        color: "#ffffff",
        padding: "20px 25px",
        borderRadius: "8px",
        boxShadow: "0 4px 20px rgba(220, 53, 69, 0.7)",
        zIndex: 9999,
        maxWidth: "400px",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <h4 style={{ margin: 0, fontSize: "18px" }}>
          🚨 CẢNH BÁO TẤN CÔNG NGUY HIỂM
        </h4>
        <button
          onClick={onClose}
          style={{
            background: "none",
            border: "none",
            color: "#fff",
            fontSize: "18px",
            cursor: "pointer",
          }}
        >
          ✖
        </button>
      </div>
      <hr style={{ borderColor: "rgba(255,255,255,0.3)", margin: "10px 0" }} />
      <p style={{ margin: "5px 0" }}>
        <b>IP Hacker:</b>{" "}
        <code
          style={{
            background: "#fff",
            color: "#000",
            padding: "2px 6px",
            borderRadius: "3px",
          }}
        >
          {alertData.client_ip}
        </code>
      </p>
      <p style={{ margin: "5px 0" }}>
        <b>Loại tấn công:</b> {alertData.attack_type}
      </p>
      <p style={{ margin: "5px 0" }}>
        <b>Điểm rủi ro:</b>{" "}
        <span style={{ fontSize: "18px", fontWeight: "bold" }}>
          {alertData.risk_score}/10
        </span>
      </p>
      <p
        style={{
          margin: "5px 0",
          fontSize: "12px",
          fontStyle: "italic",
          opacity: 0.9,
        }}
      >
        Hệ thống đã tự động gửi Email báo động khẩn cấp tới Admin!
      </p>
    </div>
  );
}
