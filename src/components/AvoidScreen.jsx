import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

function normalizeAvoidList(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }

  return raw.map((item, index) => {
    if (item && typeof item === "object") {
      return {
        id: item.id ?? `${item.type || "item"}-${index}`,
        label: String(item.label || item.alpha_tag || item.name || `Avoid ${index + 1}`),
        type: String(item.type || "item"),
      };
    }

    return {
      id: `item-${index}`,
      label: String(item),
      type: "item",
    };
  });
}

export default function AvoidScreen() {
  const { state, saveHpState, avoidCurrent, navigate } = useUI();
  const { hpState, working } = state;

  const sourceAvoid = useMemo(() => {
    if (Array.isArray(hpState.avoid_list)) {
      return hpState.avoid_list;
    }
    if (Array.isArray(hpState.avoids)) {
      return hpState.avoids;
    }
    if (Array.isArray(hpState.avoid)) {
      return hpState.avoid;
    }
    return [];
  }, [hpState.avoid_list, hpState.avoids, hpState.avoid]);

  const [avoidList, setAvoidList] = useState([]);

  useEffect(() => {
    setAvoidList(normalizeAvoidList(sourceAvoid));
  }, [sourceAvoid]);

  const clearAll = () => {
    setAvoidList([]);
  };

  const saveList = async (nextList = avoidList) => {
    try {
      await saveHpState({ avoid_list: nextList });
    } catch {
      // Context error is shown globally.
    }
  };

  const handleAvoidCurrent = async () => {
    try {
      await avoidCurrent();
    } catch {
      // Context error is shown globally.
    }
  };

  return (
    <section className="screen avoid-screen">
      <Header title="Avoid" showBack onBack={() => navigate(SCREENS.MENU)} />

      {avoidList.length === 0 ? (
        <div className="muted">No avoided items in current state.</div>
      ) : (
        <div className="list">
          {avoidList.map((item) => (
            <div key={item.id} className="row card">
              <div>
                <div>{item.label}</div>
                <div className="muted">{item.type}</div>
              </div>
              <Button
                variant="danger"
                onClick={() => {
                  const next = avoidList.filter((entry) => entry.id !== item.id);
                  setAvoidList(next);
                  saveList(next);
                }}
                disabled={working}
              >
                Remove
              </Button>
            </div>
          ))}
        </div>
      )}

      <div className="button-row">
        <Button onClick={handleAvoidCurrent} disabled={working}>
          Avoid Current
        </Button>
        <Button
          variant="secondary"
          onClick={() => {
            clearAll();
            saveList([]);
          }}
          disabled={working}
        >
          Clear
        </Button>
        <Button onClick={() => saveList()} disabled={working}>
          Save
        </Button>
      </div>

      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
