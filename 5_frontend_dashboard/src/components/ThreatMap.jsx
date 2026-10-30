import React from "react";
import { MapContainer, TileLayer, Marker, Popup } from "react-leaflet";
import L from "leaflet";

// Sửa lỗi hiển thị marker icon mặc định của Leaflet trong React
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl:
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon-2x.png",
  iconUrl:
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon.png",
  shadowUrl:
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png",
});

export default function ThreatMap({ threatIps }) {
  const defaultCenter = [20.0, 10.0]; // Tọa độ trung tâm xem toàn cầu

  const defaultLocations = [
    {
      ip: "113.161.72.11",
      country: "Vietnam (TP.HCM)",
      lat: 10.8231,
      lng: 106.6297,
      attacks: 15,
    },
    {
      ip: "185.220.101.5",
      country: "Germany (Frankfurt)",
      lat: 51.1657,
      lng: 10.4515,
      attacks: 24,
    },
    {
      ip: "45.155.205.233",
      country: "Russia (Moscow)",
      lat: 55.7558,
      lng: 37.6173,
      attacks: 42,
    },
  ];

  const locations =
    threatIps && threatIps.length > 0 ? threatIps : defaultLocations;

  return (
    <div
      style={{
        background: "#1e1e2f",
        padding: "20px",
        borderRadius: "8px",
        marginBottom: "20px",
      }}
    >
      <h3 style={{ marginTop: 0, color: "#fff" }}>
        🌐 Bản Đồ Tọa Độ Địa Lý IP Tấn Công Thời Gian Thực
      </h3>
      <div style={{ height: "350px", borderRadius: "8px", overflow: "hidden" }}>
        <MapContainer
          center={defaultCenter}
          zoom={2}
          style={{ height: "100%", width: "100%" }}
        >
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />
          {locations.map((loc, idx) => (
            <Marker
              key={idx}
              position={[loc.lat || loc.latitude, loc.lng || loc.longitude]}
            >
              <Popup>
                <div style={{ color: "#000" }}>
                  <b>IP:</b> {loc.ip || loc.ip_address}
                  <br />
                  <b>Quốc gia:</b> {loc.country || loc.country_name}
                  <br />
                  <b style={{ color: "red" }}>Tổng số vụ tấn công:</b>{" "}
                  {loc.attacks || loc.total_attacks}
                </div>
              </Popup>
            </Marker>
          ))}
        </MapContainer>
      </div>
    </div>
  );
}
