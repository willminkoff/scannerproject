import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

function normalizePersistentAvoidList(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }

  return raw.map((item, index) => {
    if (item && typeof item === "object") {
      return {
        id: String(item.id ?? `${item.type || "item"}-${index}`),
        label: String(item.label || item.alpha_tag || item.name || `Avoid ${index + 1}`),
        type: String(item.type || "item"),
        source: "persistent",
      };
    }

    return {
      id: `item-${index}`,
      label: String(item),
      type: "item",
      source: "persistent",
    };
  });
}

function normalizeRuntimeAvoids(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }
  const out = [];
  const seen = new Set();
  raw.forEach((item) => {
    const token = String(item || "").trim();
    if (!token || seen.has(token)) {
      return;
    }
    seen.add(token);
    out.push({
      id: `runtime:${token}`,
      label: token,
      type: "system",
      token,
      source: "runtime",
    });
  });
  return out;
}

export default function AvoidScreen() {
  const {
    state,
    saveHpState,
    avoidCurrent,
    clearHpAvoids,
    removeHpAvoid,
    navigate,
  } = useUI();
  const { hpState, hpAvoids, working } = state;

  const sourcePersistent = useMemo(() => {
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

  const [persistentList, setPersistentList] = useState([]);

  useEffect(() => {
    setPersistentList(normalizePersistentAvoidList(sourcePersistent));
  }, [sourcePersistent]);

  const runtimeList = useMemo(() => normalizeRuntimeAvoids(hpAvoids), [hpAvoids]);

  const savePersistent = async (nextList = persistentList) => {
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

      <div className="list">
        <div className="card">
          <div className="muted" style={{ marginBottom: "8px" }}>
            Runtime Avoids (HP Scan Pool)
          </div>
          {runtimeList.length === 0 ? (
            <div className="muted">No runtime HP avoids.</div>
          ) : (
            runtimeList.map((item) => (
              <div key={item.id} className="row" style={{ marginBottom: "6px" }}>
                <div>
                  <div>{item.label}</div>
                  <div className="muted">{item.type}</div>
                </div>
                <Button
                  variant="danger"
                  onClick={() => removeHpAvoid(item.token)}
                  disabled={working}
                >
                  Remove
                </Button>
              </div>
            ))
          )}
        </div>

        <div className="card">
          <div className="muted" style={{ marginBottom: "8px" }}>
            Persistent Avoids (State)
          </div>
          {persistentList.length === 0 ? (
            <div className="muted">No persistent avoids in current state.</div>
          ) : (
            persistentList.map((item) => (
              <div key={item.id} className="row" style={{ marginBottom: "6px" }}>
                <div>
                  <div>{item.label}</div>
                  <div className="muted">{item.type}</div>
                </div>
                <Button
                  variant="danger"
                  onClick={() => {
                    const next = persistentList.filter((entry) => entry.id !== item.id);
                    setPersistentList(next);
                    savePersistent(next);
                  }}
                  disabled={working}
                >
                  Remove
                </Button>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="button-row">
        <Button onClick={handleAvoidCurrent} disabled={working}>
          Avoid Current
        </Button>
        <Button
          variant="secondary"
          onClick={async () => {
            setPersistentList([]);
            await savePersistent([]);
            await clearHpAvoids();
          }}
          disabled={working}
        >
          Clear All
        </Button>
        <Button onClick={() => savePersistent()} disabled={working}>
          Save
        </Button>
      </div>

      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
