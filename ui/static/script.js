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

function abbreviateHitText(value, maxLen=36) {
  const text = String(value || '').trim();
  if (!text) return '';
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen - 1).trimEnd() + '…';
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

  const airbandRaw = st.last_hit_airband_label || st.last_hit_airband;
  const groundRaw = st.last_hit_ground_label || st.last_hit_ground;
  const airbandHit = abbreviateHitText(formatHitLabel(airbandRaw), 36) || '—';
  const groundHit = abbreviateHitText(formatHitLabel(groundRaw), 36) || '—';
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
  updateWxStatus(st);

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
  hitListEl.innerHTML = '<div class="hit-row head"><div>Time</div><div>Hit</div><div>Duration</div></div>';
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
    const rawHit = formatHitLabel(item.label_full || item.label || item.freq);
    const isNumeric = /^[0-9]+(\.[0-9]+)?$/.test(rawHit);
    const displayHit = isNumeric ? `${rawHit} MHz` : (abbreviateHitText(rawHit, 56) || '—');
    const durationVal = Number(item.duration || 0);
    const durationText = durationVal > 0 ? `${durationVal}s` : (item.mode || '—');

    const cTime = document.createElement('div');
    cTime.textContent = item.time || '--';
    const cHit = document.createElement('div');
    cHit.textContent = displayHit;
    cHit.title = rawHit || '';
    const cDur = document.createElement('div');
    cDur.textContent = durationText;

    row.appendChild(cTime);
    row.appendChild(cHit);
    row.appendChild(cDur);
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


// ---------------------------------------------------------------------------
// Weather Sounding Module (ACARS / Radiosonde)
// ---------------------------------------------------------------------------

(function() {
  const wxModule = document.getElementById('wx-module');
  const wxDot = document.getElementById('wx-dot');
  const wxStatusEl = document.getElementById('wx-status');
  const wxTitleEl = document.getElementById('wx-title');
  const wxMsgCount = document.getElementById('wx-msg-count');
  const wxMetCount = document.getElementById('wx-met-count');
  const wxFeed = document.getElementById('wx-feed');
  const skewtCanvas = document.getElementById('wx-skewt');
  const hodoCanvas = document.getElementById('wx-hodo');

  let wxPolling = null;
  let wxActive = false;

  // Tab switching
  document.querySelectorAll('[data-wx-tab]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-wx-tab]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.querySelectorAll('.wx-panel').forEach(p => p.classList.add('hidden'));
      document.getElementById('wx-panel-' + btn.dataset.wxTab).classList.remove('hidden');
    });
  });

  // Export buttons
  document.getElementById('wx-export-csv').addEventListener('click', () => {
    window.open('/api/wx/export?format=csv', '_blank');
  });
  document.getElementById('wx-export-json').addEventListener('click', () => {
    window.open('/api/wx/export?format=json', '_blank');
  });
  document.getElementById('wx-export-spc').addEventListener('click', () => {
    window.open('/api/wx/export?format=spc', '_blank');
  });
  document.getElementById('wx-clear').addEventListener('click', async () => {
    await post('/api/wx/clear', {});
  });

  // Called from refresh() with the /api/status payload
  window.updateWxStatus = function(st) {
    const decoder = st.wx_decoder_active;
    if (decoder) {
      wxModule.classList.remove('hidden');
      wxDot.classList.remove('bad');
      wxDot.classList.add('pulse');
      const label = decoder === 'radiosonde' ? 'Radiosonde' : 'ACARS Weather';
      wxTitleEl.textContent = label;
      wxStatusEl.textContent = 'collecting';
      wxMsgCount.textContent = st.wx_met_count || 0;
      if (!wxPolling) {
        wxPolling = setInterval(refreshWx, 5000);
        refreshWx();
      }
      wxActive = true;
    } else {
      if (wxActive) {
        wxModule.classList.add('hidden');
        wxDot.classList.remove('pulse');
        wxDot.classList.add('bad');
        wxStatusEl.textContent = 'inactive';
        if (wxPolling) { clearInterval(wxPolling); wxPolling = null; }
        wxActive = false;
      }
    }
  };

  async function refreshWx() {
    try {
      const [status, sounding, msgs] = await Promise.all([
        getJSON('/api/wx/status'),
        getJSON('/api/wx/sounding'),
        getJSON('/api/wx/messages?limit=30'),
      ]);
      wxMsgCount.textContent = status.message_count || 0;
      wxMetCount.textContent = status.met_count || 0;

      if (sounding.levels && sounding.levels.length) {
        drawSkewT(skewtCanvas, sounding.levels);
        drawHodograph(hodoCanvas, sounding.levels);
      } else {
        drawPlaceholder(skewtCanvas, 'Waiting for meteorological data...');
        drawPlaceholder(hodoCanvas, 'Waiting for wind data...');
      }

      renderFeed(msgs.messages || []);
    } catch(e) {
      // ignore fetch errors
    }
  }

  function renderFeed(messages) {
    wxFeed.innerHTML = '';
    messages.reverse().forEach(m => {
      const div = document.createElement('div');
      div.className = 'wx-msg' + (m.is_met ? ' met' : '');
      div.textContent = m.text;
      wxFeed.appendChild(div);
    });
  }

  function drawPlaceholder(canvas, text) {
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = '#666';
    ctx.font = '14px monospace';
    ctx.textAlign = 'center';
    ctx.fillText(text, W/2, H/2);
  }

  // ---- Skew-T Log-P Diagram ----

  function drawSkewT(canvas, levels) {
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const m = { top: 30, right: 70, bottom: 30, left: 55 };
    const pW = W - m.left - m.right;
    const pH = H - m.top - m.bottom;

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, W, H);

    const pMin = 100, pMax = 1050;
    const logMin = Math.log(pMin), logMax = Math.log(pMax);
    const tMin = -80, tMax = 40;
    const skewFactor = 0.7;

    function pToY(p) {
      return m.top + (Math.log(p) - logMin) / (logMax - logMin) * pH;
    }
    function tToX(t, p) {
      const skew = (pToY(pMax) - pToY(p)) * skewFactor;
      return m.left + (t - tMin) / (tMax - tMin) * pW + skew;
    }

    // Draw isotherms (vertical-ish lines, skewed)
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 0.5;
    for (let t = -80; t <= 40; t += 10) {
      ctx.beginPath();
      ctx.moveTo(tToX(t, pMax), pToY(pMax));
      ctx.lineTo(tToX(t, pMin), pToY(pMin));
      ctx.stroke();
      // Label at bottom
      if (t % 20 === 0) {
        ctx.fillStyle = '#555';
        ctx.font = '10px monospace';
        ctx.textAlign = 'center';
        ctx.fillText(t + '\u00B0', tToX(t, pMax), pToY(pMax) + 14);
      }
    }

    // Draw pressure lines (horizontal)
    const pLines = [1000, 925, 850, 700, 500, 400, 300, 250, 200, 150, 100];
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 0.5;
    pLines.forEach(p => {
      const y = pToY(p);
      ctx.beginPath();
      ctx.moveTo(m.left, y);
      ctx.lineTo(W - m.right, y);
      ctx.stroke();
      ctx.fillStyle = '#555';
      ctx.font = '10px monospace';
      ctx.textAlign = 'right';
      ctx.fillText(p + '', m.left - 4, y + 3);
    });

    // Draw 0°C isotherm in blue
    ctx.strokeStyle = 'rgba(100,100,255,0.4)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(tToX(0, pMax), pToY(pMax));
    ctx.lineTo(tToX(0, pMin), pToY(pMin));
    ctx.stroke();

    // Draw dry adiabats
    ctx.strokeStyle = 'rgba(200,120,50,0.2)';
    ctx.lineWidth = 0.5;
    for (let theta = -30; theta <= 80; theta += 10) {
      ctx.beginPath();
      let first = true;
      for (let p = pMax; p >= pMin; p -= 10) {
        // Poisson's equation: T = theta * (p/1000)^0.286
        const tK = (theta + 273.15) * Math.pow(p / 1000, 0.286);
        const tC = tK - 273.15;
        const x = tToX(tC, p), y = pToY(p);
        if (x < m.left - 30 || x > W - m.right + 30) { first = true; continue; }
        if (first) { ctx.moveTo(x, y); first = false; }
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }

    // Sort levels by pressure descending (surface first)
    levels.sort((a, b) => b.pressure_hpa - a.pressure_hpa);

    const isRadiosonde = levels.length > 0 && levels[0].source === 'radiosonde';

    // Temperature profile (red)
    ctx.strokeStyle = '#ff4444';
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    let first = true;
    levels.forEach(l => {
      if (l.temp_c === -9999) return;
      const x = tToX(l.temp_c, l.pressure_hpa);
      const y = pToY(l.pressure_hpa);
      if (isRadiosonde) {
        if (first) { ctx.moveTo(x, y); first = false; }
        else ctx.lineTo(x, y);
      } else {
        ctx.moveTo(x - 3, y); ctx.arc(x, y, 3, 0, Math.PI * 2);
      }
    });
    if (isRadiosonde) ctx.stroke();
    else { ctx.fillStyle = '#ff4444'; ctx.fill(); }

    // Dewpoint profile (blue/green)
    ctx.strokeStyle = '#44cc44';
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    first = true;
    levels.forEach(l => {
      if (l.dewpoint_c === -9999) return;
      const x = tToX(l.dewpoint_c, l.pressure_hpa);
      const y = pToY(l.pressure_hpa);
      if (isRadiosonde) {
        if (first) { ctx.moveTo(x, y); first = false; }
        else ctx.lineTo(x, y);
      } else {
        ctx.moveTo(x - 3, y); ctx.arc(x, y, 3, 0, Math.PI * 2);
      }
    });
    if (isRadiosonde) ctx.stroke();
    else { ctx.fillStyle = '#44cc44'; ctx.fill(); }

    // Wind barbs on right margin
    const barbX = W - m.right + 20;
    ctx.strokeStyle = '#ccc';
    ctx.fillStyle = '#ccc';
    ctx.lineWidth = 1;
    levels.forEach(l => {
      if (l.wind_speed_kt <= 0) return;
      const y = pToY(l.pressure_hpa);
      drawWindBarb(ctx, barbX, y, l.wind_dir_deg, l.wind_speed_kt);
    });

    // Title
    ctx.fillStyle = '#aaa';
    ctx.font = 'bold 12px monospace';
    ctx.textAlign = 'left';
    ctx.fillText('Skew-T Log-P', m.left, 16);
    ctx.font = '10px monospace';
    ctx.fillText(levels.length + ' obs', m.left + 120, 16);
  }

  function drawWindBarb(ctx, x, y, dir, speed) {
    const len = 20;
    const rad = (270 - dir) * Math.PI / 180;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(-rad);

    // Staff
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(len, 0);
    ctx.stroke();

    let remaining = speed;
    let pos = len;

    // Pennants (50 kt)
    while (remaining >= 50) {
      ctx.beginPath();
      ctx.moveTo(pos, 0);
      ctx.lineTo(pos - 4, -8);
      ctx.lineTo(pos - 4, 0);
      ctx.closePath();
      ctx.fill();
      pos -= 5;
      remaining -= 50;
    }
    // Full barbs (10 kt)
    while (remaining >= 10) {
      ctx.beginPath();
      ctx.moveTo(pos, 0);
      ctx.lineTo(pos - 2, -8);
      ctx.stroke();
      pos -= 3;
      remaining -= 10;
    }
    // Half barb (5 kt)
    if (remaining >= 5) {
      ctx.beginPath();
      ctx.moveTo(pos, 0);
      ctx.lineTo(pos - 1, -5);
      ctx.stroke();
    }

    ctx.restore();
  }

  // ---- Hodograph ----

  function drawHodograph(canvas, levels) {
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const cx = W / 2, cy = H / 2;
    const maxSpeed = 60;
    const scale = Math.min(cx, cy) * 0.8 / maxSpeed;

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, W, H);

    // Speed rings
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 0.5;
    for (let s = 10; s <= maxSpeed; s += 10) {
      ctx.beginPath();
      ctx.arc(cx, cy, s * scale, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = '#555';
      ctx.font = '9px monospace';
      ctx.textAlign = 'center';
      ctx.fillText(s + 'kt', cx + s * scale + 2, cy - 4);
    }

    // Cardinal directions
    ctx.strokeStyle = '#444';
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.moveTo(cx, cy - maxSpeed * scale); ctx.lineTo(cx, cy + maxSpeed * scale);
    ctx.moveTo(cx - maxSpeed * scale, cy); ctx.lineTo(cx + maxSpeed * scale, cy);
    ctx.stroke();

    ctx.fillStyle = '#666';
    ctx.font = '11px monospace';
    ctx.textAlign = 'center';
    ctx.fillText('N', cx, cy - maxSpeed * scale - 4);
    ctx.fillText('S', cx, cy + maxSpeed * scale + 12);
    ctx.fillText('E', cx + maxSpeed * scale + 12, cy + 4);
    ctx.fillText('W', cx - maxSpeed * scale - 12, cy + 4);

    // Plot wind vectors
    levels.sort((a, b) => a.altitude_ft - b.altitude_ft);
    const pts = levels
      .filter(l => l.wind_speed_kt > 0)
      .map(l => {
        const rad = (270 - l.wind_dir_deg) * Math.PI / 180;
        return {
          x: cx + l.wind_speed_kt * Math.cos(rad) * scale,
          y: cy - l.wind_speed_kt * Math.sin(rad) * scale,
          alt: l.altitude_ft,
        };
      });

    if (pts.length < 2) return;

    // Color by altitude band
    function altColor(alt) {
      if (alt < 10000) return '#44cc44';   // 0-3 km green
      if (alt < 20000) return '#cccc44';   // 3-6 km yellow
      if (alt < 30000) return '#cc4444';   // 6-9 km red
      return '#44cccc';                     // 9+ km cyan
    }

    // Draw segments
    ctx.lineWidth = 2;
    for (let i = 1; i < pts.length; i++) {
      ctx.strokeStyle = altColor(pts[i].alt);
      ctx.beginPath();
      ctx.moveTo(pts[i-1].x, pts[i-1].y);
      ctx.lineTo(pts[i].x, pts[i].y);
      ctx.stroke();
    }

    // Dots at each point
    pts.forEach(p => {
      ctx.fillStyle = altColor(p.alt);
      ctx.beginPath();
      ctx.arc(p.x, p.y, 2.5, 0, Math.PI * 2);
      ctx.fill();
    });

    // Title
    ctx.fillStyle = '#aaa';
    ctx.font = 'bold 12px monospace';
    ctx.textAlign = 'left';
    ctx.fillText('Hodograph', 8, 16);
    ctx.font = '10px monospace';
    ctx.fillText(pts.length + ' obs', 100, 16);

    // Legend
    const leg = [
      ['0-10kft', '#44cc44'], ['10-20kft', '#cccc44'],
      ['20-30kft', '#cc4444'], ['30kft+', '#44cccc'],
    ];
    let lx = 8, ly = H - 10;
    ctx.font = '9px monospace';
    leg.forEach(([label, color]) => {
      ctx.fillStyle = color;
      ctx.fillRect(lx, ly - 7, 8, 8);
      ctx.fillStyle = '#888';
      ctx.textAlign = 'left';
      ctx.fillText(label, lx + 10, ly);
      lx += 70;
    });
  }

  // Initial placeholder
  drawPlaceholder(skewtCanvas, 'Select ACARS or Radiosonde profile to begin');
  drawPlaceholder(hodoCanvas, 'Select ACARS or Radiosonde profile to begin');
})();
