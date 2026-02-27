import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
} from "react";
import * as hpApi from "../api/hpApi";

export const SCREENS = Object.freeze({
  MAIN: "MAIN",
  MENU: "MENU",
  LOCATION: "LOCATION",
  SERVICE_TYPES: "SERVICE_TYPES",
  RANGE: "RANGE",
  FAVORITES: "FAVORITES",
  AVOID: "AVOID",
  MODE_SELECTION: "MODE_SELECTION",
});

const initialState = {
  hpState: {},
  serviceTypes: [],
  currentScreen: SCREENS.MAIN,
  mode: "hp",
  loading: true,
  working: false,
  error: "",
  message: "",
};

function reducer(state, action) {
  switch (action.type) {
    case "LOAD_START":
      return { ...state, loading: true, error: "" };
    case "LOAD_SUCCESS":
      return {
        ...state,
        loading: false,
        error: "",
        hpState: action.payload.hpState || {},
        serviceTypes: action.payload.serviceTypes || [],
        mode: action.payload.mode || state.mode,
      };
    case "LOAD_ERROR":
      return { ...state, loading: false, error: action.payload || "Load failed" };
    case "SET_WORKING":
      return { ...state, working: Boolean(action.payload) };
    case "SET_ERROR":
      return { ...state, error: action.payload || "" };
    case "SET_MESSAGE":
      return { ...state, message: action.payload || "" };
    case "SET_HP_STATE":
      return { ...state, hpState: action.payload || {} };
    case "SET_SERVICE_TYPES":
      return { ...state, serviceTypes: action.payload || [] };
    case "SET_MODE":
      return { ...state, mode: action.payload || state.mode };
    case "NAVIGATE":
      return { ...state, currentScreen: action.payload || SCREENS.MAIN };
    default:
      return state;
  }
}

const UIContext = createContext(null);

function normalizeServiceTypes(payload) {
  const list = Array.isArray(payload?.service_types) ? payload.service_types : [];
  return list.map((item) => ({
    service_tag: Number(item?.service_tag),
    name: String(item?.name || `Service ${item?.service_tag}`),
    enabled_by_default: Boolean(item?.enabled_by_default),
  }));
}

function parseHpStateResponse(payload) {
  const hpState =
    payload && typeof payload.state === "object" && payload.state !== null
      ? payload.state
      : {};
  const mode = String(payload?.mode || "hp").toLowerCase();
  return { hpState, mode };
}

export function UIProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initialState);

  const navigate = useCallback((screen) => {
    dispatch({ type: "NAVIGATE", payload: screen });
  }, []);

  const refreshHpState = useCallback(async () => {
    const data = await hpApi.getHpState();
    const parsed = parseHpStateResponse(data);
    dispatch({ type: "SET_HP_STATE", payload: parsed.hpState });
    dispatch({ type: "SET_MODE", payload: parsed.mode });
    return parsed;
  }, []);

  const refreshServiceTypes = useCallback(async () => {
    const data = await hpApi.getServiceTypes();
    const serviceTypes = normalizeServiceTypes(data);
    dispatch({ type: "SET_SERVICE_TYPES", payload: serviceTypes });
    return serviceTypes;
  }, []);

  const refreshAll = useCallback(async () => {
    dispatch({ type: "LOAD_START" });
    try {
      const [hpPayload, svcPayload] = await Promise.all([
        hpApi.getHpState(),
        hpApi.getServiceTypes(),
      ]);

      const hp = parseHpStateResponse(hpPayload);
      const serviceTypes = normalizeServiceTypes(svcPayload);

      dispatch({
        type: "LOAD_SUCCESS",
        payload: {
          hpState: hp.hpState,
          mode: hp.mode,
          serviceTypes,
        },
      });
    } catch (err) {
      dispatch({ type: "LOAD_ERROR", payload: err.message });
    }
  }, []);

  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  const saveHpState = useCallback(
    async (updates) => {
      dispatch({ type: "SET_WORKING", payload: true });
      dispatch({ type: "SET_ERROR", payload: "" });
      try {
        const payload = { ...state.hpState, ...updates };
        const response = await hpApi.saveHpState(payload);
        const nextState =
          response?.state && typeof response.state === "object"
            ? { ...state.hpState, ...response.state }
            : payload;

        dispatch({ type: "SET_HP_STATE", payload: nextState });
        dispatch({ type: "SET_MESSAGE", payload: "State saved" });
        return response;
      } catch (err) {
        dispatch({ type: "SET_ERROR", payload: err.message });
        throw err;
      } finally {
        dispatch({ type: "SET_WORKING", payload: false });
      }
    },
    [state.hpState]
  );

  const setMode = useCallback(async (mode) => {
    dispatch({ type: "SET_WORKING", payload: true });
    dispatch({ type: "SET_ERROR", payload: "" });
    try {
      const response = await hpApi.setMode(mode);
      const nextMode = String(response?.mode || mode || "hp").toLowerCase();
      dispatch({ type: "SET_MODE", payload: nextMode });
      dispatch({ type: "SET_MESSAGE", payload: `Mode set to ${nextMode}` });
      return response;
    } catch (err) {
      dispatch({ type: "SET_ERROR", payload: err.message });
      throw err;
    } finally {
      dispatch({ type: "SET_WORKING", payload: false });
    }
  }, []);

  const runControlAction = useCallback(
    async (fn, successMessage) => {
      dispatch({ type: "SET_WORKING", payload: true });
      dispatch({ type: "SET_ERROR", payload: "" });
      try {
        const response = await fn();
        if (successMessage) {
          dispatch({ type: "SET_MESSAGE", payload: successMessage });
        }
        await refreshHpState();
        return response;
      } catch (err) {
        dispatch({ type: "SET_ERROR", payload: err.message });
        throw err;
      } finally {
        dispatch({ type: "SET_WORKING", payload: false });
      }
    },
    [refreshHpState]
  );

  const holdScan = useCallback(
    async () => runControlAction(() => hpApi.holdScan(), "Hold command sent"),
    [runControlAction]
  );

  const nextScan = useCallback(
    async () => runControlAction(() => hpApi.nextScan(), "Next command sent"),
    [runControlAction]
  );

  const avoidCurrent = useCallback(
    async (payload = {}) =>
      runControlAction(() => hpApi.avoid(payload), "Avoid command sent"),
    [runControlAction]
  );

  const value = useMemo(
    () => ({
      state,
      dispatch,
      navigate,
      refreshAll,
      refreshHpState,
      refreshServiceTypes,
      saveHpState,
      setMode,
      holdScan,
      nextScan,
      avoidCurrent,
      SCREENS,
    }),
    [
      state,
      navigate,
      refreshAll,
      refreshHpState,
      refreshServiceTypes,
      saveHpState,
      setMode,
      holdScan,
      nextScan,
      avoidCurrent,
    ]
  );

  return <UIContext.Provider value={value}>{children}</UIContext.Provider>;
}

export function useUI() {
  const ctx = useContext(UIContext);
  if (!ctx) {
    throw new Error("useUI must be used inside UIProvider");
  }
  return ctx;
}
