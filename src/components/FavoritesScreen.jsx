import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import * as hpApi from "../api/hpApi";
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

function channelKey(channel) {
  if (!channel || typeof channel !== "object") {
    return "";
  }
  const kind = String(channel.kind || "").trim().toLowerCase();
  if (kind === "trunked") {
    const talkgroup = Number(channel.talkgroup) || 0;
    const controls = Array.isArray(channel.control_channels)
      ? [...new Set(channel.control_channels.map((value) => Number(value).toFixed(6)))].sort()
      : [];
    return `trunked|${talkgroup}|${controls.join(",")}`;
  }
  if (kind === "conventional") {
    const frequency = Number(channel.frequency) || 0;
    return `conventional|${frequency.toFixed(6)}|${String(channel.alpha_tag || "").trim().toLowerCase()}`;
  }
  return "";
}

function favoriteKey(entry) {
  if (!entry || typeof entry !== "object") {
    return "";
  }
  const kind = String(entry.kind || "").trim().toLowerCase();
  if (kind === "trunked") {
    const talkgroup = Number(entry.talkgroup) || 0;
    const controls = Array.isArray(entry.control_channels)
      ? [...new Set(entry.control_channels.map((value) => Number(value).toFixed(6)))].sort()
      : [];
    return `trunked|${talkgroup}|${controls.join(",")}`;
  }
  if (kind === "conventional") {
    const frequency = Number(entry.frequency) || 0;
    return `conventional|${frequency.toFixed(6)}|${String(entry.alpha_tag || "").trim().toLowerCase()}`;
  }
  return "";
}

function channelToFavorite(channel) {
  if (!channel || typeof channel !== "object") {
    return null;
  }
  const kind = String(channel.kind || "").trim().toLowerCase();
  if (kind === "trunked") {
    const talkgroup = parseIntValue(channel.talkgroup);
    const controls = Array.isArray(channel.control_channels)
      ? channel.control_channels
          .map((value) => parseFloatValue(value))
          .filter((value) => value !== null && value > 0)
          .map((value) => Number(value.toFixed(6)))
      : [];
    if (talkgroup === null || talkgroup <= 0 || controls.length === 0) {
      return null;
    }
    return {
      id: makeId("trunk"),
      kind: "trunked",
      system_name: String(channel.system_name || "").trim(),
      department_name: String(channel.department_name || "").trim(),
      alpha_tag: String(channel.alpha_tag || "").trim(),
      talkgroup: String(talkgroup),
      service_tag: parseIntValue(channel.service_tag) || 0,
      control_channels: Array.from(new Set(controls)).sort((a, b) => a - b),
    };
  }
  if (kind === "conventional") {
    const frequency = parseFloatValue(channel.frequency);
    if (frequency === null || frequency <= 0) {
      return null;
    }
    return {
      id: makeId("conv"),
      kind: "conventional",
      alpha_tag: String(channel.alpha_tag || "").trim(),
      frequency: Number(frequency.toFixed(6)),
      service_tag: parseIntValue(channel.service_tag) || 0,
    };
  }
  return null;
}

export default function FavoritesScreen() {
  const { state, saveHpState, navigate } = useUI();
  const { hpState, working } = state;

  const [favoritesName, setFavoritesName] = useState("My Favorites");
  const [entries, setEntries] = useState([]);
  const [error, setError] = useState("");
  const [wizardMessage, setWizardMessage] = useState("");

  const [countries, setCountries] = useState([]);
  const [states, setStates] = useState([]);
  const [counties, setCounties] = useState([]);
  const [systems, setSystems] = useState([]);
  const [channels, setChannels] = useState([]);

  const [countryId, setCountryId] = useState(1);
  const [stateId, setStateId] = useState(0);
  const [countyId, setCountyId] = useState(0);
  const [systemType, setSystemType] = useState("digital");
  const [systemId, setSystemId] = useState("");
  const [systemQuery, setSystemQuery] = useState("");
  const [channelQuery, setChannelQuery] = useState("");
  const [channelsTruncated, setChannelsTruncated] = useState(false);
  const [selectedChannelIds, setSelectedChannelIds] = useState([]);
  const [wizardLoading, setWizardLoading] = useState(false);

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

  useEffect(() => {
    let cancelled = false;
    const loadCountries = async () => {
      setWizardLoading(true);
      try {
        const payload = await hpApi.getFavoritesWizardCountries();
        if (cancelled) {
          return;
        }
        const rows = Array.isArray(payload?.countries) ? payload.countries : [];
        setCountries(rows);
        if (rows.length > 0) {
          const preferred = rows.find((row) => Number(row.country_id) === 1) || rows[0];
          setCountryId(Number(preferred.country_id) || 1);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message || "Failed to load countries.");
        }
      } finally {
        if (!cancelled) {
          setWizardLoading(false);
        }
      }
    };
    loadCountries();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!countryId) {
      return;
    }
    let cancelled = false;
    const loadStates = async () => {
      setWizardLoading(true);
      setSystems([]);
      setChannels([]);
      setSystemId("");
      setSelectedChannelIds([]);
      try {
        const payload = await hpApi.getFavoritesWizardStates(countryId);
        if (cancelled) {
          return;
        }
        const rows = Array.isArray(payload?.states) ? payload.states : [];
        setStates(rows);
        if (rows.length > 0) {
          setStateId(Number(rows[0].state_id) || 0);
        } else {
          setStateId(0);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message || "Failed to load states.");
        }
      } finally {
        if (!cancelled) {
          setWizardLoading(false);
        }
      }
    };
    loadStates();
    return () => {
      cancelled = true;
    };
  }, [countryId]);

  useEffect(() => {
    if (!stateId) {
      return;
    }
    let cancelled = false;
    const loadCounties = async () => {
      setWizardLoading(true);
      setSystems([]);
      setChannels([]);
      setSystemId("");
      setSelectedChannelIds([]);
      try {
        const payload = await hpApi.getFavoritesWizardCounties(stateId);
        if (cancelled) {
          return;
        }
        const rows = Array.isArray(payload?.counties) ? payload.counties : [];
        setCounties(rows);
        if (rows.length > 0) {
          setCountyId(Number(rows[0].county_id) || 0);
        } else {
          setCountyId(0);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message || "Failed to load counties.");
        }
      } finally {
        if (!cancelled) {
          setWizardLoading(false);
        }
      }
    };
    loadCounties();
    return () => {
      cancelled = true;
    };
  }, [stateId]);

  useEffect(() => {
    if (!stateId) {
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      setWizardLoading(true);
      try {
        const payload = await hpApi.getFavoritesWizardSystems({
          stateId,
          countyId,
          systemType,
          q: systemQuery.trim(),
        });
        if (cancelled) {
          return;
        }
        const rows = Array.isArray(payload?.systems) ? payload.systems : [];
        setSystems(rows);
        if (rows.length > 0) {
          const nextId = String(rows[0].id || "").trim();
          setSystemId(nextId);
        } else {
          setSystemId("");
          setChannels([]);
          setSelectedChannelIds([]);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message || "Failed to load systems.");
        }
      } finally {
        if (!cancelled) {
          setWizardLoading(false);
        }
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [stateId, countyId, systemType, systemQuery]);

  useEffect(() => {
    if (!systemId) {
      setChannels([]);
      setSelectedChannelIds([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      setWizardLoading(true);
      try {
        const payload = await hpApi.getFavoritesWizardChannels({
          systemType,
          systemId,
          q: channelQuery.trim(),
          limit: 500,
        });
        if (cancelled) {
          return;
        }
        const rows = Array.isArray(payload?.channels) ? payload.channels : [];
        setChannels(rows);
        setChannelsTruncated(Boolean(payload?.truncated));
        setSelectedChannelIds([]);
      } catch (err) {
        if (!cancelled) {
          setError(err.message || "Failed to load channels.");
        }
      } finally {
        if (!cancelled) {
          setWizardLoading(false);
        }
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [systemType, systemId, channelQuery]);

  const toggleChannel = (id) => {
    setSelectedChannelIds((current) => {
      const token = String(id || "");
      if (current.includes(token)) {
        return current.filter((value) => value !== token);
      }
      return [...current, token];
    });
  };

  const addSelectedChannels = () => {
    setError("");
    setWizardMessage("");
    const selectedSet = new Set(selectedChannelIds);
    const selected = channels.filter((channel) => selectedSet.has(String(channel.id || "")));
    if (selected.length === 0) {
      setError("No channels selected.");
      return;
    }
    setEntries((current) => {
      const existingKeys = new Set(current.map((entry) => favoriteKey(entry)).filter(Boolean));
      const additions = [];
      selected.forEach((channel) => {
        const next = channelToFavorite(channel);
        if (!next) {
          return;
        }
        const key = favoriteKey(next) || channelKey(channel);
        if (!key || existingKeys.has(key)) {
          return;
        }
        existingKeys.add(key);
        additions.push(next);
      });
      setWizardMessage(
        additions.length > 0
          ? `Added ${additions.length} channel${additions.length === 1 ? "" : "s"} to favorites.`
          : "All selected channels were already in favorites."
      );
      setSelectedChannelIds([]);
      return [...current, ...additions];
    });
  };

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
          Favorites Wizard (HP2 style)
        </div>
        <div className="muted">Country</div>
        <select
          className="input"
          value={countryId}
          onChange={(event) => setCountryId(Number(event.target.value) || 1)}
        >
          {countries.map((row) => (
            <option key={row.country_id} value={row.country_id}>
              {row.name}
            </option>
          ))}
        </select>

        <div className="muted">State / Province</div>
        <select
          className="input"
          value={stateId}
          onChange={(event) => setStateId(Number(event.target.value) || 0)}
        >
          {states.map((row) => (
            <option key={row.state_id} value={row.state_id}>
              {row.name}
            </option>
          ))}
        </select>

        <div className="muted">County</div>
        <select
          className="input"
          value={countyId}
          onChange={(event) => setCountyId(Number(event.target.value) || 0)}
        >
          {counties.map((row) => (
            <option key={`${row.county_id}:${row.name}`} value={row.county_id}>
              {row.name}
            </option>
          ))}
        </select>

        <div className="row" style={{ marginTop: "8px" }}>
          <label>
            <input
              type="radio"
              name="wizard-system-type"
              value="digital"
              checked={systemType === "digital"}
              onChange={() => setSystemType("digital")}
            />{" "}
            Digital
          </label>
          <label>
            <input
              type="radio"
              name="wizard-system-type"
              value="analog"
              checked={systemType === "analog"}
              onChange={() => setSystemType("analog")}
            />{" "}
            Analog
          </label>
        </div>

        <div className="muted" style={{ marginTop: "8px" }}>
          System Search
        </div>
        <input
          className="input"
          value={systemQuery}
          onChange={(event) => setSystemQuery(event.target.value)}
          placeholder="Filter systems"
        />

        <div className="muted">System</div>
        <select
          className="input"
          value={systemId}
          onChange={(event) => setSystemId(String(event.target.value || ""))}
        >
          {systems.map((row) => (
            <option key={`${row.system_type}:${row.id}`} value={row.id}>
              {row.name}
            </option>
          ))}
        </select>

        <div className="muted" style={{ marginTop: "8px" }}>
          Channel Search
        </div>
        <input
          className="input"
          value={channelQuery}
          onChange={(event) => setChannelQuery(event.target.value)}
          placeholder="Filter channels / talkgroups"
        />

        <div className="row" style={{ marginTop: "8px" }}>
          <Button
            variant="secondary"
            onClick={() => setSelectedChannelIds(channels.map((row) => String(row.id || "")))}
            disabled={channels.length === 0 || working}
          >
            Select All
          </Button>
          <Button
            variant="secondary"
            onClick={() => setSelectedChannelIds([])}
            disabled={selectedChannelIds.length === 0 || working}
          >
            Clear Selection
          </Button>
          <Button onClick={addSelectedChannels} disabled={selectedChannelIds.length === 0 || working}>
            Add Selected
          </Button>
        </div>

        {channelsTruncated ? (
          <div className="muted" style={{ marginTop: "6px" }}>
            Showing first 500 channels. Narrow search to see more.
          </div>
        ) : null}
        <div className="muted" style={{ marginTop: "6px" }}>
          Loaded {channels.length} channel{channels.length === 1 ? "" : "s"}.
        </div>
        {wizardLoading ? <div className="muted">Loading wizard data…</div> : null}

        <div className="list" style={{ marginTop: "8px", maxHeight: "320px", overflowY: "auto" }}>
          {channels.map((row) => {
            const id = String(row.id || "");
            const selected = selectedChannelIds.includes(id);
            const secondary =
              row.kind === "trunked"
                ? `TGID ${row.talkgroup} • ${row.department_name || "Department"}`
                : `${Number(row.frequency || 0).toFixed(4)} MHz • ${row.department_name || "Department"}`;
            return (
              <label key={id} className="row" style={{ marginBottom: "6px" }}>
                <span>
                  <strong>{row.alpha_tag || row.department_name || "Channel"}</strong>
                  <div className="muted">{secondary}</div>
                </span>
                <input type="checkbox" checked={selected} onChange={() => toggleChannel(id)} />
              </label>
            );
          })}
          {channels.length === 0 ? <div className="muted">No channels found for current selection.</div> : null}
        </div>
      </div>

      <div className="card">
        <div className="muted" style={{ marginBottom: "8px" }}>
          Manual Add (optional)
        </div>
        <div className="muted" style={{ marginBottom: "6px" }}>
          Trunked
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

        <div className="muted" style={{ marginBottom: "6px", marginTop: "10px" }}>
          Conventional
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
                    {entry.alpha_tag || "Channel"} - {entry.control_channels.join(", ")} MHz
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

      {wizardMessage ? <div className="message">{wizardMessage}</div> : null}
      {error ? <div className="error">{error}</div> : null}
      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
