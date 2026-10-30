import React from "react";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend,
} from "chart.js";
import { Bar } from "react-chartjs-2";

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend,
);

export default function AttackChart({ stats }) {
  const data = {
    labels: ["SQL Injection", "Reflected/Stored XSS", "Request Bình Thường"],
    datasets: [
      {
        label: "Số Lượng Request Bắt Được",
        data: [
          stats?.sqli_count || 5,
          stats?.xss_count || 3,
          stats?.normal_count || 4,
        ],
        backgroundColor: [
          "rgba(244, 67, 54, 0.85)", // Đỏ cho SQLi
          "rgba(255, 152, 0, 0.85)", // Cam cho XSS
          "rgba(76, 175, 80, 0.85)", // Xanh lá cho Normal
        ],
        borderColor: ["#f44336", "#ff9800", "#4caf50"],
        borderWidth: 1,
      },
    ],
  };

  const options = {
    responsive: true,
    plugins: {
      legend: { position: "top", labels: { color: "#ffffff" } },
      title: {
        display: true,
        text: "📊 Biểu Đồ Thống Kê Phân Loại Tấn Công Theo AI",
        color: "#ffffff",
        font: { size: 16 },
      },
    },
    scales: {
      x: { ticks: { color: "#cccccc" }, grid: { color: "#333344" } },
      y: { ticks: { color: "#cccccc" }, grid: { color: "#333344" } },
    },
  };

  return (
    <div
      style={{
        background: "#1e1e2f",
        padding: "20px",
        borderRadius: "8px",
        marginBottom: "20px",
      }}
    >
      <Bar options={options} data={data} />
    </div>
  );
}
