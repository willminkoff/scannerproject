import ee from"https://esm.sh/react@18";import{createRoot as Ye}from"https://esm.sh/react-dom@18/client";import D from"https://esm.sh/react@18";import ge,{createContext as be,useCallback as w,useContext as he,useEffect as se,useMemo as Ne,useReducer as Ee}from"https://esm.sh/react@18";var Se={"Content-Type":"application/json"};async function O(e,{method:r="GET",body:t}={}){let a={method:r,headers:{...Se}};t!==void 0&&(a.body=JSON.stringify(t));let o=await fetch(e,a),n=await o.text(),d={};try{d=n?JSON.parse(n):{}}catch{d={raw:n}}if(!o.ok){let f=d?.error||`Request failed (${o.status})`,m=new Error(f);throw m.status=o.status,m.payload=d,m}return d}function F(){return O("/api/hp/state")}function te(e){return O("/api/hp/state",{method:"POST",body:e})}function V(){return O("/api/hp/service-types")}function $(){return O("/api/status")}function re(e){return O("/api/mode",{method:"POST",body:{mode:e}})}function ae(e={}){return O("/api/hp/hold",{method:"POST",body:e})}function oe(e={}){return O("/api/hp/next",{method:"POST",body:e})}function ne(e={}){return O("/api/hp/avoid",{method:"POST",body:e})}var i=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),_e={hpState:{},serviceTypes:[],liveStatus:{},currentScreen:i.MAIN,mode:"hp",loading:!0,working:!1,error:"",message:""};function Ae(e,r){switch(r.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:r.payload.hpState||{},serviceTypes:r.payload.serviceTypes||[],liveStatus:r.payload.liveStatus||{},mode:r.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:r.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!r.payload};case"SET_ERROR":return{...e,error:r.payload||""};case"SET_MESSAGE":return{...e,message:r.payload||""};case"SET_HP_STATE":return{...e,hpState:r.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:r.payload||[]};case"SET_LIVE_STATUS":return{...e,liveStatus:r.payload||{}};case"SET_MODE":return{...e,mode:r.payload||e.mode};case"NAVIGATE":return{...e,currentScreen:r.payload||i.MAIN};default:return e}}var le=be(null);function ie(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(t=>({service_tag:Number(t?.service_tag),name:String(t?.name||`Service ${t?.service_tag}`),enabled_by_default:!!t?.enabled_by_default}))}function de(e){let r=e&&typeof e.state=="object"&&e.state!==null?e.state:{},t=String(e?.mode||"hp").toLowerCase();return{hpState:r,mode:t}}function ce({children:e}){let[r,t]=Ee(Ae,_e),a=w(v=>{t({type:"NAVIGATE",payload:v})},[]),o=w(async()=>{let v=await F(),s=de(v);return t({type:"SET_HP_STATE",payload:s.hpState}),t({type:"SET_MODE",payload:s.mode}),s},[]),n=w(async()=>{let v=await V(),s=ie(v);return t({type:"SET_SERVICE_TYPES",payload:s}),s},[]),d=w(async()=>{let v=await $();return t({type:"SET_LIVE_STATUS",payload:v||{}}),v},[]),f=w(async()=>{t({type:"LOAD_START"});try{let[v,s]=await Promise.all([F(),V()]),S={};try{S=await $()}catch{S={}}let L=de(v),G=ie(s);t({type:"LOAD_SUCCESS",payload:{hpState:L.hpState,mode:L.mode,serviceTypes:G,liveStatus:S}})}catch(v){t({type:"LOAD_ERROR",payload:v.message})}},[]);se(()=>{f()},[f]),se(()=>{let v=setInterval(()=>{d().catch(()=>{})},2500);return()=>clearInterval(v)},[d]);let m=w(async v=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let s={...r.hpState,...v},S=await te(s),L=S?.state&&typeof S.state=="object"?{...r.hpState,...S.state}:s;return t({type:"SET_HP_STATE",payload:L}),t({type:"SET_MESSAGE",payload:"State saved"}),S}catch(s){throw t({type:"SET_ERROR",payload:s.message}),s}finally{t({type:"SET_WORKING",payload:!1})}},[r.hpState]),b=w(async v=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let s=await re(v),S=String(s?.mode||v||"hp").toLowerCase();return t({type:"SET_MODE",payload:S}),t({type:"SET_MESSAGE",payload:`Mode set to ${S}`}),s}catch(s){throw t({type:"SET_ERROR",payload:s.message}),s}finally{t({type:"SET_WORKING",payload:!1})}},[]),p=w(async(v,s)=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let S=await v();return s&&t({type:"SET_MESSAGE",payload:s}),await o(),await d(),S}catch(S){throw t({type:"SET_ERROR",payload:S.message}),S}finally{t({type:"SET_WORKING",payload:!1})}},[o,d]),u=w(async()=>p(()=>ae(),"Hold command sent"),[p]),l=w(async()=>p(()=>oe(),"Next command sent"),[p]),g=w(async(v={})=>p(()=>ne(v),"Avoid command sent"),[p]),T=Ne(()=>({state:r,dispatch:t,navigate:a,refreshAll:f,refreshHpState:o,refreshServiceTypes:n,refreshStatus:d,saveHpState:m,setMode:b,holdScan:u,nextScan:l,avoidCurrent:g,SCREENS:i}),[r,a,f,o,n,d,m,b,u,l,g]);return ge.createElement(le.Provider,{value:T},e)}function N(){let e=he(le);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import I from"https://esm.sh/react@18";import c,{useEffect as xe,useMemo as we,useState as ke}from"https://esm.sh/react@18";import Te from"https://esm.sh/react@18";function y({children:e,onClick:r,type:t="button",variant:a="primary",className:o="",disabled:n=!1}){return Te.createElement("button",{type:t,className:`btn ${a==="secondary"?"btn-secondary":a==="danger"?"btn-danger":""} ${o}`.trim(),onClick:r,disabled:n},e)}import B from"https://esm.sh/react@18";function E({title:e,subtitle:r="",showBack:t=!1,onBack:a}){return B.createElement("div",{className:"header"},B.createElement("div",null,B.createElement("h1",null,e),r?B.createElement("div",{className:"sub"},r):null),t?B.createElement("button",{type:"button",className:"btn btn-secondary",onClick:a},"Back"):null)}function U(e){return e==null||e===""?"--":String(e)}function z(){let{state:e,holdScan:r,nextScan:t,navigate:a}=N(),{hpState:o,liveStatus:n,working:d,error:f,message:m}=e,b=String(n?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),p=String(n?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),u=e.mode==="hp"&&p||b,l=we(()=>{let _=[];return b&&_.push({id:b,label:`Analog (${b})`}),p&&p!==b&&_.push({id:p,label:`Digital (${p})`}),_},[b,p]),[g,T]=ke(u);xe(()=>{l.some(fe=>fe.id===g)||T(u||l[0]?.id||"")},[u,g,l]);let v=n?.digital_scheduler_active_system||n?.digital_profile||o.system_name||o.system,s=n?.digital_last_label||o.department_name||o.department,S=n?.digital_last_tgid??o.tgid??o.talkgroup_id,L=(()=>{let _=Number(n?.digital_preflight?.playlist_frequency_hz?.[0]||n?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(_)&&_>0?(_/1e6).toFixed(4):o.frequency??o.freq})(),G=n?.digital_control_channel_locked?"Locked":n?.digital_control_decode_available?"Decoding":o.signal??o.signal_strength,me=async()=>{try{await r()}catch{}},ve=async()=>{try{await t()}catch{}};return c.createElement("section",{className:"screen main-screen"},c.createElement(E,{title:"Home Patrol 3",subtitle:`Mode: ${e.mode.toUpperCase()}`}),c.createElement("div",{className:"field-grid"},c.createElement("div",{className:"card"},c.createElement("div",{className:"muted"},"System"),c.createElement("div",null,U(v))),c.createElement("div",{className:"card"},c.createElement("div",{className:"muted"},"Department"),c.createElement("div",null,U(s))),c.createElement("div",{className:"card"},c.createElement("div",{className:"muted"},"TGID"),c.createElement("div",null,U(S))),c.createElement("div",{className:"card"},c.createElement("div",{className:"muted"},"Frequency"),c.createElement("div",null,U(L))),c.createElement("div",{className:"card"},c.createElement("div",{className:"muted"},"Signal"),c.createElement("div",null,U(G)))),c.createElement("div",{className:"button-row"},c.createElement(y,{onClick:me,disabled:d},"HOLD"),c.createElement(y,{onClick:ve,disabled:d},"NEXT"),c.createElement(y,{variant:"secondary",onClick:()=>a(i.MENU),disabled:d},"MENU")),c.createElement("div",{className:"card",style:{marginTop:"12px"}},c.createElement("div",{className:"row",style:{marginBottom:"8px"}},c.createElement("div",{className:"muted"},"Live Stream"),g?c.createElement("a",{href:`/stream/${g}`,target:"_blank",rel:"noreferrer"},"Open"):null),c.createElement("div",{className:"row",style:{marginBottom:"8px"}},c.createElement("select",{className:"input",value:g,onChange:_=>T(_.target.value),style:{maxWidth:"260px"}},l.map(_=>c.createElement("option",{key:_.id,value:_.id},_.label)))),c.createElement("audio",{controls:!0,preload:"none",style:{width:"100%"},src:g?`/stream/${g}`:"/stream/"})),f?c.createElement("div",{className:"error"},f):null,m?c.createElement("div",{className:"message"},m):null)}import H from"https://esm.sh/react@18";var Ce=[{id:i.LOCATION,label:"Location"},{id:i.SERVICE_TYPES,label:"Service Types"},{id:i.RANGE,label:"Range"},{id:i.FAVORITES,label:"Favorites"},{id:i.AVOID,label:"Avoid"},{id:i.MODE_SELECTION,label:"Mode Selection"}];function j(){let{navigate:e,state:r}=N();return H.createElement("section",{className:"screen menu"},H.createElement(E,{title:"Menu",showBack:!0,onBack:()=>e(i.MAIN)}),H.createElement("div",{className:"menu-list"},Ce.map(t=>H.createElement(y,{key:t.id,variant:"secondary",className:"menu-item",onClick:()=>e(t.id),disabled:r.working},t.label))),r.error?H.createElement("div",{className:"error"},r.error):null)}import h,{useEffect as Me,useState as P}from"https://esm.sh/react@18";function pe(e){if(e===""||e===null||e===void 0)return null;let r=Number(e);return Number.isFinite(r)?r:NaN}function q(){let{state:e,saveHpState:r,navigate:t}=N(),{hpState:a,working:o}=e,[n,d]=P(""),[f,m]=P(""),[b,p]=P(""),[u,l]=P(!0),[g,T]=P("");return Me(()=>{d(a.zip||a.postal_code||""),m(a.lat!==void 0&&a.lat!==null?String(a.lat):a.latitude!==void 0&&a.latitude!==null?String(a.latitude):""),p(a.lon!==void 0&&a.lon!==null?String(a.lon):a.longitude!==void 0&&a.longitude!==null?String(a.longitude):""),l(a.use_location!==!1)},[a]),h.createElement("section",{className:"screen location-screen"},h.createElement(E,{title:"Location",showBack:!0,onBack:()=>t(i.MENU)}),h.createElement("div",{className:"list"},h.createElement("label",null,h.createElement("div",{className:"muted"},"ZIP"),h.createElement("input",{className:"input",value:n,onChange:s=>d(s.target.value.trim()),placeholder:"37201"})),h.createElement("label",null,h.createElement("div",{className:"muted"},"Latitude"),h.createElement("input",{className:"input",value:f,onChange:s=>m(s.target.value),placeholder:"36.12"})),h.createElement("label",null,h.createElement("div",{className:"muted"},"Longitude"),h.createElement("input",{className:"input",value:b,onChange:s=>p(s.target.value),placeholder:"-86.67"})),h.createElement("label",{className:"row"},h.createElement("span",null,"Use location for scanning"),h.createElement("input",{type:"checkbox",checked:u,onChange:s=>l(s.target.checked)}))),h.createElement("div",{className:"button-row"},h.createElement(y,{onClick:async()=>{if(T(""),n&&!/^\d{5}(-\d{4})?$/.test(n)){T("ZIP must be 5 digits or ZIP+4.");return}let s=pe(f),S=pe(b);if(Number.isNaN(s)||Number.isNaN(S)){T("Latitude and longitude must be valid numbers.");return}if(s!==null&&(s<-90||s>90)){T("Latitude must be between -90 and 90.");return}if(S!==null&&(S<-180||S>180)){T("Longitude must be between -180 and 180.");return}try{await r({zip:n,lat:s,lon:S,use_location:u}),t(i.MENU)}catch{}},disabled:o},"Save")),g?h.createElement("div",{className:"error"},g):null,e.error?h.createElement("div",{className:"error"},e.error):null)}import M,{useEffect as Ie,useMemo as Oe,useState as Le}from"https://esm.sh/react@18";function W(){let{state:e,saveHpState:r,navigate:t}=N(),{hpState:a,serviceTypes:o,working:n}=e,d=Oe(()=>o.filter(u=>u.enabled_by_default).map(u=>Number(u.service_tag)),[o]),[f,m]=Le([]);Ie(()=>{let u=Array.isArray(a.enabled_service_tags)?a.enabled_service_tags.map(Number):d;m(Array.from(new Set(u)).filter(l=>Number.isFinite(l)))},[a.enabled_service_tags,d]);let b=u=>{m(l=>l.includes(u)?l.filter(g=>g!==u):[...l,u])},p=async()=>{try{await r({enabled_service_tags:[...f].sort((u,l)=>u-l)}),t(i.MENU)}catch{}};return M.createElement("section",{className:"screen service-types-screen"},M.createElement(E,{title:"Service Types",showBack:!0,onBack:()=>t(i.MENU)}),M.createElement("div",{className:"checkbox-list"},o.map(u=>{let l=Number(u.service_tag),g=f.includes(l);return M.createElement("label",{key:l,className:"row card"},M.createElement("span",null,u.name),M.createElement("input",{type:"checkbox",checked:g,onChange:()=>b(l)}))})),M.createElement("div",{className:"button-row"},M.createElement(y,{onClick:p,disabled:n},"Save")),e.error?M.createElement("div",{className:"error"},e.error):null)}import k,{useEffect as Be,useState as Ue}from"https://esm.sh/react@18";function K(){let{state:e,saveHpState:r,navigate:t}=N(),{hpState:a,working:o}=e,[n,d]=Ue(15);return Be(()=>{let m=Number(a.range_miles);d(Number.isFinite(m)?m:15)},[a.range_miles]),k.createElement("section",{className:"screen range-screen"},k.createElement(E,{title:"Range",showBack:!0,onBack:()=>t(i.MENU)}),k.createElement("div",{className:"card"},k.createElement("div",{className:"row"},k.createElement("span",null,"Range Miles"),k.createElement("strong",null,n)),k.createElement("input",{className:"range",type:"range",min:"0",max:"100",step:"1",value:n,onChange:m=>d(Number(m.target.value))})),k.createElement("div",{className:"button-row"},k.createElement(y,{onClick:async()=>{try{await r({range_miles:n}),t(i.MENU)}catch{}},disabled:o},"Save")),e.error?k.createElement("div",{className:"error"},e.error):null)}import C,{useEffect as He,useMemo as Pe,useState as De}from"https://esm.sh/react@18";function Ge(e){return Array.isArray(e)?e.map((r,t)=>r&&typeof r=="object"?{id:r.id??r.name??`fav-${t}`,name:String(r.name||r.label||`Favorite ${t+1}`),enabled:r.enabled!==!1}:{id:`fav-${t}`,name:String(r),enabled:!0}):[]}function Y(){let{state:e,saveHpState:r,navigate:t}=N(),{hpState:a,working:o}=e,n=Pe(()=>Array.isArray(a.favorites)?a.favorites:Array.isArray(a.favorites_list)?a.favorites_list:[],[a.favorites,a.favorites_list]),[d,f]=De([]);He(()=>{f(Ge(n))},[n]);let m=p=>{f(u=>u.map(l=>l.id===p?{...l,enabled:!l.enabled}:l))},b=async()=>{try{await r({favorites:d}),t(i.MENU)}catch{}};return C.createElement("section",{className:"screen favorites-screen"},C.createElement(E,{title:"Favorites",showBack:!0,onBack:()=>t(i.MENU)}),d.length===0?C.createElement("div",{className:"muted"},"No favorites in current state."):C.createElement("div",{className:"list"},d.map(p=>C.createElement("label",{key:p.id,className:"row card"},C.createElement("span",null,p.name),C.createElement("input",{type:"checkbox",checked:p.enabled,onChange:()=>m(p.id)})))),C.createElement("div",{className:"button-row"},C.createElement(y,{onClick:b,disabled:o},"Save")),e.error?C.createElement("div",{className:"error"},e.error):null)}import A,{useEffect as Fe,useMemo as Ve,useState as $e}from"https://esm.sh/react@18";function ze(e){return Array.isArray(e)?e.map((r,t)=>r&&typeof r=="object"?{id:r.id??`${r.type||"item"}-${t}`,label:String(r.label||r.alpha_tag||r.name||`Avoid ${t+1}`),type:String(r.type||"item")}:{id:`item-${t}`,label:String(r),type:"item"}):[]}function Z(){let{state:e,saveHpState:r,avoidCurrent:t,navigate:a}=N(),{hpState:o,working:n}=e,d=Ve(()=>Array.isArray(o.avoid_list)?o.avoid_list:Array.isArray(o.avoids)?o.avoids:Array.isArray(o.avoid)?o.avoid:[],[o.avoid_list,o.avoids,o.avoid]),[f,m]=$e([]);Fe(()=>{m(ze(d))},[d]);let b=()=>{m([])},p=async(l=f)=>{try{await r({avoid_list:l})}catch{}},u=async()=>{try{await t()}catch{}};return A.createElement("section",{className:"screen avoid-screen"},A.createElement(E,{title:"Avoid",showBack:!0,onBack:()=>a(i.MENU)}),f.length===0?A.createElement("div",{className:"muted"},"No avoided items in current state."):A.createElement("div",{className:"list"},f.map(l=>A.createElement("div",{key:l.id,className:"row card"},A.createElement("div",null,A.createElement("div",null,l.label),A.createElement("div",{className:"muted"},l.type)),A.createElement(y,{variant:"danger",onClick:()=>{let g=f.filter(T=>T.id!==l.id);m(g),p(g)},disabled:n},"Remove")))),A.createElement("div",{className:"button-row"},A.createElement(y,{onClick:u,disabled:n},"Avoid Current"),A.createElement(y,{variant:"secondary",onClick:()=>{b(),p([])},disabled:n},"Clear"),A.createElement(y,{onClick:()=>p(),disabled:n},"Save")),e.error?A.createElement("div",{className:"error"},e.error):null)}import x,{useEffect as je,useState as qe}from"https://esm.sh/react@18";function J(){let{state:e,setMode:r,navigate:t}=N(),[a,o]=qe("hp");return je(()=>{o(e.mode||"hp")},[e.mode]),x.createElement("section",{className:"screen mode-selection-screen"},x.createElement(E,{title:"Mode Selection",showBack:!0,onBack:()=>t(i.MENU)}),x.createElement("div",{className:"list"},x.createElement("label",{className:"row card"},x.createElement("span",null,"HP Mode"),x.createElement("input",{type:"radio",name:"scan-mode",value:"hp",checked:a==="hp",onChange:d=>o(d.target.value)})),x.createElement("label",{className:"row card"},x.createElement("span",null,"Expert Mode"),x.createElement("input",{type:"radio",name:"scan-mode",value:"expert",checked:a==="expert",onChange:d=>o(d.target.value)}))),x.createElement("div",{className:"button-row"},x.createElement(y,{onClick:async()=>{try{await r(a),t(i.MENU)}catch{}},disabled:e.working},"Save")),e.error?x.createElement("div",{className:"error"},e.error):null)}import We from"https://esm.sh/react@18";function X({label:e="Loading..."}){return We.createElement("div",{className:"loading"},e)}function Q(){let{state:e}=N();if(e.loading)return I.createElement(X,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case i.MENU:return I.createElement(j,null);case i.LOCATION:return I.createElement(q,null);case i.SERVICE_TYPES:return I.createElement(W,null);case i.RANGE:return I.createElement(K,null);case i.FAVORITES:return I.createElement(Y,null);case i.AVOID:return I.createElement(Z,null);case i.MODE_SELECTION:return I.createElement(J,null);case i.MAIN:default:return I.createElement(z,null)}}var Ke=`
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: Arial, Helvetica, sans-serif;
    background: #101317;
    color: #e9eef5;
  }
  .app-shell {
    min-height: 100vh;
    max-width: 520px;
    margin: 0 auto;
    padding: 12px;
  }
  .screen {
    background: #1b2129;
    border: 1px solid #2b3441;
    border-radius: 10px;
    padding: 14px;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
  }
  .header h1 {
    font-size: 1.1rem;
    margin: 0;
  }
  .header .sub {
    color: #9fb0c7;
    font-size: 0.85rem;
  }
  .btn {
    border: 1px solid #3f4f65;
    background: #2a3647;
    color: #e9eef5;
    border-radius: 8px;
    padding: 8px 12px;
    cursor: pointer;
    font-size: 0.9rem;
  }
  .btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .btn-secondary {
    background: #232b35;
  }
  .btn-danger {
    background: #5f2631;
    border-color: #7d3442;
  }
  .button-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 12px;
  }
  .field-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .card {
    border: 1px solid #2b3441;
    background: #11161d;
    border-radius: 8px;
    padding: 10px;
  }
  .menu-list,
  .checkbox-list,
  .list {
    display: grid;
    gap: 8px;
  }
  .input,
  .range {
    width: 100%;
    padding: 8px;
    border-radius: 8px;
    border: 1px solid #3f4f65;
    background: #0f141a;
    color: #e9eef5;
  }
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .muted {
    color: #9fb0c7;
  }
  .error {
    color: #ff7f90;
    margin-top: 8px;
  }
  .message {
    color: #7edc9f;
    margin-top: 8px;
  }
  .loading {
    padding: 20px 8px;
    text-align: center;
    color: #9fb0c7;
  }
`;function R(){return D.createElement(ce,null,D.createElement("div",{className:"app-shell"},D.createElement("style",null,Ke),D.createElement(Q,null)))}var ue=document.getElementById("root");if(!ue)throw new Error("Missing #root mount element");Ye(ue).render(ee.createElement(ee.StrictMode,null,ee.createElement(R,null)));
