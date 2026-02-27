import React from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

const menuItems = [
  { id: SCREENS.LOCATION, label: "Location" },
  { id: SCREENS.SERVICE_TYPES, label: "Service Types" },
  { id: SCREENS.RANGE, label: "Range" },
  { id: SCREENS.FAVORITES, label: "Favorites" },
  { id: SCREENS.AVOID, label: "Avoid" },
  { id: SCREENS.MODE_SELECTION, label: "Mode Selection" },
];

export default function Menu() {
  const { navigate, state } = useUI();

  return (
    <section className="screen menu">
      <Header title="Menu" showBack onBack={() => navigate(SCREENS.MAIN)} />

      <div className="menu-list">
        {menuItems.map((item) => (
          <Button
            key={item.id}
            variant="secondary"
            className="menu-item"
            onClick={() => navigate(item.id)}
            disabled={state.working}
          >
            {item.label}
          </Button>
        ))}
      </div>

      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
