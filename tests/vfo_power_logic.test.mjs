import fs from 'fs';
const code = fs.readFileSync('/tmp/keypad_extracted.js','utf8');
const mk = () => { const o = { textContent:'', disabled:false, _c:new Set(), _a:{} };
  o.classList = { add:c=>o._c.add(c), remove:c=>o._c.delete(c), toggle:(c,on)=>on?o._c.add(c):o._c.delete(c), contains:c=>o._c.has(c) };
  o.setAttribute=(k,v)=>o._a[k]=v; return o; };
const els = {}; for (const id of ['vfo-kp-display','vfo-kp-value','vfo-kp-tune','vfo-kp-hint','vfo-kp-presets','vfo-kp-power']) els[id]=mk();
globalThis.document = { getElementById: id => els[id] || null };
globalThis.state = { vfoTunedHz:146520000, vfoMuted:false };
let posted=[], failNext=false;
globalThis.postAPI = async (ep,d) => { posted.push([ep,d]); if(failNext){failNext=false; throw new Error('HTTP 503');} return {ok:true, muted: d.state==='on'}; };
globalThis.logActivity=()=>{}; globalThis.refresh=async()=>{}; globalThis.messageFromError=(e,f)=>f; globalThis.escapeHtml=s=>String(s);
const K = new Function(code + '; return {renderVfoPower, toggleVfoPower};')();

let pass=0,fail=0; const t=(n,c,g)=>{ if(c){pass++;console.log(`  PASS  ${n}`);} else {fail++;console.log(`  FAIL  ${n} (got ${JSON.stringify(g)})`);} };
const btn = els['vfo-kp-power'];

console.log('--- label reflects state ---');
state.vfoMuted=false; K.renderVfoPower();
t('running -> "Stop VFO"', btn.textContent==='Stop VFO', btn.textContent);
t('running -> is-running class', btn._c.has('is-running') && !btn._c.has('is-stopped'), [...btn._c]);
state.vfoMuted=true; K.renderVfoPower();
t('muted -> "Start VFO"', btn.textContent==='Start VFO', btn.textContent);
t('muted -> is-stopped class', btn._c.has('is-stopped') && !btn._c.has('is-running'), [...btn._c]);
t('aria-pressed tracks state', btn._a['aria-pressed']==='true', btn._a);

console.log('--- click: running -> stop ---');
state.vfoMuted=false; posted=[]; await K.toggleVfoPower();
t('POSTs /api/vfo/mute state=on', posted.length===1 && posted[0][0]==='/api/vfo/mute' && posted[0][1].state==='on', posted);
t('label becomes "Start VFO"', btn.textContent==='Start VFO', btn.textContent);
t('button re-enabled', btn.disabled===false, btn.disabled);

console.log('--- click: stopped -> start ---');
posted=[]; await K.toggleVfoPower();
t('POSTs state=off', posted[0][1].state==='off', posted);
t('label back to "Stop VFO"', btn.textContent==='Stop VFO', btn.textContent);

console.log('--- server readback wins over optimistic flip ---');
state.vfoMuted=false; posted=[];
globalThis.postAPI = async (ep,d)=>{ posted.push([ep,d]); return {ok:true, muted:false}; };  // server says still running
await K.toggleVfoPower();
t('server muted:false overrides optimistic true', state.vfoMuted===false && btn.textContent==='Stop VFO', btn.textContent);

console.log('--- error reverts ---');
globalThis.postAPI = async (ep,d)=>{ posted.push([ep,d]); throw new Error('HTTP 503'); };
state.vfoMuted=false; K.renderVfoPower(); await K.toggleVfoPower();
t('reverts to running on failure', state.vfoMuted===false && btn.textContent==='Stop VFO', btn.textContent);
t('button re-enabled after failure', btn.disabled===false, btn.disabled);
t('hint shows the error', els['vfo-kp-hint'].textContent.includes('failed'), els['vfo-kp-hint'].textContent);

console.log(`\n  ${pass} passed, ${fail} failed`);
process.exit(fail?1:0);
