import React, { useEffect, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

const AUTOLOCATE_TIMEOUT_MS = 12000;
const IP_GEOLOOKUP_URL = "https://ipapi.co/json/";
const REVERSE_GEOLOOKUP_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client";

function parseNumber(value) {
  if (value === "" || value === null || value === undefined) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : NaN;
}

function describeGeoError(err) {
  const code = Number(err?.code);
  if (code === 1) {
    return "Location permission denied.";
  }
  if (code === 2) {
    return "Current GPS location is unavailable.";
  }
  if (code === 3) {
    return "GPS location request timed out.";
  }
  if (typeof window !== "undefined" && window.isSecureContext === false) {
    return "GPS location requires HTTPS or localhost.";
  }
  return String(err?.message || "GPS location lookup failed.").trim();
}

function parseIpLocation(payload) {
  const data = payload && typeof payload === "object" ? payload : {};
  const lat = Number("latitude" in data ? data.latitude : data.lat);
  const lon = Number("longitude" in data ? data.longitude : data.lon);
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
    throw new Error("IP location response did not include valid coordinates.");
  }
  return {
    lat,
    lon,
    zip: String(data.postal || data.postcode || data.zip || "").trim(),
    county: String(data.county || "").trim(),
  };
}

function formatZipCountySummary(zip, county) {
  const parts = [];
  const postal = String(zip || "").trim();
  const area = String(county || "").trim();
  if (postal) {
    parts.push(`ZIP ${postal}`);
  }
  if (area) {
    parts.push(area);
  }
  return parts.join(" • ");
}

function parseReverseGeocode(payload) {
  const data = payload && typeof payload === "object" ? payload : {};
  const zip = String(
    "postcode" in data
      ? data.postcode
      : "postal_code" in data
      ? data.postal_code
      : "postal" in data
      ? data.postal
      : "zip" in data
      ? data.zip
      : ""
  ).trim();
  let county = String(data.county || "").trim();
  if (!county) {
    const administrative = Array.isArray(data?.localityInfo?.administrative)
      ? data.localityInfo.administrative
      : [];
    for (let i = 0; i < administrative.length; i += 1) {
      const row = administrative[i] && typeof administrative[i] === "object" ? administrative[i] : {};
      const name = String(row.name || "").trim();
      const description = String(row.description || "").toLowerCase();
      if (!name) {
        continue;
      }
      if (description.includes("county") || / county$/i.test(name)) {
        county = name;
        break;
      }
    }
  }
  return { zip, county };
}

async function resolveZipCounty(lat, lon) {
  if (!Number.isFinite(Number(lat)) || !Number.isFinite(Number(lon))) {
    return { zip: "", county: "" };
  }
  const url = `${REVERSE_GEOLOOKUP_URL}?latitude=${encodeURIComponent(String(lat))}&longitude=${encodeURIComponent(String(lon))}&localityLanguage=en`;
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Reverse location lookup failed (HTTP ${response.status}).`);
  }
  const payload = await response.json();
  return parseReverseGeocode(payload);
}

async function resolveCurrentLocation() {
  let gpsFailure = null;
  if (typeof navigator !== "undefined" && navigator?.geolocation?.getCurrentPosition) {
    try {
      const position = await new Promise((resolve, reject) => {
        navigator.geolocation.getCurrentPosition(resolve, reject, {
          enableHighAccuracy: true,
          timeout: AUTOLOCATE_TIMEOUT_MS,
          maximumAge: 30000,
        });
      });
      const lat = Number(position?.coords?.latitude);
      const lon = Number(position?.coords?.longitude);
      if (Number.isFinite(lat) && Number.isFinite(lon) && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) {
        return {
          lat,
          lon,
          source: "gps",
          fallbackReason: "",
        };
      }
      gpsFailure = new Error("GPS returned invalid coordinates.");
    } catch (err) {
      gpsFailure = err;
    }
  } else {
    gpsFailure = new Error("GPS location is not available in this browser.");
  }

  const gpsReason = describeGeoError(gpsFailure);
  const response = await fetch(IP_GEOLOOKUP_URL, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${gpsReason} IP fallback failed (HTTP ${response.status}).`);
  }
  const payload = await response.json();
  const coords = parseIpLocation(payload);
  return {
    ...coords,
    source: "ip",
    fallbackReason: gpsReason,
  };
}

export default function LocationScreen() {
  const { state, saveHpState, navigate } = useUI();
  const { hpState, working } = state;

  const [zip, setZip] = useState("");
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [useLocation, setUseLocation] = useState(true);
  const [localError, setLocalError] = useState("");
  const [localMessage, setLocalMessage] = useState("");
  const [locating, setLocating] = useState(false);
  const [locationSummary, setLocationSummary] = useState("");

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
    setLocationSummary("");
  }, [hpState]);

  const handleSave = async () => {
    setLocalError("");
    setLocalMessage("");
    setLocationSummary("");

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
      } else if (zip && useLocation) {
        payload.resolve_zip = true;
      }

      await saveHpState(payload);
      navigate(SCREENS.MENU);
    } catch {
      // Context error is shown globally.
    }
  };

  const handleAutoLocate = async () => {
    if (locating || working) {
      return;
    }
    setLocalError("");
    setLocalMessage("");
    setLocationSummary("");
    setLocating(true);
    try {
      const location = await resolveCurrentLocation();
      let details = {
        zip: String(location.zip || "").trim(),
        county: String(location.county || "").trim(),
      };
      try {
        const resolved = await resolveZipCounty(location.lat, location.lon);
        details = {
          zip: String(resolved.zip || details.zip || "").trim(),
          county: String(resolved.county || details.county || "").trim(),
        };
      } catch {
        // Non-blocking: coordinates are still valid even if reverse lookup fails.
      }
      const nextZip = String(details.zip || zip || "").trim();
      const detailText = formatZipCountySummary(nextZip, details.county);
      setLat(String(location.lat));
      setLon(String(location.lon));
      setZip(nextZip);
      setUseLocation(true);
      setLocationSummary(detailText ? `Auto-locate populated ${detailText}.` : "");
      await saveHpState({
        zip: nextZip,
        use_location: true,
        lat: location.lat,
        lon: location.lon,
      });
      if (location.source === "ip") {
        const reason = String(location.fallbackReason || "").trim();
        setLocalMessage(
          `GPS unavailable, used IP location.${detailText ? ` ${detailText}.` : ""}${reason ? ` ${reason}` : ""}`
        );
      } else {
        setLocalMessage(`GPS location applied.${detailText ? ` ${detailText}.` : ""}`);
      }
    } catch (err) {
      setLocalError(String(err?.message || "Unable to detect current location."));
    } finally {
      setLocating(false);
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

      {locationSummary ? <div className="muted location-meta">{locationSummary}</div> : null}

      <div className="button-row">
        <Button onClick={handleSave} disabled={working || locating}>
          Save
        </Button>
      </div>
      <div className="button-row location-action-row">
        <Button
          onClick={handleAutoLocate}
          disabled={working || locating}
          variant="secondary"
          className="location-autolocate-btn"
        >
          {locating ? "Locating..." : "Use Current Location"}
        </Button>
      </div>

      {localMessage ? <div className="message">{localMessage}</div> : null}
      {localError ? <div className="error">{localError}</div> : null}
      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
