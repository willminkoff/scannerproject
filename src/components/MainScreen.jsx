import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

function formatValue(value) {
  if (value === null || value === undefined || value === "") {
    return "--";
  }
  return String(value);
}

export default function MainScreen() {
  const { state, holdScan, nextScan, navigate } = useUI();
  const { hpState, liveStatus, working, error, message } = state;

  const analogMount = String(liveStatus?.stream_mount || "ANALOG.mp3")
    .trim()
    .replace(/^\//, "");
  const digitalMount = String(liveStatus?.digital_stream_mount || "DIGITAL.mp3")
    .trim()
    .replace(/^\//, "");
  const defaultMount = state.mode === "hp" ? digitalMount || analogMount : analogMount;
  const streamOptions = useMemo(() => {
    const out = [];
    if (analogMount) {
      out.push({ id: analogMount, label: `Analog (${analogMount})` });
    }
    if (digitalMount && digitalMount !== analogMount) {
      out.push({ id: digitalMount, label: `Digital (${digitalMount})` });
    }
    return out;
  }, [analogMount, digitalMount]);
  const [selectedMount, setSelectedMount] = useState(defaultMount);

  useEffect(() => {
    const valid = streamOptions.some((item) => item.id === selectedMount);
    if (!valid) {
      setSelectedMount(defaultMount || streamOptions[0]?.id || "");
    }
  }, [defaultMount, selectedMount, streamOptions]);

  const system =
    liveStatus?.digital_scheduler_active_system ||
    liveStatus?.digital_profile ||
    hpState.system_name ||
    hpState.system;
  const department =
    liveStatus?.digital_last_label || hpState.department_name || hpState.department;
  const tgid = liveStatus?.digital_last_tgid ?? hpState.tgid ?? hpState.talkgroup_id;
  const frequency = (() => {
    const firstHz = Number(
      liveStatus?.digital_preflight?.playlist_frequency_hz?.[0] ||
        liveStatus?.digital_playlist_frequency_hz?.[0] ||
        0
    );
    if (Number.isFinite(firstHz) && firstHz > 0) {
      return (firstHz / 1_000_000).toFixed(4);
    }
    return hpState.frequency ?? hpState.freq;
  })();
  const signal = liveStatus?.digital_control_channel_locked
    ? "Locked"
    : liveStatus?.digital_control_decode_available
    ? "Decoding"
    : hpState.signal ?? hpState.signal_strength;

  const handleHold = async () => {
    try {
      await holdScan();
    } catch {
      // Context tracks and surfaces errors.
    }
  };

  const handleNext = async () => {
    try {
      await nextScan();
    } catch {
      // Context tracks and surfaces errors.
    }
  };

  return (
    <section className="screen main-screen">
      <Header title="Home Patrol 3" subtitle={`Mode: ${state.mode.toUpperCase()}`} />

      <div className="field-grid">
        <div className="card">
          <div className="muted">System</div>
          <div>{formatValue(system)}</div>
        </div>
        <div className="card">
          <div className="muted">Department</div>
          <div>{formatValue(department)}</div>
        </div>
        <div className="card">
          <div className="muted">TGID</div>
          <div>{formatValue(tgid)}</div>
        </div>
        <div className="card">
          <div className="muted">Frequency</div>
          <div>{formatValue(frequency)}</div>
        </div>
        <div className="card">
          <div className="muted">Signal</div>
          <div>{formatValue(signal)}</div>
        </div>
      </div>

      <div className="button-row">
        <Button onClick={handleHold} disabled={working}>
          HOLD
        </Button>
        <Button onClick={handleNext} disabled={working}>
          NEXT
        </Button>
        <Button
          variant="secondary"
          onClick={() => navigate(SCREENS.MENU)}
          disabled={working}
        >
          MENU
        </Button>
      </div>

      <div className="card" style={{ marginTop: "12px" }}>
        <div className="row" style={{ marginBottom: "8px" }}>
          <div className="muted">Live Stream</div>
          {selectedMount ? (
            <a href={`/stream/${selectedMount}`} target="_blank" rel="noreferrer">
              Open
            </a>
          ) : null}
        </div>
        <div className="row" style={{ marginBottom: "8px" }}>
          <select
            className="input"
            value={selectedMount}
            onChange={(e) => setSelectedMount(e.target.value)}
            style={{ maxWidth: "260px" }}
          >
            {streamOptions.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </div>
        <audio
          controls
          preload="none"
          style={{ width: "100%" }}
          src={selectedMount ? `/stream/${selectedMount}` : "/stream/"}
        />
      </div>

      {error ? <div className="error">{error}</div> : null}
      {message ? <div className="message">{message}</div> : null}
    </section>
  );
}
