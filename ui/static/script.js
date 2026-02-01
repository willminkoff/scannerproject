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
let actionMsg = '';
let actionMsgTarget = null;

const GAIN_STEPS = [
  0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7,
  16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8,
  36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6,
];
const DBFS_MIN = -120;
const DBFS_MAX = 0;

let currentProfileAirband = null;
let currentProfileGround = null;
let hitsView = false;
let activePage = 0;
let avoidsAirband = null;
let avoidsGround = null;

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
    card.innerHTML = `<div><b>${p.label}</b></div>` + (p.exists ? '' : `<small>Missing: ${p.path}</small>`);
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

async function refresh(allowSetSliders=false) {
  const st = await getJSON('/api/status');

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

  updateWarn(st.missing_profiles);
  avoidsAirband = st.avoids_airband;
  avoidsGround = st.avoids_ground;
  updateAvoidsForPage();

  if (currentProfileAirband === null || currentProfileAirband !== st.profile_airband) {
    currentProfileAirband = st.profile_airband;
    buildProfiles(profilesAirbandEl, st.profiles_airband, st.profile_airband, 'airband');
  }
  if (currentProfileGround === null || currentProfileGround !== st.profile_ground) {
    currentProfileGround = st.profile_ground;
    buildProfiles(profilesGroundEl, st.profiles_ground, st.profile_ground, 'ground');
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
  const url = `http://${location.hostname}:8000/GND.mp3`;
  window.open(url, '_blank', 'noopener');
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
