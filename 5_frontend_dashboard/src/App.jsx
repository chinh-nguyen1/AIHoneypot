import React, { useEffect, useRef, useState } from "react";
import OverviewCards from "./components/OverviewCards";
import AIAdvisorCard from "./components/AIAdvisorCard";
import LiveLogTable from "./components/LiveLogTable";
import AttackChart from "./components/AttackChart";
import ThreatMap from "./components/ThreatMap";
import AlertToast from "./components/AlertToast";
import { fetchDashboardStats, fetchLiveLogs } from "./services/api";

function initialRequestState() {
  return { data: null, loading: true, refreshing: false, error: null };
}

function errorMessage(error) {
  return error instanceof Error ? error.message : "Đã xảy ra lỗi không xác định.";
}

export default function App() {
  const [statsState, setStatsState] = useState(initialRequestState);
  const [logsState, setLogsState] = useState(initialRequestState);
  const [recommendation, setRecommendation] = useState(null);
  const [activeAlert, setActiveAlert] = useState(null);
  const lastAlertEventId = useRef(null);

  useEffect(() => {
    let active = true;

    const fetchLiveData = async () => {
      setStatsState((current) => ({
        ...current,
        loading: current.data === null,
        refreshing: current.data !== null,
      }));
      setLogsState((current) => ({
        ...current,
        loading: current.data === null,
        refreshing: current.data !== null,
      }));

      const [statsResult, logsResult] = await Promise.allSettled([
        fetchDashboardStats(),
        fetchLiveLogs(),
      ]);
      if (!active) return;

      if (statsResult.status === "fulfilled") {
        setStatsState({
          data: statsResult.value,
          loading: false,
          refreshing: false,
          error: null,
        });
      } else {
        setStatsState((current) => ({
          ...current,
          loading: false,
          refreshing: false,
          error: errorMessage(statsResult.reason),
        }));
      }

      if (logsResult.status === "fulfilled") {
        const logs = logsResult.value;
        setLogsState({
          data: logs,
          loading: false,
          refreshing: false,
          error: null,
        });

        const latestLog = logs[0];
        if (latestLog) {
          setRecommendation(
            typeof latestLog.recommendation === "string" &&
              latestLog.recommendation.trim()
              ? latestLog.recommendation
              : null,
          );

          if (
            latestLog.classification_status === "CLASSIFIED" &&
            latestLog.attack_type &&
            typeof latestLog.risk_score === "number"
          ) {
            if (
              latestLog.id !== lastAlertEventId.current &&
              latestLog.risk_score >= 8
            ) {
              setActiveAlert(latestLog);
            }
            lastAlertEventId.current = latestLog.id;
          }
        } else {
          setRecommendation(null);
        }
      } else {
        setRecommendation(null);
        setLogsState((current) => ({
          ...current,
          loading: false,
          refreshing: false,
          error: errorMessage(logsResult.reason),
        }));
      }
    };

    fetchLiveData();
    const interval = setInterval(fetchLiveData, 2000);

    return () => {
      active = false;
      clearInterval(interval);
    };
  }, []);

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

      <OverviewCards
        stats={statsState.data}
        loading={statsState.loading}
        refreshing={statsState.refreshing}
        error={statsState.error}
      />

      <div
        style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "20px" }}
      >
        <AttackChart
          stats={statsState.data}
          loading={statsState.loading}
          refreshing={statsState.refreshing}
          error={statsState.error}
        />
        <ThreatMap locations={[]} />
      </div>

      <AIAdvisorCard recommendation={recommendation} />

      <LiveLogTable
        logs={logsState.data}
        loading={logsState.loading}
        refreshing={logsState.refreshing}
        error={logsState.error}
      />

      <AlertToast
        alertData={activeAlert}
        onClose={() => setActiveAlert(null)}
      />
    </div>
  );
}
