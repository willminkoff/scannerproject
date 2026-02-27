import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

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

  const [selected, setSelected] = useState([]);

  useEffect(() => {
    const fromState = Array.isArray(hpState.enabled_service_tags)
      ? hpState.enabled_service_tags.map(Number)
      : fallbackEnabled;
    setSelected(Array.from(new Set(fromState)).filter((tag) => Number.isFinite(tag)));
  }, [hpState.enabled_service_tags, fallbackEnabled]);

  const toggleTag = (tag) => {
    setSelected((current) =>
      current.includes(tag)
        ? current.filter((item) => item !== tag)
        : [...current, tag]
    );
  };

  const save = async () => {
    try {
      await saveHpState({
        enabled_service_tags: [...selected].sort((a, b) => a - b),
      });
      navigate(SCREENS.MENU);
    } catch {
      // Context error is shown globally.
    }
  };

  return (
    <section className="screen service-types-screen">
      <Header title="Service Types" showBack onBack={() => navigate(SCREENS.MENU)} />

      <div className="checkbox-list">
        {serviceTypes.map((item) => {
          const tag = Number(item.service_tag);
          const checked = selected.includes(tag);
          return (
            <label key={tag} className="row card">
              <span>{item.name}</span>
              <input
                type="checkbox"
                checked={checked}
                onChange={() => toggleTag(tag)}
              />
            </label>
          );
        })}
      </div>

      <div className="button-row">
        <Button onClick={save} disabled={working}>
          Save
        </Button>
      </div>

      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
