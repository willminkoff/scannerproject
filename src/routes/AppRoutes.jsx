import React from "react";
import { useUI, SCREENS } from "../context/UIContext";
import MainScreen from "../components/MainScreen";
import Menu from "../components/Menu";
import LocationScreen from "../components/LocationScreen";
import ServiceTypesScreen from "../components/ServiceTypesScreen";
import RangeScreen from "../components/RangeScreen";
import FavoritesScreen from "../components/FavoritesScreen";
import AvoidScreen from "../components/AvoidScreen";
import ModeSelectionScreen from "../components/ModeSelectionScreen";
import Loading from "../components/Shared/Loading";

export default function AppRoutes() {
  const { state } = useUI();

  if (state.loading) {
    return <Loading label="Loading HomePatrol state..." />;
  }

  switch (state.currentScreen) {
    case SCREENS.MENU:
      return <Menu />;
    case SCREENS.LOCATION:
      return <LocationScreen />;
    case SCREENS.SERVICE_TYPES:
      return <ServiceTypesScreen />;
    case SCREENS.RANGE:
      return <RangeScreen />;
    case SCREENS.FAVORITES:
      return <FavoritesScreen />;
    case SCREENS.AVOID:
      return <AvoidScreen />;
    case SCREENS.MODE_SELECTION:
      return <ModeSelectionScreen />;
    case SCREENS.MAIN:
    default:
      return <MainScreen />;
  }
}
