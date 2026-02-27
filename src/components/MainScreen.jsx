import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";

function formatValue(value) {
  if (value === null || value === undefined || value === "") {
    return "--";
  }
  return String(value);
}

function signalBarText(level) {
  const clamped = Math.max(0, Math.min(4, Number(level) || 0));
  return `${"|".repeat(clamped)}${".".repeat(4 - clamped)}`;
}

function formatRangeMiles(value) {
  const range = Number(value);
  if (!Number.isFinite(range)) {
    return "Range";
  }
  return Number.isInteger(range) ? `Range ${range}` : `Range ${range.toFixed(1)}`;
}

export default function MainScreen() {
  const { state, holdScan, nextScan, avoidCurrent, navigate } = useUI();
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
  const [submenuRow, setSubmenuRow] = useState("");
  const [audioMuted, setAudioMuted] = useState(false);
  const [hint, setHint] = useState("");

  useEffect(() => {
    if (streamSource === "digital" && !hasDigital) {
      setStreamSource(hasAnalog ? "analog" : "digital");
      return;
    }
    if (streamSource === "analog" && !hasAnalog && hasDigital) {
      setStreamSource("digital");
    }
  }, [hasAnalog, hasDigital, streamSource]);

  useEffect(() => {
    if (!error && !message) {
      return;
    }
    setHint("");
  }, [error, message]);

  const selectedMount =
    streamSource === "digital"
      ? digitalMount || analogMount
      : analogMount || digitalMount;
  const isDigitalSource = streamSource === "digital" && hasDigital;
  const hpScanMode = String(hpState.mode || "full_database")
    .trim()
    .toLowerCase();
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
  const channelLabel = isDigitalSource
    ? liveStatus?.digital_last_label || hpState.channel_name || hpState.channel || department
    : department;
  const channelService = isDigitalSource
    ? liveStatus?.digital_last_mode || hpState.service_type || hpState.service || ""
    : "";
  const channelLine = isDigitalSource
    ? channelLabel
    : department;
  const channelMeta = isDigitalSource
    ? `${formatValue(channelService || "Digital")} • ${formatValue(tgid)} • ${signal}`
    : `${formatValue(frequency)} • ${signal}`;
  const signalBars = isDigitalSource
    ? liveStatus?.digital_control_channel_locked
      ? 4
      : liveStatus?.digital_control_decode_available
      ? 3
      : 1
    : liveStatus?.rtl_active
    ? 3
    : 1;
  const holdLocked =
    String(liveStatus?.digital_scan_mode || "").toLowerCase() === "single_system";
  const scannerStatus = holdLocked ? "HOLD" : "SCAN";
  const favoriteDescriptor = useMemo(() => {
    if (hpScanMode !== "favorites") {
      return "Full Database";
    }
    const favorites = Array.isArray(hpState.favorites) ? hpState.favorites : [];
    if (favorites.length === 0) {
      return "Favorites";
    }
    const enabled = favorites.filter((entry) => Boolean(entry?.enabled));
    if (enabled.length === 0) {
      return "Favorites";
    }
    const bySource = enabled.find((entry) => {
      const entryType = String(entry?.type || "").trim().toLowerCase();
      if (isDigitalSource) {
        return entryType === "digital";
      }
      return entryType === "analog";
    });
    const selected = bySource || enabled[0];
    const label = String(selected?.label || "").trim();
    return label || "Favorites";
  }, [hpScanMode, hpState.favorites, isDigitalSource]);

  const doHold = async () => {
    try {
      await holdScan();
    } catch {
      // Context handles error display.
    }
  };

  const doNext = async () => {
    try {
      await nextScan();
    } catch {
      // Context handles error display.
    }
  };

  const doAvoid = async () => {
    try {
      await avoidCurrent();
    } catch {
      // Context handles error display.
    }
  };

  const onSubmenuAction = async (action, rowKey) => {
    if (action === "info") {
      if (rowKey === "system") {
        setHint(`System: ${formatValue(system)}`);
      } else if (rowKey === "department") {
        setHint(`Department: ${formatValue(department)}`);
      } else {
        setHint(`Channel: ${formatValue(channelLine)} (${formatValue(channelMeta)})`);
      }
      setSubmenuRow("");
      return;
    }

    if (action === "advanced") {
      setHint("Advanced options are still being wired in HP3.");
      setSubmenuRow("");
      return;
    }

    if (action === "prev") {
      setHint("Previous-channel stepping is not wired yet in HP3.");
      setSubmenuRow("");
      return;
    }

    if (action === "fave") {
      setSubmenuRow("");
      navigate(SCREENS.FAVORITES);
      return;
    }

    if (!isDigitalSource) {
      setHint("Switch Audio Source to Digital for HOLD/NEXT/AVOID controls.");
      setSubmenuRow("");
      return;
    }

    if (action === "hold") {
      await doHold();
    } else if (action === "next") {
      await doNext();
    } else if (action === "avoid") {
      await doAvoid();
    }
    setSubmenuRow("");
  };

  const radioControls = useMemo(
    () => [
      {
        id: "squelch",
        label: "Squelch",
        onClick: () => setHint("Squelch is currently managed from SB3 analog controls."),
      },
      {
        id: "range",
        label: formatRangeMiles(hpState.range_miles),
        onClick: () => navigate(SCREENS.RANGE),
      },
      {
        id: "atten",
        label: "Atten",
        onClick: () => setHint("Attenuation toggle is not wired yet in HP3."),
      },
      {
        id: "gps",
        label: "GPS",
        onClick: () => navigate(SCREENS.LOCATION),
      },
      {
        id: "help",
        label: "Help",
        onClick: () => navigate(SCREENS.MENU),
      },
    ],
    [hpState.range_miles, navigate]
  );

  const submenuActions = {
    system: [
      { id: "info", label: "Info" },
      { id: "advanced", label: "Advanced" },
      { id: "prev", label: "Prev" },
      { id: "next", label: "Next" },
      { id: "avoid", label: "Avoid" },
    ],
    department: [
      { id: "info", label: "Info" },
      { id: "advanced", label: "Advanced" },
      { id: "prev", label: "Prev" },
      { id: "next", label: "Next" },
      { id: "avoid", label: "Avoid" },
    ],
    channel: [
      { id: "info", label: "Info" },
      { id: "advanced", label: "Advanced" },
      { id: "prev", label: "Prev" },
      { id: "hold", label: "Hold" },
      { id: "next", label: "Next" },
      { id: "avoid", label: "Avoid" },
      { id: "fave", label: "Fave" },
    ],
  };

  return (
    <section className="screen main-screen hp2-main">
      <div className="hp2-radio-bar">
        <div className="hp2-radio-buttons">
          {radioControls.map((item) => (
            <button
              key={item.id}
              type="button"
              className="hp2-radio-btn"
              onClick={item.onClick}
              disabled={working}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="hp2-status-icons">
          <span className={`hp2-icon ${holdLocked ? "on" : ""}`}>{scannerStatus}</span>
          <span className="hp2-icon">SIG {signalBarText(signalBars)}</span>
          <span className="hp2-icon">{isDigitalSource ? "DIG" : "ANA"}</span>
        </div>
      </div>

      <div className="hp2-lines">
        <div className="hp2-line">
          <div className="hp2-line-label">System / Favorite List</div>
          <div className="hp2-line-body">
            <div className="hp2-line-primary">{formatValue(system)}</div>
            <div className="hp2-line-secondary">{favoriteDescriptor}</div>
          </div>
          <button
            type="button"
            className="hp2-subtab"
            onClick={() => setSubmenuRow((current) => (current === "system" ? "" : "system"))}
            disabled={working}
          >
            {"<"}
          </button>
        </div>

        <div className="hp2-line">
          <div className="hp2-line-label">Department</div>
          <div className="hp2-line-body">
            <div className="hp2-line-primary">{formatValue(department)}</div>
            <div className="hp2-line-secondary">Service: {formatValue(hpState.mode)}</div>
          </div>
          <button
            type="button"
            className="hp2-subtab"
            onClick={() => setSubmenuRow((current) => (current === "department" ? "" : "department"))}
            disabled={working}
          >
            {"<"}
          </button>
        </div>

        <div className="hp2-line channel">
          <div className="hp2-line-label">Channel</div>
          <div className="hp2-line-body">
            <div className="hp2-line-primary">{formatValue(channelLine)}</div>
            <div className="hp2-line-secondary">{formatValue(channelMeta)}</div>
          </div>
          <button
            type="button"
            className="hp2-subtab"
            onClick={() => setSubmenuRow((current) => (current === "channel" ? "" : "channel"))}
            disabled={working}
          >
            {"<"}
          </button>
        </div>
      </div>

      {submenuRow ? (
        <div className="hp2-submenu-popup">
          {submenuActions[submenuRow]?.map((item) => (
            <button
              key={item.id}
              type="button"
              className="hp2-submenu-btn"
              onClick={() => onSubmenuAction(item.id, submenuRow)}
              disabled={working}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}

      <div className="hp2-feature-bar">
        <button
          type="button"
          className="hp2-feature-btn"
          onClick={() => navigate(SCREENS.MENU)}
          disabled={working}
        >
          Menu
        </button>
        <button
          type="button"
          className="hp2-feature-btn"
          onClick={() => setHint("Replay is not wired yet in HP3.")}
          disabled={working}
        >
          Replay
        </button>
        <button
          type="button"
          className="hp2-feature-btn"
          onClick={() => setHint("Recording controls are not wired yet in HP3.")}
          disabled={working}
        >
          Record
        </button>
        <button
          type="button"
          className="hp2-feature-btn"
          onClick={() => setAudioMuted((value) => !value)}
          disabled={working}
        >
          {audioMuted ? "Unmute" : "Mute"}
        </button>
      </div>

      <div className="hp2-web-audio">
        <div className="hp2-audio-head">
          <div className="muted">Web Audio Stream</div>
          {selectedMount ? (
            <a href={`/stream/${selectedMount}`} target="_blank" rel="noreferrer">
              Open
            </a>
          ) : null}
        </div>
        <div className="hp2-source-switch">
          <Button
            variant={streamSource === "analog" ? "primary" : "secondary"}
            onClick={() => setStreamSource("analog")}
            disabled={!hasAnalog || working}
          >
            Analog
          </Button>
          <Button
            variant={streamSource === "digital" ? "primary" : "secondary"}
            onClick={() => setStreamSource("digital")}
            disabled={!hasDigital || working}
          >
            Digital
          </Button>
        </div>
        <div className="muted hp2-audio-meta">
          Source: {isDigitalSource ? "Digital" : "Analog"} ({selectedMount || "no mount"})
        </div>
        <audio
          controls
          preload="none"
          muted={audioMuted}
          className="hp2-audio-player"
          src={selectedMount ? `/stream/${selectedMount}` : "/stream/"}
        />
      </div>

      {hint ? <div className="message">{hint}</div> : null}
      {!isDigitalSource ? (
        <div className="muted">
          HOLD/NEXT/AVOID actions require Digital source.
        </div>
      ) : null}

      {error ? <div className="error">{error}</div> : null}
      {message ? <div className="message">{message}</div> : null}
    </section>
  );
}
