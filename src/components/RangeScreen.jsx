import React, { useEffect, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

export default function RangeScreen() {
  const { state, saveHpState, navigate } = useUI();
  const { hpState, working } = state;
  const [rangeMiles, setRangeMiles] = useState(15);

  useEffect(() => {
    const nextRange = Number(hpState.range_miles);
    setRangeMiles(Number.isFinite(nextRange) ? nextRange : 15);
  }, [hpState.range_miles]);

  const handleSave = async () => {
    try {
      await saveHpState({ range_miles: rangeMiles });
      navigate(SCREENS.MENU);
    } catch {
      // Context error is shown globally.
    }
  };

  return (
    <section className="screen range-screen">
      <Header title="Range" showBack onBack={() => navigate(SCREENS.MENU)} />

      <div className="card">
        <div className="row">
          <span>Range Miles</span>
          <strong>{rangeMiles}</strong>
        </div>
        <input
          className="range"
          type="range"
          min="0"
          max="100"
          step="1"
          value={rangeMiles}
          onChange={(e) => setRangeMiles(Number(e.target.value))}
        />
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
