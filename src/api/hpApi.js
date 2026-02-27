const JSON_HEADERS = {
  "Content-Type": "application/json",
};

async function request(path, { method = "GET", body } = {}) {
  const config = {
    method,
    headers: { ...JSON_HEADERS },
  };

  if (body !== undefined) {
    config.body = JSON.stringify(body);
  }

  const response = await fetch(path, config);
  const raw = await response.text();

  let payload = {};
  try {
    payload = raw ? JSON.parse(raw) : {};
  } catch {
    payload = { raw };
  }

  if (!response.ok) {
    const message = payload?.error || `Request failed (${response.status})`;
    const err = new Error(message);
    err.status = response.status;
    err.payload = payload;
    throw err;
  }

  return payload;
}

export function getHpState() {
  return request("/api/hp/state");
}

export function saveHpState(state) {
  return request("/api/hp/state", { method: "POST", body: state });
}

export function getServiceTypes() {
  return request("/api/hp/service-types");
}

function buildQuery(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      return;
    }
    query.set(key, String(value));
  });
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export function getFavoritesWizardCountries() {
  return request("/api/hp/favorites-wizard/countries");
}

export function getFavoritesWizardStates(countryId) {
  return request(`/api/hp/favorites-wizard/states${buildQuery({ country_id: countryId })}`);
}

export function getFavoritesWizardCounties(stateId) {
  return request(`/api/hp/favorites-wizard/counties${buildQuery({ state_id: stateId })}`);
}

export function getFavoritesWizardSystems({ stateId, countyId, systemType, q }) {
  return request(
    `/api/hp/favorites-wizard/systems${buildQuery({
      state_id: stateId,
      county_id: countyId,
      system_type: systemType,
      q,
    })}`
  );
}

export function getFavoritesWizardChannels({ systemType, systemId, q, limit = 500 }) {
  return request(
    `/api/hp/favorites-wizard/channels${buildQuery({
      system_type: systemType,
      system_id: systemId,
      q,
      limit,
    })}`
  );
}

export function getHpAvoids() {
  return request("/api/hp/avoids");
}

export function clearHpAvoids() {
  return request("/api/hp/avoids", { method: "POST", body: { action: "clear" } });
}

export function removeHpAvoid(system) {
  return request("/api/hp/avoids", {
    method: "POST",
    body: { action: "remove", system },
  });
}

export function getStatus() {
  return request("/api/status");
}

export function setMode(mode) {
  return request("/api/mode", { method: "POST", body: { mode } });
}

export function holdScan(payload = {}) {
  return request("/api/hp/hold", { method: "POST", body: payload });
}

export function nextScan(payload = {}) {
  return request("/api/hp/next", { method: "POST", body: payload });
}

export function avoid(payload = {}) {
  return request("/api/hp/avoid", { method: "POST", body: payload });
}

const hpApi = {
  getHpState,
  saveHpState,
  getServiceTypes,
  getFavoritesWizardCountries,
  getFavoritesWizardStates,
  getFavoritesWizardCounties,
  getFavoritesWizardSystems,
  getFavoritesWizardChannels,
  getHpAvoids,
  clearHpAvoids,
  removeHpAvoid,
  getStatus,
  setMode,
  holdScan,
  nextScan,
  avoid,
};

export default hpApi;
