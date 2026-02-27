import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
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
  liveStatus: {},
  hpAvoids: [],
  currentScreen: SCREENS.MAIN,
  mode: "hp",
  sseConnected: false,
  loading: true,
  working: false,
  error: "",
  message: "",
};

const STICKY_STATUS_KEYS = [
  "digital_scheduler_active_system",
  "digital_scheduler_active_system_label",
  "digital_scheduler_next_system",
  "digital_scheduler_next_system_label",
  "digital_scheduler_active_department_label",
  "digital_last_label",
  "digital_channel_label",
  "digital_department_label",
  "digital_system_label",
  "digital_last_mode",
  "digital_last_tgid",
  "digital_profile",
  "digital_scan_mode",
  "stream_mount",
  "digital_stream_mount",
  "profile_airband",
  "profile_ground",
  "last_hit_airband_label",
  "last_hit_ground_label",
];

function hasStatusValue(value) {
  if (value === null || value === undefined) {
    return false;
  }
  if (typeof value === "string") {
    return value.trim() !== "";
  }
  if (Array.isArray(value)) {
    return value.length > 0;
  }
  return true;
}

function normalizeHpAvoids(raw) {
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
    out.push(token);
  });
  return out;
}

function mergeLiveStatus(previous, incomingRaw) {
  const incoming =
    incomingRaw && typeof incomingRaw === "object" ? incomingRaw : {};
  const merged = { ...(previous || {}), ...incoming };
  STICKY_STATUS_KEYS.forEach((key) => {
    if (!hasStatusValue(incoming[key]) && hasStatusValue(previous?.[key])) {
      merged[key] = previous[key];
    }
  });
  return merged;
}

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
        liveStatus: action.payload.liveStatus || {},
        hpAvoids: action.payload.hpAvoids || [],
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
    case "SET_HP_AVOIDS":
      return { ...state, hpAvoids: normalizeHpAvoids(action.payload) };
    case "SET_LIVE_STATUS":
      return {
        ...state,
        liveStatus: mergeLiveStatus(state.liveStatus, action.payload),
        hpAvoids: Array.isArray(action.payload?.hp_avoids)
          ? normalizeHpAvoids(action.payload.hp_avoids)
          : state.hpAvoids,
      };
    case "SET_MODE":
      return { ...state, mode: action.payload || state.mode };
    case "SET_SSE_CONNECTED":
      return { ...state, sseConnected: Boolean(action.payload) };
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
  const statusInFlightRef = useRef(false);

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

  const refreshHpAvoids = useCallback(async () => {
    const payload = await hpApi.getHpAvoids();
    const avoids = normalizeHpAvoids(payload?.avoids);
    dispatch({ type: "SET_HP_AVOIDS", payload: avoids });
    return avoids;
  }, []);

  const refreshStatus = useCallback(async () => {
    if (statusInFlightRef.current) {
      return null;
    }
    statusInFlightRef.current = true;
    try {
      const payload = await hpApi.getStatus();
      dispatch({ type: "SET_LIVE_STATUS", payload: payload || {} });
      return payload;
    } finally {
      statusInFlightRef.current = false;
    }
  }, []);

  const refreshAll = useCallback(async () => {
    dispatch({ type: "LOAD_START" });
    try {
      const [hpPayload, svcPayload, avoidsPayload] = await Promise.all([
        hpApi.getHpState(),
        hpApi.getServiceTypes(),
        hpApi.getHpAvoids(),
      ]);

      const hp = parseHpStateResponse(hpPayload);
      const serviceTypes = normalizeServiceTypes(svcPayload);
      const hpAvoids = normalizeHpAvoids(avoidsPayload?.avoids);

      dispatch({
        type: "LOAD_SUCCESS",
        payload: {
          hpState: hp.hpState,
          mode: hp.mode,
          serviceTypes,
          liveStatus: {},
          hpAvoids,
        },
      });
    } catch (err) {
      dispatch({ type: "LOAD_ERROR", payload: err.message });
    }
  }, []);

  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  useEffect(() => {
    const timer = setInterval(() => {
      refreshStatus().catch(() => {});
    }, state.sseConnected ? 10000 : 5000);
    return () => clearInterval(timer);
  }, [refreshStatus, state.sseConnected]);

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      return undefined;
    }

    let closed = false;
    let eventSource = null;
    let retryTimer = null;

    const connect = () => {
      if (closed) {
        return;
      }
      eventSource = new EventSource("/api/stream");
      eventSource.onopen = () => {
        dispatch({ type: "SET_SSE_CONNECTED", payload: true });
      };
      eventSource.addEventListener("status", (event) => {
        try {
          const payload = JSON.parse(event?.data || "{}");
          dispatch({ type: "SET_LIVE_STATUS", payload });
        } catch {
          // Ignore malformed stream payloads.
        }
      });
      eventSource.onerror = () => {
        dispatch({ type: "SET_SSE_CONNECTED", payload: false });
        if (eventSource) {
          eventSource.close();
          eventSource = null;
        }
        if (!closed) {
          retryTimer = setTimeout(connect, 2000);
        }
      };
    };

    connect();

    return () => {
      closed = true;
      dispatch({ type: "SET_SSE_CONNECTED", payload: false });
      if (retryTimer) {
        clearTimeout(retryTimer);
      }
      if (eventSource) {
        eventSource.close();
      }
    };
  }, []);

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
        if (Array.isArray(response?.avoids)) {
          dispatch({ type: "SET_HP_AVOIDS", payload: response.avoids });
        }
        if (successMessage) {
          dispatch({ type: "SET_MESSAGE", payload: successMessage });
        }
        await refreshHpState();
        await refreshStatus();
        return response;
      } catch (err) {
        dispatch({ type: "SET_ERROR", payload: err.message });
        throw err;
      } finally {
        dispatch({ type: "SET_WORKING", payload: false });
      }
    },
    [refreshHpState, refreshStatus]
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

  const clearHpAvoids = useCallback(
    async () => runControlAction(() => hpApi.clearHpAvoids(), "Runtime avoids cleared"),
    [runControlAction]
  );

  const removeHpAvoid = useCallback(
    async (system) =>
      runControlAction(() => hpApi.removeHpAvoid(system), "Avoid removed"),
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
      refreshHpAvoids,
      refreshStatus,
      saveHpState,
      setMode,
      holdScan,
      nextScan,
      avoidCurrent,
      clearHpAvoids,
      removeHpAvoid,
      SCREENS,
    }),
    [
      state,
      navigate,
      refreshAll,
      refreshHpState,
      refreshServiceTypes,
      refreshHpAvoids,
      refreshStatus,
      saveHpState,
      setMode,
      holdScan,
      nextScan,
      avoidCurrent,
      clearHpAvoids,
      removeHpAvoid,
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
