import React from "react";
import { MapContainer, TileLayer, Marker, Popup } from "react-leaflet";
import L from "leaflet";

delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl:
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon-2x.png",
  iconUrl:
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon.png",
  shadowUrl:
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png",
});

function isVerifiedLocation(location) {
  return (
    location?.verified === true &&
    Number.isFinite(location?.latitude) &&
    location.latitude >= -90 &&
    location.latitude <= 90 &&
    Number.isFinite(location?.longitude) &&
    location.longitude >= -180 &&
    location.longitude <= 180
  );
}

export default function ThreatMap({ locations = [] }) {
  const verifiedLocations = Array.isArray(locations)
    ? locations.filter(isVerifiedLocation)
    : [];

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
      <h3 style={{ marginTop: 0 }}>🌐 Bản đồ vị trí sự kiện</h3>
      {verifiedLocations.length === 0 ? (
        <p role="status">
          Dữ liệu vị trí địa lý đã xác minh hiện chưa khả dụng.
        </p>
      ) : (
        <div
          style={{ height: "350px", borderRadius: "8px", overflow: "hidden" }}
        >
          <MapContainer
            center={[
              verifiedLocations[0].latitude,
              verifiedLocations[0].longitude,
            ]}
            zoom={2}
            style={{ height: "100%", width: "100%" }}
          >
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
              url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            />
            {verifiedLocations.map((location) => (
              <Marker
                key={
                  location.ip_address ||
                  location.id ||
                  `${location.latitude}:${location.longitude}`
                }
                position={[location.latitude, location.longitude]}
              >
                <Popup>
                  <div style={{ color: "#000" }}>
                    {location.ip_address && (
                      <>
                        <b>IP:</b> {location.ip_address}
                        <br />
                      </>
                    )}
                    {location.country_name && (
                      <>
                        <b>Quốc gia:</b> {location.country_name}
                        <br />
                      </>
                    )}
                    {Number.isInteger(location.total_attacks) && (
                      <>
                        <b>Số sự kiện:</b> {location.total_attacks}
                      </>
                    )}
                  </div>
                </Popup>
              </Marker>
            ))}
          </MapContainer>
        </div>
      )}
    </div>
  );
}
