// 5_frontend_dashboard/src/services/api.js
import axios from "axios";

const API_BASE_URL = "http://localhost:8000"; // Hoặc đường dẫn Backend kết nối MySQL

export const fetchDashboardStats = async () => {
  try {
    const response = await axios.get(`${API_BASE_URL}/stats`);
    return response.data;
  } catch (error) {
    console.error("Lỗi lấy dữ liệu thống kê:", error);
    return null;
  }
};

export const fetchLiveLogs = async () => {
  try {
    const response = await axios.get(`${API_BASE_URL}/logs/live`);
    return response.data;
  } catch (error) {
    console.error("Lỗi lấy luồng log:", error);
    return [];
  }
};
