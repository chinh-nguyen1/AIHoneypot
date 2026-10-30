import React, { useState, useEffect } from "react";
import OverviewCards from "./components/OverviewCards";
import AIAdvisorCard from "./components/AIAdvisorCard";
import LiveLogTable from "./components/LiveLogTable";
import AttackChart from "./components/AttackChart";
import ThreatMap from "./components/ThreatMap";
import AlertToast from "./components/AlertToast";

export default function App() {
  // Khởi tạo state rỗng ban đầu (sẽ được tự động lấp đầy từ CSDL)
  const [stats, setStats] = useState({
    total_logs: 0,
    sqli_count: 0,
    xss_count: 0,
    normal_count: 0,
    unique_ips: 0,
  });
  const [logs, setLogs] = useState([]);
  const [recommendation, setRecommendation] = useState(
    "Hệ thống đang sẵn sàng giám sát và tiếp nhận phân tích từ AI...",
  );
  const [activeAlert, setActiveAlert] = useState(null);
  const [lastAlertTime, setLastAlertTime] = useState(null);

  useEffect(() => {
    // Hàm lấy dữ liệu thời gian thực từ AI Engine Backend
    const fetchLiveData = async () => {
      try {
        // 1. Tải thẻ số liệu thống kê tổng hợp
        const resStats = await fetch("http://localhost:8000/stats");
        if (resStats.ok) {
          const dataStats = await resStats.json();
          setStats(dataStats);
        }

        // 2. Tải danh sách log mới nhất từ CSDL
        const resLogs = await fetch("http://localhost:8000/logs/live");
        if (resLogs.ok) {
          const dataLogs = await resLogs.json();
          setLogs(dataLogs);

          // Nếu có log mới và điểm rủi ro cao (>= 8/10) -> Kích hoạt Popup Cảnh báo
          if (dataLogs && dataLogs.length > 0) {
            const latestLog = dataLogs[0];

            // Chỉ bật Toast nếu là cuộc tấn công mới chưa được báo trước đó
            if (
              latestLog.risk_score >= 8 &&
              latestLog.created_at !== lastAlertTime
            ) {
              setActiveAlert({
                client_ip: latestLog.client_ip,
                attack_type: latestLog.attack_type,
                risk_score: latestLog.risk_score,
              });
              setLastAlertTime(latestLog.created_at);
            }

            // Cập nhật lời khuyên vá lỗi mới nhất từ AI
            if (latestLog.recommendation) {
              setRecommendation(latestLog.recommendation);
            }
          }
        }
      } catch (error) {
        console.error(
          "Lỗi kết nối tới Server Backend (http://localhost:8000):",
          error,
        );
      }
    };

    fetchLiveData(); // Tải ngay lập tức khi vừa vào trang
    const interval = setInterval(fetchLiveData, 2000); // Tự động làm mới dữ liệu mỗi 2 giây

    return () => clearInterval(interval);
  }, [lastAlertTime]);

  return (
    <div
      style={{
        background: "#12121c",
        minHeight: "100vh",
        padding: "30px",
        fontFamily: "Arial, sans-serif",
      }}
    >
      <header
        style={{
          marginBottom: "30px",
          borderBottom: "1px solid #2d2d44",
          paddingBottom: "15px",
        }}
      >
        <h1 style={{ color: "#fff", margin: 0 }}>
          🛡️ AI-WEBHONEYPOT SOC DASHBOARD
        </h1>
        <p style={{ color: "#888", margin: "5px 0 0 0" }}>
          Giám sát bẫy Web và Phân tích Mối đe dọa Thời gian thực
        </p>
      </header>

      {/* Thẻ thống kê tổng quan */}
      <OverviewCards stats={stats} />

      {/* Biểu đồ phân bổ tấn công và Bản đồ IP */}
      <div
        style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "20px" }}
      >
        <AttackChart stats={stats} />
        <ThreatMap threatIps={[]} />
      </div>

      {/* Khuyến nghị vá lỗi từ Gemini LLM AI */}
      <AIAdvisorCard recommendation={recommendation} />

      {/* Bảng Log thời gian thực */}
      <LiveLogTable logs={logs} />

      {/* Popup Cảnh báo khẩn khi Risk Score >= 8 */}
      <AlertToast
        alertData={activeAlert}
        onClose={() => setActiveAlert(null)}
      />
    </div>
  );
}
