import React from "react";

export default function AIAdvisorCard({ recommendation }) {
  return (
    <div
      style={{
        background: "#252538",
        borderLeft: "5px solid #2196f3",
        padding: "15px",
        borderRadius: "6px",
        color: "#fff",
        marginBottom: "20px",
      }}
    >
      <h4 style={{ margin: "0 0 10px 0", color: "#64b5f6" }}>
        🤖 AI Security Advisor - Khuyến Nghị Khắc Phục
      </h4>
      <p style={{ margin: 0, fontSize: "14px", lineHeight: "1.5" }}>
        {recommendation ||
          "Chưa phát hiện lỗ hổng nghiêm trọng mới. Hệ thống Web bẫy đang hoạt động ổn định."}
      </p>
    </div>
  );
}
