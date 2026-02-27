import J from"https://esm.sh/react@18";import{createRoot as De}from"https://esm.sh/react-dom@18/client";import H from"https://esm.sh/react@18";import ce,{createContext as pe,useCallback as T,useContext as ue,useEffect as me,useMemo as ve,useReducer as fe}from"https://esm.sh/react@18";var de={"Content-Type":"application/json"};async function I(e,{method:t="GET",body:r}={}){let a={method:t,headers:{...de}};r!==void 0&&(a.body=JSON.stringify(r));let o=await fetch(e,a),i=await o.text(),s={};try{s=i?JSON.parse(i):{}}catch{s={raw:i}}if(!o.ok){let f=s?.error||`Request failed (${o.status})`,u=new Error(f);throw u.status=o.status,u.payload=s,u}return s}function P(){return I("/api/hp/state")}function X(e){return I("/api/hp/state",{method:"POST",body:e})}function D(){return I("/api/hp/service-types")}function Q(e){return I("/api/mode",{method:"POST",body:{mode:e}})}function R(e={}){return I("/api/hp/hold",{method:"POST",body:e})}function ee(e={}){return I("/api/hp/next",{method:"POST",body:e})}function re(e={}){return I("/api/hp/avoid",{method:"POST",body:e})}var n=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),Se={hpState:{},serviceTypes:[],currentScreen:n.MAIN,mode:"hp",loading:!0,working:!1,error:"",message:""};function ye(e,t){switch(t.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:t.payload.hpState||{},serviceTypes:t.payload.serviceTypes||[],mode:t.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:t.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!t.payload};case"SET_ERROR":return{...e,error:t.payload||""};case"SET_MESSAGE":return{...e,message:t.payload||""};case"SET_HP_STATE":return{...e,hpState:t.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:t.payload||[]};case"SET_MODE":return{...e,mode:t.payload||e.mode};case"NAVIGATE":return{...e,currentScreen:t.payload||n.MAIN};default:return e}}var oe=pe(null);function te(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(r=>({service_tag:Number(r?.service_tag),name:String(r?.name||`Service ${r?.service_tag}`),enabled_by_default:!!r?.enabled_by_default}))}function ae(e){let t=e&&typeof e.state=="object"&&e.state!==null?e.state:{},r=String(e?.mode||"hp").toLowerCase();return{hpState:t,mode:r}}function ne({children:e}){let[t,r]=fe(ye,Se),a=T(c=>{r({type:"NAVIGATE",payload:c})},[]),o=T(async()=>{let c=await P(),m=ae(c);return r({type:"SET_HP_STATE",payload:m.hpState}),r({type:"SET_MODE",payload:m.mode}),m},[]),i=T(async()=>{let c=await D(),m=te(c);return r({type:"SET_SERVICE_TYPES",payload:m}),m},[]),s=T(async()=>{r({type:"LOAD_START"});try{let[c,m]=await Promise.all([P(),D()]),l=ae(c),C=te(m);r({type:"LOAD_SUCCESS",payload:{hpState:l.hpState,mode:l.mode,serviceTypes:C}})}catch(c){r({type:"LOAD_ERROR",payload:c.message})}},[]);me(()=>{s()},[s]);let f=T(async c=>{r({type:"SET_WORKING",payload:!0}),r({type:"SET_ERROR",payload:""});try{let m={...t.hpState,...c},l=await X(m),C=l?.state&&typeof l.state=="object"?{...t.hpState,...l.state}:m;return r({type:"SET_HP_STATE",payload:C}),r({type:"SET_MESSAGE",payload:"State saved"}),l}catch(m){throw r({type:"SET_ERROR",payload:m.message}),m}finally{r({type:"SET_WORKING",payload:!1})}},[t.hpState]),u=T(async c=>{r({type:"SET_WORKING",payload:!0}),r({type:"SET_ERROR",payload:""});try{let m=await Q(c),l=String(m?.mode||c||"hp").toLowerCase();return r({type:"SET_MODE",payload:l}),r({type:"SET_MESSAGE",payload:`Mode set to ${l}`}),m}catch(m){throw r({type:"SET_ERROR",payload:m.message}),m}finally{r({type:"SET_WORKING",payload:!1})}},[]),E=T(async(c,m)=>{r({type:"SET_WORKING",payload:!0}),r({type:"SET_ERROR",payload:""});try{let l=await c();return m&&r({type:"SET_MESSAGE",payload:m}),await o(),l}catch(l){throw r({type:"SET_ERROR",payload:l.message}),l}finally{r({type:"SET_WORKING",payload:!1})}},[o]),y=T(async()=>E(()=>R(),"Hold command sent"),[E]),p=T(async()=>E(()=>ee(),"Next command sent"),[E]),d=T(async(c={})=>E(()=>re(c),"Avoid command sent"),[E]),A=ve(()=>({state:t,dispatch:r,navigate:a,refreshAll:s,refreshHpState:o,refreshServiceTypes:i,saveHpState:f,setMode:u,holdScan:y,nextScan:p,avoidCurrent:d,SCREENS:n}),[t,a,s,o,i,f,u,y,p,d]);return ce.createElement(oe.Provider,{value:A},e)}function b(){let e=ue(oe);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import M from"https://esm.sh/react@18";import v from"https://esm.sh/react@18";import ge from"https://esm.sh/react@18";function S({children:e,onClick:t,type:r="button",variant:a="primary",className:o="",disabled:i=!1}){return ge.createElement("button",{type:r,className:`btn ${a==="secondary"?"btn-secondary":a==="danger"?"btn-danger":""} ${o}`.trim(),onClick:t,disabled:i},e)}import O from"https://esm.sh/react@18";function N({title:e,subtitle:t="",showBack:r=!1,onBack:a}){return O.createElement("div",{className:"header"},O.createElement("div",null,O.createElement("h1",null,e),t?O.createElement("div",{className:"sub"},t):null),r?O.createElement("button",{type:"button",className:"btn btn-secondary",onClick:a},"Back"):null)}function L(e){return e==null||e===""?"--":String(e)}function G(){let{state:e,holdScan:t,nextScan:r,navigate:a}=b(),{hpState:o,working:i,error:s,message:f}=e,u=o.system_name||o.system,E=o.department_name||o.department,y=o.tgid??o.talkgroup_id,p=o.frequency??o.freq,d=o.signal??o.signal_strength,A=async()=>{try{await t()}catch{}},c=async()=>{try{await r()}catch{}};return v.createElement("section",{className:"screen main-screen"},v.createElement(N,{title:"Home Patrol 3",subtitle:`Mode: ${e.mode.toUpperCase()}`}),v.createElement("div",{className:"field-grid"},v.createElement("div",{className:"card"},v.createElement("div",{className:"muted"},"System"),v.createElement("div",null,L(u))),v.createElement("div",{className:"card"},v.createElement("div",{className:"muted"},"Department"),v.createElement("div",null,L(E))),v.createElement("div",{className:"card"},v.createElement("div",{className:"muted"},"TGID"),v.createElement("div",null,L(y))),v.createElement("div",{className:"card"},v.createElement("div",{className:"muted"},"Frequency"),v.createElement("div",null,L(p))),v.createElement("div",{className:"card"},v.createElement("div",{className:"muted"},"Signal"),v.createElement("div",null,L(d)))),v.createElement("div",{className:"button-row"},v.createElement(S,{onClick:A,disabled:i},"HOLD"),v.createElement(S,{onClick:c,disabled:i},"NEXT"),v.createElement(S,{variant:"secondary",onClick:()=>a(n.MENU),disabled:i},"MENU")),s?v.createElement("div",{className:"error"},s):null,f?v.createElement("div",{className:"message"},f):null)}import B from"https://esm.sh/react@18";var be=[{id:n.LOCATION,label:"Location"},{id:n.SERVICE_TYPES,label:"Service Types"},{id:n.RANGE,label:"Range"},{id:n.FAVORITES,label:"Favorites"},{id:n.AVOID,label:"Avoid"},{id:n.MODE_SELECTION,label:"Mode Selection"}];function F(){let{navigate:e,state:t}=b();return B.createElement("section",{className:"screen menu"},B.createElement(N,{title:"Menu",showBack:!0,onBack:()=>e(n.MAIN)}),B.createElement("div",{className:"menu-list"},be.map(r=>B.createElement(S,{key:r.id,variant:"secondary",className:"menu-item",onClick:()=>e(r.id),disabled:t.working},r.label))),t.error?B.createElement("div",{className:"error"},t.error):null)}import g,{useEffect as Ee,useState as U}from"https://esm.sh/react@18";function se(e){if(e===""||e===null||e===void 0)return null;let t=Number(e);return Number.isFinite(t)?t:NaN}function V(){let{state:e,saveHpState:t,navigate:r}=b(),{hpState:a,working:o}=e,[i,s]=U(""),[f,u]=U(""),[E,y]=U(""),[p,d]=U(!0),[A,c]=U("");return Ee(()=>{s(a.zip||a.postal_code||""),u(a.lat!==void 0&&a.lat!==null?String(a.lat):a.latitude!==void 0&&a.latitude!==null?String(a.latitude):""),y(a.lon!==void 0&&a.lon!==null?String(a.lon):a.longitude!==void 0&&a.longitude!==null?String(a.longitude):""),d(a.use_location!==!1)},[a]),g.createElement("section",{className:"screen location-screen"},g.createElement(N,{title:"Location",showBack:!0,onBack:()=>r(n.MENU)}),g.createElement("div",{className:"list"},g.createElement("label",null,g.createElement("div",{className:"muted"},"ZIP"),g.createElement("input",{className:"input",value:i,onChange:l=>s(l.target.value.trim()),placeholder:"37201"})),g.createElement("label",null,g.createElement("div",{className:"muted"},"Latitude"),g.createElement("input",{className:"input",value:f,onChange:l=>u(l.target.value),placeholder:"36.12"})),g.createElement("label",null,g.createElement("div",{className:"muted"},"Longitude"),g.createElement("input",{className:"input",value:E,onChange:l=>y(l.target.value),placeholder:"-86.67"})),g.createElement("label",{className:"row"},g.createElement("span",null,"Use location for scanning"),g.createElement("input",{type:"checkbox",checked:p,onChange:l=>d(l.target.checked)}))),g.createElement("div",{className:"button-row"},g.createElement(S,{onClick:async()=>{if(c(""),i&&!/^\d{5}(-\d{4})?$/.test(i)){c("ZIP must be 5 digits or ZIP+4.");return}let l=se(f),C=se(E);if(Number.isNaN(l)||Number.isNaN(C)){c("Latitude and longitude must be valid numbers.");return}if(l!==null&&(l<-90||l>90)){c("Latitude must be between -90 and 90.");return}if(C!==null&&(C<-180||C>180)){c("Longitude must be between -180 and 180.");return}try{await t({zip:i,lat:l,lon:C,use_location:p}),r(n.MENU)}catch{}},disabled:o},"Save")),A?g.createElement("div",{className:"error"},A):null,e.error?g.createElement("div",{className:"error"},e.error):null)}import k,{useEffect as Ne,useMemo as he,useState as Ae}from"https://esm.sh/react@18";function $(){let{state:e,saveHpState:t,navigate:r}=b(),{hpState:a,serviceTypes:o,working:i}=e,s=he(()=>o.filter(p=>p.enabled_by_default).map(p=>Number(p.service_tag)),[o]),[f,u]=Ae([]);Ne(()=>{let p=Array.isArray(a.enabled_service_tags)?a.enabled_service_tags.map(Number):s;u(Array.from(new Set(p)).filter(d=>Number.isFinite(d)))},[a.enabled_service_tags,s]);let E=p=>{u(d=>d.includes(p)?d.filter(A=>A!==p):[...d,p])},y=async()=>{try{await t({enabled_service_tags:[...f].sort((p,d)=>p-d)}),r(n.MENU)}catch{}};return k.createElement("section",{className:"screen service-types-screen"},k.createElement(N,{title:"Service Types",showBack:!0,onBack:()=>r(n.MENU)}),k.createElement("div",{className:"checkbox-list"},o.map(p=>{let d=Number(p.service_tag),A=f.includes(d);return k.createElement("label",{key:d,className:"row card"},k.createElement("span",null,p.name),k.createElement("input",{type:"checkbox",checked:A,onChange:()=>E(d)}))})),k.createElement("div",{className:"button-row"},k.createElement(S,{onClick:y,disabled:i},"Save")),e.error?k.createElement("div",{className:"error"},e.error):null)}import x,{useEffect as _e,useState as Te}from"https://esm.sh/react@18";function z(){let{state:e,saveHpState:t,navigate:r}=b(),{hpState:a,working:o}=e,[i,s]=Te(15);return _e(()=>{let u=Number(a.range_miles);s(Number.isFinite(u)?u:15)},[a.range_miles]),x.createElement("section",{className:"screen range-screen"},x.createElement(N,{title:"Range",showBack:!0,onBack:()=>r(n.MENU)}),x.createElement("div",{className:"card"},x.createElement("div",{className:"row"},x.createElement("span",null,"Range Miles"),x.createElement("strong",null,i)),x.createElement("input",{className:"range",type:"range",min:"0",max:"100",step:"1",value:i,onChange:u=>s(Number(u.target.value))})),x.createElement("div",{className:"button-row"},x.createElement(S,{onClick:async()=>{try{await t({range_miles:i}),r(n.MENU)}catch{}},disabled:o},"Save")),e.error?x.createElement("div",{className:"error"},e.error):null)}import w,{useEffect as xe,useMemo as we,useState as Ce}from"https://esm.sh/react@18";function ke(e){return Array.isArray(e)?e.map((t,r)=>t&&typeof t=="object"?{id:t.id??t.name??`fav-${r}`,name:String(t.name||t.label||`Favorite ${r+1}`),enabled:t.enabled!==!1}:{id:`fav-${r}`,name:String(t),enabled:!0}):[]}function j(){let{state:e,saveHpState:t,navigate:r}=b(),{hpState:a,working:o}=e,i=we(()=>Array.isArray(a.favorites)?a.favorites:Array.isArray(a.favorites_list)?a.favorites_list:[],[a.favorites,a.favorites_list]),[s,f]=Ce([]);xe(()=>{f(ke(i))},[i]);let u=y=>{f(p=>p.map(d=>d.id===y?{...d,enabled:!d.enabled}:d))},E=async()=>{try{await t({favorites:s}),r(n.MENU)}catch{}};return w.createElement("section",{className:"screen favorites-screen"},w.createElement(N,{title:"Favorites",showBack:!0,onBack:()=>r(n.MENU)}),s.length===0?w.createElement("div",{className:"muted"},"No favorites in current state."):w.createElement("div",{className:"list"},s.map(y=>w.createElement("label",{key:y.id,className:"row card"},w.createElement("span",null,y.name),w.createElement("input",{type:"checkbox",checked:y.enabled,onChange:()=>u(y.id)})))),w.createElement("div",{className:"button-row"},w.createElement(S,{onClick:E,disabled:o},"Save")),e.error?w.createElement("div",{className:"error"},e.error):null)}import h,{useEffect as Me,useMemo as Ie,useState as Oe}from"https://esm.sh/react@18";function Le(e){return Array.isArray(e)?e.map((t,r)=>t&&typeof t=="object"?{id:t.id??`${t.type||"item"}-${r}`,label:String(t.label||t.alpha_tag||t.name||`Avoid ${r+1}`),type:String(t.type||"item")}:{id:`item-${r}`,label:String(t),type:"item"}):[]}function K(){let{state:e,saveHpState:t,avoidCurrent:r,navigate:a}=b(),{hpState:o,working:i}=e,s=Ie(()=>Array.isArray(o.avoid_list)?o.avoid_list:Array.isArray(o.avoids)?o.avoids:Array.isArray(o.avoid)?o.avoid:[],[o.avoid_list,o.avoids,o.avoid]),[f,u]=Oe([]);Me(()=>{u(Le(s))},[s]);let E=()=>{u([])},y=async(d=f)=>{try{await t({avoid_list:d})}catch{}},p=async()=>{try{await r()}catch{}};return h.createElement("section",{className:"screen avoid-screen"},h.createElement(N,{title:"Avoid",showBack:!0,onBack:()=>a(n.MENU)}),f.length===0?h.createElement("div",{className:"muted"},"No avoided items in current state."):h.createElement("div",{className:"list"},f.map(d=>h.createElement("div",{key:d.id,className:"row card"},h.createElement("div",null,h.createElement("div",null,d.label),h.createElement("div",{className:"muted"},d.type)),h.createElement(S,{variant:"danger",onClick:()=>{let A=f.filter(c=>c.id!==d.id);u(A),y(A)},disabled:i},"Remove")))),h.createElement("div",{className:"button-row"},h.createElement(S,{onClick:p,disabled:i},"Avoid Current"),h.createElement(S,{variant:"secondary",onClick:()=>{E(),y([])},disabled:i},"Clear"),h.createElement(S,{onClick:()=>y(),disabled:i},"Save")),e.error?h.createElement("div",{className:"error"},e.error):null)}import _,{useEffect as Be,useState as Ue}from"https://esm.sh/react@18";function W(){let{state:e,setMode:t,navigate:r}=b(),[a,o]=Ue("hp");return Be(()=>{o(e.mode||"hp")},[e.mode]),_.createElement("section",{className:"screen mode-selection-screen"},_.createElement(N,{title:"Mode Selection",showBack:!0,onBack:()=>r(n.MENU)}),_.createElement("div",{className:"list"},_.createElement("label",{className:"row card"},_.createElement("span",null,"HP Mode"),_.createElement("input",{type:"radio",name:"scan-mode",value:"hp",checked:a==="hp",onChange:s=>o(s.target.value)})),_.createElement("label",{className:"row card"},_.createElement("span",null,"Expert Mode"),_.createElement("input",{type:"radio",name:"scan-mode",value:"expert",checked:a==="expert",onChange:s=>o(s.target.value)}))),_.createElement("div",{className:"button-row"},_.createElement(S,{onClick:async()=>{try{await t(a),r(n.MENU)}catch{}},disabled:e.working},"Save")),e.error?_.createElement("div",{className:"error"},e.error):null)}import He from"https://esm.sh/react@18";function q({label:e="Loading..."}){return He.createElement("div",{className:"loading"},e)}function Y(){let{state:e}=b();if(e.loading)return M.createElement(q,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case n.MENU:return M.createElement(F,null);case n.LOCATION:return M.createElement(V,null);case n.SERVICE_TYPES:return M.createElement($,null);case n.RANGE:return M.createElement(z,null);case n.FAVORITES:return M.createElement(j,null);case n.AVOID:return M.createElement(K,null);case n.MODE_SELECTION:return M.createElement(W,null);case n.MAIN:default:return M.createElement(G,null)}}var Pe=`
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
`;function Z(){return H.createElement(ne,null,H.createElement("div",{className:"app-shell"},H.createElement("style",null,Pe),H.createElement(Y,null)))}var ie=document.getElementById("root");if(!ie)throw new Error("Missing #root mount element");De(ie).render(J.createElement(J.StrictMode,null,J.createElement(Z,null)));
