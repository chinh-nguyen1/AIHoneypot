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

const chartColors = [
  ["rgba(244, 67, 54, 0.85)", "#f44336"],
  ["rgba(255, 152, 0, 0.85)", "#ff9800"],
  ["rgba(76, 175, 80, 0.85)", "#4caf50"],
  ["rgba(96, 125, 139, 0.85)", "#607d8b"],
  ["rgba(156, 39, 176, 0.85)", "#9c27b0"],
];

export function createAttackChartData(stats) {
  const attackCounts = stats.attack_counts;
  const labels = Object.keys(attackCounts);
  const colors = labels.map((_, index) => chartColors[index % chartColors.length]);

  return {
    labels,
    datasets: [
      {
        label: "Số sự kiện theo nhãn phân loại",
        data: labels.map((label) => attackCounts[label]),
        backgroundColor: colors.map(([background]) => background),
        borderColor: colors.map(([, border]) => border),
        borderWidth: 1,
      },
    ],
  };
}

function StatusMessage({ children, error = false }) {
  return (
    <p role={error ? "alert" : "status"} style={{ color: error ? "#ff8a80" : "#aaa" }}>
      {children}
    </p>
  );
}

export default function AttackChart({
  stats,
  loading,
  refreshing,
  error,
}) {
  const chartData =
    stats && stats.attack_counts ? createAttackChartData(stats) : null;
  const hasClassifications = chartData?.labels.length > 0;

  const options = {
    responsive: true,
    plugins: {
      legend: { position: "top", labels: { color: "#ffffff" } },
      title: {
        display: true,
        text: "📊 Phân loại sự kiện theo nhãn AI",
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
        color: "#fff",
      }}
    >
      {error && (
        <StatusMessage error>
          Không thể cập nhật biểu đồ: {error}
          {stats && " Đang hiển thị dữ liệu lần tải thành công trước đó."}
        </StatusMessage>
      )}
      {!error && loading && <StatusMessage>Đang tải dữ liệu biểu đồ...</StatusMessage>}
      {!error && refreshing && (
        <StatusMessage>Đang cập nhật dữ liệu biểu đồ...</StatusMessage>
      )}
      {!stats && error ? null : !stats || loading ? null : !chartData ? (
        <StatusMessage error>
          API thống kê không có dữ liệu phân loại hợp lệ.
        </StatusMessage>
      ) : !hasClassifications ? (
        <StatusMessage>Chưa có sự kiện được phân loại để hiển thị.</StatusMessage>
      ) : (
        <Bar options={options} data={chartData} />
      )}
    </div>
  );
}
