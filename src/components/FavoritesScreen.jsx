import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

function normalizeFavorites(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }

  const out = [];
  const seen = new Set();

  raw.forEach((item, index) => {
    if (!item || typeof item !== "object") {
      return;
    }

    const token = String(item.id || "").trim();
    const parts = token ? token.split(":") : [];
    let type = String(item.type || item.kind || "").trim().toLowerCase();
    let target = String(item.target || "").trim().toLowerCase();
    let profileId = String(item.profile_id || item.profileId || item.profile || "").trim();

    if (!profileId && parts.length > 0) {
      if (parts[0].toLowerCase() === "digital" && parts.length >= 2) {
        type = "digital";
        profileId = parts.slice(1).join(":").trim();
      } else if (parts[0].toLowerCase() === "analog" && parts.length >= 3) {
        type = "analog";
        target = String(parts[1] || "").trim().toLowerCase();
        profileId = parts.slice(2).join(":").trim();
      }
    }

    if (!type && target) {
      type = "analog";
    }
    if (type === "digital") {
      target = "";
    }
    if (type !== "digital" && type !== "analog") {
      return;
    }
    if (type === "analog" && target !== "airband" && target !== "ground") {
      return;
    }
    if (!profileId) {
      return;
    }

    const id = type === "digital" ? `digital:${profileId}` : `analog:${target}:${profileId}`;
    if (seen.has(id)) {
      return;
    }
    seen.add(id);

    out.push({
      id,
      type,
      target,
      profile_id: profileId,
      label: String(item.label || item.name || profileId),
      enabled: item.enabled === true,
      _index: index,
    });
  });

  return out;
}

function groupItems(items) {
  return {
    analog_airband: items
      .filter((entry) => entry.type === "analog" && entry.target === "airband")
      .sort((a, b) => a._index - b._index),
    analog_ground: items
      .filter((entry) => entry.type === "analog" && entry.target === "ground")
      .sort((a, b) => a._index - b._index),
    digital: items
      .filter((entry) => entry.type === "digital")
      .sort((a, b) => a._index - b._index),
  };
}

export default function FavoritesScreen() {
  const { state, saveHpState, navigate } = useUI();
  const { hpState, working } = state;

  const sourceFavorites = useMemo(() => {
    if (Array.isArray(hpState.favorites)) {
      return hpState.favorites;
    }
    if (Array.isArray(hpState.favorites_list)) {
      return hpState.favorites_list;
    }
    return [];
  }, [hpState.favorites, hpState.favorites_list]);

  const [favorites, setFavorites] = useState([]);
  const grouped = useMemo(() => groupItems(favorites), [favorites]);

  useEffect(() => {
    setFavorites(normalizeFavorites(sourceFavorites));
  }, [sourceFavorites]);

  const setActiveFavorite = (groupKey, profileId) => {
    setFavorites((current) =>
      current.map((item) => {
        const itemGroup =
          item.type === "digital" ? "digital" : `analog_${item.target}`;
        if (itemGroup !== groupKey) {
          return item;
        }
        return { ...item, enabled: item.profile_id === profileId };
      })
    );
  };

  const handleSave = async () => {
    try {
      await saveHpState({ favorites });
      navigate(SCREENS.MENU);
    } catch {
      // Context error is shown globally.
    }
  };

  return (
    <section className="screen favorites-screen">
      <Header title="Favorites" showBack onBack={() => navigate(SCREENS.MENU)} />

      {favorites.length === 0 ? (
        <div className="muted">No favorites in current state.</div>
      ) : (
        <div className="list">
          <div className="card">
            <div className="muted" style={{ marginBottom: "8px" }}>
              Analog Airband
            </div>
            {grouped.analog_airband.length === 0 ? (
              <div className="muted">No airband profiles found.</div>
            ) : (
              grouped.analog_airband.map((item) => (
                <label key={item.id} className="row" style={{ marginBottom: "6px" }}>
                  <span>{item.label}</span>
                  <input
                    type="radio"
                    name="favorites-analog-airband"
                    checked={item.enabled}
                    onChange={() => setActiveFavorite("analog_airband", item.profile_id)}
                  />
                </label>
              ))
            )}
          </div>

          <div className="card">
            <div className="muted" style={{ marginBottom: "8px" }}>
              Analog Ground
            </div>
            {grouped.analog_ground.length === 0 ? (
              <div className="muted">No ground profiles found.</div>
            ) : (
              grouped.analog_ground.map((item) => (
                <label key={item.id} className="row" style={{ marginBottom: "6px" }}>
                  <span>{item.label}</span>
                  <input
                    type="radio"
                    name="favorites-analog-ground"
                    checked={item.enabled}
                    onChange={() => setActiveFavorite("analog_ground", item.profile_id)}
                  />
                </label>
              ))
            )}
          </div>

          <div className="card">
            <div className="muted" style={{ marginBottom: "8px" }}>
              Digital
            </div>
            {grouped.digital.length === 0 ? (
              <div className="muted">No digital profiles found.</div>
            ) : (
              grouped.digital.map((item) => (
                <label key={item.id} className="row" style={{ marginBottom: "6px" }}>
                  <span>{item.label}</span>
                  <input
                    type="radio"
                    name="favorites-digital"
                    checked={item.enabled}
                    onChange={() => setActiveFavorite("digital", item.profile_id)}
                  />
                </label>
              ))
            )}
          </div>
        </div>
      )}

      <div className="muted" style={{ marginTop: "8px" }}>
        Saving favorites sets the active analog/digital profiles for HP3 playback.
      </div>

      <div className="button-row">
        <Button onClick={handleSave} disabled={working}>
          Save
        </Button>
      </div>

      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
