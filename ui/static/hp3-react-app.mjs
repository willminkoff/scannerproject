import ge from"https://esm.sh/react@18";import{createRoot as Ut}from"https://esm.sh/react-dom@18/client";import W from"https://esm.sh/react@18";import ot,{createContext as it,useCallback as O,useContext as st,useEffect as re,useMemo as lt,useReducer as dt}from"https://esm.sh/react@18";var rt={"Content-Type":"application/json"};async function P(e,{method:t="GET",body:a}={}){let n={method:t,headers:{...rt}};a!==void 0&&(n.body=JSON.stringify(a));let i=await fetch(e,n),o=await i.text(),r={};try{r=o?JSON.parse(o):{}}catch{r={raw:o}}if(!i.ok){let d=r?.error||`Request failed (${i.status})`,l=new Error(d);throw l.status=i.status,l.payload=r,l}return r}function R(){return P("/api/hp/state")}function Te(e){return P("/api/hp/state",{method:"POST",body:e})}function ee(){return P("/api/hp/service-types")}function te(){return P("/api/hp/avoids")}function Ce(){return P("/api/hp/avoids",{method:"POST",body:{action:"clear"}})}function ke(e){return P("/api/hp/avoids",{method:"POST",body:{action:"remove",system:e}})}function ae(){return P("/api/status")}function Ie(e){return P("/api/mode",{method:"POST",body:{mode:e}})}function Oe(e={}){return P("/api/hp/hold",{method:"POST",body:e})}function Me(e={}){return P("/api/hp/next",{method:"POST",body:e})}function Le(e={}){return P("/api/hp/avoid",{method:"POST",body:e})}var c=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),pt={hpState:{},serviceTypes:[],liveStatus:{},hpAvoids:[],currentScreen:c.MAIN,mode:"hp",sseConnected:!1,loading:!0,working:!1,error:"",message:""},ct=["digital_scheduler_active_system","digital_scheduler_active_system_label","digital_scheduler_next_system","digital_scheduler_next_system_label","digital_scheduler_active_department_label","digital_last_label","digital_channel_label","digital_department_label","digital_system_label","digital_last_mode","digital_last_tgid","digital_profile","digital_scan_mode","stream_mount","digital_stream_mount","profile_airband","profile_ground","last_hit_airband_label","last_hit_ground_label"];function He(e){return e==null?!1:typeof e=="string"?e.trim()!=="":Array.isArray(e)?e.length>0:!0}function Y(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach(n=>{let i=String(n||"").trim();!i||a.has(i)||(a.add(i),t.push(i))}),t}function ut(e,t){let a=t&&typeof t=="object"?t:{},n={...e||{},...a};return ct.forEach(i=>{!He(a[i])&&He(e?.[i])&&(n[i]=e[i])}),n}function mt(e,t){switch(t.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:t.payload.hpState||{},serviceTypes:t.payload.serviceTypes||[],liveStatus:t.payload.liveStatus||{},hpAvoids:t.payload.hpAvoids||[],mode:t.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:t.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!t.payload};case"SET_ERROR":return{...e,error:t.payload||""};case"SET_MESSAGE":return{...e,message:t.payload||""};case"SET_HP_STATE":return{...e,hpState:t.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:t.payload||[]};case"SET_HP_AVOIDS":return{...e,hpAvoids:Y(t.payload)};case"SET_LIVE_STATUS":return{...e,liveStatus:ut(e.liveStatus,t.payload),hpAvoids:Array.isArray(t.payload?.hp_avoids)?Y(t.payload.hp_avoids):e.hpAvoids};case"SET_MODE":return{...e,mode:t.payload||e.mode};case"SET_SSE_CONNECTED":return{...e,sseConnected:!!t.payload};case"NAVIGATE":return{...e,currentScreen:t.payload||c.MAIN};default:return e}}var De=it(null);function Be(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(a=>({service_tag:Number(a?.service_tag),name:String(a?.name||`Service ${a?.service_tag}`),enabled_by_default:!!a?.enabled_by_default}))}function Pe(e){let t=e&&typeof e.state=="object"&&e.state!==null?e.state:{},a=String(e?.mode||"hp").toLowerCase();return{hpState:t,mode:a}}function Ue({children:e}){let[t,a]=dt(mt,pt),n=O(u=>{a({type:"NAVIGATE",payload:u})},[]),i=O(async()=>{let u=await R(),m=Pe(u);return a({type:"SET_HP_STATE",payload:m.hpState}),a({type:"SET_MODE",payload:m.mode}),m},[]),o=O(async()=>{let u=await ee(),m=Be(u);return a({type:"SET_SERVICE_TYPES",payload:m}),m},[]),r=O(async()=>{let u=await te(),m=Y(u?.avoids);return a({type:"SET_HP_AVOIDS",payload:m}),m},[]),d=O(async()=>{let u=await ae();return a({type:"SET_LIVE_STATUS",payload:u||{}}),u},[]),l=O(async()=>{a({type:"LOAD_START"});try{let[u,m,x]=await Promise.all([R(),ee(),te()]),B={};try{B=await ae()}catch{B={}}let k=Pe(u),U=Be(m),A=Y(x?.avoids);a({type:"LOAD_SUCCESS",payload:{hpState:k.hpState,mode:k.mode,serviceTypes:U,liveStatus:B,hpAvoids:A}})}catch(u){a({type:"LOAD_ERROR",payload:u.message})}},[]);re(()=>{l()},[l]),re(()=>{let u=setInterval(()=>{d().catch(()=>{})},t.sseConnected?1e4:2500);return()=>clearInterval(u)},[d,t.sseConnected]),re(()=>{if(typeof EventSource>"u")return;let u=!1,m=null,x=null,B=()=>{u||(m=new EventSource("/api/stream"),m.onopen=()=>{a({type:"SET_SSE_CONNECTED",payload:!0})},m.addEventListener("status",k=>{try{let U=JSON.parse(k?.data||"{}");a({type:"SET_LIVE_STATUS",payload:U})}catch{}}),m.onerror=()=>{a({type:"SET_SSE_CONNECTED",payload:!1}),m&&(m.close(),m=null),u||(x=setTimeout(B,2e3))})};return B(),()=>{u=!0,a({type:"SET_SSE_CONNECTED",payload:!1}),x&&clearTimeout(x),m&&m.close()}},[]);let f=O(async u=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let m={...t.hpState,...u},x=await Te(m),B=x?.state&&typeof x.state=="object"?{...t.hpState,...x.state}:m;return a({type:"SET_HP_STATE",payload:B}),a({type:"SET_MESSAGE",payload:"State saved"}),x}catch(m){throw a({type:"SET_ERROR",payload:m.message}),m}finally{a({type:"SET_WORKING",payload:!1})}},[t.hpState]),_=O(async u=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let m=await Ie(u),x=String(m?.mode||u||"hp").toLowerCase();return a({type:"SET_MODE",payload:x}),a({type:"SET_MESSAGE",payload:`Mode set to ${x}`}),m}catch(m){throw a({type:"SET_ERROR",payload:m.message}),m}finally{a({type:"SET_WORKING",payload:!1})}},[]),s=O(async(u,m)=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let x=await u();return Array.isArray(x?.avoids)&&a({type:"SET_HP_AVOIDS",payload:x.avoids}),m&&a({type:"SET_MESSAGE",payload:m}),await i(),await d(),x}catch(x){throw a({type:"SET_ERROR",payload:x.message}),x}finally{a({type:"SET_WORKING",payload:!1})}},[i,d]),v=O(async()=>s(()=>Oe(),"Hold command sent"),[s]),N=O(async()=>s(()=>Me(),"Next command sent"),[s]),T=O(async(u={})=>s(()=>Le(u),"Avoid command sent"),[s]),y=O(async()=>s(()=>Ce(),"Runtime avoids cleared"),[s]),h=O(async u=>s(()=>ke(u),"Avoid removed"),[s]),L=lt(()=>({state:t,dispatch:a,navigate:n,refreshAll:l,refreshHpState:i,refreshServiceTypes:o,refreshHpAvoids:r,refreshStatus:d,saveHpState:f,setMode:_,holdScan:v,nextScan:N,avoidCurrent:T,clearHpAvoids:y,removeHpAvoid:h,SCREENS:c}),[t,n,l,i,o,r,d,f,_,v,N,T,y,h]);return ot.createElement(De.Provider,{value:L},e)}function C(){let e=st(De);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import G from"https://esm.sh/react@18";import p,{useEffect as $e,useMemo as Ge,useState as K}from"https://esm.sh/react@18";import ft from"https://esm.sh/react@18";function E({children:e,onClick:t,type:a="button",variant:n="primary",className:i="",disabled:o=!1}){return ft.createElement("button",{type:a,className:`btn ${n==="secondary"?"btn-secondary":n==="danger"?"btn-danger":""} ${i}`.trim(),onClick:t,disabled:o},e)}function M(e){return e==null||e===""?"--":String(e)}function gt(e){let t=Math.max(0,Math.min(4,Number(e)||0));return`${"|".repeat(t)}${".".repeat(4-t)}`}function vt(e){let t=Number(e);return Number.isFinite(t)?Number.isInteger(t)?`Range ${t}`:`Range ${t.toFixed(1)}`:"Range"}function bt(e,t){let a=t==="ground"?e?.profile_ground:e?.profile_airband,n=t==="ground"?e?.profiles_ground:e?.profiles_airband,i=Array.isArray(n)?n:[],o=String(a||"").trim();if(!o)return"";let r=i.find(l=>String(l?.id||"").trim().toLowerCase()===o.toLowerCase());return String(r?.label||"").trim()||o}function yt(e,t){let a=String(t||"").trim(),n=String(e||"").trim();if(!n&&!a)return{department:"--",channel:"--"};let i=[" | "," - "," / "," \u2014 "," \u2013 ","::"];for(let o of i){if(!n.includes(o))continue;let[r,...d]=n.split(o),l=String(r||"").trim(),f=String(d.join(o)||"").trim();if(l&&f)return{department:l,channel:f}}return a&&n&&a.toLowerCase()!==n.toLowerCase()?{department:a,channel:n}:{department:n||a||"--",channel:n||"--"}}function ne(){let{state:e,holdScan:t,nextScan:a,avoidCurrent:n,navigate:i}=C(),{hpState:o,liveStatus:r,working:d,error:l,message:f}=e,_=String(r?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),s=String(r?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),v=!!_,N=!!s,T=(e.mode==="hp"||e.mode==="expert")&&N?"digital":"analog",[y,h]=K(T),[L,u]=K(""),[m,x]=K(!1),[B,k]=K("");$e(()=>{if(y==="digital"&&!N){h(v?"analog":"digital");return}y==="analog"&&!v&&N&&h("digital")},[v,N,y]),$e(()=>{!l&&!f||k("")},[l,f]);let U=y==="digital"?s||_:_||s,A=y==="digital"&&N,ve=String(o.mode||"full_database").trim().toLowerCase(),be=String(r?.profile_airband||"").trim(),J=bt(r,"airband"),qe=r?.last_hit_airband_label||r?.last_hit_ground_label||r?.last_hit||"",Z=yt(qe,J),ye=A?r?.digital_scheduler_active_system_label||r?.digital_system_label||r?.digital_scheduler_active_system||r?.digital_profile||o.system_name||o.system:be||J||"Airband",q=A?r?.digital_department_label||r?.digital_scheduler_active_department_label||o.department_name||o.department||r?.digital_profile||r?.digital_last_label:Z.department||o.department_name||o.department,he=A?r?.digital_last_tgid??o.tgid??o.talkgroup_id:"--",X=A?(()=>{let g=Number(r?.digital_preflight?.playlist_frequency_hz?.[0]||r?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(g)&&g>0?(g/1e6).toFixed(4):o.frequency??o.freq})():r?.last_hit_airband||r?.last_hit_ground||r?.last_hit||"--",Se=!!(r?.digital_control_channel_metric_ready??r?.digital_control_decode_available),_e=A?r?.digital_control_channel_locked?"Locked":Se?"Decoding":o.signal??o.signal_strength:r?.rtl_active?"Active":"Idle",Ne=A?r?.digital_channel_label||r?.digital_last_label||o.channel_name||o.channel||q:Z.channel||q,Ye=A&&(r?.digital_last_mode||o.service_type||o.service)||"",xe=A?Ne:Z.channel||Ne,Ee=A?[M(Ye||"Digital"),he!=="--"?`TGID ${M(he)}`:"",X!=="--"?`${M(X)} MHz`:"",_e].filter(Boolean).join(" \u2022 "):`${M(X)} \u2022 ${_e}`,Ke=A?r?.digital_control_channel_locked?4:Se?3:1:r?.rtl_active?3:1,Ae=String(r?.digital_scan_mode||"").toLowerCase()==="single_system",We=Ae?"HOLD":"SCAN",Je=Ge(()=>{if(ve!=="favorites")return"Full Database";let g=Array.isArray(o.favorites)?o.favorites:[];if(g.length===0)return"Favorites";let V=g.filter(Q=>!!Q?.enabled);if(V.length===0)return"Favorites";let at=V.find(Q=>{let we=String(Q?.type||"").trim().toLowerCase();return A?we==="digital":we==="analog"})||V[0];return String(at?.label||"").trim()||"Favorites"},[ve,o.favorites,A]),Ze=async()=>{try{await t()}catch{}},Xe=async()=>{try{await a()}catch{}},Qe=async()=>{try{await n()}catch{}},Re=async(g,V)=>{if(g==="info"){k(V==="system"?`System: ${M(ye)}`:V==="department"?`Department: ${M(q)}`:`Channel: ${M(xe)} (${M(Ee)})`),u("");return}if(g==="advanced"){k("Advanced options are still being wired in HP3."),u("");return}if(g==="prev"){k("Previous-channel stepping is not wired yet in HP3."),u("");return}if(g==="fave"){u(""),i(c.FAVORITES);return}if(!A){k("Switch Audio Source to Digital for HOLD/NEXT/AVOID controls."),u("");return}g==="hold"?await Ze():g==="next"?await Xe():g==="avoid"&&await Qe(),u("")},et=Ge(()=>[{id:"squelch",label:"Squelch",onClick:()=>k("Squelch is currently managed from SB3 analog controls.")},{id:"range",label:vt(o.range_miles),onClick:()=>i(c.RANGE)},{id:"atten",label:"Atten",onClick:()=>k("Attenuation toggle is not wired yet in HP3.")},{id:"gps",label:"GPS",onClick:()=>i(c.LOCATION)},{id:"help",label:"Help",onClick:()=>i(c.MENU)}],[o.range_miles,i]),tt={system:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],department:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],channel:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"hold",label:"Hold"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"},{id:"fave",label:"Fave"}]};return p.createElement("section",{className:"screen main-screen hp2-main"},p.createElement("div",{className:"hp2-radio-bar"},p.createElement("div",{className:"hp2-radio-buttons"},et.map(g=>p.createElement("button",{key:g.id,type:"button",className:"hp2-radio-btn",onClick:g.onClick,disabled:d},g.label))),p.createElement("div",{className:"hp2-status-icons"},p.createElement("span",{className:`hp2-icon ${Ae?"on":""}`},We),p.createElement("span",{className:"hp2-icon"},"SIG ",gt(Ke)),p.createElement("span",{className:"hp2-icon"},A?"DIG":"ANA"))),p.createElement("div",{className:"hp2-lines"},p.createElement("div",{className:"hp2-line"},p.createElement("div",{className:"hp2-line-label"},"System / Favorite List"),p.createElement("div",{className:"hp2-line-body"},p.createElement("div",{className:"hp2-line-primary"},M(ye)),p.createElement("div",{className:"hp2-line-secondary"},Je)),p.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>u(g=>g==="system"?"":"system"),disabled:d},"<")),p.createElement("div",{className:"hp2-line"},p.createElement("div",{className:"hp2-line-label"},"Department"),p.createElement("div",{className:"hp2-line-body"},p.createElement("div",{className:"hp2-line-primary"},M(q)),p.createElement("div",{className:"hp2-line-secondary"},A?`Profile: ${M(r?.digital_profile)}`:`Profile: ${M(J||be||"Airband")}`)),p.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>u(g=>g==="department"?"":"department"),disabled:d},"<")),p.createElement("div",{className:"hp2-line channel"},p.createElement("div",{className:"hp2-line-label"},"Channel"),p.createElement("div",{className:"hp2-line-body"},p.createElement("div",{className:"hp2-line-primary"},M(xe)),p.createElement("div",{className:"hp2-line-secondary"},M(Ee))),p.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>u(g=>g==="channel"?"":"channel"),disabled:d},"<"))),L?p.createElement("div",{className:"hp2-submenu-popup"},tt[L]?.map(g=>p.createElement("button",{key:g.id,type:"button",className:"hp2-submenu-btn",onClick:()=>Re(g.id,L),disabled:d},g.label))):null,p.createElement("div",{className:"hp2-feature-bar"},p.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>i(c.MENU),disabled:d},"Menu"),p.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>k("Replay is not wired yet in HP3."),disabled:d},"Replay"),p.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>k("Recording controls are not wired yet in HP3."),disabled:d},"Record"),p.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>x(g=>!g),disabled:d},m?"Unmute":"Mute")),p.createElement("div",{className:"hp2-web-audio"},p.createElement("div",{className:"hp2-audio-head"},p.createElement("div",{className:"muted"},"Web Audio Stream"),U?p.createElement("a",{href:`/stream/${U}`,target:"_blank",rel:"noreferrer"},"Open"):null),p.createElement("div",{className:"hp2-source-switch"},p.createElement(E,{variant:y==="analog"?"primary":"secondary",onClick:()=>h("analog"),disabled:!v||d},"Analog"),p.createElement(E,{variant:y==="digital"?"primary":"secondary",onClick:()=>h("digital"),disabled:!N||d},"Digital")),p.createElement("div",{className:"muted hp2-audio-meta"},"Source: ",A?"Digital":"Analog"," (",U||"no mount",")"),p.createElement("audio",{controls:!0,preload:"none",muted:m,className:"hp2-audio-player",src:U?`/stream/${U}`:"/stream/"})),B?p.createElement("div",{className:"message"},B):null,A?null:p.createElement("div",{className:"muted"},"HOLD/NEXT/AVOID actions require Digital source."),l?p.createElement("div",{className:"error"},l):null,f?p.createElement("div",{className:"message"},f):null)}import z from"https://esm.sh/react@18";import F from"https://esm.sh/react@18";function I({title:e,subtitle:t="",showBack:a=!1,onBack:n}){return F.createElement("div",{className:"header"},F.createElement("div",null,F.createElement("h1",null,e),t?F.createElement("div",{className:"sub"},t):null),a?F.createElement("button",{type:"button",className:"btn btn-secondary",onClick:n},"Back"):null)}var ht=[{id:c.LOCATION,label:"Set Your Location"},{id:c.SERVICE_TYPES,label:"Select Service Types"},{id:c.RANGE,label:"Set Range"},{id:c.FAVORITES,label:"Manage Favorites"},{id:c.AVOID,label:"Avoid Options"},{id:c.MODE_SELECTION,label:"Mode Selection"}];function oe(){let{navigate:e,state:t}=C();return z.createElement("section",{className:"screen menu"},z.createElement(I,{title:"Menu",showBack:!0,onBack:()=>e(c.MAIN)}),z.createElement("div",{className:"menu-list"},ht.map(a=>z.createElement(E,{key:a.id,variant:"secondary",className:"menu-item",onClick:()=>e(a.id),disabled:t.working},a.label))),t.error?z.createElement("div",{className:"error"},t.error):null)}import w,{useEffect as St,useState as j}from"https://esm.sh/react@18";function Ve(e){if(e===""||e===null||e===void 0)return null;let t=Number(e);return Number.isFinite(t)?t:NaN}function ie(){let{state:e,saveHpState:t,navigate:a}=C(),{hpState:n,working:i}=e,[o,r]=j(""),[d,l]=j(""),[f,_]=j(""),[s,v]=j(!0),[N,T]=j("");return St(()=>{r(n.zip||n.postal_code||""),l(n.lat!==void 0&&n.lat!==null?String(n.lat):n.latitude!==void 0&&n.latitude!==null?String(n.latitude):""),_(n.lon!==void 0&&n.lon!==null?String(n.lon):n.longitude!==void 0&&n.longitude!==null?String(n.longitude):""),v(n.use_location!==!1)},[n]),w.createElement("section",{className:"screen location-screen"},w.createElement(I,{title:"Location",showBack:!0,onBack:()=>a(c.MENU)}),w.createElement("div",{className:"list"},w.createElement("label",null,w.createElement("div",{className:"muted"},"ZIP"),w.createElement("input",{className:"input",value:o,onChange:h=>r(h.target.value.trim()),placeholder:"37201"})),w.createElement("label",null,w.createElement("div",{className:"muted"},"Latitude"),w.createElement("input",{className:"input",value:d,onChange:h=>l(h.target.value),placeholder:"36.12"})),w.createElement("label",null,w.createElement("div",{className:"muted"},"Longitude"),w.createElement("input",{className:"input",value:f,onChange:h=>_(h.target.value),placeholder:"-86.67"})),w.createElement("label",{className:"row"},w.createElement("span",null,"Use location for scanning"),w.createElement("input",{type:"checkbox",checked:s,onChange:h=>v(h.target.checked)}))),w.createElement("div",{className:"button-row"},w.createElement(E,{onClick:async()=>{if(T(""),o&&!/^\d{5}(-\d{4})?$/.test(o)){T("ZIP must be 5 digits or ZIP+4.");return}let h=Ve(d),L=Ve(f);if(Number.isNaN(h)||Number.isNaN(L)){T("Latitude and longitude must be valid numbers.");return}if(h!==null&&(h<-90||h>90)){T("Latitude must be between -90 and 90.");return}if(L!==null&&(L<-180||L>180)){T("Longitude must be between -180 and 180.");return}try{await t({zip:o,lat:h,lon:L,use_location:s}),a(c.MENU)}catch{}},disabled:i},"Save")),N?w.createElement("div",{className:"error"},N):null,e.error?w.createElement("div",{className:"error"},e.error):null)}import $,{useEffect as _t,useMemo as Nt,useState as xt}from"https://esm.sh/react@18";function se(){let{state:e,saveHpState:t,navigate:a}=C(),{hpState:n,serviceTypes:i,working:o}=e,r=Nt(()=>i.filter(s=>s.enabled_by_default).map(s=>Number(s.service_tag)),[i]),[d,l]=xt([]);_t(()=>{let s=Array.isArray(n.enabled_service_tags)?n.enabled_service_tags.map(Number):r;l(Array.from(new Set(s)).filter(v=>Number.isFinite(v)))},[n.enabled_service_tags,r]);let f=s=>{l(v=>v.includes(s)?v.filter(N=>N!==s):[...v,s])},_=async()=>{try{await t({enabled_service_tags:[...d].sort((s,v)=>s-v)}),a(c.MENU)}catch{}};return $.createElement("section",{className:"screen service-types-screen"},$.createElement(I,{title:"Service Types",showBack:!0,onBack:()=>a(c.MENU)}),$.createElement("div",{className:"checkbox-list"},i.map(s=>{let v=Number(s.service_tag),N=d.includes(v);return $.createElement("label",{key:v,className:"row card"},$.createElement("span",null,s.name),$.createElement("input",{type:"checkbox",checked:N,onChange:()=>f(v)}))})),$.createElement("div",{className:"button-row"},$.createElement(E,{onClick:_,disabled:o},"Save")),e.error?$.createElement("div",{className:"error"},e.error):null)}import D,{useEffect as Et,useState as At}from"https://esm.sh/react@18";function le(){let{state:e,saveHpState:t,navigate:a}=C(),{hpState:n,working:i}=e,[o,r]=At(15);Et(()=>{let l=Number(n.range_miles);r(Number.isFinite(l)?l:15)},[n.range_miles]);let d=async()=>{try{await t({range_miles:o}),a(c.MENU)}catch{}};return D.createElement("section",{className:"screen range-screen"},D.createElement(I,{title:"Range",showBack:!0,onBack:()=>a(c.MENU)}),D.createElement("div",{className:"card"},D.createElement("div",{className:"row"},D.createElement("span",null,"Range Miles"),D.createElement("strong",null,o.toFixed(1))),D.createElement("input",{className:"range",type:"range",min:"0",max:"30",step:"0.5",value:o,onChange:l=>r(Number(l.target.value))})),D.createElement("div",{className:"button-row"},D.createElement(E,{onClick:d,disabled:i},"Save")),e.error?D.createElement("div",{className:"error"},e.error):null)}import b,{useEffect as wt,useMemo as Fe,useState as Tt}from"https://esm.sh/react@18";function Ct(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach((n,i)=>{if(!n||typeof n!="object")return;let o=String(n.id||"").trim(),r=o?o.split(":"):[],d=String(n.type||n.kind||"").trim().toLowerCase(),l=String(n.target||"").trim().toLowerCase(),f=String(n.profile_id||n.profileId||n.profile||"").trim();if(!f&&r.length>0&&(r[0].toLowerCase()==="digital"&&r.length>=2?(d="digital",f=r.slice(1).join(":").trim()):r[0].toLowerCase()==="analog"&&r.length>=3&&(d="analog",l=String(r[1]||"").trim().toLowerCase(),f=r.slice(2).join(":").trim())),!d&&l&&(d="analog"),d==="digital"&&(l=""),d!=="digital"&&d!=="analog"||d==="analog"&&l!=="airband"&&l!=="ground"||!f)return;let _=d==="digital"?`digital:${f}`:`analog:${l}:${f}`;a.has(_)||(a.add(_),t.push({id:_,type:d,target:l,profile_id:f,label:String(n.label||n.name||f),enabled:n.enabled===!0,_index:i}))}),t}function kt(e){return{analog_airband:e.filter(t=>t.type==="analog"&&t.target==="airband").sort((t,a)=>t._index-a._index),analog_ground:e.filter(t=>t.type==="analog"&&t.target==="ground").sort((t,a)=>t._index-a._index),digital:e.filter(t=>t.type==="digital").sort((t,a)=>t._index-a._index)}}function de(){let{state:e,saveHpState:t,navigate:a}=C(),{hpState:n,working:i}=e,o=Fe(()=>Array.isArray(n.favorites)?n.favorites:Array.isArray(n.favorites_list)?n.favorites_list:[],[n.favorites,n.favorites_list]),[r,d]=Tt([]),l=Fe(()=>kt(r),[r]);wt(()=>{d(Ct(o))},[o]);let f=(s,v)=>{d(N=>N.map(T=>(T.type==="digital"?"digital":`analog_${T.target}`)!==s?T:{...T,enabled:T.profile_id===v}))},_=async()=>{try{await t({favorites:r}),a(c.MENU)}catch{}};return b.createElement("section",{className:"screen favorites-screen"},b.createElement(I,{title:"Favorites",showBack:!0,onBack:()=>a(c.MENU)}),r.length===0?b.createElement("div",{className:"muted"},"No favorites in current state."):b.createElement("div",{className:"list"},b.createElement("div",{className:"card"},b.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Analog Airband"),l.analog_airband.length===0?b.createElement("div",{className:"muted"},"No airband profiles found."):l.analog_airband.map(s=>b.createElement("label",{key:s.id,className:"row",style:{marginBottom:"6px"}},b.createElement("span",null,s.label),b.createElement("input",{type:"radio",name:"favorites-analog-airband",checked:s.enabled,onChange:()=>f("analog_airband",s.profile_id)})))),b.createElement("div",{className:"card"},b.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Analog Ground"),l.analog_ground.length===0?b.createElement("div",{className:"muted"},"No ground profiles found."):l.analog_ground.map(s=>b.createElement("label",{key:s.id,className:"row",style:{marginBottom:"6px"}},b.createElement("span",null,s.label),b.createElement("input",{type:"radio",name:"favorites-analog-ground",checked:s.enabled,onChange:()=>f("analog_ground",s.profile_id)})))),b.createElement("div",{className:"card"},b.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Digital"),l.digital.length===0?b.createElement("div",{className:"muted"},"No digital profiles found."):l.digital.map(s=>b.createElement("label",{key:s.id,className:"row",style:{marginBottom:"6px"}},b.createElement("span",null,s.label),b.createElement("input",{type:"radio",name:"favorites-digital",checked:s.enabled,onChange:()=>f("digital",s.profile_id)}))))),b.createElement("div",{className:"muted",style:{marginTop:"8px"}},"Saving favorites sets the active analog/digital profiles for HP3 playback."),b.createElement("div",{className:"button-row"},b.createElement(E,{onClick:_,disabled:i},"Save")),e.error?b.createElement("div",{className:"error"},e.error):null)}import S,{useEffect as It,useMemo as ze,useState as Ot}from"https://esm.sh/react@18";function Mt(e){return Array.isArray(e)?e.map((t,a)=>t&&typeof t=="object"?{id:String(t.id??`${t.type||"item"}-${a}`),label:String(t.label||t.alpha_tag||t.name||`Avoid ${a+1}`),type:String(t.type||"item"),source:"persistent"}:{id:`item-${a}`,label:String(t),type:"item",source:"persistent"}):[]}function Lt(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach(n=>{let i=String(n||"").trim();!i||a.has(i)||(a.add(i),t.push({id:`runtime:${i}`,label:i,type:"system",token:i,source:"runtime"}))}),t}function pe(){let{state:e,saveHpState:t,avoidCurrent:a,clearHpAvoids:n,removeHpAvoid:i,navigate:o}=C(),{hpState:r,hpAvoids:d,working:l}=e,f=ze(()=>Array.isArray(r.avoid_list)?r.avoid_list:Array.isArray(r.avoids)?r.avoids:Array.isArray(r.avoid)?r.avoid:[],[r.avoid_list,r.avoids,r.avoid]),[_,s]=Ot([]);It(()=>{s(Mt(f))},[f]);let v=ze(()=>Lt(d),[d]),N=async(y=_)=>{try{await t({avoid_list:y})}catch{}},T=async()=>{try{await a()}catch{}};return S.createElement("section",{className:"screen avoid-screen"},S.createElement(I,{title:"Avoid",showBack:!0,onBack:()=>o(c.MENU)}),S.createElement("div",{className:"list"},S.createElement("div",{className:"card"},S.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Runtime Avoids (HP Scan Pool)"),v.length===0?S.createElement("div",{className:"muted"},"No runtime HP avoids."):v.map(y=>S.createElement("div",{key:y.id,className:"row",style:{marginBottom:"6px"}},S.createElement("div",null,S.createElement("div",null,y.label),S.createElement("div",{className:"muted"},y.type)),S.createElement(E,{variant:"danger",onClick:()=>i(y.token),disabled:l},"Remove")))),S.createElement("div",{className:"card"},S.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Persistent Avoids (State)"),_.length===0?S.createElement("div",{className:"muted"},"No persistent avoids in current state."):_.map(y=>S.createElement("div",{key:y.id,className:"row",style:{marginBottom:"6px"}},S.createElement("div",null,S.createElement("div",null,y.label),S.createElement("div",{className:"muted"},y.type)),S.createElement(E,{variant:"danger",onClick:()=>{let h=_.filter(L=>L.id!==y.id);s(h),N(h)},disabled:l},"Remove"))))),S.createElement("div",{className:"button-row"},S.createElement(E,{onClick:T,disabled:l},"Avoid Current"),S.createElement(E,{variant:"secondary",onClick:async()=>{s([]),await N([]),await n()},disabled:l},"Clear All"),S.createElement(E,{onClick:()=>N(),disabled:l},"Save")),e.error?S.createElement("div",{className:"error"},e.error):null)}import H,{useEffect as Ht,useState as Bt}from"https://esm.sh/react@18";function ce(){let{state:e,setMode:t,navigate:a}=C(),[n,i]=Bt("hp");return Ht(()=>{i(e.mode||"hp")},[e.mode]),H.createElement("section",{className:"screen mode-selection-screen"},H.createElement(I,{title:"Mode Selection",showBack:!0,onBack:()=>a(c.MENU)}),H.createElement("div",{className:"list"},H.createElement("label",{className:"row card"},H.createElement("span",null,"HP Mode"),H.createElement("input",{type:"radio",name:"scan-mode",value:"hp",checked:n==="hp",onChange:r=>i(r.target.value)})),H.createElement("label",{className:"row card"},H.createElement("span",null,"Expert Mode"),H.createElement("input",{type:"radio",name:"scan-mode",value:"expert",checked:n==="expert",onChange:r=>i(r.target.value)}))),H.createElement("div",{className:"button-row"},H.createElement(E,{onClick:async()=>{try{await t(n),a(c.MENU)}catch{}},disabled:e.working},"Save")),e.error?H.createElement("div",{className:"error"},e.error):null)}import Pt from"https://esm.sh/react@18";function ue({label:e="Loading..."}){return Pt.createElement("div",{className:"loading"},e)}function me(){let{state:e}=C();if(e.loading)return G.createElement(ue,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case c.MENU:return G.createElement(oe,null);case c.LOCATION:return G.createElement(ie,null);case c.SERVICE_TYPES:return G.createElement(se,null);case c.RANGE:return G.createElement(le,null);case c.FAVORITES:return G.createElement(de,null);case c.AVOID:return G.createElement(pe,null);case c.MODE_SELECTION:return G.createElement(ce,null);case c.MAIN:default:return G.createElement(ne,null)}}var Dt=`
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
`;function fe(){return W.createElement(Ue,null,W.createElement("div",{className:"app-shell"},W.createElement("style",null,Dt),W.createElement(me,null)))}var je=document.getElementById("root");if(!je)throw new Error("Missing #root mount element");Ut(je).render(ge.createElement(ge.StrictMode,null,ge.createElement(fe,null)));
