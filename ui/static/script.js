const profilesAirbandEl = document.getElementById('profiles-airband');
const profilesGroundEl = document.getElementById('profiles-ground');
const warnEl = document.getElementById('warn');
const avoidsEl = document.getElementById('avoids-summary');
const viewMainEl = document.getElementById('view-main');
const viewHitsEl = document.getElementById('view-hits');
const hitListEl = document.getElementById('hit-list');
const tabAirbandEl = document.getElementById('tab-airband');
const tabGroundEl = document.getElementById('tab-ground');
const pagerEl = document.getElementById('pager');
const pagerInnerEl = document.getElementById('pager-inner');
const btnRestartAirbandEl = document.getElementById('btn-restart-airband');
const btnRestartGroundEl = document.getElementById('btn-restart-ground');
const btnOpenSqlAirbandEl = document.getElementById('btn-open-sql-airband');
const btnOpenSqlGroundEl = document.getElementById('btn-open-sql-ground');
const manageTargetAirbandEl = document.getElementById('manage-target-airband');
const manageTargetGroundEl = document.getElementById('manage-target-ground');
const manageIdEl = document.getElementById('manage-id');
const manageLabelEl = document.getElementById('manage-label');
const manageCreateEl = document.getElementById('manage-create');
const manageRenameEl = document.getElementById('manage-rename');
const manageDeleteEl = document.getElementById('manage-delete');
const manageStatusEl = document.getElementById('manage-status');
const editProfileEl = document.getElementById('edit-profile');
const editTextEl = document.getElementById('edit-text');
const editLoadEl = document.getElementById('edit-load');
const editSaveEl = document.getElementById('edit-save');
const editStatusEl = document.getElementById('edit-status');
const audioAirbandEl = document.getElementById('audio-airband');
const audioGroundEl = document.getElementById('audio-ground');
const lnkStreamAirbandEl = document.getElementById('lnk-stream-airband');
const lnkStreamGroundEl = document.getElementById('lnk-stream-ground');
const mountAirbandEl = document.getElementById('mount-name-airband');
const mountGroundEl = document.getElementById('mount-name-ground');
const digitalDotEl = document.getElementById('digital-dot');
const digitalStatusEl = document.getElementById('digital-status');
const digitalBackendEl = document.getElementById('digital-backend');
const digitalProfileEl = document.getElementById('digital-profile');
const digitalLastLabelEl = document.getElementById('digital-last-label');
const digitalLastMetaEl = document.getElementById('digital-last-meta');
const digitalErrorEl = document.getElementById('digital-error');
const digitalStartEl = document.getElementById('digital-start');
const digitalStopEl = document.getElementById('digital-stop');
const digitalRestartEl = document.getElementById('digital-restart');
const digitalMuteEl = document.getElementById('digital-mute');
const digitalProfileSelectEl = document.getElementById('digital-profile-select');
const digitalProfileStatusEl = document.getElementById('digital-profile-status');
let actionMsg = '';
let actionMsgTarget = null;

const GAIN_STEPS = [
  0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7,
  16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8,
  36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6,
];
const DBFS_MIN = -120;
const DBFS_MAX = 0;
const AUDIO_RECOVER_COOLDOWN_MS = 8000;
const AUDIO_WAITING_GRACE_MS = 2500;
const AUDIO_PROGRESS_CHECK_MS = 3000;
const AUDIO_PROGRESS_STALL_MS = 12000;

let currentProfileAirband = null;
let currentProfileGround = null;
let hitsView = false;
let activePage = 0;
let avoidsAirband = null;
let avoidsGround = null;
let profilesCache = null;
let profilesCacheAt = 0;
let digitalProfilesCache = null;
let digitalProfilesCacheAt = 0;
let digitalMuted = false;
let streamMount = 'scannerbox.mp3';
let icecastPort = 8000;
let streamProxyEnabled = true;
let streamBaseUrl = '';
const audioRecoverState = new WeakMap();

const controlTargets = {
  airband: {
    gainEl: document.getElementById('gain-airband'),
    filterEl: document.getElementById('filter-airband'),
    selectedGainEl: document.getElementById('selected-gain-airband'),
    selectedFilterEl: document.getElementById('selected-filter-airband'),
    selectedDbfsEl: document.getElementById('selected-dbfs-airband'),
    appliedGainEl: document.getElementById('applied-gain-airband'),
    appliedFilterEl: document.getElementById('applied-filter-airband'),
    appliedDbfsEl: document.getElementById('applied-dbfs-airband'),
    sqlDbfsEl: document.getElementById('dbfs-airband'),
    dirty: false,
    filterDirty: false,
    applyInFlight: false,
    filterApplyInFlight: false,
    openInFlight: false,
    lastAppliedGain: null,
    lastAppliedDbfs: null,
    lastAppliedFilter: null,
  },
  ground: {
    gainEl: document.getElementById('gain-ground'),
    filterEl: document.getElementById('filter-ground'),
    selectedGainEl: document.getElementById('selected-gain-ground'),
    selectedFilterEl: document.getElementById('selected-filter-ground'),
    selectedDbfsEl: document.getElementById('selected-dbfs-ground'),
    appliedGainEl: document.getElementById('applied-gain-ground'),
    appliedFilterEl: document.getElementById('applied-filter-ground'),
    appliedDbfsEl: document.getElementById('applied-dbfs-ground'),
    sqlDbfsEl: document.getElementById('dbfs-ground'),
    dirty: false,
    filterDirty: false,
    applyInFlight: false,
    filterApplyInFlight: false,
    openInFlight: false,
    lastAppliedGain: null,
    lastAppliedDbfs: null,
    lastAppliedFilter: null,
  },
};

Object.values(controlTargets).forEach(target => {
  target.gainEl.max = String(GAIN_STEPS.length - 1);
});

function gainIndexFromValue(value) {
  let best = 0;
  let bestDiff = Infinity;
  GAIN_STEPS.forEach((g, idx) => {
    const diff = Math.abs(g - value);
    if (diff < bestDiff) {
      bestDiff = diff;
      best = idx;
    }
  });
  return best;
}

function updateSelectedGain(target) {
  const controls = controlTargets[target];
  const idx = Number(controls.gainEl.value || 0);
  controls.selectedGainEl.textContent = GAIN_STEPS[idx].toFixed(1);
}

function updateSelectedDbfs(target) {
  const controls = controlTargets[target];
  const dbfs = Number(controls.sqlDbfsEl.value || 0);
  controls.selectedDbfsEl.textContent = dbfs.toFixed(0);
}

function updateSelectedFilter(target) {
  const controls = controlTargets[target];
  const cutoff = Number(controls.filterEl.value || 3500);
  controls.selectedFilterEl.textContent = `${cutoff.toFixed(0)}`;
}

function formatHitLabel(value) {
  if (value === null || value === undefined) return '';
  const text = String(value).trim();
  if (!text) return '';
  if (/^[0-9]+(\.[0-9]+)?$/.test(text)) {
    const num = Number(text);
    if (Number.isFinite(num)) {
      return num.toFixed(3);
    }
  }
  return text;
}

function formatTimeMs(timeMs) {
  if (!timeMs) return '—';
  const d = new Date(timeMs);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleTimeString();
}

function setDigitalStatusMessage(message, isError=false) {
  if (!digitalProfileStatusEl) return;
  digitalProfileStatusEl.textContent = message || '';
  if (!message) {
    digitalProfileStatusEl.classList.remove('ok', 'bad');
    return;
  }
  digitalProfileStatusEl.classList.toggle('ok', !isError);
  digitalProfileStatusEl.classList.toggle('bad', isError);
}

function updateDigitalStatus(st) {
  if (!digitalStatusEl) return;
  const active = !!st.digital_active;
  digitalMuted = !!st.digital_muted;
  if (digitalDotEl) {
    digitalDotEl.classList.remove('good', 'bad', 'neutral', 'pulse');
    digitalDotEl.classList.add(active ? 'pulse' : 'bad');
    if (active) digitalDotEl.classList.add('good');
  }
  digitalStatusEl.textContent = active ? 'Running' : 'Stopped';
  if (digitalBackendEl) digitalBackendEl.textContent = st.digital_backend || '—';
  if (digitalProfileEl) digitalProfileEl.textContent = st.digital_profile || '—';
  if (digitalLastLabelEl) digitalLastLabelEl.textContent = st.digital_last_label || '—';
  if (digitalLastMetaEl) {
    const meta = [];
    const timeText = formatTimeMs(Number(st.digital_last_time || 0));
    if (timeText !== '—') meta.push(timeText);
    if (st.digital_last_mode) meta.push(st.digital_last_mode);
    digitalLastMetaEl.textContent = meta.length ? meta.join(' · ') : '—';
  }
  if (digitalErrorEl) {
    const err = st.digital_last_error || '';
    digitalErrorEl.textContent = err;
    digitalErrorEl.classList.toggle('bad', !!err);
  }
  if (digitalMuteEl) {
    digitalMuteEl.textContent = digitalMuted ? 'Unmute' : 'Mute';
    digitalMuteEl.classList.toggle('primary', digitalMuted);
  }
}

function updateDigitalProfileSelect(data) {
  if (!digitalProfileSelectEl) return;
  const list = (data && data.profiles) || [];
  digitalProfileSelectEl.innerHTML = '';
  if (!list.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '(no profiles)';
    digitalProfileSelectEl.appendChild(opt);
    digitalProfileSelectEl.disabled = true;
    return;
  }
  digitalProfileSelectEl.disabled = false;
  list.forEach(profileId => {
    const opt = document.createElement('option');
    opt.value = profileId;
    opt.textContent = profileId;
    digitalProfileSelectEl.appendChild(opt);
  });
  const active = (data && data.active) || '';
  if (active && list.includes(active)) {
    digitalProfileSelectEl.value = active;
  } else if (list.length) {
    digitalProfileSelectEl.value = list[0];
  }
}

async function refreshDigitalProfiles(force=false) {
  if (!digitalProfileSelectEl) return;
  if (!force && digitalProfilesCache && Date.now() - digitalProfilesCacheAt < 8000) {
    updateDigitalProfileSelect(digitalProfilesCache);
    return;
  }
  const data = await getJSON('/api/digital/profiles');
  if (data && data.ok !== false) {
    digitalProfilesCache = data;
    digitalProfilesCacheAt = Date.now();
    updateDigitalProfileSelect(data);
    return;
  }
  const err = (data && data.error) || 'Digital profiles unavailable';
  setDigitalStatusMessage(err, true);
}

function updateWarn(missingProfiles) {
  const parts = [];
  if (missingProfiles.length) {
    parts.push('Missing profile file(s): ' + missingProfiles.join(' • '));
  }
  // Only show action message if it's for the current page
  if (actionMsg && (actionMsgTarget === null || (actionMsgTarget === 'airband' && activePage === 0) || (actionMsgTarget === 'ground' && activePage === 1))) {
    parts.push(actionMsg);
  }
  warnEl.textContent = parts.join(' • ');
}

function updateAvoids(avoids) {
  if (!avoidsEl) return;
  if (!avoids) {
    avoidsEl.textContent = '';
    return;
  }
  const count = avoids.count || 0;
  const sample = (avoids.sample || []).filter(Boolean);
  let text = count ? `Avoids: ${count} for this profile` : 'Avoids: none';
  if (sample.length) {
    text += ` (${sample.join(', ')})`;
  }
  avoidsEl.textContent = text;
}

function updateAvoidsForPage() {
  const avoids = activePage === 1 ? avoidsGround : avoidsAirband;
  updateAvoids(avoids);
}

function buildProfiles(profilesEl, profiles, selected, target) {
  profilesEl.innerHTML = '';
  if (!profiles.length) {
    profilesEl.classList.add('hidden');
    return;
  }
  profilesEl.classList.remove('hidden');
  const noneProfile = profiles.find(p => p.label === 'No Profile' || p.id.startsWith('none_'));
  profiles.forEach(p => {
    if (p.id.startsWith('none_')) {
      return;
    }
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'profile-card' + (p.id === selected ? ' selected' : '');
    card.setAttribute('aria-pressed', p.id === selected ? 'true' : 'false');
    card.innerHTML = `<div><b>${p.label}</b></div><small>${p.id}</small>` + (p.exists ? '' : `<small>Missing: ${p.path}</small>`);
    card.addEventListener('click', async () => {
      let nextId = p.id;
      if (p.id === selected && noneProfile && noneProfile.id !== selected) {
        nextId = noneProfile.id;
      } else if (p.id === selected) {
        return;
      }
      await post('/api/profile', {profile: nextId, target});
      await refresh(true);
    });
    profilesEl.appendChild(card);
  });
}

function streamUrl() {
  const mount = (streamMount || 'scannerbox.mp3').replace(/^\/+/, '');
  if (streamProxyEnabled) {
    return `${location.origin}/stream/${encodeURIComponent(mount)}`;
  }
  return `http://${location.hostname}:${icecastPort}/${mount}`;
}

function icecastRootUrl() {
  return `http://${location.hostname}:${icecastPort}/`;
}

function syncStreamLinks() {
  const base = streamUrl();
  streamBaseUrl = base;
  if (audioAirbandEl) audioAirbandEl.src = base;
  if (audioGroundEl) audioGroundEl.src = base;
  if (lnkStreamAirbandEl) {
    lnkStreamAirbandEl.href = icecastRootUrl();
    lnkStreamAirbandEl.target = '_blank';
    lnkStreamAirbandEl.rel = 'noopener';
  }
  if (lnkStreamGroundEl) {
    lnkStreamGroundEl.href = icecastRootUrl();
    lnkStreamGroundEl.target = '_blank';
    lnkStreamGroundEl.rel = 'noopener';
  }
  if (mountAirbandEl) mountAirbandEl.textContent = streamMount || 'scannerbox.mp3';
  if (mountGroundEl) mountGroundEl.textContent = streamMount || 'scannerbox.mp3';
}

function clearAudioWaitingTimer(audioEl) {
  const state = audioRecoverState.get(audioEl);
  if (!state || !state.waitingTimer) return;
  clearTimeout(state.waitingTimer);
  state.waitingTimer = null;
}

function markAudioProgress(audioEl) {
  const state = audioRecoverState.get(audioEl);
  if (!state) return;
  const pos = Number(audioEl.currentTime || 0);
  if (!Number.isFinite(pos)) return;
  state.lastPosition = pos;
  state.lastAdvanceTs = Date.now();
}

function reloadAudioElement(audioEl, reason) {
  if (!audioEl) return;
  const base = streamUrl();
  const wasPaused = audioEl.paused;
  audioEl.src = `${base}?t=${Date.now()}`;
  audioEl.load();
  if (!wasPaused) {
    audioEl.play().catch(() => {});
  }
}

function maybeAutoRecoverAudio(audioEl, reason) {
  if (!audioEl || audioEl.paused) return;
  const now = Date.now();
  let state = audioRecoverState.get(audioEl);
  if (!state) {
    state = {lastReloadTs: 0, waitingTimer: null, watchTimer: null, lastPosition: -1, lastAdvanceTs: 0};
    audioRecoverState.set(audioEl, state);
  }
  if ((now - state.lastReloadTs) < AUDIO_RECOVER_COOLDOWN_MS) return;
  state.lastReloadTs = now;
  reloadAudioElement(audioEl, `auto-${reason}`);
  state.lastAdvanceTs = now;
}

function attachAudioAutoRecover(audioEl) {
  if (!audioEl) return;
  if (audioEl.dataset.autorecoverBound === '1') return;
  audioEl.dataset.autorecoverBound = '1';
  audioRecoverState.set(audioEl, {lastReloadTs: 0, waitingTimer: null, watchTimer: null, lastPosition: -1, lastAdvanceTs: 0});
  audioEl.addEventListener('playing', () => {
    clearAudioWaitingTimer(audioEl);
    markAudioProgress(audioEl);
  });
  audioEl.addEventListener('canplay', () => {
    clearAudioWaitingTimer(audioEl);
    markAudioProgress(audioEl);
  });
  audioEl.addEventListener('timeupdate', () => markAudioProgress(audioEl));
  audioEl.addEventListener('stalled', () => maybeAutoRecoverAudio(audioEl, 'stalled'));
  audioEl.addEventListener('error', () => maybeAutoRecoverAudio(audioEl, 'error'));
  audioEl.addEventListener('ended', () => maybeAutoRecoverAudio(audioEl, 'ended'));
  audioEl.addEventListener('waiting', () => {
    clearAudioWaitingTimer(audioEl);
    const state = audioRecoverState.get(audioEl);
    if (!state) return;
    state.waitingTimer = setTimeout(() => {
      if (!audioEl.paused && audioEl.readyState < 3) {
        maybeAutoRecoverAudio(audioEl, 'waiting');
      }
    }, AUDIO_WAITING_GRACE_MS);
  });
  const state = audioRecoverState.get(audioEl);
  if (state && !state.watchTimer) {
    state.watchTimer = setInterval(() => {
      if (audioEl.paused) return;
      const pos = Number(audioEl.currentTime || 0);
      if (!Number.isFinite(pos)) return;
      const now = Date.now();
      if (state.lastPosition < 0 || pos > (state.lastPosition + 0.05)) {
        state.lastPosition = pos;
        state.lastAdvanceTs = now;
        return;
      }
      if (!state.lastAdvanceTs) {
        state.lastAdvanceTs = now;
        return;
      }
      if ((now - state.lastAdvanceTs) >= AUDIO_PROGRESS_STALL_MS) {
        maybeAutoRecoverAudio(audioEl, 'no-progress');
      }
    }, AUDIO_PROGRESS_CHECK_MS);
  }
}

async function getJSON(url) {
  const r = await fetch(url, {cache:'no-store'});
  return await r.json();
}
async function post(url, obj) {
  const body = new URLSearchParams(obj).toString();
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});
  return await r.json();
}

function setControlsFromStatus(target, gain, squelchDbfs, filter, allowSetSliders) {
  const controls = controlTargets[target];
  controls.appliedGainEl.textContent = gain.toFixed(1);
  controls.appliedDbfsEl.textContent = squelchDbfs.toFixed(0);
  controls.appliedFilterEl.textContent = `${filter.toFixed(0)}`;
  controls.lastAppliedGain = gain;
  controls.lastAppliedDbfs = squelchDbfs;
  controls.lastAppliedFilter = filter;
  if (allowSetSliders && !controls.dirty && !controls.filterDirty) {
    controls.gainEl.value = gainIndexFromValue(gain);
    controls.sqlDbfsEl.value = Math.max(DBFS_MIN, Math.min(DBFS_MAX, Math.round(squelchDbfs))).toFixed(0);
    controls.filterEl.value = filter.toFixed(0);
    updateSelectedGain(target);
    updateSelectedDbfs(target);
    updateSelectedFilter(target);
  }
}

function getManageTarget() {
  return manageTargetGroundEl && manageTargetGroundEl.checked ? 'ground' : 'airband';
}

function sanitizeProfileId(label) {
  return String(label || '')
    .toLowerCase()
    .replace(/[^a-z0-9 _-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .slice(0, 40);
}

function refreshManageCloneOptions() {
  // Clone UI removed; keep function for older calls as a no-op.
  return;
}

function refreshEditProfileOptions() {
  if (!profilesCache || !editProfileEl) return;
  const target = getManageTarget();
  const list = target === 'ground' ? profilesCache.profiles_ground : profilesCache.profiles_airband;
  const currentSelection = editProfileEl.value;
  editProfileEl.innerHTML = '';
  list.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = `${p.label} (${p.id})`;
    editProfileEl.appendChild(opt);
  });
  const activeId = getSelectedProfileId(target);
  if (currentSelection && list.some(p => p.id === currentSelection)) {
    editProfileEl.value = currentSelection;
  } else if (activeId) {
    editProfileEl.value = activeId;
  } else if (list.length) {
    editProfileEl.value = list[0].id;
  }
}

function setManageStatus(message, isError=false) {
  if (!manageStatusEl) return;
  manageStatusEl.textContent = message || '';
  manageStatusEl.style.color = isError ? '#f59e0b' : '';
}

function getManageSelectedId() {
  const target = getManageTarget();
  const selected = editProfileEl && editProfileEl.value;
  if (selected) return selected;
  return getSelectedProfileId(target);
}

function formatFreqsText(freqs, labels) {
  const out = [];
  const hasLabels = Array.isArray(labels) && labels.length === freqs.length && labels.length > 0;
  for (let i = 0; i < freqs.length; i++) {
    const f = String(freqs[i] || '').trim();
    if (!f) continue;
    if (hasLabels) {
      const l = String(labels[i] || '').trim();
      out.push(l ? `${f} ${l}` : f);
    } else {
      out.push(f);
    }
  }
  return out.join('\n');
}

function getSelectedProfileId(target) {
  if (!profilesCache) return '';
  return target === 'ground' ? profilesCache.active_ground_id : profilesCache.active_airband_id;
}

async function refreshProfiles() {
  const data = await getJSON('/api/profiles');
  profilesCache = data;
  profilesCacheAt = Date.now();
  if (profilesCache) {
    currentProfileAirband = data.active_airband_id || currentProfileAirband;
    currentProfileGround = data.active_ground_id || currentProfileGround;
    buildProfiles(profilesAirbandEl, data.profiles_airband || [], currentProfileAirband, 'airband');
    buildProfiles(profilesGroundEl, data.profiles_ground || [], currentProfileGround, 'ground');
    refreshManageCloneOptions();
    refreshEditProfileOptions();
  }
}

async function refresh(allowSetSliders=false) {
  const st = await getJSON('/api/status');
  if (typeof st.stream_proxy_enabled === 'boolean') {
    streamProxyEnabled = st.stream_proxy_enabled;
  }
  const port = Number(st.icecast_port);
  if (Number.isFinite(port)) {
    icecastPort = port;
  }
  if (typeof st.stream_mount === 'string' && st.stream_mount.trim()) {
    streamMount = st.stream_mount.trim();
  }
  const base = streamUrl();
  if (streamBaseUrl !== base) {
    syncStreamLinks();
  }

  const airbandHit = formatHitLabel(st.last_hit_airband) || '—';
  const groundHit = formatHitLabel(st.last_hit_ground) || '—';
  document.getElementById('txt-hit-airband').textContent = airbandHit;
  document.getElementById('txt-hit-ground').textContent = groundHit;

  // LED indicator logic for SDR2 (ground)
  const dotGround = document.getElementById('dot-ground');
  if (dotGround) {
    if (st.ground_active) {
      dotGround.classList.remove('bad', 'neutral', 'good');
      dotGround.classList.add('pulse');
    } else {
      dotGround.classList.remove('pulse', 'good');
      dotGround.classList.add('bad');
    }
  }

  setControlsFromStatus(
    'airband',
    st.airband_gain,
    st.airband_squelch_dbfs ?? 0,
    st.airband_filter || 3500,
    allowSetSliders
  );
  setControlsFromStatus(
    'ground',
    st.ground_gain,
    st.ground_squelch_dbfs ?? 0,
    st.ground_filter || 3500,
    allowSetSliders
  );

  updateDigitalStatus(st);

  updateWarn(st.missing_profiles);
  avoidsAirband = st.avoids_airband;
  avoidsGround = st.avoids_ground;
  updateAvoidsForPage();

  if (!profilesCache || Date.now() - profilesCacheAt > 5000) {
    await refreshProfiles();
  }
  if (!digitalProfilesCache || Date.now() - digitalProfilesCacheAt > 8000) {
    await refreshDigitalProfiles();
  }

  if (allowSetSliders) {
    updateSelectedGain('airband');
    updateSelectedGain('ground');
    updateSelectedDbfs('airband');
    updateSelectedDbfs('ground');
  }
}

function renderHitList(items) {
  hitListEl.innerHTML = '<div class="hit-row head"><div>Time</div><div>Frequency</div><div>Duration</div></div>';
  if (!items || !items.length) {
    const empty = document.createElement('div');
    empty.className = 'hit-empty';
    empty.textContent = 'No hits yet.';
    hitListEl.appendChild(empty);
    return;
  }
  items.forEach(item => {
    const row = document.createElement('div');
    row.className = 'hit-row';
    const freqText = formatHitLabel(item.freq);
    const isNumeric = /^[0-9]+(\.[0-9]+)?$/.test(freqText);
    const displayFreq = isNumeric ? `${freqText} MHz` : (freqText || '—');
    row.innerHTML = `<div>${item.time}</div><div>${displayFreq}</div><div>${item.duration}s</div>`;
    hitListEl.appendChild(row);
  });
}

async function refreshHitList() {
  const data = await getJSON('/api/hits');
  renderHitList(data.items || []);
}

function wireControls(target) {
  const controls = controlTargets[target];
  controls.gainEl.addEventListener('input', () => {
    controls.dirty = true;
    updateSelectedGain(target);
  });
  controls.sqlDbfsEl.addEventListener('input', () => {
    controls.dirty = true;
    updateSelectedDbfs(target);
  });
  controls.filterEl.addEventListener('input', () => {
    controls.filterDirty = true;
    updateSelectedFilter(target);
  });
  controls.gainEl.addEventListener('change', () => applyControls(target));
  controls.sqlDbfsEl.addEventListener('change', () => applyControls(target));
  controls.filterEl.addEventListener('change', () => applyFilter(target));
}

wireControls('airband');
wireControls('ground');

// Wire embedded players
syncStreamLinks();
attachAudioAutoRecover(audioAirbandEl);
attachAudioAutoRecover(audioGroundEl);
if (manageTargetAirbandEl) manageTargetAirbandEl.addEventListener('change', refreshManageCloneOptions);
if (manageTargetGroundEl) manageTargetGroundEl.addEventListener('change', refreshManageCloneOptions);
if (manageTargetAirbandEl) manageTargetAirbandEl.addEventListener('change', refreshEditProfileOptions);
if (manageTargetGroundEl) manageTargetGroundEl.addEventListener('change', refreshEditProfileOptions);
// dropdown change preserved by refreshEditProfileOptions
if (manageLabelEl) {
  manageLabelEl.addEventListener('input', () => {
    if (!manageIdEl || manageIdEl.value.trim()) return;
    manageIdEl.value = sanitizeProfileId(manageLabelEl.value);
  });
}
if (manageCreateEl) {
  manageCreateEl.addEventListener('click', async () => {
    const target = getManageTarget();
    const label = (manageLabelEl && manageLabelEl.value || '').trim();
    let profileId = (manageIdEl && manageIdEl.value || '').trim();
    if (!profileId) profileId = sanitizeProfileId(label);
    profileId = sanitizeProfileId(profileId);
    if (manageIdEl) manageIdEl.value = profileId;
    if (!profileId) {
      setManageStatus('Enter an ID or label to create a profile.', true);
      return;
    }
    const res = await post('/api/profile/create', {
      id: profileId,
      label,
      airband: target === 'airband',
    });
    if (!res.ok) {
      actionMsg = res.error || 'Create failed';
      setManageStatus(actionMsg, true);
    } else {
      actionMsg = 'Profile created';
      setManageStatus(actionMsg, false);
    }
    await refreshProfiles();
    if (res.ok && res.profile && editProfileEl) {
      editProfileEl.value = res.profile.id;
      if (editStatusEl) editStatusEl.textContent = `${target} ready: ${res.profile.id}`;
    }
  });
}
if (manageRenameEl) {
  manageRenameEl.addEventListener('click', async () => {
    const target = getManageTarget();
    const profileId = getManageSelectedId();
    const label = (manageLabelEl && manageLabelEl.value || '').trim();
    if (!profileId || !label) return;
    const res = await post('/api/profile/update', {id: profileId, label});
    actionMsg = res.ok ? 'Profile renamed' : (res.error || 'Rename failed');
    setManageStatus(actionMsg, !res.ok);
    await refreshProfiles();
  });
}
if (manageDeleteEl) {
  manageDeleteEl.addEventListener('click', async () => {
    const profileId = getManageSelectedId();
    if (!profileId) return;
    if (!confirm(`Delete profile ${profileId}?`)) return;
    const res = await post('/api/profile/delete', {id: profileId});
    actionMsg = res.ok ? 'Profile deleted' : (res.error || 'Delete failed');
    setManageStatus(actionMsg, !res.ok);
    await refreshProfiles();
  });
}

async function digitalAction(action) {
  const res = await post(`/api/digital/${action}`, {});
  if (res.ok) {
    setDigitalStatusMessage(`Digital ${action} ok`, false);
  } else {
    setDigitalStatusMessage(res.error || `${action} failed`, true);
  }
  await refresh(false);
}

if (digitalStartEl) digitalStartEl.addEventListener('click', () => digitalAction('start'));
if (digitalStopEl) digitalStopEl.addEventListener('click', () => digitalAction('stop'));
if (digitalRestartEl) digitalRestartEl.addEventListener('click', () => digitalAction('restart'));
if (digitalMuteEl) {
  digitalMuteEl.addEventListener('click', async () => {
    const next = !digitalMuted;
    const res = await post('/api/digital/mute', {muted: next});
    if (res.ok) {
      setDigitalStatusMessage(next ? 'Digital muted' : 'Digital unmuted', false);
    } else {
      setDigitalStatusMessage(res.error || 'Mute failed', true);
    }
    await refresh(false);
  });
}

if (digitalProfileSelectEl) {
  digitalProfileSelectEl.addEventListener('change', async () => {
    const profileId = digitalProfileSelectEl.value;
    if (!profileId) return;
    setDigitalStatusMessage('Applying profile...', false);
    const res = await post('/api/digital/profile', {profileId});
    if (res.ok) {
      setDigitalStatusMessage('Profile updated', false);
    } else {
      setDigitalStatusMessage(res.error || 'Profile update failed', true);
    }
    await refreshDigitalProfiles(true);
    await refresh(false);
  });
}

if (editLoadEl) {
  editLoadEl.addEventListener('click', async () => {
    const target = getManageTarget();
    const id = editProfileEl && editProfileEl.value;
    if (!id) return;
    const res = await getJSON(`/api/profile?id=${encodeURIComponent(id)}`);
    if (!res.ok) {
      if (editStatusEl) editStatusEl.textContent = res.error || 'Load failed';
      return;
    }
    if (editTextEl) editTextEl.value = formatFreqsText(res.freqs || [], res.labels || []);
    if (editStatusEl) editStatusEl.textContent = `${target} loaded: ${id}`;
  });
}

if (editSaveEl) {
  editSaveEl.addEventListener('click', async () => {
    const target = getManageTarget();
    const id = editProfileEl && editProfileEl.value;
    const freqs_text = (editTextEl && editTextEl.value || '').trim();
    if (!id) {
      if (editStatusEl) editStatusEl.textContent = 'Pick a profile to save.';
      return;
    }
    if (!freqs_text) {
      if (editStatusEl) editStatusEl.textContent = 'Add at least one frequency before saving.';
      return;
    }
    const res = await post('/api/profile/update_freqs', {id, freqs_text});
    if (!res.ok) {
      if (editStatusEl) editStatusEl.textContent = res.error || 'Save failed';
      return;
    }
    if (editStatusEl) editStatusEl.textContent = res.changed ? `${target} saved (scanner updated)` : `${target} saved`;
    await refreshProfiles();
    await refresh(false);
  });
}

async function applyControls(target) {
  const controls = controlTargets[target];
  if (controls.applyInFlight) return;
  controls.applyInFlight = true;
  try {
    // Snap gain to nearest valid step before sending
    let gainIdx = Number(controls.gainEl.value || 0);
    let gain = GAIN_STEPS[gainIdx];
    const squelchDbfs = Number(controls.sqlDbfsEl.value || 0);
    const gainSame = controls.lastAppliedGain !== null && Math.abs(gain - controls.lastAppliedGain) < 0.001;
    const dbfsSame = controls.lastAppliedDbfs !== null && Math.abs(squelchDbfs - controls.lastAppliedDbfs) < 0.001;
    if (gainSame && dbfsSame) {
      controls.dirty = false;
      return;
    }
    const result = await post('/api/apply', {
      gain,
      target,
      squelch_mode: 'dbfs',
      squelch_dbfs: squelchDbfs,
    });
    if (result && result.ok !== false) {
      // After apply, refresh status but keep slider position to avoid snap-back
      controls.dirty = false;
      await refresh(false);
    } else {
      // On error, revert to backend value
      controls.dirty = false;
      await refresh(true);
    }
  } finally {
    controls.applyInFlight = false;
  }
}

async function applyFilter(target) {
  const controls = controlTargets[target];
  if (controls.filterApplyInFlight) return;
  controls.filterApplyInFlight = true;
  try {
    const cutoff_hz = Number(controls.filterEl.value || 3500);
    const filterSame = controls.lastAppliedFilter !== null && Math.abs(cutoff_hz - controls.lastAppliedFilter) < 0.01;
    if (filterSame) {
      controls.filterDirty = false;
      return;
    }
    await post('/api/filter', {cutoff_hz, target});
    controls.filterDirty = false;
    controls.lastAppliedFilter = cutoff_hz;
    await refresh(true);
  } finally {
    controls.filterApplyInFlight = false;
  }
}

async function restartUnit(target) {
  const res = await post('/api/restart', {target});
  if (res.ok) {
    actionMsg = target === 'ground' ? 'Ground restarted' : 'Airband restarted';
  } else {
    actionMsg = res.error || 'Restart failed';
  }
  await refresh(false);
}

async function openSquelchMomentary(target, durationMs) {
  const controls = controlTargets[target];
  if (controls.openInFlight || controls.applyInFlight) return;
  controls.openInFlight = true;
  const previousDbfs = Number(controls.sqlDbfsEl.value || 0);
  controls.sqlDbfsEl.value = String(DBFS_MIN);
  updateSelectedDbfs(target);
  try {
    await applyControls(target);
  } finally {
    setTimeout(async () => {
      controls.sqlDbfsEl.value = String(previousDbfs);
      updateSelectedDbfs(target);
      try {
        await applyControls(target);
      } finally {
        controls.openInFlight = false;
      }
    }, durationMs);
  }
}

btnRestartAirbandEl.addEventListener('click', async ()=> {
  await restartUnit('airband');
});

btnRestartGroundEl.addEventListener('click', async ()=> {
  await restartUnit('ground');
});

btnOpenSqlAirbandEl.addEventListener('click', async ()=> {
  await openSquelchMomentary('airband', 2000);
});

btnOpenSqlGroundEl.addEventListener('click', async ()=> {
  await openSquelchMomentary('ground', 2000);
});

document.getElementById('btn-avoid').addEventListener('click', async ()=> {
  const target = activePage === 1 ? 'ground' : 'airband';
  const res = await post('/api/avoid', {target});
  actionMsg = res.ok ? `Avoided ${res.freq}` : (res.error || 'Avoid failed');
  actionMsgTarget = target;
  await refresh(true);
  // Momentarily open squelch to skip past the avoided frequency
  if (res.ok) {
    await openSquelchMomentary(target, 800);
  }
});

document.getElementById('btn-clear-avoids').addEventListener('click', async ()=> {
  const target = activePage === 1 ? 'ground' : 'airband';
  const res = await post('/api/avoid-clear', {target});
  actionMsg = res.ok ? 'Cleared avoids' : (res.error || 'Clear avoids failed');
  actionMsgTarget = target;
  await refresh(true);
});

function setPage(index) {
  activePage = index;
  pagerInnerEl.style.transform = `translateX(-${index * 100}%)`;
  tabAirbandEl.classList.toggle('active', index === 0);
  tabGroundEl.classList.toggle('active', index === 1);
  updateAvoidsForPage();
}

tabAirbandEl.addEventListener('click', () => setPage(0));
tabGroundEl.addEventListener('click', () => setPage(1));

let touchStartX = null;
pagerEl.addEventListener('touchstart', (event) => {
  if (!event.touches.length) return;
  touchStartX = event.touches[0].clientX;
}, {passive: true});
pagerEl.addEventListener('touchend', (event) => {
  if (touchStartX === null) return;
  const touch = event.changedTouches[0];
  if (!touch) return;
  const delta = touchStartX - touch.clientX;
  if (Math.abs(delta) > 40) {
    if (delta > 0 && activePage < 1) {
      setPage(activePage + 1);
    } else if (delta < 0 && activePage > 0) {
      setPage(activePage - 1);
    }
  }
  touchStartX = null;
}, {passive: true});

document.getElementById('btn-play').addEventListener('click', ()=> {
  window.open(streamUrl(), '_blank', 'noopener');
});

async function showHitList() {
  hitsView = true;
  viewMainEl.classList.add('hidden');
  viewHitsEl.classList.remove('hidden');
  await refreshHitList();
}

document.getElementById('btn-hit-airband').addEventListener('click', showHitList);
document.getElementById('btn-hit-ground').addEventListener('click', showHitList);

document.getElementById('btn-hit-back').addEventListener('click', ()=> {
  hitsView = false;
  viewHitsEl.classList.add('hidden');
  viewMainEl.classList.remove('hidden');
});

document.getElementById('lnk-diagnostic').addEventListener('click', async (e)=> {
  e.preventDefault();
  actionMsg = 'Generating log...';
  updateWarn([]);
  const res = await post('/api/diagnostic', {});
  if (res.ok) {
    actionMsg = `Log saved: ${res.path}`;
  } else {
    actionMsg = res.error || 'Log failed';
  }
  await refresh(false);
});

setPage(0);
refresh(true);
setInterval(async ()=> {
  await refresh(false);
  if (hitsView) {
    await refreshHitList();
  }
}, 1500);
