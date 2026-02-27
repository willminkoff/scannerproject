import React, { useEffect, useState } from "react";
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
  const hasAnalog = Boolean(analogMount);
  const hasDigital = Boolean(digitalMount);
  const defaultSource =
    state.mode === "hp" || state.mode === "expert"
      ? hasDigital
        ? "digital"
        : "analog"
      : "analog";
  const [streamSource, setStreamSource] = useState(defaultSource);

  useEffect(() => {
    if (streamSource === "digital" && !hasDigital) {
      setStreamSource(hasAnalog ? "analog" : "digital");
      return;
    }
    if (streamSource === "analog" && !hasAnalog && hasDigital) {
      setStreamSource("digital");
    }
  }, [hasAnalog, hasDigital, streamSource]);

  const selectedMount =
    streamSource === "digital"
      ? digitalMount || analogMount
      : analogMount || digitalMount;
  const isDigitalSource = streamSource === "digital" && hasDigital;
  const sourceLabel = isDigitalSource ? "Digital" : "Analog";
  const system = isDigitalSource
    ? liveStatus?.digital_scheduler_active_system ||
      liveStatus?.digital_profile ||
      hpState.system_name ||
      hpState.system
    : liveStatus?.profile_airband || "Airband";
  const department = isDigitalSource
    ? liveStatus?.digital_last_label || hpState.department_name || hpState.department
    : liveStatus?.last_hit_airband_label ||
      liveStatus?.last_hit_ground_label ||
      liveStatus?.last_hit ||
      hpState.department_name ||
      hpState.department;
  const tgid = isDigitalSource
    ? liveStatus?.digital_last_tgid ?? hpState.tgid ?? hpState.talkgroup_id
    : "--";
  const frequency = isDigitalSource
    ? (() => {
        const firstHz = Number(
          liveStatus?.digital_preflight?.playlist_frequency_hz?.[0] ||
            liveStatus?.digital_playlist_frequency_hz?.[0] ||
            0
        );
        if (Number.isFinite(firstHz) && firstHz > 0) {
          return (firstHz / 1_000_000).toFixed(4);
        }
        return hpState.frequency ?? hpState.freq;
      })()
    : liveStatus?.last_hit_airband ||
      liveStatus?.last_hit_ground ||
      liveStatus?.last_hit ||
      "--";
  const signal = isDigitalSource
    ? liveStatus?.digital_control_channel_locked
      ? "Locked"
      : liveStatus?.digital_control_decode_available
      ? "Decoding"
      : hpState.signal ?? hpState.signal_strength
    : liveStatus?.rtl_active
    ? "Active"
    : "Idle";
  const scannerControlDisabled = working || !isDigitalSource;

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
        <Button onClick={handleHold} disabled={scannerControlDisabled}>
          HOLD
        </Button>
        <Button onClick={handleNext} disabled={scannerControlDisabled}>
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
      {!isDigitalSource ? (
        <div className="muted" style={{ marginTop: "8px" }}>
          HOLD/NEXT control digital scanning only. Switch source to Digital to control scan.
        </div>
      ) : null}

      <div className="card" style={{ marginTop: "12px" }}>
        <div className="row" style={{ marginBottom: "8px" }}>
          <div className="muted">Audio Source</div>
          {selectedMount ? (
            <a href={`/stream/${selectedMount}`} target="_blank" rel="noreferrer">
              Open
            </a>
          ) : null}
        </div>
        <div className="button-row" style={{ marginTop: 0 }}>
          <Button
            variant={streamSource === "analog" ? "primary" : "secondary"}
            onClick={() => setStreamSource("analog")}
            disabled={!hasAnalog}
          >
            Analog
          </Button>
          <Button
            variant={streamSource === "digital" ? "primary" : "secondary"}
            onClick={() => setStreamSource("digital")}
            disabled={!hasDigital}
          >
            Digital
          </Button>
        </div>
        <div className="muted" style={{ marginTop: "8px", marginBottom: "8px" }}>
          Monitoring {sourceLabel} ({selectedMount || "no mount"})
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
