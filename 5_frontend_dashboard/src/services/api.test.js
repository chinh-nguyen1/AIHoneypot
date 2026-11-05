jest.mock("axios", () => ({ get: jest.fn() }));

const validStats = {
  total_logs: 4,
  classified_count: 3,
  unclassified_count: 1,
  attack_counts: { Normal: 1, SQLi: 1, Suspicious: 1 },
  sqli_count: 1,
  xss_count: 0,
  normal_count: 1,
  unique_ips: 2,
};

const validLog = {
  id: 8,
  created_at: "2026-11-01T04:00:00",
  client_ip: "192.0.2.10",
  http_method: "GET",
  request_uri: "/",
  attack_type: null,
  confidence_score: null,
  risk_score: null,
  classification_status: "UNCLASSIFIED",
};

function loadApi(baseUrl = "") {
  jest.resetModules();
  if (baseUrl) {
    process.env.REACT_APP_API_BASE_URL = baseUrl;
  } else {
    delete process.env.REACT_APP_API_BASE_URL;
  }
  return { ...require("./api"), axiosMock: require("axios") };
}

describe("Dashboard API client", () => {
  test("returns validated statistics and uses configured URL", async () => {
    const { fetchDashboardStats, axiosMock } = loadApi(
      "http://dashboard-api.test/",
    );
    axiosMock.get.mockResolvedValue({ status: 200, data: validStats });

    await expect(fetchDashboardStats()).resolves.toEqual(validStats);
    expect(axiosMock.get).toHaveBeenCalledWith(
      "http://dashboard-api.test/stats",
      { timeout: 8000 },
    );
  });

  test("preserves the development fallback URL", async () => {
    const { fetchLiveLogs, axiosMock } = loadApi();
    const logWithRecommendation = {
      ...validLog,
      recommendation: "Use parameterized SQL statements.",
    };
    axiosMock.get.mockResolvedValue({
      status: 200,
      data: [logWithRecommendation, { ...validLog, recommendation: null }],
    });

    await expect(fetchLiveLogs()).resolves.toEqual([
      logWithRecommendation,
      { ...validLog, recommendation: null },
    ]);

    expect(axiosMock.get).toHaveBeenCalledWith(
      "http://localhost:8000/logs/live",
      { timeout: 8000 },
    );
  });

  test("rejects malformed recommendation values", async () => {
    const { fetchLiveLogs, axiosMock } = loadApi();
    axiosMock.get.mockResolvedValue({
      status: 200,
      data: [{ ...validLog, recommendation: { text: "not a string" } }],
    });

    await expect(fetchLiveLogs()).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });

  test("does not turn HTTP errors into empty data", async () => {
    const { fetchLiveLogs, DashboardApiError, axiosMock } = loadApi();
    axiosMock.get.mockRejectedValue({ response: { status: 503 } });

    await expect(fetchLiveLogs()).rejects.toMatchObject({
      name: "DashboardApiError",
      code: "HTTP_ERROR",
      status: 503,
    });
    expect(DashboardApiError).toBeDefined();
  });

  test("reports network and timeout errors separately", async () => {
    const { fetchDashboardStats, axiosMock } = loadApi();
    axiosMock.get.mockRejectedValueOnce(new Error("network down"));
    await expect(fetchDashboardStats()).rejects.toMatchObject({
      code: "NETWORK_ERROR",
    });

    axiosMock.get.mockRejectedValueOnce({ code: "ECONNABORTED" });
    await expect(fetchDashboardStats()).rejects.toMatchObject({
      code: "TIMEOUT",
    });
  });

  test("rejects invalid JSON and missing statistics fields", async () => {
    const { fetchDashboardStats, axiosMock } = loadApi();
    axiosMock.get.mockResolvedValueOnce({ status: 200, data: "not-json" });
    await expect(fetchDashboardStats()).rejects.toMatchObject({
      code: "INVALID_JSON",
    });

    axiosMock.get.mockResolvedValueOnce({
      status: 200,
      data: { ...validStats, xss_count: undefined },
    });
    await expect(fetchDashboardStats()).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });

  test("accepts real zero counts and rejects malformed event rows", async () => {
    const { fetchDashboardStats, fetchLiveLogs, axiosMock } = loadApi();
    const zeroStats = {
      ...validStats,
      total_logs: 0,
      classified_count: 0,
      unclassified_count: 0,
      attack_counts: {},
      sqli_count: 0,
      xss_count: 0,
      normal_count: 0,
      unique_ips: 0,
    };
    axiosMock.get.mockResolvedValueOnce({ status: 200, data: zeroStats });
    await expect(fetchDashboardStats()).resolves.toEqual(zeroStats);

    axiosMock.get.mockResolvedValueOnce({
      status: 200,
      data: [{ ...validLog, id: undefined }],
    });
    await expect(fetchLiveLogs()).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });
});
