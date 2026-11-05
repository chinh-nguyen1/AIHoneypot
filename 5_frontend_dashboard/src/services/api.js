import axios from "axios";

const API_BASE_URL = (
  process.env.REACT_APP_API_BASE_URL || "http://localhost:8000"
).replace(/\/+$/, "");
const REQUEST_TIMEOUT_MS = 8000;

export class DashboardApiError extends Error {
  constructor(message, { code = "API_ERROR", status = null } = {}) {
    super(message);
    this.name = "DashboardApiError";
    this.code = code;
    this.status = status;
  }
}

function isCount(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function validateStats(data) {
  const requiredCounts = [
    "total_logs",
    "classified_count",
    "unclassified_count",
    "sqli_count",
    "xss_count",
    "normal_count",
    "unique_ips",
  ];
  if (
    !data ||
    typeof data !== "object" ||
    Array.isArray(data) ||
    requiredCounts.some((field) => !isCount(data[field])) ||
    !data.attack_counts ||
    typeof data.attack_counts !== "object" ||
    Array.isArray(data.attack_counts)
  ) {
    throw new DashboardApiError("API thống kê trả về dữ liệu không hợp lệ.", {
      code: "INVALID_RESPONSE",
    });
  }

  for (const [label, count] of Object.entries(data.attack_counts)) {
    if (!label || !isCount(count)) {
      throw new DashboardApiError(
        "API thống kê có nhãn hoặc số lượng phân loại không hợp lệ.",
        { code: "INVALID_RESPONSE" },
      );
    }
  }
  return data;
}

function isEventId(value) {
  return (
    (typeof value === "string" && value.length > 0) ||
    (Number.isSafeInteger(value) && value > 0)
  );
}

function validateLog(log) {
  if (!log || typeof log !== "object" || Array.isArray(log)) {
    throw new DashboardApiError("API log trả về một sự kiện không hợp lệ.", {
      code: "INVALID_RESPONSE",
    });
  }

  const validNullableClassification =
    (log.attack_type === null || typeof log.attack_type === "string") &&
    (log.confidence_score === null ||
      (typeof log.confidence_score === "number" &&
        Number.isFinite(log.confidence_score) &&
        log.confidence_score >= 0 &&
        log.confidence_score <= 1)) &&
    (log.risk_score === null ||
      (Number.isInteger(log.risk_score) &&
        log.risk_score >= 1 &&
        log.risk_score <= 10));

  if (
    !isEventId(log.id) ||
    typeof log.created_at !== "string" ||
    typeof log.client_ip !== "string" ||
    typeof log.http_method !== "string" ||
    typeof log.request_uri !== "string" ||
    !validNullableClassification ||
    !["CLASSIFIED", "UNCLASSIFIED"].includes(log.classification_status) ||
    (log.classification_status === "UNCLASSIFIED" &&
      (log.attack_type !== null ||
        log.confidence_score !== null ||
        log.risk_score !== null))
  ) {
    throw new DashboardApiError("API log trả về một sự kiện không hợp lệ.", {
      code: "INVALID_RESPONSE",
    });
  }

  if (
    log.recommendation !== undefined &&
    log.recommendation !== null &&
    typeof log.recommendation !== "string"
  ) {
    throw new DashboardApiError(
      "API log trả về khuyến nghị không hợp lệ.",
      { code: "INVALID_RESPONSE" },
    );
  }
}

function validateLogs(data) {
  if (!Array.isArray(data)) {
    throw new DashboardApiError("API log phải trả về một danh sách.", {
      code: "INVALID_RESPONSE",
    });
  }
  data.forEach(validateLog);
  return data;
}

async function get(path, validate) {
  let response;
  try {
    response = await axios.get(`${API_BASE_URL}${path}`, {
      timeout: REQUEST_TIMEOUT_MS,
    });
  } catch (error) {
    const status = error?.response?.status;
    if (status) {
      throw new DashboardApiError(
        `API trả về lỗi HTTP ${status}.`,
        { code: "HTTP_ERROR", status },
      );
    }
    if (error?.code === "ECONNABORTED" || error?.code === "ETIMEDOUT") {
      throw new DashboardApiError("Kết nối API đã hết thời gian chờ.", {
        code: "TIMEOUT",
      });
    }
    throw new DashboardApiError("Không thể kết nối tới AI Engine API.", {
      code: "NETWORK_ERROR",
    });
  }

  if (
    !response ||
    !Number.isInteger(response.status) ||
    response.status < 200 ||
    response.status >= 300
  ) {
    throw new DashboardApiError(
      `API trả về lỗi HTTP ${response?.status ?? "không xác định"}.`,
      { code: "HTTP_ERROR", status: response?.status ?? null },
    );
  }

  let data = response.data;
  if (typeof data === "string") {
    try {
      data = JSON.parse(data);
    } catch {
      throw new DashboardApiError("API không trả về JSON hợp lệ.", {
        code: "INVALID_JSON",
      });
    }
  }
  return validate(data);
}

export const fetchDashboardStats = () => get("/stats", validateStats);

export const fetchLiveLogs = () => get("/logs/live", validateLogs);
