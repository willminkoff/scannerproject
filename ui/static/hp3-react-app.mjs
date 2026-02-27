import oe from"https://esm.sh/react@18";import{createRoot as ba}from"https://esm.sh/react-dom@18/client";import j from"https://esm.sh/react@18";import Ge,{createContext as ze,useCallback as M,useContext as Fe,useEffect as Se,useMemo as Ve,useReducer as je}from"https://esm.sh/react@18";var De={"Content-Type":"application/json"};async function U(e,{method:a="GET",body:t}={}){let n={method:a,headers:{...De}};t!==void 0&&(n.body=JSON.stringify(t));let l=await fetch(e,n),o=await l.text(),r={};try{r=o?JSON.parse(o):{}}catch{r={raw:o}}if(!l.ok){let s=r?.error||`Request failed (${l.status})`,u=new Error(s);throw u.status=l.status,u.payload=r,u}return r}function q(){return U("/api/hp/state")}function fe(e){return U("/api/hp/state",{method:"POST",body:e})}function W(){return U("/api/hp/service-types")}function Y(){return U("/api/status")}function be(e){return U("/api/mode",{method:"POST",body:{mode:e}})}function ve(e={}){return U("/api/hp/hold",{method:"POST",body:e})}function he(e={}){return U("/api/hp/next",{method:"POST",body:e})}function ye(e={}){return U("/api/hp/avoid",{method:"POST",body:e})}var p=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),qe={hpState:{},serviceTypes:[],liveStatus:{},currentScreen:p.MAIN,mode:"hp",loading:!0,working:!1,error:"",message:""};function We(e,a){switch(a.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:a.payload.hpState||{},serviceTypes:a.payload.serviceTypes||[],liveStatus:a.payload.liveStatus||{},mode:a.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:a.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!a.payload};case"SET_ERROR":return{...e,error:a.payload||""};case"SET_MESSAGE":return{...e,message:a.payload||""};case"SET_HP_STATE":return{...e,hpState:a.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:a.payload||[]};case"SET_LIVE_STATUS":return{...e,liveStatus:a.payload||{}};case"SET_MODE":return{...e,mode:a.payload||e.mode};case"NAVIGATE":return{...e,currentScreen:a.payload||p.MAIN};default:return e}}var _e=ze(null);function Ne(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(t=>({service_tag:Number(t?.service_tag),name:String(t?.name||`Service ${t?.service_tag}`),enabled_by_default:!!t?.enabled_by_default}))}function xe(e){let a=e&&typeof e.state=="object"&&e.state!==null?e.state:{},t=String(e?.mode||"hp").toLowerCase();return{hpState:a,mode:t}}function Ee({children:e}){let[a,t]=je(We,qe),n=M(m=>{t({type:"NAVIGATE",payload:m})},[]),l=M(async()=>{let m=await q(),c=xe(m);return t({type:"SET_HP_STATE",payload:c.hpState}),t({type:"SET_MODE",payload:c.mode}),c},[]),o=M(async()=>{let m=await W(),c=Ne(m);return t({type:"SET_SERVICE_TYPES",payload:c}),c},[]),r=M(async()=>{let m=await Y();return t({type:"SET_LIVE_STATUS",payload:m||{}}),m},[]),s=M(async()=>{t({type:"LOAD_START"});try{let[m,c]=await Promise.all([q(),W()]),v={};try{v=await Y()}catch{v={}}let w=xe(m),z=Ne(c);t({type:"LOAD_SUCCESS",payload:{hpState:w.hpState,mode:w.mode,serviceTypes:z,liveStatus:v}})}catch(m){t({type:"LOAD_ERROR",payload:m.message})}},[]);Se(()=>{s()},[s]),Se(()=>{let m=setInterval(()=>{r().catch(()=>{})},2500);return()=>clearInterval(m)},[r]);let u=M(async m=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let c={...a.hpState,...m},v=await fe(c),w=v?.state&&typeof v.state=="object"?{...a.hpState,...v.state}:c;return t({type:"SET_HP_STATE",payload:w}),t({type:"SET_MESSAGE",payload:"State saved"}),v}catch(c){throw t({type:"SET_ERROR",payload:c.message}),c}finally{t({type:"SET_WORKING",payload:!1})}},[a.hpState]),f=M(async m=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let c=await be(m),v=String(c?.mode||m||"hp").toLowerCase();return t({type:"SET_MODE",payload:v}),t({type:"SET_MESSAGE",payload:`Mode set to ${v}`}),c}catch(c){throw t({type:"SET_ERROR",payload:c.message}),c}finally{t({type:"SET_WORKING",payload:!1})}},[]),y=M(async(m,c)=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let v=await m();return c&&t({type:"SET_MESSAGE",payload:c}),await l(),await r(),v}catch(v){throw t({type:"SET_ERROR",payload:v.message}),v}finally{t({type:"SET_WORKING",payload:!1})}},[l,r]),d=M(async()=>y(()=>ve(),"Hold command sent"),[y]),g=M(async()=>y(()=>he(),"Next command sent"),[y]),S=M(async(m={})=>y(()=>ye(m),"Avoid command sent"),[y]),E=Ve(()=>({state:a,dispatch:t,navigate:n,refreshAll:s,refreshHpState:l,refreshServiceTypes:o,refreshStatus:r,saveHpState:u,setMode:f,holdScan:d,nextScan:g,avoidCurrent:S,SCREENS:p}),[a,n,s,l,o,r,u,f,d,g,S]);return Ge.createElement(_e.Provider,{value:E},e)}function _(){let e=Fe(_e);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import H from"https://esm.sh/react@18";import i,{useEffect as Ae,useMemo as Ke,useState as V}from"https://esm.sh/react@18";import Ye from"https://esm.sh/react@18";function N({children:e,onClick:a,type:t="button",variant:n="primary",className:l="",disabled:o=!1}){return Ye.createElement("button",{type:t,className:`btn ${n==="secondary"?"btn-secondary":n==="danger"?"btn-danger":""} ${l}`.trim(),onClick:a,disabled:o},e)}function O(e){return e==null||e===""?"--":String(e)}function Ze(e){let a=Math.max(0,Math.min(4,Number(e)||0));return`${"|".repeat(a)}${".".repeat(4-a)}`}function Je(e){let a=Number(e);return Number.isFinite(a)?Number.isInteger(a)?`Range ${a}`:`Range ${a.toFixed(1)}`:"Range"}function K(){let{state:e,holdScan:a,nextScan:t,avoidCurrent:n,navigate:l}=_(),{hpState:o,liveStatus:r,working:s,error:u,message:f}=e,y=String(r?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),d=String(r?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),g=!!y,S=!!d,E=(e.mode==="hp"||e.mode==="expert")&&S?"digital":"analog",[m,c]=V(E),[v,w]=V(""),[z,Te]=V(!1),[ie,I]=V("");Ae(()=>{if(m==="digital"&&!S){c(g?"analog":"digital");return}m==="analog"&&!g&&S&&c("digital")},[g,S,m]),Ae(()=>{!u&&!f||I("")},[u,f]);let P=m==="digital"?d||y:y||d,C=m==="digital"&&S,se=C?r?.digital_scheduler_active_system||r?.digital_profile||o.system_name||o.system:r?.profile_airband||"Airband",F=C?r?.digital_last_label||o.department_name||o.department:r?.last_hit_airband_label||r?.last_hit_ground_label||r?.last_hit||o.department_name||o.department,le=C?r?.digital_last_tgid??o.tgid??o.talkgroup_id:"--",de=C?(()=>{let b=Number(r?.digital_preflight?.playlist_frequency_hz?.[0]||r?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(b)&&b>0?(b/1e6).toFixed(4):o.frequency??o.freq})():r?.last_hit_airband||r?.last_hit_ground||r?.last_hit||"--",pe=C?r?.digital_control_channel_locked?"Locked":r?.digital_control_decode_available?"Decoding":o.signal??o.signal_strength:r?.rtl_active?"Active":"Idle",ce=C&&le!=="--"?`TGID ${le}`:F,ue=C?`${O(de)} MHz \u2022 ${pe}`:`${O(de)} \u2022 ${pe}`,Ie=C?r?.digital_control_channel_locked?4:r?.digital_control_decode_available?3:1:r?.rtl_active?3:1,me=String(r?.digital_scan_mode||"").toLowerCase()==="single_system",Me=me?"HOLD":"SCAN",Oe=async()=>{try{await a()}catch{}},Le=async()=>{try{await t()}catch{}},Be=async()=>{try{await n()}catch{}},He=async(b,ge)=>{if(b==="info"){I(ge==="system"?`System: ${O(se)}`:ge==="department"?`Department: ${O(F)}`:`Channel: ${O(ce)} (${O(ue)})`),w("");return}if(b==="advanced"){I("Advanced options are still being wired in HP3."),w("");return}if(b==="prev"){I("Previous-channel stepping is not wired yet in HP3."),w("");return}if(b==="fave"){w(""),l(p.FAVORITES);return}if(!C){I("Switch Audio Source to Digital for HOLD/NEXT/AVOID controls."),w("");return}b==="hold"?await Oe():b==="next"?await Le():b==="avoid"&&await Be(),w("")},Ue=Ke(()=>[{id:"squelch",label:"Squelch",onClick:()=>I("Squelch is currently managed from SB3 analog controls.")},{id:"range",label:Je(o.range_miles),onClick:()=>l(p.RANGE)},{id:"atten",label:"Atten",onClick:()=>I("Attenuation toggle is not wired yet in HP3.")},{id:"gps",label:"GPS",onClick:()=>l(p.LOCATION)},{id:"help",label:"Help",onClick:()=>l(p.MENU)}],[o.range_miles,l]),Pe={system:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],department:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],channel:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"hold",label:"Hold"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"},{id:"fave",label:"Fave"}]};return i.createElement("section",{className:"screen main-screen hp2-main"},i.createElement("div",{className:"hp2-radio-bar"},i.createElement("div",{className:"hp2-radio-buttons"},Ue.map(b=>i.createElement("button",{key:b.id,type:"button",className:"hp2-radio-btn",onClick:b.onClick,disabled:s},b.label))),i.createElement("div",{className:"hp2-status-icons"},i.createElement("span",{className:`hp2-icon ${me?"on":""}`},Me),i.createElement("span",{className:"hp2-icon"},"SIG ",Ze(Ie)),i.createElement("span",{className:"hp2-icon"},C?"DIG":"ANA"))),i.createElement("div",{className:"hp2-lines"},i.createElement("div",{className:"hp2-line"},i.createElement("div",{className:"hp2-line-label"},"System / Favorite List"),i.createElement("div",{className:"hp2-line-body"},i.createElement("div",{className:"hp2-line-primary"},O(se)),i.createElement("div",{className:"hp2-line-secondary"},"Mode: ",e.mode.toUpperCase())),i.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>w(b=>b==="system"?"":"system"),disabled:s},"<")),i.createElement("div",{className:"hp2-line"},i.createElement("div",{className:"hp2-line-label"},"Department"),i.createElement("div",{className:"hp2-line-body"},i.createElement("div",{className:"hp2-line-primary"},O(F)),i.createElement("div",{className:"hp2-line-secondary"},"Service: ",O(o.mode))),i.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>w(b=>b==="department"?"":"department"),disabled:s},"<")),i.createElement("div",{className:"hp2-line channel"},i.createElement("div",{className:"hp2-line-label"},"Channel"),i.createElement("div",{className:"hp2-line-body"},i.createElement("div",{className:"hp2-line-primary"},O(ce)),i.createElement("div",{className:"hp2-line-secondary"},O(ue))),i.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>w(b=>b==="channel"?"":"channel"),disabled:s},"<"))),v?i.createElement("div",{className:"hp2-submenu-popup"},Pe[v]?.map(b=>i.createElement("button",{key:b.id,type:"button",className:"hp2-submenu-btn",onClick:()=>He(b.id,v),disabled:s},b.label))):null,i.createElement("div",{className:"hp2-feature-bar"},i.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>l(p.MENU),disabled:s},"Menu"),i.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>I("Replay is not wired yet in HP3."),disabled:s},"Replay"),i.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>I("Recording controls are not wired yet in HP3."),disabled:s},"Record"),i.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>Te(b=>!b),disabled:s},z?"Unmute":"Mute")),i.createElement("div",{className:"hp2-web-audio"},i.createElement("div",{className:"hp2-audio-head"},i.createElement("div",{className:"muted"},"Web Audio Stream"),P?i.createElement("a",{href:`/stream/${P}`,target:"_blank",rel:"noreferrer"},"Open"):null),i.createElement("div",{className:"hp2-source-switch"},i.createElement(N,{variant:m==="analog"?"primary":"secondary",onClick:()=>c("analog"),disabled:!g||s},"Analog"),i.createElement(N,{variant:m==="digital"?"primary":"secondary",onClick:()=>c("digital"),disabled:!S||s},"Digital")),i.createElement("div",{className:"muted hp2-audio-meta"},"Source: ",C?"Digital":"Analog"," (",P||"no mount",")"),i.createElement("audio",{controls:!0,preload:"none",muted:z,className:"hp2-audio-player",src:P?`/stream/${P}`:"/stream/"})),ie?i.createElement("div",{className:"message"},ie):null,C?null:i.createElement("div",{className:"muted"},"HOLD/NEXT/AVOID actions require Digital source."),u?i.createElement("div",{className:"error"},u):null,f?i.createElement("div",{className:"message"},f):null)}import $ from"https://esm.sh/react@18";import D from"https://esm.sh/react@18";function A({title:e,subtitle:a="",showBack:t=!1,onBack:n}){return D.createElement("div",{className:"header"},D.createElement("div",null,D.createElement("h1",null,e),a?D.createElement("div",{className:"sub"},a):null),t?D.createElement("button",{type:"button",className:"btn btn-secondary",onClick:n},"Back"):null)}var Xe=[{id:p.LOCATION,label:"Set Your Location"},{id:p.SERVICE_TYPES,label:"Select Service Types"},{id:p.RANGE,label:"Set Range"},{id:p.FAVORITES,label:"Manage Favorites"},{id:p.AVOID,label:"Avoid Options"},{id:p.MODE_SELECTION,label:"Mode Selection"}];function Z(){let{navigate:e,state:a}=_();return $.createElement("section",{className:"screen menu"},$.createElement(A,{title:"Menu",showBack:!0,onBack:()=>e(p.MAIN)}),$.createElement("div",{className:"menu-list"},Xe.map(t=>$.createElement(N,{key:t.id,variant:"secondary",className:"menu-item",onClick:()=>e(t.id),disabled:a.working},t.label))),a.error?$.createElement("div",{className:"error"},a.error):null)}import x,{useEffect as Qe,useState as G}from"https://esm.sh/react@18";function we(e){if(e===""||e===null||e===void 0)return null;let a=Number(e);return Number.isFinite(a)?a:NaN}function J(){let{state:e,saveHpState:a,navigate:t}=_(),{hpState:n,working:l}=e,[o,r]=G(""),[s,u]=G(""),[f,y]=G(""),[d,g]=G(!0),[S,E]=G("");return Qe(()=>{r(n.zip||n.postal_code||""),u(n.lat!==void 0&&n.lat!==null?String(n.lat):n.latitude!==void 0&&n.latitude!==null?String(n.latitude):""),y(n.lon!==void 0&&n.lon!==null?String(n.lon):n.longitude!==void 0&&n.longitude!==null?String(n.longitude):""),g(n.use_location!==!1)},[n]),x.createElement("section",{className:"screen location-screen"},x.createElement(A,{title:"Location",showBack:!0,onBack:()=>t(p.MENU)}),x.createElement("div",{className:"list"},x.createElement("label",null,x.createElement("div",{className:"muted"},"ZIP"),x.createElement("input",{className:"input",value:o,onChange:c=>r(c.target.value.trim()),placeholder:"37201"})),x.createElement("label",null,x.createElement("div",{className:"muted"},"Latitude"),x.createElement("input",{className:"input",value:s,onChange:c=>u(c.target.value),placeholder:"36.12"})),x.createElement("label",null,x.createElement("div",{className:"muted"},"Longitude"),x.createElement("input",{className:"input",value:f,onChange:c=>y(c.target.value),placeholder:"-86.67"})),x.createElement("label",{className:"row"},x.createElement("span",null,"Use location for scanning"),x.createElement("input",{type:"checkbox",checked:d,onChange:c=>g(c.target.checked)}))),x.createElement("div",{className:"button-row"},x.createElement(N,{onClick:async()=>{if(E(""),o&&!/^\d{5}(-\d{4})?$/.test(o)){E("ZIP must be 5 digits or ZIP+4.");return}let c=we(s),v=we(f);if(Number.isNaN(c)||Number.isNaN(v)){E("Latitude and longitude must be valid numbers.");return}if(c!==null&&(c<-90||c>90)){E("Latitude must be between -90 and 90.");return}if(v!==null&&(v<-180||v>180)){E("Longitude must be between -180 and 180.");return}try{await a({zip:o,lat:c,lon:v,use_location:d}),t(p.MENU)}catch{}},disabled:l},"Save")),S?x.createElement("div",{className:"error"},S):null,e.error?x.createElement("div",{className:"error"},e.error):null)}import B,{useEffect as Re,useMemo as ea,useState as aa}from"https://esm.sh/react@18";function X(){let{state:e,saveHpState:a,navigate:t}=_(),{hpState:n,serviceTypes:l,working:o}=e,r=ea(()=>l.filter(d=>d.enabled_by_default).map(d=>Number(d.service_tag)),[l]),[s,u]=aa([]);Re(()=>{let d=Array.isArray(n.enabled_service_tags)?n.enabled_service_tags.map(Number):r;u(Array.from(new Set(d)).filter(g=>Number.isFinite(g)))},[n.enabled_service_tags,r]);let f=d=>{u(g=>g.includes(d)?g.filter(S=>S!==d):[...g,d])},y=async()=>{try{await a({enabled_service_tags:[...s].sort((d,g)=>d-g)}),t(p.MENU)}catch{}};return B.createElement("section",{className:"screen service-types-screen"},B.createElement(A,{title:"Service Types",showBack:!0,onBack:()=>t(p.MENU)}),B.createElement("div",{className:"checkbox-list"},l.map(d=>{let g=Number(d.service_tag),S=s.includes(g);return B.createElement("label",{key:g,className:"row card"},B.createElement("span",null,d.name),B.createElement("input",{type:"checkbox",checked:S,onChange:()=>f(g)}))})),B.createElement("div",{className:"button-row"},B.createElement(N,{onClick:y,disabled:o},"Save")),e.error?B.createElement("div",{className:"error"},e.error):null)}import L,{useEffect as ta,useState as ra}from"https://esm.sh/react@18";function Q(){let{state:e,saveHpState:a,navigate:t}=_(),{hpState:n,working:l}=e,[o,r]=ra(15);ta(()=>{let u=Number(n.range_miles);r(Number.isFinite(u)?u:15)},[n.range_miles]);let s=async()=>{try{await a({range_miles:o}),t(p.MENU)}catch{}};return L.createElement("section",{className:"screen range-screen"},L.createElement(A,{title:"Range",showBack:!0,onBack:()=>t(p.MENU)}),L.createElement("div",{className:"card"},L.createElement("div",{className:"row"},L.createElement("span",null,"Range Miles"),L.createElement("strong",null,o.toFixed(1))),L.createElement("input",{className:"range",type:"range",min:"0",max:"30",step:"0.5",value:o,onChange:u=>r(Number(u.target.value))})),L.createElement("div",{className:"button-row"},L.createElement(N,{onClick:s,disabled:l},"Save")),e.error?L.createElement("div",{className:"error"},e.error):null)}import h,{useEffect as na,useMemo as ke,useState as oa}from"https://esm.sh/react@18";function ia(e){if(!Array.isArray(e))return[];let a=[],t=new Set;return e.forEach((n,l)=>{if(!n||typeof n!="object")return;let o=String(n.id||"").trim(),r=o?o.split(":"):[],s=String(n.type||n.kind||"").trim().toLowerCase(),u=String(n.target||"").trim().toLowerCase(),f=String(n.profile_id||n.profileId||n.profile||"").trim();if(!f&&r.length>0&&(r[0].toLowerCase()==="digital"&&r.length>=2?(s="digital",f=r.slice(1).join(":").trim()):r[0].toLowerCase()==="analog"&&r.length>=3&&(s="analog",u=String(r[1]||"").trim().toLowerCase(),f=r.slice(2).join(":").trim())),!s&&u&&(s="analog"),s==="digital"&&(u=""),s!=="digital"&&s!=="analog"||s==="analog"&&u!=="airband"&&u!=="ground"||!f)return;let y=s==="digital"?`digital:${f}`:`analog:${u}:${f}`;t.has(y)||(t.add(y),a.push({id:y,type:s,target:u,profile_id:f,label:String(n.label||n.name||f),enabled:n.enabled===!0,_index:l}))}),a}function sa(e){return{analog_airband:e.filter(a=>a.type==="analog"&&a.target==="airband").sort((a,t)=>a._index-t._index),analog_ground:e.filter(a=>a.type==="analog"&&a.target==="ground").sort((a,t)=>a._index-t._index),digital:e.filter(a=>a.type==="digital").sort((a,t)=>a._index-t._index)}}function R(){let{state:e,saveHpState:a,navigate:t}=_(),{hpState:n,working:l}=e,o=ke(()=>Array.isArray(n.favorites)?n.favorites:Array.isArray(n.favorites_list)?n.favorites_list:[],[n.favorites,n.favorites_list]),[r,s]=oa([]),u=ke(()=>sa(r),[r]);na(()=>{s(ia(o))},[o]);let f=(d,g)=>{s(S=>S.map(E=>(E.type==="digital"?"digital":`analog_${E.target}`)!==d?E:{...E,enabled:E.profile_id===g}))},y=async()=>{try{await a({favorites:r}),t(p.MENU)}catch{}};return h.createElement("section",{className:"screen favorites-screen"},h.createElement(A,{title:"Favorites",showBack:!0,onBack:()=>t(p.MENU)}),r.length===0?h.createElement("div",{className:"muted"},"No favorites in current state."):h.createElement("div",{className:"list"},h.createElement("div",{className:"card"},h.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Analog Airband"),u.analog_airband.length===0?h.createElement("div",{className:"muted"},"No airband profiles found."):u.analog_airband.map(d=>h.createElement("label",{key:d.id,className:"row",style:{marginBottom:"6px"}},h.createElement("span",null,d.label),h.createElement("input",{type:"radio",name:"favorites-analog-airband",checked:d.enabled,onChange:()=>f("analog_airband",d.profile_id)})))),h.createElement("div",{className:"card"},h.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Analog Ground"),u.analog_ground.length===0?h.createElement("div",{className:"muted"},"No ground profiles found."):u.analog_ground.map(d=>h.createElement("label",{key:d.id,className:"row",style:{marginBottom:"6px"}},h.createElement("span",null,d.label),h.createElement("input",{type:"radio",name:"favorites-analog-ground",checked:d.enabled,onChange:()=>f("analog_ground",d.profile_id)})))),h.createElement("div",{className:"card"},h.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Digital"),u.digital.length===0?h.createElement("div",{className:"muted"},"No digital profiles found."):u.digital.map(d=>h.createElement("label",{key:d.id,className:"row",style:{marginBottom:"6px"}},h.createElement("span",null,d.label),h.createElement("input",{type:"radio",name:"favorites-digital",checked:d.enabled,onChange:()=>f("digital",d.profile_id)}))))),h.createElement("div",{className:"muted",style:{marginTop:"8px"}},"Saving favorites sets the active analog/digital profiles for HP3 playback."),h.createElement("div",{className:"button-row"},h.createElement(N,{onClick:y,disabled:l},"Save")),e.error?h.createElement("div",{className:"error"},e.error):null)}import k,{useEffect as la,useMemo as da,useState as pa}from"https://esm.sh/react@18";function ca(e){return Array.isArray(e)?e.map((a,t)=>a&&typeof a=="object"?{id:a.id??`${a.type||"item"}-${t}`,label:String(a.label||a.alpha_tag||a.name||`Avoid ${t+1}`),type:String(a.type||"item")}:{id:`item-${t}`,label:String(a),type:"item"}):[]}function ee(){let{state:e,saveHpState:a,avoidCurrent:t,navigate:n}=_(),{hpState:l,working:o}=e,r=da(()=>Array.isArray(l.avoid_list)?l.avoid_list:Array.isArray(l.avoids)?l.avoids:Array.isArray(l.avoid)?l.avoid:[],[l.avoid_list,l.avoids,l.avoid]),[s,u]=pa([]);la(()=>{u(ca(r))},[r]);let f=()=>{u([])},y=async(g=s)=>{try{await a({avoid_list:g})}catch{}},d=async()=>{try{await t()}catch{}};return k.createElement("section",{className:"screen avoid-screen"},k.createElement(A,{title:"Avoid",showBack:!0,onBack:()=>n(p.MENU)}),s.length===0?k.createElement("div",{className:"muted"},"No avoided items in current state."):k.createElement("div",{className:"list"},s.map(g=>k.createElement("div",{key:g.id,className:"row card"},k.createElement("div",null,k.createElement("div",null,g.label),k.createElement("div",{className:"muted"},g.type)),k.createElement(N,{variant:"danger",onClick:()=>{let S=s.filter(E=>E.id!==g.id);u(S),y(S)},disabled:o},"Remove")))),k.createElement("div",{className:"button-row"},k.createElement(N,{onClick:d,disabled:o},"Avoid Current"),k.createElement(N,{variant:"secondary",onClick:()=>{f(),y([])},disabled:o},"Clear"),k.createElement(N,{onClick:()=>y(),disabled:o},"Save")),e.error?k.createElement("div",{className:"error"},e.error):null)}import T,{useEffect as ua,useState as ma}from"https://esm.sh/react@18";function ae(){let{state:e,setMode:a,navigate:t}=_(),[n,l]=ma("hp");return ua(()=>{l(e.mode||"hp")},[e.mode]),T.createElement("section",{className:"screen mode-selection-screen"},T.createElement(A,{title:"Mode Selection",showBack:!0,onBack:()=>t(p.MENU)}),T.createElement("div",{className:"list"},T.createElement("label",{className:"row card"},T.createElement("span",null,"HP Mode"),T.createElement("input",{type:"radio",name:"scan-mode",value:"hp",checked:n==="hp",onChange:r=>l(r.target.value)})),T.createElement("label",{className:"row card"},T.createElement("span",null,"Expert Mode"),T.createElement("input",{type:"radio",name:"scan-mode",value:"expert",checked:n==="expert",onChange:r=>l(r.target.value)}))),T.createElement("div",{className:"button-row"},T.createElement(N,{onClick:async()=>{try{await a(n),t(p.MENU)}catch{}},disabled:e.working},"Save")),e.error?T.createElement("div",{className:"error"},e.error):null)}import ga from"https://esm.sh/react@18";function te({label:e="Loading..."}){return ga.createElement("div",{className:"loading"},e)}function re(){let{state:e}=_();if(e.loading)return H.createElement(te,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case p.MENU:return H.createElement(Z,null);case p.LOCATION:return H.createElement(J,null);case p.SERVICE_TYPES:return H.createElement(X,null);case p.RANGE:return H.createElement(Q,null);case p.FAVORITES:return H.createElement(R,null);case p.AVOID:return H.createElement(ee,null);case p.MODE_SELECTION:return H.createElement(ae,null);case p.MAIN:default:return H.createElement(K,null)}}var fa=`
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: Tahoma, Verdana, sans-serif;
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

  .hp2-main {
    padding: 0;
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid #4b535f;
    background: linear-gradient(180deg, #0f1218 0%, #0a0e14 100%);
    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.45);
  }
  .hp2-radio-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 10px;
    border-bottom: 1px solid #303844;
    background: linear-gradient(180deg, #232d3b 0%, #1b2430 100%);
  }
  .hp2-radio-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .hp2-radio-btn {
    border: 1px solid #556175;
    background: #2d394b;
    color: #dbe6f5;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .hp2-radio-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .hp2-status-icons {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .hp2-icon {
    border: 1px solid #5a687b;
    border-radius: 4px;
    padding: 2px 5px;
    font-size: 0.68rem;
    color: #d7e2f2;
    background: #1a2431;
    min-width: 42px;
    text-align: center;
  }
  .hp2-icon.on {
    color: #ffe39a;
    border-color: #d0a34c;
    background: #3a2f1e;
  }
  .hp2-lines {
    padding: 8px;
  }
  .hp2-line {
    display: grid;
    grid-template-columns: 136px 1fr 26px;
    border: 1px solid #344050;
    border-radius: 5px;
    overflow: hidden;
    background: #121821;
    margin-bottom: 6px;
  }
  .hp2-line:last-child {
    margin-bottom: 0;
  }
  .hp2-line-label {
    padding: 8px 7px;
    border-right: 1px solid #344050;
    color: #b7c9df;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    display: flex;
    align-items: center;
    background: #18222f;
  }
  .hp2-line-body {
    padding: 7px 10px;
    min-height: 52px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 3px;
  }
  .hp2-line-primary {
    font-size: 1.02rem;
    color: #ffb54a;
    line-height: 1.15;
  }
  .hp2-line-secondary {
    color: #9fb0c7;
    font-size: 0.78rem;
    line-height: 1.2;
  }
  .hp2-line.channel .hp2-line-primary {
    color: #ffe169;
  }
  .hp2-subtab {
    border: 0;
    border-left: 1px dashed #3a4656;
    background: #111821;
    color: #d2dced;
    font-size: 0.95rem;
    font-weight: bold;
    cursor: pointer;
  }
  .hp2-subtab:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .hp2-submenu-popup {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 8px 10px;
    background: #131a24;
    border-top: 1px dashed #394657;
    border-bottom: 1px dashed #394657;
  }
  .hp2-submenu-btn {
    border: 1px solid #54637a;
    border-radius: 6px;
    background: #222e3f;
    color: #dbe7f8;
    padding: 5px 10px;
    font-size: 0.82rem;
    cursor: pointer;
  }
  .hp2-submenu-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .hp2-feature-bar {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 1px;
    background: #2a3342;
    border-top: 1px solid #3a4452;
  }
  .hp2-feature-btn {
    border: 0;
    background: #1b2431;
    color: #d7e2f5;
    font-size: 0.83rem;
    padding: 10px 6px;
    cursor: pointer;
  }
  .hp2-feature-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .hp2-web-audio {
    border-top: 1px solid #303a46;
    padding: 10px;
    background: #0d131c;
  }
  .hp2-audio-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
  }
  .hp2-source-switch {
    display: flex;
    gap: 8px;
    margin-bottom: 6px;
  }
  .hp2-audio-meta {
    margin: 6px 0 8px;
  }
  .hp2-audio-player {
    width: 100%;
  }
  @media (max-width: 520px) {
    .hp2-line {
      grid-template-columns: 118px 1fr 24px;
    }
    .hp2-line-primary {
      font-size: 0.95rem;
    }
    .hp2-radio-btn {
      font-size: 0.72rem;
      padding: 2px 5px;
    }
  }
`;function ne(){return j.createElement(Ee,null,j.createElement("div",{className:"app-shell"},j.createElement("style",null,fa),j.createElement(re,null)))}var Ce=document.getElementById("root");if(!Ce)throw new Error("Missing #root mount element");ba(Ce).render(oe.createElement(oe.StrictMode,null,oe.createElement(ne,null)));
