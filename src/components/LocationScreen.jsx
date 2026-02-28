import React, { useEffect, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

function parseNumber(value) {
  if (value === "" || value === null || value === undefined) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : NaN;
}

export default function LocationScreen() {
  const { state, saveHpState, navigate } = useUI();
  const { hpState, working } = state;

  const [zip, setZip] = useState("");
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [useLocation, setUseLocation] = useState(true);
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    setZip(hpState.zip || hpState.postal_code || "");
    setLat(
      hpState.lat !== undefined && hpState.lat !== null
        ? String(hpState.lat)
        : hpState.latitude !== undefined && hpState.latitude !== null
        ? String(hpState.latitude)
        : ""
    );
    setLon(
      hpState.lon !== undefined && hpState.lon !== null
        ? String(hpState.lon)
        : hpState.longitude !== undefined && hpState.longitude !== null
        ? String(hpState.longitude)
        : ""
    );
    setUseLocation(hpState.use_location !== false);
  }, [hpState]);

  const handleSave = async () => {
    setLocalError("");

    if (zip && !/^\d{5}(-\d{4})?$/.test(zip)) {
      setLocalError("ZIP must be 5 digits or ZIP+4.");
      return;
    }

    const parsedLat = parseNumber(lat);
    const parsedLon = parseNumber(lon);

    if (Number.isNaN(parsedLat) || Number.isNaN(parsedLon)) {
      setLocalError("Latitude and longitude must be valid numbers.");
      return;
    }
    if ((parsedLat === null) !== (parsedLon === null)) {
      setLocalError("Enter both latitude and longitude, or leave both blank.");
      return;
    }

    if (parsedLat !== null && (parsedLat < -90 || parsedLat > 90)) {
      setLocalError("Latitude must be between -90 and 90.");
      return;
    }

    if (parsedLon !== null && (parsedLon < -180 || parsedLon > 180)) {
      setLocalError("Longitude must be between -180 and 180.");
      return;
    }

    try {
      const payload = {
        zip,
        use_location: useLocation,
      };
      if (parsedLat !== null && parsedLon !== null) {
        payload.lat = parsedLat;
        payload.lon = parsedLon;
      } else if (zip) {
        payload.resolve_zip = true;
      }

      await saveHpState(payload);
      navigate(SCREENS.MENU);
    } catch {
      // Context error is shown globally.
    }
  };

  return (
    <section className="screen location-screen">
      <Header title="Location" showBack onBack={() => navigate(SCREENS.MENU)} />

      <div className="list">
        <label>
          <div className="muted">ZIP</div>
          <input
            className="input"
            value={zip}
            onChange={(e) => setZip(e.target.value.trim())}
            placeholder="37201"
          />
        </label>

        <label>
          <div className="muted">Latitude</div>
          <input
            className="input"
            value={lat}
            onChange={(e) => setLat(e.target.value)}
            placeholder="36.12"
          />
        </label>

        <label>
          <div className="muted">Longitude</div>
          <input
            className="input"
            value={lon}
            onChange={(e) => setLon(e.target.value)}
            placeholder="-86.67"
          />
        </label>

        <label className="row">
          <span>Use location for scanning</span>
          <input
            type="checkbox"
            checked={useLocation}
            onChange={(e) => setUseLocation(e.target.checked)}
          />
        </label>
      </div>

      <div className="button-row">
        <Button onClick={handleSave} disabled={working}>
          Save
        </Button>
      </div>

      {localError ? <div className="error">{localError}</div> : null}
      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
