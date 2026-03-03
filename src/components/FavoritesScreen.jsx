import React, { useEffect, useMemo, useState } from "react";
import { SCREENS, useUI } from "../context/UIContext";

const PAGE_SIZE = 8;
const SLOT_COUNT = 8;
const FULL_DB_TILE_ID = "action:full_database";
const CREATE_LIST_TILE_ID = "action:create_list";

function normalizeLabel(value, fallback = "My Favorites") {
  const text = String(value || "").trim();
  return text || fallback;
}

function listTileId(label) {
  return `list:${normalizeLabel(label).toLowerCase()}`;
}

function collectListLabels(hpState) {
  const out = [];
  const seen = new Set();
  const push = (value) => {
    const label = normalizeLabel(value, "");
    if (!label) {
      return;
    }
    const token = label.toLowerCase();
    if (seen.has(token)) {
      return;
    }
    seen.add(token);
    out.push(label);
  };

  const favorites = Array.isArray(hpState?.favorites) ? hpState.favorites : [];
  favorites.forEach((item) => {
    if (!item || typeof item !== "object") {
      return;
    }
    push(item.label);
  });
  push(hpState?.favorites_name || "My Favorites");
  if (out.length === 0) {
    out.push("My Favorites");
  }
  return out;
}

function buildFavoritesMetadata(labels, activeLabel) {
  const activeToken = normalizeLabel(activeLabel).toLowerCase();
  return labels.map((label, index) => {
    const safeLabel = normalizeLabel(label);
    const slug = safeLabel
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
    return {
      id: slug ? `fav-${slug}` : `fav-${index + 1}`,
      type: "list",
      target: "favorites",
      profile_id: "",
      label: safeLabel,
      enabled: safeLabel.toLowerCase() === activeToken,
    };
  });
}

function pageSlots(tiles, pageIndex) {
  const start = pageIndex * PAGE_SIZE;
  const page = tiles.slice(start, start + PAGE_SIZE);
  const slots = [...page];
  while (slots.length < SLOT_COUNT) {
    slots.push(null);
  }
  return slots;
}

export default function FavoritesScreen() {
  const { state, saveHpState, navigate } = useUI();
  const { hpState, working } = state;

  const [selectedTileId, setSelectedTileId] = useState(FULL_DB_TILE_ID);
  const [pageIndex, setPageIndex] = useState(0);
  const [localMessage, setLocalMessage] = useState("");
  const [localError, setLocalError] = useState("");

  const listLabels = useMemo(() => collectListLabels(hpState), [hpState.favorites, hpState.favorites_name]);

  const tiles = useMemo(() => {
    const listTiles = listLabels.map((label) => ({
      id: listTileId(label),
      label,
      kind: "list",
    }));
    const out = [
      {
        id: FULL_DB_TILE_ID,
        label: "Select Database to Monitor",
        kind: "action",
        multiline: true,
      },
    ];
    if (listTiles[0]) {
      out.push(listTiles[0]);
    }
    out.push({
      id: CREATE_LIST_TILE_ID,
      label: "Create New List",
      kind: "action",
    });
    if (listTiles[1]) {
      out.push(listTiles[1]);
    }
    for (let idx = 2; idx < listTiles.length; idx += 1) {
      out.push(listTiles[idx]);
    }
    return out;
  }, [listLabels]);

  const totalPages = Math.max(1, Math.ceil(tiles.length / PAGE_SIZE));

  useEffect(() => {
    const mode = String(hpState.mode || "").trim().toLowerCase();
    const activeTile =
      mode === "favorites"
        ? listTileId(hpState.favorites_name || "My Favorites")
        : FULL_DB_TILE_ID;
    setSelectedTileId(activeTile);
    const index = Math.max(
      0,
      tiles.findIndex((tile) => tile.id === activeTile)
    );
    setPageIndex(Math.floor(index / PAGE_SIZE));
  }, [hpState.mode, hpState.favorites_name, tiles]);

  useEffect(() => {
    if (pageIndex >= totalPages) {
      setPageIndex(Math.max(0, totalPages - 1));
    }
  }, [pageIndex, totalPages]);

  const slots = useMemo(() => pageSlots(tiles, pageIndex), [tiles, pageIndex]);

  const saveFavoriteSelection = async (mode, favoritesName, labelsOverride) => {
    const labels = Array.isArray(labelsOverride) && labelsOverride.length > 0 ? labelsOverride : listLabels;
    const nextName = normalizeLabel(favoritesName || hpState.favorites_name || "My Favorites");
    await saveHpState({
      mode,
      favorites_name: nextName,
      favorites: buildFavoritesMetadata(labels, nextName),
    });
  };

  const activateFullDatabase = async () => {
    setLocalError("");
    setLocalMessage("");
    try {
      await saveFavoriteSelection("full_database", hpState.favorites_name || "My Favorites");
      setSelectedTileId(FULL_DB_TILE_ID);
      setLocalMessage("Monitoring Full Database.");
    } catch (err) {
      setLocalError(err?.message || "Failed to switch to Full Database.");
    }
  };

  const activateFavoritesList = async (label, labelsOverride) => {
    const nextLabel = normalizeLabel(label);
    setLocalError("");
    setLocalMessage("");
    try {
      await saveFavoriteSelection("favorites", nextLabel, labelsOverride);
      setSelectedTileId(listTileId(nextLabel));
      setLocalMessage(`Selected favorites list: ${nextLabel}`);
    } catch (err) {
      setLocalError(err?.message || "Failed to select favorites list.");
    }
  };

  const createList = async () => {
    const proposed = window.prompt("New favorites list name", "New List");
    if (proposed === null) {
      return;
    }
    const nextLabel = normalizeLabel(proposed, "");
    if (!nextLabel) {
      setLocalError("List name is required.");
      return;
    }
    const token = nextLabel.toLowerCase();
    if (listLabels.some((label) => normalizeLabel(label).toLowerCase() === token)) {
      setLocalError("That list name already exists.");
      return;
    }
    const nextLabels = [...listLabels, nextLabel];
    await activateFavoritesList(nextLabel, nextLabels);
  };

  const onTilePress = async (tile) => {
    if (!tile || working) {
      return;
    }
    if (tile.id === FULL_DB_TILE_ID) {
      await activateFullDatabase();
      return;
    }
    if (tile.id === CREATE_LIST_TILE_ID) {
      await createList();
      return;
    }
    if (tile.kind === "list") {
      await activateFavoritesList(tile.label);
    }
  };

  const onListen = async () => {
    const selectedTile = tiles.find((tile) => tile.id === selectedTileId) || null;
    if (!selectedTile) {
      navigate(SCREENS.MAIN);
      return;
    }
    if (selectedTile.id === FULL_DB_TILE_ID) {
      await activateFullDatabase();
      navigate(SCREENS.MAIN);
      return;
    }
    if (selectedTile.id === CREATE_LIST_TILE_ID) {
      await createList();
      return;
    }
    await activateFavoritesList(selectedTile.label);
    navigate(SCREENS.MAIN);
  };

  return (
    <section className="screen hp2-picker favorites-screen">
      <div className="hp2-picker-top">
        <div className="hp2-picker-title">Manage Favorites Lists</div>
        <div className="hp2-picker-top-right">
          <span className="hp2-picker-help">Help</span>
          <span className="hp2-picker-status">L</span>
          <span className="hp2-picker-status">SIG</span>
          <span className="hp2-picker-status">BAT</span>
        </div>
      </div>

      <div className="hp2-picker-grid">
        {slots.map((tile, index) => {
          if (!tile) {
            return <div key={`empty-${index}`} className="hp2-picker-tile hp2-picker-tile-empty" />;
          }
          const isActive = tile.id === selectedTileId;
          return (
            <button
              key={tile.id}
              type="button"
              className={`hp2-picker-tile ${isActive ? "active" : ""} ${tile.multiline ? "multiline" : ""}`}
              onClick={() => onTilePress(tile)}
              disabled={working}
            >
              {tile.label}
            </button>
          );
        })}
      </div>

      <div className="hp2-picker-bottom hp2-picker-bottom-4">
        <button type="button" className="hp2-picker-btn listen" onClick={onListen} disabled={working}>
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
