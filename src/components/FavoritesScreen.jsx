import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";
import Button from "./Shared/Button";
import Header from "./Shared/Header";

function normalizeFavorites(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }

  return raw.map((item, index) => {
    if (item && typeof item === "object") {
      return {
        id: item.id ?? item.name ?? `fav-${index}`,
        name: String(item.name || item.label || `Favorite ${index + 1}`),
        enabled: item.enabled !== false,
      };
    }

    return {
      id: `fav-${index}`,
      name: String(item),
      enabled: true,
    };
  });
}

export default function FavoritesScreen() {
  const { state, saveHpState, navigate } = useUI();
  const { hpState, working } = state;

  const sourceFavorites = useMemo(() => {
    if (Array.isArray(hpState.favorites)) {
      return hpState.favorites;
    }
    if (Array.isArray(hpState.favorites_list)) {
      return hpState.favorites_list;
    }
    return [];
  }, [hpState.favorites, hpState.favorites_list]);

  const [favorites, setFavorites] = useState([]);

  useEffect(() => {
    setFavorites(normalizeFavorites(sourceFavorites));
  }, [sourceFavorites]);

  const toggleFavorite = (id) => {
    setFavorites((current) =>
      current.map((item) =>
        item.id === id ? { ...item, enabled: !item.enabled } : item
      )
    );
  };

  const handleSave = async () => {
    try {
      await saveHpState({ favorites });
      navigate(SCREENS.MENU);
    } catch {
      // Context error is shown globally.
    }
  };

  return (
    <section className="screen favorites-screen">
      <Header title="Favorites" showBack onBack={() => navigate(SCREENS.MENU)} />

      {favorites.length === 0 ? (
        <div className="muted">No favorites in current state.</div>
      ) : (
        <div className="list">
          {favorites.map((item) => (
            <label key={item.id} className="row card">
              <span>{item.name}</span>
              <input
                type="checkbox"
                checked={item.enabled}
                onChange={() => toggleFavorite(item.id)}
              />
            </label>
          ))}
        </div>
      )}

      <div className="button-row">
        <Button onClick={handleSave} disabled={working}>
          Save
        </Button>
      </div>

      {state.error ? <div className="error">{state.error}</div> : null}
    </section>
  );
}
