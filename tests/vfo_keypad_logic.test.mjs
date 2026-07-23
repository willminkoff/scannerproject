import fs from 'fs';
const code = fs.readFileSync('/tmp/keypad_extracted.js','utf8');

// --- minimal DOM/app shim -------------------------------------------------
const mk = () => ({ textContent:'', disabled:false, _c:new Set(),
  classList:{ add(c){this._c.add(c)}, remove(c){this._c.delete(c)},
    toggle(c,on){on?this._c.add(c):this._c.delete(c)}, contains(c){return this._c.has(c)} } });
mk.prototype = {};
const els = { 'vfo-kp-display':mk(), 'vfo-kp-value':mk(), 'vfo-kp-tune':mk(), 'vfo-kp-hint':mk(), 'vfo-kp-presets':mk() };
for (const k of Object.keys(els)) els[k].classList._c = new Set();
globalThis.document = { getElementById: id => els[id] || null };
globalThis.state = { vfoTunedHz: 146520000 };
let posted = [];
globalThis.postAPI = async (ep, data) => { posted.push([ep,data]); return { ok:true, listen_hz: data.freq*1e6 }; };
globalThis.logActivity = ()=>{}; globalThis.refresh = async()=>{};
globalThis.messageFromError = (e,f)=>f; globalThis.escapeHtml = s=>String(s);

const fn = new Function(code + '; return {vfoKpParse, vfoKpPress, vfoKpClear, vfoKpTune, renderVfoKeypad, VFO_PRESETS, get entry(){return vfoKpEntry}};');
const K = fn();

let pass=0, fail=0;
const t = (name, cond, got) => { if(cond){pass++; console.log(`  PASS  ${name}`);} else {fail++; console.log(`  FAIL  ${name}  (got: ${JSON.stringify(got)})`);} };

console.log('--- digit entry ---');
K.vfoKpClear(); '146'.split('').forEach(d=>K.vfoKpPress(d));
t('digits 1,4,6 -> "146"', K.entry==='146', K.entry);
K.vfoKpPress('.'); t('decimal appends', K.entry==='146.', K.entry);
'520'.split('').forEach(d=>K.vfoKpPress(d));
t('-> "146.520"', K.entry==='146.520', K.entry);

console.log('--- decimal rules ---');
K.vfoKpPress('.'); t('second decimal REJECTED', K.entry==='146.520', K.entry);
K.vfoKpClear(); K.vfoKpPress('.');
t('leading "." becomes "0."', K.entry==='0.', K.entry);

console.log('--- backspace / clear ---');
K.vfoKpClear(); '146.5'.split('').forEach(d=>K.vfoKpPress(d));
K.vfoKpPress('back'); t('backspace removes last', K.entry==='146.', K.entry);
K.vfoKpPress('back'); t('backspace removes dot', K.entry==='146', K.entry);
K.vfoKpClear(); t('clear empties', K.entry==='', K.entry);
K.vfoKpPress('back'); t('backspace on empty is safe', K.entry==='', K.entry);

console.log('--- all 12 grid keys dispatch individually ---');
for (const d of ['0','1','2','3','4','5','6','7','8','9']) {
  K.vfoKpClear(); K.vfoKpPress(d);
  t(`key "${d}" appends`, K.entry===d, K.entry);
}
K.vfoKpClear(); K.vfoKpPress('5'); K.vfoKpPress('.');
t('key "." appends', K.entry==='5.', K.entry);
K.vfoKpPress('back'); t('key "back" removes', K.entry==='5', K.entry);
// 8-digit cap is deliberate: the longest valid entry (1766.000) is 7 digits.
K.vfoKpClear(); '1234567890'.split('').forEach(d=>K.vfoKpPress(d));
t('entry capped at 8 digits (by design)', K.entry==='12345678', K.entry);
t('longest real freq 1766.000 fits under cap', K.vfoKpParse('1766.000')===1766);

console.log('--- validation ---');
t('146.520 valid', K.vfoKpParse('146.520')===146.52);
t('"" invalid', K.vfoKpParse('')===null);
t('"abc" invalid', K.vfoKpParse('abc')===null);
t('"9999999" invalid (>1766)', K.vfoKpParse('9999999')===null);
t('"1" invalid (<24)', K.vfoKpParse('1')===null);
t('"24" valid (lower bound)', K.vfoKpParse('24')===24);
t('"1766" valid (upper bound)', K.vfoKpParse('1766')===1766);
t('"1766.1" invalid (>max)', K.vfoKpParse('1766.1')===null);
t('"1.2.3" invalid', K.vfoKpParse('1.2.3')===null);
t('"-146" invalid', K.vfoKpParse('-146')===null);

console.log('--- tune dispatch ---');
posted=[]; K.vfoKpClear(); '146.520'.split('').forEach(d=>K.vfoKpPress(d));
await K.vfoKpTune();
t('POSTs /api/tune target=vfo freq=146.52', posted.length===1 && posted[0][0]==='/api/tune' && posted[0][1].target==='vfo' && posted[0][1].freq===146.52, posted);
t('entry cleared after tune', K.entry==='', K.entry);

posted=[]; K.vfoKpClear(); 'abc'.split('').forEach(d=>K.vfoKpPress(d));
const bad = await K.vfoKpTune();
t('invalid entry does NOT submit', posted.length===0 && bad===false, posted);

console.log('--- presets ---');
t('10 presets', K.VFO_PRESETS.length===10, K.VFO_PRESETS.length);
posted=[]; await K.vfoKpTune(K.VFO_PRESETS[4].mhz);
t('preset fires tune with its freq (146.67)', posted.length===1 && posted[0][1].freq===146.670, posted);
t('every preset in 24-1766 range', K.VFO_PRESETS.every(p=>K.vfoKpParse(String(p.mhz))!==null));

console.log(`\n  ${pass} passed, ${fail} failed`);
process.exit(fail?1:0);
