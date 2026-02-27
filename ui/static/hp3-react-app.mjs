import ee from"https://esm.sh/react@18";import{createRoot as Xe}from"https://esm.sh/react-dom@18/client";import D from"https://esm.sh/react@18";import Ee,{createContext as _e,useCallback as x,useContext as Ae,useEffect as ie,useMemo as Te,useReducer as xe}from"https://esm.sh/react@18";var he={"Content-Type":"application/json"};async function M(e,{method:r="GET",body:t}={}){let a={method:r,headers:{...he}};t!==void 0&&(a.body=JSON.stringify(t));let o=await fetch(e,a),n=await o.text(),l={};try{l=n?JSON.parse(n):{}}catch{l={raw:n}}if(!o.ok){let S=l?.error||`Request failed (${o.status})`,v=new Error(S);throw v.status=o.status,v.payload=l,v}return l}function F(){return M("/api/hp/state")}function re(e){return M("/api/hp/state",{method:"POST",body:e})}function V(){return M("/api/hp/service-types")}function $(){return M("/api/status")}function ae(e){return M("/api/mode",{method:"POST",body:{mode:e}})}function oe(e={}){return M("/api/hp/hold",{method:"POST",body:e})}function ne(e={}){return M("/api/hp/next",{method:"POST",body:e})}function se(e={}){return M("/api/hp/avoid",{method:"POST",body:e})}var i=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),we={hpState:{},serviceTypes:[],liveStatus:{},currentScreen:i.MAIN,mode:"hp",loading:!0,working:!1,error:"",message:""};function ke(e,r){switch(r.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:r.payload.hpState||{},serviceTypes:r.payload.serviceTypes||[],liveStatus:r.payload.liveStatus||{},mode:r.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:r.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!r.payload};case"SET_ERROR":return{...e,error:r.payload||""};case"SET_MESSAGE":return{...e,message:r.payload||""};case"SET_HP_STATE":return{...e,hpState:r.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:r.payload||[]};case"SET_LIVE_STATUS":return{...e,liveStatus:r.payload||{}};case"SET_MODE":return{...e,mode:r.payload||e.mode};case"NAVIGATE":return{...e,currentScreen:r.payload||i.MAIN};default:return e}}var ce=_e(null);function le(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(t=>({service_tag:Number(t?.service_tag),name:String(t?.name||`Service ${t?.service_tag}`),enabled_by_default:!!t?.enabled_by_default}))}function de(e){let r=e&&typeof e.state=="object"&&e.state!==null?e.state:{},t=String(e?.mode||"hp").toLowerCase();return{hpState:r,mode:t}}function pe({children:e}){let[r,t]=xe(ke,we),a=x(m=>{t({type:"NAVIGATE",payload:m})},[]),o=x(async()=>{let m=await F(),s=de(m);return t({type:"SET_HP_STATE",payload:s.hpState}),t({type:"SET_MODE",payload:s.mode}),s},[]),n=x(async()=>{let m=await V(),s=le(m);return t({type:"SET_SERVICE_TYPES",payload:s}),s},[]),l=x(async()=>{let m=await $();return t({type:"SET_LIVE_STATUS",payload:m||{}}),m},[]),S=x(async()=>{t({type:"LOAD_START"});try{let[m,s]=await Promise.all([F(),V()]),p={};try{p=await $()}catch{p={}}let O=de(m),P=le(s);t({type:"LOAD_SUCCESS",payload:{hpState:O.hpState,mode:O.mode,serviceTypes:P,liveStatus:p}})}catch(m){t({type:"LOAD_ERROR",payload:m.message})}},[]);ie(()=>{S()},[S]),ie(()=>{let m=setInterval(()=>{l().catch(()=>{})},2500);return()=>clearInterval(m)},[l]);let v=x(async m=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let s={...r.hpState,...m},p=await re(s),O=p?.state&&typeof p.state=="object"?{...r.hpState,...p.state}:s;return t({type:"SET_HP_STATE",payload:O}),t({type:"SET_MESSAGE",payload:"State saved"}),p}catch(s){throw t({type:"SET_ERROR",payload:s.message}),s}finally{t({type:"SET_WORKING",payload:!1})}},[r.hpState]),E=x(async m=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let s=await ae(m),p=String(s?.mode||m||"hp").toLowerCase();return t({type:"SET_MODE",payload:p}),t({type:"SET_MESSAGE",payload:`Mode set to ${p}`}),s}catch(s){throw t({type:"SET_ERROR",payload:s.message}),s}finally{t({type:"SET_WORKING",payload:!1})}},[]),f=x(async(m,s)=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let p=await m();return s&&t({type:"SET_MESSAGE",payload:s}),await o(),await l(),p}catch(p){throw t({type:"SET_ERROR",payload:p.message}),p}finally{t({type:"SET_WORKING",payload:!1})}},[o,l]),u=x(async()=>f(()=>oe(),"Hold command sent"),[f]),d=x(async()=>f(()=>ne(),"Next command sent"),[f]),_=x(async(m={})=>f(()=>se(m),"Avoid command sent"),[f]),h=Te(()=>({state:r,dispatch:t,navigate:a,refreshAll:S,refreshHpState:o,refreshServiceTypes:n,refreshStatus:l,saveHpState:v,setMode:E,holdScan:u,nextScan:d,avoidCurrent:_,SCREENS:i}),[r,a,S,o,n,l,v,E,u,d,_]);return Ee.createElement(ce.Provider,{value:h},e)}function b(){let e=Ae(ce);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import I from"https://esm.sh/react@18";import c,{useEffect as Ie,useState as Me}from"https://esm.sh/react@18";import Ce from"https://esm.sh/react@18";function g({children:e,onClick:r,type:t="button",variant:a="primary",className:o="",disabled:n=!1}){return Ce.createElement("button",{type:t,className:`btn ${a==="secondary"?"btn-secondary":a==="danger"?"btn-danger":""} ${o}`.trim(),onClick:r,disabled:n},e)}import L from"https://esm.sh/react@18";function N({title:e,subtitle:r="",showBack:t=!1,onBack:a}){return L.createElement("div",{className:"header"},L.createElement("div",null,L.createElement("h1",null,e),r?L.createElement("div",{className:"sub"},r):null),t?L.createElement("button",{type:"button",className:"btn btn-secondary",onClick:a},"Back"):null)}function B(e){return e==null||e===""?"--":String(e)}function z(){let{state:e,holdScan:r,nextScan:t,navigate:a}=b(),{hpState:o,liveStatus:n,working:l,error:S,message:v}=e,E=String(n?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),f=String(n?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),u=!!E,d=!!f,_=(e.mode==="hp"||e.mode==="expert")&&d?"digital":"analog",[h,m]=Me(_);Ie(()=>{if(h==="digital"&&!d){m(u?"analog":"digital");return}h==="analog"&&!u&&d&&m("digital")},[u,d,h]);let s=h==="digital"?f||E:E||f,p=h==="digital"&&d,O=p?"Digital":"Analog",P=p?n?.digital_scheduler_active_system||n?.digital_profile||o.system_name||o.system:n?.profile_airband||"Airband",ve=p?n?.digital_last_label||o.department_name||o.department:n?.last_hit_airband_label||n?.last_hit_ground_label||n?.last_hit||o.department_name||o.department,fe=p?n?.digital_last_tgid??o.tgid??o.talkgroup_id:"--",Se=p?(()=>{let G=Number(n?.digital_preflight?.playlist_frequency_hz?.[0]||n?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(G)&&G>0?(G/1e6).toFixed(4):o.frequency??o.freq})():n?.last_hit_airband||n?.last_hit_ground||n?.last_hit||"--",ge=p?n?.digital_control_channel_locked?"Locked":n?.digital_control_decode_available?"Decoding":o.signal??o.signal_strength:n?.rtl_active?"Active":"Idle",te=l||!p,ye=async()=>{try{await r()}catch{}},be=async()=>{try{await t()}catch{}};return c.createElement("section",{className:"screen main-screen"},c.createElement(N,{title:"Home Patrol 3",subtitle:`Mode: ${e.mode.toUpperCase()}`}),c.createElement("div",{className:"field-grid"},c.createElement("div",{className:"card"},c.createElement("div",{className:"muted"},"System"),c.createElement("div",null,B(P))),c.createElement("div",{className:"card"},c.createElement("div",{className:"muted"},"Department"),c.createElement("div",null,B(ve))),c.createElement("div",{className:"card"},c.createElement("div",{className:"muted"},"TGID"),c.createElement("div",null,B(fe))),c.createElement("div",{className:"card"},c.createElement("div",{className:"muted"},"Frequency"),c.createElement("div",null,B(Se))),c.createElement("div",{className:"card"},c.createElement("div",{className:"muted"},"Signal"),c.createElement("div",null,B(ge)))),c.createElement("div",{className:"button-row"},c.createElement(g,{onClick:ye,disabled:te},"HOLD"),c.createElement(g,{onClick:be,disabled:te},"NEXT"),c.createElement(g,{variant:"secondary",onClick:()=>a(i.MENU),disabled:l},"MENU")),p?null:c.createElement("div",{className:"muted",style:{marginTop:"8px"}},"HOLD/NEXT control digital scanning only. Switch source to Digital to control scan."),c.createElement("div",{className:"card",style:{marginTop:"12px"}},c.createElement("div",{className:"row",style:{marginBottom:"8px"}},c.createElement("div",{className:"muted"},"Audio Source"),s?c.createElement("a",{href:`/stream/${s}`,target:"_blank",rel:"noreferrer"},"Open"):null),c.createElement("div",{className:"button-row",style:{marginTop:0}},c.createElement(g,{variant:h==="analog"?"primary":"secondary",onClick:()=>m("analog"),disabled:!u},"Analog"),c.createElement(g,{variant:h==="digital"?"primary":"secondary",onClick:()=>m("digital"),disabled:!d},"Digital")),c.createElement("div",{className:"muted",style:{marginTop:"8px",marginBottom:"8px"}},"Monitoring ",O," (",s||"no mount",")"),c.createElement("audio",{controls:!0,preload:"none",style:{width:"100%"},src:s?`/stream/${s}`:"/stream/"})),S?c.createElement("div",{className:"error"},S):null,v?c.createElement("div",{className:"message"},v):null)}import U from"https://esm.sh/react@18";var Oe=[{id:i.LOCATION,label:"Location"},{id:i.SERVICE_TYPES,label:"Service Types"},{id:i.RANGE,label:"Range"},{id:i.FAVORITES,label:"Favorites"},{id:i.AVOID,label:"Avoid"},{id:i.MODE_SELECTION,label:"Mode Selection"}];function j(){let{navigate:e,state:r}=b();return U.createElement("section",{className:"screen menu"},U.createElement(N,{title:"Menu",showBack:!0,onBack:()=>e(i.MAIN)}),U.createElement("div",{className:"menu-list"},Oe.map(t=>U.createElement(g,{key:t.id,variant:"secondary",className:"menu-item",onClick:()=>e(t.id),disabled:r.working},t.label))),r.error?U.createElement("div",{className:"error"},r.error):null)}import y,{useEffect as Le,useState as H}from"https://esm.sh/react@18";function ue(e){if(e===""||e===null||e===void 0)return null;let r=Number(e);return Number.isFinite(r)?r:NaN}function q(){let{state:e,saveHpState:r,navigate:t}=b(),{hpState:a,working:o}=e,[n,l]=H(""),[S,v]=H(""),[E,f]=H(""),[u,d]=H(!0),[_,h]=H("");return Le(()=>{l(a.zip||a.postal_code||""),v(a.lat!==void 0&&a.lat!==null?String(a.lat):a.latitude!==void 0&&a.latitude!==null?String(a.latitude):""),f(a.lon!==void 0&&a.lon!==null?String(a.lon):a.longitude!==void 0&&a.longitude!==null?String(a.longitude):""),d(a.use_location!==!1)},[a]),y.createElement("section",{className:"screen location-screen"},y.createElement(N,{title:"Location",showBack:!0,onBack:()=>t(i.MENU)}),y.createElement("div",{className:"list"},y.createElement("label",null,y.createElement("div",{className:"muted"},"ZIP"),y.createElement("input",{className:"input",value:n,onChange:s=>l(s.target.value.trim()),placeholder:"37201"})),y.createElement("label",null,y.createElement("div",{className:"muted"},"Latitude"),y.createElement("input",{className:"input",value:S,onChange:s=>v(s.target.value),placeholder:"36.12"})),y.createElement("label",null,y.createElement("div",{className:"muted"},"Longitude"),y.createElement("input",{className:"input",value:E,onChange:s=>f(s.target.value),placeholder:"-86.67"})),y.createElement("label",{className:"row"},y.createElement("span",null,"Use location for scanning"),y.createElement("input",{type:"checkbox",checked:u,onChange:s=>d(s.target.checked)}))),y.createElement("div",{className:"button-row"},y.createElement(g,{onClick:async()=>{if(h(""),n&&!/^\d{5}(-\d{4})?$/.test(n)){h("ZIP must be 5 digits or ZIP+4.");return}let s=ue(S),p=ue(E);if(Number.isNaN(s)||Number.isNaN(p)){h("Latitude and longitude must be valid numbers.");return}if(s!==null&&(s<-90||s>90)){h("Latitude must be between -90 and 90.");return}if(p!==null&&(p<-180||p>180)){h("Longitude must be between -180 and 180.");return}try{await r({zip:n,lat:s,lon:p,use_location:u}),t(i.MENU)}catch{}},disabled:o},"Save")),_?y.createElement("div",{className:"error"},_):null,e.error?y.createElement("div",{className:"error"},e.error):null)}import C,{useEffect as Be,useMemo as Ue,useState as He}from"https://esm.sh/react@18";function K(){let{state:e,saveHpState:r,navigate:t}=b(),{hpState:a,serviceTypes:o,working:n}=e,l=Ue(()=>o.filter(u=>u.enabled_by_default).map(u=>Number(u.service_tag)),[o]),[S,v]=He([]);Be(()=>{let u=Array.isArray(a.enabled_service_tags)?a.enabled_service_tags.map(Number):l;v(Array.from(new Set(u)).filter(d=>Number.isFinite(d)))},[a.enabled_service_tags,l]);let E=u=>{v(d=>d.includes(u)?d.filter(_=>_!==u):[...d,u])},f=async()=>{try{await r({enabled_service_tags:[...S].sort((u,d)=>u-d)}),t(i.MENU)}catch{}};return C.createElement("section",{className:"screen service-types-screen"},C.createElement(N,{title:"Service Types",showBack:!0,onBack:()=>t(i.MENU)}),C.createElement("div",{className:"checkbox-list"},o.map(u=>{let d=Number(u.service_tag),_=S.includes(d);return C.createElement("label",{key:d,className:"row card"},C.createElement("span",null,u.name),C.createElement("input",{type:"checkbox",checked:_,onChange:()=>E(d)}))})),C.createElement("div",{className:"button-row"},C.createElement(g,{onClick:f,disabled:n},"Save")),e.error?C.createElement("div",{className:"error"},e.error):null)}import w,{useEffect as De,useState as Pe}from"https://esm.sh/react@18";function W(){let{state:e,saveHpState:r,navigate:t}=b(),{hpState:a,working:o}=e,[n,l]=Pe(15);return De(()=>{let v=Number(a.range_miles);l(Number.isFinite(v)?v:15)},[a.range_miles]),w.createElement("section",{className:"screen range-screen"},w.createElement(N,{title:"Range",showBack:!0,onBack:()=>t(i.MENU)}),w.createElement("div",{className:"card"},w.createElement("div",{className:"row"},w.createElement("span",null,"Range Miles"),w.createElement("strong",null,n)),w.createElement("input",{className:"range",type:"range",min:"0",max:"100",step:"1",value:n,onChange:v=>l(Number(v.target.value))})),w.createElement("div",{className:"button-row"},w.createElement(g,{onClick:async()=>{try{await r({range_miles:n}),t(i.MENU)}catch{}},disabled:o},"Save")),e.error?w.createElement("div",{className:"error"},e.error):null)}import k,{useEffect as Ge,useMemo as Fe,useState as Ve}from"https://esm.sh/react@18";function $e(e){return Array.isArray(e)?e.map((r,t)=>r&&typeof r=="object"?{id:r.id??r.name??`fav-${t}`,name:String(r.name||r.label||`Favorite ${t+1}`),enabled:r.enabled!==!1}:{id:`fav-${t}`,name:String(r),enabled:!0}):[]}function Y(){let{state:e,saveHpState:r,navigate:t}=b(),{hpState:a,working:o}=e,n=Fe(()=>Array.isArray(a.favorites)?a.favorites:Array.isArray(a.favorites_list)?a.favorites_list:[],[a.favorites,a.favorites_list]),[l,S]=Ve([]);Ge(()=>{S($e(n))},[n]);let v=f=>{S(u=>u.map(d=>d.id===f?{...d,enabled:!d.enabled}:d))},E=async()=>{try{await r({favorites:l}),t(i.MENU)}catch{}};return k.createElement("section",{className:"screen favorites-screen"},k.createElement(N,{title:"Favorites",showBack:!0,onBack:()=>t(i.MENU)}),l.length===0?k.createElement("div",{className:"muted"},"No favorites in current state."):k.createElement("div",{className:"list"},l.map(f=>k.createElement("label",{key:f.id,className:"row card"},k.createElement("span",null,f.name),k.createElement("input",{type:"checkbox",checked:f.enabled,onChange:()=>v(f.id)})))),k.createElement("div",{className:"button-row"},k.createElement(g,{onClick:E,disabled:o},"Save")),e.error?k.createElement("div",{className:"error"},e.error):null)}import A,{useEffect as ze,useMemo as je,useState as qe}from"https://esm.sh/react@18";function Ke(e){return Array.isArray(e)?e.map((r,t)=>r&&typeof r=="object"?{id:r.id??`${r.type||"item"}-${t}`,label:String(r.label||r.alpha_tag||r.name||`Avoid ${t+1}`),type:String(r.type||"item")}:{id:`item-${t}`,label:String(r),type:"item"}):[]}function Z(){let{state:e,saveHpState:r,avoidCurrent:t,navigate:a}=b(),{hpState:o,working:n}=e,l=je(()=>Array.isArray(o.avoid_list)?o.avoid_list:Array.isArray(o.avoids)?o.avoids:Array.isArray(o.avoid)?o.avoid:[],[o.avoid_list,o.avoids,o.avoid]),[S,v]=qe([]);ze(()=>{v(Ke(l))},[l]);let E=()=>{v([])},f=async(d=S)=>{try{await r({avoid_list:d})}catch{}},u=async()=>{try{await t()}catch{}};return A.createElement("section",{className:"screen avoid-screen"},A.createElement(N,{title:"Avoid",showBack:!0,onBack:()=>a(i.MENU)}),S.length===0?A.createElement("div",{className:"muted"},"No avoided items in current state."):A.createElement("div",{className:"list"},S.map(d=>A.createElement("div",{key:d.id,className:"row card"},A.createElement("div",null,A.createElement("div",null,d.label),A.createElement("div",{className:"muted"},d.type)),A.createElement(g,{variant:"danger",onClick:()=>{let _=S.filter(h=>h.id!==d.id);v(_),f(_)},disabled:n},"Remove")))),A.createElement("div",{className:"button-row"},A.createElement(g,{onClick:u,disabled:n},"Avoid Current"),A.createElement(g,{variant:"secondary",onClick:()=>{E(),f([])},disabled:n},"Clear"),A.createElement(g,{onClick:()=>f(),disabled:n},"Save")),e.error?A.createElement("div",{className:"error"},e.error):null)}import T,{useEffect as We,useState as Ye}from"https://esm.sh/react@18";function J(){let{state:e,setMode:r,navigate:t}=b(),[a,o]=Ye("hp");return We(()=>{o(e.mode||"hp")},[e.mode]),T.createElement("section",{className:"screen mode-selection-screen"},T.createElement(N,{title:"Mode Selection",showBack:!0,onBack:()=>t(i.MENU)}),T.createElement("div",{className:"list"},T.createElement("label",{className:"row card"},T.createElement("span",null,"HP Mode"),T.createElement("input",{type:"radio",name:"scan-mode",value:"hp",checked:a==="hp",onChange:l=>o(l.target.value)})),T.createElement("label",{className:"row card"},T.createElement("span",null,"Expert Mode"),T.createElement("input",{type:"radio",name:"scan-mode",value:"expert",checked:a==="expert",onChange:l=>o(l.target.value)}))),T.createElement("div",{className:"button-row"},T.createElement(g,{onClick:async()=>{try{await r(a),t(i.MENU)}catch{}},disabled:e.working},"Save")),e.error?T.createElement("div",{className:"error"},e.error):null)}import Ze from"https://esm.sh/react@18";function X({label:e="Loading..."}){return Ze.createElement("div",{className:"loading"},e)}function Q(){let{state:e}=b();if(e.loading)return I.createElement(X,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case i.MENU:return I.createElement(j,null);case i.LOCATION:return I.createElement(q,null);case i.SERVICE_TYPES:return I.createElement(K,null);case i.RANGE:return I.createElement(W,null);case i.FAVORITES:return I.createElement(Y,null);case i.AVOID:return I.createElement(Z,null);case i.MODE_SELECTION:return I.createElement(J,null);case i.MAIN:default:return I.createElement(z,null)}}var Je=`
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
`;function R(){return D.createElement(pe,null,D.createElement("div",{className:"app-shell"},D.createElement("style",null,Je),D.createElement(Q,null)))}var me=document.getElementById("root");if(!me)throw new Error("Missing #root mount element");Xe(me).render(ee.createElement(ee.StrictMode,null,ee.createElement(R,null)));
