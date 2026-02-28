import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";

const PAGE_SIZE = 8;
const SLOT_COUNT = 8;

function toNumber(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num : 0;
}

export default function ServiceTypesScreen() {
  const { state, saveHpState, navigate } = useUI();
  const { hpState, serviceTypes, working } = state;

  const fallbackEnabled = useMemo(
    () =>
      serviceTypes
        .filter((item) => item.enabled_by_default)
        .map((item) => Number(item.service_tag)),
    [serviceTypes]
  );

  const orderedTypes = useMemo(() => {
    return [...serviceTypes].sort((a, b) => {
      const aTag = toNumber(a.service_tag);
      const bTag = toNumber(b.service_tag);
      if (aTag !== bTag) {
        return aTag - bTag;
      }
      return String(a.name || "").localeCompare(String(b.name || ""));
    });
  }, [serviceTypes]);

  const [selected, setSelected] = useState([]);
  const [pageIndex, setPageIndex] = useState(0);
  const [localMessage, setLocalMessage] = useState("");
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    const fromState = Array.isArray(hpState.enabled_service_tags)
      ? hpState.enabled_service_tags.map(Number)
      : fallbackEnabled;
    setSelected(Array.from(new Set(fromState)).filter((tag) => Number.isFinite(tag) && tag > 0));
  }, [hpState.enabled_service_tags, fallbackEnabled]);

  const totalPages = Math.max(1, Math.ceil(orderedTypes.length / PAGE_SIZE));

  useEffect(() => {
    if (pageIndex >= totalPages) {
      setPageIndex(Math.max(0, totalPages - 1));
    }
  }, [pageIndex, totalPages]);

  const pageItems = useMemo(() => {
    const start = pageIndex * PAGE_SIZE;
    const page = orderedTypes.slice(start, start + PAGE_SIZE);
    const slots = [...page];
    while (slots.length < SLOT_COUNT) {
      slots.push(null);
    }
    return slots;
  }, [orderedTypes, pageIndex]);

  const toggleTag = (tag) => {
    setLocalError("");
    setLocalMessage("");
    setSelected((current) =>
      current.includes(tag)
        ? current.filter((item) => item !== tag)
        : [...current, tag]
    );
  };

  const saveSelection = async (onSuccess) => {
    setLocalError("");
    setLocalMessage("");
    try {
      await saveHpState({
        enabled_service_tags: [...selected].sort((a, b) => a - b),
      });
      if (typeof onSuccess === "function") {
        onSuccess();
      } else {
        setLocalMessage("Service types saved.");
      }
    } catch (err) {
      setLocalError(err?.message || "Failed to save service types.");
    }
  };

  return (
    <section className="screen hp2-picker service-types-screen">
      <div className="hp2-picker-top">
        <div className="hp2-picker-title">Select Service Types</div>
        <div className="hp2-picker-top-right">
          <span className="hp2-picker-help">Help</span>
          <span className="hp2-picker-status">L</span>
          <span className="hp2-picker-status">SIG</span>
          <span className="hp2-picker-status">BAT</span>
        </div>
      </div>

      <div className="hp2-picker-grid">
        {pageItems.map((item, index) => {
          if (!item) {
            return <div key={`empty-${index}`} className="hp2-picker-tile hp2-picker-tile-empty" />;
          }

          const tag = Number(item.service_tag);
          const enabled = selected.includes(tag);
          return (
            <button
              key={`${tag}-${item.name}`}
              type="button"
              className={`hp2-picker-tile ${enabled ? "active" : ""}`}
              onClick={() => toggleTag(tag)}
              disabled={working}
            >
              {item.name}
            </button>
          );
        })}
      </div>

      <div className="hp2-picker-bottom hp2-picker-bottom-5">
        <button
          type="button"
          className="hp2-picker-btn listen"
          onClick={() => saveSelection(() => navigate(SCREENS.MAIN))}
          disabled={working}
        >
          Listen
        </button>
        <button
          type="button"
          className="hp2-picker-btn"
          onClick={() => navigate(SCREENS.MENU)}
          disabled={working}
        >
          Back
        </button>
        <button
          type="button"
          className="hp2-picker-btn"
          onClick={() => saveSelection(() => navigate(SCREENS.MENU))}
          disabled={working}
        >
          Accept
        </button>
        <button
          type="button"
          className="hp2-picker-btn"
          onClick={() => setPageIndex((current) => Math.max(0, current - 1))}
          disabled={working || pageIndex <= 0}
        >
          ^
        </button>
        <button
          type="button"
          className="hp2-picker-btn"
          onClick={() => setPageIndex((current) => Math.min(totalPages - 1, current + 1))}
          disabled={working || pageIndex >= totalPages - 1}
        >
          v
        </button>
      </div>

      <div className="muted hp2-picker-page">
        Page {pageIndex + 1} / {totalPages}
      </div>

      {localMessage ? <div className="message">{localMessage}</div> : null}
      {localError ? <div className="error">{localError}</div> : null}
      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
