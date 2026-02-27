import React, { useEffect, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

export default function ModeSelectionScreen() {
  const { state, setMode, navigate } = useUI();
  const [mode, setLocalMode] = useState("hp");

  useEffect(() => {
    setLocalMode(state.mode || "hp");
  }, [state.mode]);

  const handleSave = async () => {
    try {
      await setMode(mode);
      navigate(SCREENS.MENU);
    } catch {
      // Context error is shown globally.
    }
  };

  return (
    <section className="screen mode-selection-screen">
      <Header
        title="Mode Selection"
        showBack
        onBack={() => navigate(SCREENS.MENU)}
      />

      <div className="list">
        <label className="row card">
          <span>HP Mode</span>
          <input
            type="radio"
            name="scan-mode"
            value="hp"
            checked={mode === "hp"}
            onChange={(e) => setLocalMode(e.target.value)}
          />
        </label>

        <label className="row card">
          <span>Expert Mode</span>
          <input
            type="radio"
            name="scan-mode"
            value="expert"
            checked={mode === "expert"}
            onChange={(e) => setLocalMode(e.target.value)}
          />
        </label>
      </div>

      <div className="button-row">
        <Button onClick={handleSave} disabled={state.working}>
          Save
        </Button>
      </div>

      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
