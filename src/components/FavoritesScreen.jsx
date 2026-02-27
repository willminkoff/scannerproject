import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

function parseFloatValue(value) {
  const num = Number(String(value || "").trim());
  if (!Number.isFinite(num)) {
    return null;
  }
  return num;
}

function parseIntValue(value) {
  const num = Number.parseInt(String(value || "").trim(), 10);
  if (!Number.isFinite(num)) {
    return null;
  }
  return num;
}

function parseControlChannels(rawText) {
  const tokens = String(rawText || "")
    .split(/[,\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const seen = new Set();
  const out = [];
  tokens.forEach((token) => {
    const value = parseFloatValue(token);
    if (value === null || value <= 0) {
      return;
    }
    const mhz = Number(value.toFixed(6));
    if (seen.has(mhz)) {
      return;
    }
    seen.add(mhz);
    out.push(mhz);
  });
  return out.sort((a, b) => a - b);
}

function normalizeCustomFavorites(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }
  const out = [];
  raw.forEach((item, index) => {
    if (!item || typeof item !== "object") {
      return;
    }
    const kind = String(item.kind || "").trim().toLowerCase();
    if (kind !== "trunked" && kind !== "conventional") {
      return;
    }
    const id = String(item.id || `fav-${index + 1}`).trim() || `fav-${index + 1}`;
    if (kind === "trunked") {
      const talkgroup = parseIntValue(item.talkgroup || item.tgid);
      const controlChannels = Array.isArray(item.control_channels)
        ? item.control_channels
            .map((value) => parseFloatValue(value))
            .filter((value) => value !== null && value > 0)
            .map((value) => Number(value.toFixed(6)))
        : [];
      if (talkgroup === null || talkgroup <= 0 || controlChannels.length === 0) {
        return;
      }
      out.push({
        id,
        kind: "trunked",
        system_name: String(item.system_name || "").trim(),
        department_name: String(item.department_name || "").trim(),
        alpha_tag: String(item.alpha_tag || item.channel_name || "").trim(),
        talkgroup: String(talkgroup),
        service_tag: parseIntValue(item.service_tag) || 0,
        control_channels: Array.from(new Set(controlChannels)).sort((a, b) => a - b),
      });
      return;
    }
    const frequency = parseFloatValue(item.frequency);
    if (frequency === null || frequency <= 0) {
      return;
    }
    out.push({
      id,
      kind: "conventional",
      alpha_tag: String(item.alpha_tag || item.channel_name || "").trim(),
      frequency: Number(frequency.toFixed(6)),
      service_tag: parseIntValue(item.service_tag) || 0,
    });
  });
  return out;
}

function makeId(prefix) {
  const random = Math.random().toString(16).slice(2, 8);
  return `${prefix}-${Date.now()}-${random}`;
}

export default function FavoritesScreen() {
  const { state, saveHpState, navigate } = useUI();
  const { hpState, working } = state;

  const [favoritesName, setFavoritesName] = useState("My Favorites");
  const [entries, setEntries] = useState([]);
  const [error, setError] = useState("");
  const [trunkedForm, setTrunkedForm] = useState({
    system_name: "",
    department_name: "",
    alpha_tag: "",
    talkgroup: "",
    service_tag: "",
    control_channels: "",
  });
  const [conventionalForm, setConventionalForm] = useState({
    alpha_tag: "",
    frequency: "",
    service_tag: "",
  });

  useEffect(() => {
    setFavoritesName(String(hpState.favorites_name || "My Favorites").trim() || "My Favorites");
    setEntries(normalizeCustomFavorites(hpState.custom_favorites));
  }, [hpState.favorites_name, hpState.custom_favorites]);

  const trunkedEntries = useMemo(
    () => entries.filter((entry) => entry.kind === "trunked"),
    [entries]
  );
  const conventionalEntries = useMemo(
    () => entries.filter((entry) => entry.kind === "conventional"),
    [entries]
  );

  const removeEntry = (id) => {
    setEntries((current) => current.filter((entry) => entry.id !== id));
  };

  const addTrunkedEntry = () => {
    setError("");
    const talkgroup = parseIntValue(trunkedForm.talkgroup);
    if (talkgroup === null || talkgroup <= 0) {
      setError("Trunked talkgroup must be a positive integer.");
      return;
    }
    const controlChannels = parseControlChannels(trunkedForm.control_channels);
    if (controlChannels.length === 0) {
      setError("At least one trunked control channel is required.");
      return;
    }
    const serviceTag = parseIntValue(trunkedForm.service_tag) || 0;
    const entry = {
      id: makeId("trunk"),
      kind: "trunked",
      system_name: String(trunkedForm.system_name || "").trim(),
      department_name: String(trunkedForm.department_name || "").trim(),
      alpha_tag: String(trunkedForm.alpha_tag || "").trim(),
      talkgroup: String(talkgroup),
      service_tag: serviceTag,
      control_channels: controlChannels,
    };
    setEntries((current) => [...current, entry]);
    setTrunkedForm({
      system_name: trunkedForm.system_name,
      department_name: trunkedForm.department_name,
      alpha_tag: "",
      talkgroup: "",
      service_tag: trunkedForm.service_tag,
      control_channels: trunkedForm.control_channels,
    });
  };

  const addConventionalEntry = () => {
    setError("");
    const frequency = parseFloatValue(conventionalForm.frequency);
    if (frequency === null || frequency <= 0) {
      setError("Conventional frequency must be a positive number.");
      return;
    }
    const serviceTag = parseIntValue(conventionalForm.service_tag) || 0;
    const entry = {
      id: makeId("conv"),
      kind: "conventional",
      alpha_tag: String(conventionalForm.alpha_tag || "").trim(),
      frequency: Number(frequency.toFixed(6)),
      service_tag: serviceTag,
    };
    setEntries((current) => [...current, entry]);
    setConventionalForm({
      alpha_tag: "",
      frequency: "",
      service_tag: conventionalForm.service_tag,
    });
  };

  const saveFavorites = async () => {
    setError("");
    const listName = String(favoritesName || "").trim() || "My Favorites";
    try {
      await saveHpState({
        mode: "favorites",
        favorites_name: listName,
        custom_favorites: entries,
      });
      navigate(SCREENS.MENU);
    } catch {
      // Context error is shown globally.
    }
  };

  const switchToFullDatabase = async () => {
    setError("");
    try {
      await saveHpState({ mode: "full_database" });
      navigate(SCREENS.MENU);
    } catch {
      // Context error is shown globally.
    }
  };

  return (
    <section className="screen favorites-screen">
      <Header title="Favorites" showBack onBack={() => navigate(SCREENS.MENU)} />

      <div className="card">
        <div className="muted" style={{ marginBottom: "8px" }}>
          Favorites List Name
        </div>
        <input
          className="input"
          value={favoritesName}
          onChange={(event) => setFavoritesName(event.target.value)}
          placeholder="My Favorites"
        />
      </div>

      <div className="card">
        <div className="muted" style={{ marginBottom: "8px" }}>
          Add Trunked Favorite
        </div>
        <input
          className="input"
          value={trunkedForm.system_name}
          onChange={(event) =>
            setTrunkedForm((current) => ({ ...current, system_name: event.target.value }))
          }
          placeholder="System name"
        />
        <input
          className="input"
          value={trunkedForm.department_name}
          onChange={(event) =>
            setTrunkedForm((current) => ({ ...current, department_name: event.target.value }))
          }
          placeholder="Department name"
        />
        <input
          className="input"
          value={trunkedForm.alpha_tag}
          onChange={(event) =>
            setTrunkedForm((current) => ({ ...current, alpha_tag: event.target.value }))
          }
          placeholder="Channel label (alpha tag)"
        />
        <input
          className="input"
          value={trunkedForm.talkgroup}
          onChange={(event) =>
            setTrunkedForm((current) => ({ ...current, talkgroup: event.target.value }))
          }
          placeholder="Talkgroup (decimal)"
        />
        <input
          className="input"
          value={trunkedForm.control_channels}
          onChange={(event) =>
            setTrunkedForm((current) => ({ ...current, control_channels: event.target.value }))
          }
          placeholder="Control channels MHz (comma separated)"
        />
        <input
          className="input"
          value={trunkedForm.service_tag}
          onChange={(event) =>
            setTrunkedForm((current) => ({ ...current, service_tag: event.target.value }))
          }
          placeholder="Service tag (optional)"
        />
        <div className="button-row">
          <Button onClick={addTrunkedEntry} disabled={working}>
            Add Trunked
          </Button>
        </div>
      </div>

      <div className="card">
        <div className="muted" style={{ marginBottom: "8px" }}>
          Add Conventional Favorite
        </div>
        <input
          className="input"
          value={conventionalForm.alpha_tag}
          onChange={(event) =>
            setConventionalForm((current) => ({ ...current, alpha_tag: event.target.value }))
          }
          placeholder="Channel label (alpha tag)"
        />
        <input
          className="input"
          value={conventionalForm.frequency}
          onChange={(event) =>
            setConventionalForm((current) => ({ ...current, frequency: event.target.value }))
          }
          placeholder="Frequency MHz"
        />
        <input
          className="input"
          value={conventionalForm.service_tag}
          onChange={(event) =>
            setConventionalForm((current) => ({ ...current, service_tag: event.target.value }))
          }
          placeholder="Service tag (optional)"
        />
        <div className="button-row">
          <Button onClick={addConventionalEntry} disabled={working}>
            Add Conventional
          </Button>
        </div>
      </div>

      <div className="card">
        <div className="muted" style={{ marginBottom: "8px" }}>
          Current Favorites ({entries.length})
        </div>
        {entries.length === 0 ? (
          <div className="muted">No custom favorites yet.</div>
        ) : (
          <div className="list">
            {trunkedEntries.map((entry) => (
              <div key={entry.id} className="row" style={{ marginBottom: "8px" }}>
                <div>
                  <div>
                    <strong>{entry.system_name || "Custom Trunked"}</strong>
                  </div>
                  <div className="muted">
                    {entry.department_name || "Department"} - TGID {entry.talkgroup}
                  </div>
                  <div className="muted">
                    {entry.control_channels.join(", ")} MHz
                  </div>
                </div>
                <Button variant="danger" onClick={() => removeEntry(entry.id)} disabled={working}>
                  Remove
                </Button>
              </div>
            ))}
            {conventionalEntries.map((entry) => (
              <div key={entry.id} className="row" style={{ marginBottom: "8px" }}>
                <div>
                  <div>
                    <strong>{entry.alpha_tag || "Conventional"}</strong>
                  </div>
                  <div className="muted">
                    {entry.frequency.toFixed(4)} MHz
                    {entry.service_tag > 0 ? ` - Service ${entry.service_tag}` : ""}
                  </div>
                </div>
                <Button variant="danger" onClick={() => removeEntry(entry.id)} disabled={working}>
                  Remove
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="button-row">
        <Button onClick={saveFavorites} disabled={working}>
          Save Favorites Mode
        </Button>
        <Button variant="secondary" onClick={switchToFullDatabase} disabled={working}>
          Use Full Database
        </Button>
      </div>

      {error ? <div className="error">{error}</div> : null}
      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
