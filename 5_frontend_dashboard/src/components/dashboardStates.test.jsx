import React from "react";
import { act } from "react-dom/test-utils";
import { createRoot } from "react-dom/client";
import OverviewCards from "./OverviewCards";
import AttackChart, { createAttackChartData } from "./AttackChart";
import LiveLogTable, { getEventRowKey } from "./LiveLogTable";
import ThreatMap from "./ThreatMap";
import AIAdvisorCard from "./AIAdvisorCard";

jest.mock("react-chartjs-2", () => ({
  Bar: () => <div data-testid="attack-chart" />,
}));
jest.mock("react-leaflet", () => ({
  MapContainer: () => null,
  TileLayer: () => null,
  Marker: () => null,
  Popup: () => null,
}));
jest.mock("leaflet", () => ({
  Icon: {
    Default: {
      prototype: {},
      mergeOptions: jest.fn(),
    },
  },
}));

const stats = {
  total_logs: 0,
  classified_count: 0,
  unclassified_count: 0,
  attack_counts: {},
  sqli_count: 0,
  xss_count: 0,
  normal_count: 0,
  unique_ips: 0,
};

function renderText(element) {
  const container = document.createElement("div");
  const root = createRoot(container);
  act(() => root.render(element));
  const text = container.textContent;
  act(() => root.unmount());
  return text;
}

describe("Dashboard states and real values", () => {
  test("statistics with zero values remain zero", () => {
    const text = renderText(<OverviewCards stats={stats} />);
    expect(text).toContain("Tổng số sự kiện0");
    expect(text).toContain("Tấn công SQL Injection0");
    expect(text).toContain("Chưa phân loại0");
  });

  test("chart has no demo fallback and preserves unknown labels", () => {
    expect(createAttackChartData(stats).labels).toEqual([]);
    const data = createAttackChartData({
      ...stats,
      attack_counts: { Normal: 0, Suspicious: 2 },
    });
    expect(data.labels).toEqual(["Normal", "Suspicious"]);
    expect(data.datasets[0].data).toEqual([0, 2]);
  });

  test("loading, error, and empty log states are distinct", () => {
    expect(
      renderText(<LiveLogTable logs={null} loading />),
    ).toContain("Đang tải sự kiện");
    expect(
      renderText(<LiveLogTable logs={null} error="API unavailable" />),
    ).toContain("Không thể cập nhật log");
    expect(
      renderText(<LiveLogTable logs={[]} loading={false} />),
    ).toContain("Chưa có sự kiện");
  });

  test("uses stable event IDs and presents absent classification explicitly", () => {
    const event = {
      id: 42,
      created_at: "2026-11-01T04:00:00",
      client_ip: "192.0.2.10",
      http_method: "GET",
      request_uri: "/",
      attack_type: null,
      confidence_score: null,
      risk_score: null,
      classification_status: "UNCLASSIFIED",
    };
    expect(getEventRowKey(event)).toBe(42);
    const text = renderText(<LiveLogTable logs={[event]} loading={false} />);
    expect(text).toContain("Chưa phân loại");
    expect(text).toContain("2026-11-01T04:00:00");
    expect(text).not.toContain("/10");

    const incompleteText = renderText(
      <LiveLogTable
        logs={[
          {
            ...event,
            classification_status: "CLASSIFIED",
          },
        ]}
        loading={false}
      />,
    );
    expect(incompleteText).toContain("Phân loại không khả dụng");
  });

  test("threat map shows unavailable state with no verified locations", () => {
    const text = renderText(<ThreatMap locations={[]} />);
    expect(text).toContain("Dữ liệu vị trí địa lý đã xác minh hiện chưa khả dụng");

    const unverifiedText = renderText(
      <ThreatMap
        locations={[
          {
            ip_address: "192.0.2.10",
            latitude: 10,
            longitude: 106,
          },
        ]}
      />,
    );
    expect(unverifiedText).toContain(
      "Dữ liệu vị trí địa lý đã xác minh hiện chưa khả dụng",
    );
  });

  test("AI card does not invent an analysis recommendation", () => {
    const text = renderText(<AIAdvisorCard recommendation={null} />);
    expect(text).toContain("Chưa có khuyến nghị từ response API hiện tại");
    expect(text).not.toContain("đang hoạt động ổn định");
  });

  test("AI card displays a recommendation supplied by the API", () => {
    const text = renderText(
      <AIAdvisorCard recommendation="Use parameterized SQL statements." />,
    );
    expect(text).toContain("Use parameterized SQL statements.");
    expect(text).not.toContain("Chưa có khuyến nghị");
  });
});
