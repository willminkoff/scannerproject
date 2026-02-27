import be from"https://esm.sh/react@18";import{createRoot as Vt}from"https://esm.sh/react-dom@18/client";import J from"https://esm.sh/react@18";import lt,{createContext as dt,useCallback as L,useContext as pt,useEffect as ne,useMemo as ct,useRef as ut,useReducer as mt}from"https://esm.sh/react@18";var it={"Content-Type":"application/json"};async function B(e,{method:t="GET",body:a}={}){let n={method:t,headers:{...it}};a!==void 0&&(n.body=JSON.stringify(a));let i=await fetch(e,n),o=await i.text(),r={};try{r=o?JSON.parse(o):{}}catch{r={raw:o}}if(!i.ok){let p=r?.error||`Request failed (${i.status})`,s=new Error(p);throw s.status=i.status,s.payload=r,s}return r}function te(){return B("/api/hp/state")}function Te(e){return B("/api/hp/state",{method:"POST",body:e})}function ae(){return B("/api/hp/service-types")}function re(){return B("/api/hp/avoids")}function ke(){return B("/api/hp/avoids",{method:"POST",body:{action:"clear"}})}function Ie(e){return B("/api/hp/avoids",{method:"POST",body:{action:"remove",system:e}})}function Oe(){return B("/api/status")}function Le(e){return B("/api/mode",{method:"POST",body:{mode:e}})}function Me(e={}){return B("/api/hp/hold",{method:"POST",body:e})}function He(e={}){return B("/api/hp/next",{method:"POST",body:e})}function Pe(e={}){return B("/api/hp/avoid",{method:"POST",body:e})}var c=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),ft={hpState:{},serviceTypes:[],liveStatus:{},hpAvoids:[],currentScreen:c.MAIN,mode:"hp",sseConnected:!1,loading:!0,working:!1,error:"",message:""},gt=["digital_scheduler_active_system","digital_scheduler_active_system_label","digital_scheduler_next_system","digital_scheduler_next_system_label","digital_scheduler_active_department_label","digital_last_label","digital_channel_label","digital_department_label","digital_system_label","digital_last_mode","digital_last_tgid","digital_profile","digital_scan_mode","stream_mount","digital_stream_mount","profile_airband","profile_ground","last_hit_airband_label","last_hit_ground_label"];function Be(e){return e==null?!1:typeof e=="string"?e.trim()!=="":Array.isArray(e)?e.length>0:!0}function K(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach(n=>{let i=String(n||"").trim();!i||a.has(i)||(a.add(i),t.push(i))}),t}function vt(e,t){let a=t&&typeof t=="object"?t:{},n={...e||{},...a};return gt.forEach(i=>{!Be(a[i])&&Be(e?.[i])&&(n[i]=e[i])}),n}function bt(e,t){switch(t.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:t.payload.hpState||{},serviceTypes:t.payload.serviceTypes||[],liveStatus:t.payload.liveStatus||{},hpAvoids:t.payload.hpAvoids||[],mode:t.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:t.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!t.payload};case"SET_ERROR":return{...e,error:t.payload||""};case"SET_MESSAGE":return{...e,message:t.payload||""};case"SET_HP_STATE":return{...e,hpState:t.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:t.payload||[]};case"SET_HP_AVOIDS":return{...e,hpAvoids:K(t.payload)};case"SET_LIVE_STATUS":return{...e,liveStatus:vt(e.liveStatus,t.payload),hpAvoids:Array.isArray(t.payload?.hp_avoids)?K(t.payload.hp_avoids):e.hpAvoids};case"SET_MODE":return{...e,mode:t.payload||e.mode};case"SET_SSE_CONNECTED":return{...e,sseConnected:!!t.payload};case"NAVIGATE":return{...e,currentScreen:t.payload||c.MAIN};default:return e}}var $e=dt(null);function De(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(a=>({service_tag:Number(a?.service_tag),name:String(a?.name||`Service ${a?.service_tag}`),enabled_by_default:!!a?.enabled_by_default}))}function Ue(e){let t=e&&typeof e.state=="object"&&e.state!==null?e.state:{},a=String(e?.mode||"hp").toLowerCase();return{hpState:t,mode:a}}function Fe({children:e}){let[t,a]=mt(bt,ft),n=ut(!1),i=L(f=>{a({type:"NAVIGATE",payload:f})},[]),o=L(async()=>{let f=await te(),u=Ue(f);return a({type:"SET_HP_STATE",payload:u.hpState}),a({type:"SET_MODE",payload:u.mode}),u},[]),r=L(async()=>{let f=await ae(),u=De(f);return a({type:"SET_SERVICE_TYPES",payload:u}),u},[]),p=L(async()=>{let f=await re(),u=K(f?.avoids);return a({type:"SET_HP_AVOIDS",payload:u}),u},[]),s=L(async()=>{if(n.current)return null;n.current=!0;try{let f=await Oe();return a({type:"SET_LIVE_STATUS",payload:f||{}}),f}finally{n.current=!1}},[]),g=L(async()=>{a({type:"LOAD_START"});try{let[f,u,_]=await Promise.all([te(),ae(),re()]),w=Ue(f),$=De(u),A=K(_?.avoids);a({type:"LOAD_SUCCESS",payload:{hpState:w.hpState,mode:w.mode,serviceTypes:$,liveStatus:{},hpAvoids:A}})}catch(f){a({type:"LOAD_ERROR",payload:f.message})}},[]);ne(()=>{g()},[g]),ne(()=>{let f=setInterval(()=>{s().catch(()=>{})},t.sseConnected?1e4:5e3);return()=>clearInterval(f)},[s,t.sseConnected]),ne(()=>{if(typeof EventSource>"u")return;let f=!1,u=null,_=null,w=()=>{f||(u=new EventSource("/api/stream"),u.onopen=()=>{a({type:"SET_SSE_CONNECTED",payload:!0})},u.addEventListener("status",$=>{try{let A=JSON.parse($?.data||"{}");a({type:"SET_LIVE_STATUS",payload:A})}catch{}}),u.onerror=()=>{a({type:"SET_SSE_CONNECTED",payload:!1}),u&&(u.close(),u=null),f||(_=setTimeout(w,2e3))})};return w(),()=>{f=!0,a({type:"SET_SSE_CONNECTED",payload:!1}),_&&clearTimeout(_),u&&u.close()}},[]);let N=L(async f=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let u={...t.hpState,...f},_=await Te(u),w=_?.state&&typeof _.state=="object"?{...t.hpState,..._.state}:u;return a({type:"SET_HP_STATE",payload:w}),a({type:"SET_MESSAGE",payload:"State saved"}),_}catch(u){throw a({type:"SET_ERROR",payload:u.message}),u}finally{a({type:"SET_WORKING",payload:!1})}},[t.hpState]),d=L(async f=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let u=await Le(f),_=String(u?.mode||f||"hp").toLowerCase();return a({type:"SET_MODE",payload:_}),a({type:"SET_MESSAGE",payload:`Mode set to ${_}`}),u}catch(u){throw a({type:"SET_ERROR",payload:u.message}),u}finally{a({type:"SET_WORKING",payload:!1})}},[]),m=L(async(f,u)=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let _=await f();return Array.isArray(_?.avoids)&&a({type:"SET_HP_AVOIDS",payload:_.avoids}),u&&a({type:"SET_MESSAGE",payload:u}),await o(),await s(),_}catch(_){throw a({type:"SET_ERROR",payload:_.message}),_}finally{a({type:"SET_WORKING",payload:!1})}},[o,s]),x=L(async()=>m(()=>Me(),"Hold command sent"),[m]),T=L(async()=>m(()=>He(),"Next command sent"),[m]),y=L(async(f={})=>m(()=>Pe(f),"Avoid command sent"),[m]),h=L(async()=>m(()=>ke(),"Runtime avoids cleared"),[m]),O=L(async f=>m(()=>Ie(f),"Avoid removed"),[m]),P=ct(()=>({state:t,dispatch:a,navigate:i,refreshAll:g,refreshHpState:o,refreshServiceTypes:r,refreshHpAvoids:p,refreshStatus:s,saveHpState:N,setMode:d,holdScan:x,nextScan:T,avoidCurrent:y,clearHpAvoids:h,removeHpAvoid:O,SCREENS:c}),[t,i,g,o,r,p,s,N,d,x,T,y,h,O]);return lt.createElement($e.Provider,{value:P},e)}function k(){let e=pt($e);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import G from"https://esm.sh/react@18";import l,{useEffect as Ge,useMemo as oe,useState as W}from"https://esm.sh/react@18";import yt from"https://esm.sh/react@18";function E({children:e,onClick:t,type:a="button",variant:n="primary",className:i="",disabled:o=!1}){return yt.createElement("button",{type:a,className:`btn ${n==="secondary"?"btn-secondary":n==="danger"?"btn-danger":""} ${i}`.trim(),onClick:t,disabled:o},e)}function M(e){return e==null||e===""?"--":String(e)}function ht(e){let t=Math.max(0,Math.min(4,Number(e)||0));return`${"|".repeat(t)}${".".repeat(4-t)}`}function St(e){let t=Number(e);return Number.isFinite(t)?Number.isInteger(t)?`Range ${t}`:`Range ${t.toFixed(1)}`:"Range"}function _t(e,t){let a=t==="ground"?e?.profile_ground:e?.profile_airband,n=t==="ground"?e?.profiles_ground:e?.profiles_airband,i=Array.isArray(n)?n:[],o=String(a||"").trim();if(!o)return"";let r=i.find(s=>String(s?.id||"").trim().toLowerCase()===o.toLowerCase());return String(r?.label||"").trim()||o}function Nt(e,t){let a=String(t||"").trim(),n=String(e||"").trim();if(!n&&!a)return{department:"--",channel:"--"};let i=[" | "," - "," / "," \u2014 "," \u2013 ","::"];for(let o of i){if(!n.includes(o))continue;let[r,...p]=n.split(o),s=String(r||"").trim(),g=String(p.join(o)||"").trim();if(s&&g)return{department:s,channel:g}}return a&&n&&a.toLowerCase()!==n.toLowerCase()?{department:a,channel:n}:{department:n||a||"--",channel:n||"--"}}function ie(){let{state:e,holdScan:t,nextScan:a,avoidCurrent:n,navigate:i}=k(),{hpState:o,liveStatus:r,working:p,error:s,message:g}=e,N=String(r?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),d=String(r?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),m=!!N,x=!!d,T=(e.mode==="hp"||e.mode==="expert")&&x?"digital":"analog",[y,h]=W(T),[O,P]=W(""),[f,u]=W(!1),[_,w]=W("");Ge(()=>{if(y==="digital"&&!x){h(m?"analog":"digital");return}y==="analog"&&!m&&x&&h("digital")},[m,x,y]),Ge(()=>{!s&&!g||w("")},[s,g]);let $=y==="digital"?d||N:N||d,A=y==="digital"&&x,ye=String(o.mode||"full_database").trim().toLowerCase(),Ye=oe(()=>{let D=(Array.isArray(o.favorites)?o.favorites:[]).filter(V=>!V||!V.enabled?!1:String(V.type||"").trim().toLowerCase()==="analog");if(D.length===0)return"";let R=D.find(V=>String(V.target||"").trim().toLowerCase()==="airband")||D[0];return String(R?.label||"").trim()},[o.favorites]),Ke=String(r?.profile_airband||"").trim(),Z=_t(r,"airband")||Ye||Ke||"Airband",We=r?.last_hit_airband_label||r?.last_hit_ground_label||r?.last_hit||"",X=Nt(We,Z),he=A?r?.digital_scheduler_active_system_label||r?.digital_system_label||r?.digital_scheduler_active_system||r?.digital_profile||o.system_name||o.system:Z,Y=A?r?.digital_department_label||r?.digital_scheduler_active_department_label||o.department_name||o.department||r?.digital_profile||r?.digital_last_label:X.department||o.department_name||o.department,Se=A?r?.digital_last_tgid??o.tgid??o.talkgroup_id:"--",Q=A?(()=>{let v=Number(r?.digital_preflight?.playlist_frequency_hz?.[0]||r?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(v)&&v>0?(v/1e6).toFixed(4):o.frequency??o.freq})():r?.last_hit_airband||r?.last_hit_ground||r?.last_hit||"--",_e=!!(r?.digital_control_channel_metric_ready??r?.digital_control_decode_available),Ne=A?r?.digital_control_channel_locked?"Locked":_e?"Decoding":o.signal??o.signal_strength:r?.rtl_active?"Active":"Idle",xe=A?r?.digital_channel_label||r?.digital_last_label||o.channel_name||o.channel||Y:X.channel||Y,Je=A&&(r?.digital_last_mode||o.service_type||o.service)||"",Ee=A?xe:X.channel||xe,Ae=A?[M(Je||"Digital"),Se!=="--"?`TGID ${M(Se)}`:"",Q!=="--"?`${M(Q)} MHz`:"",Ne].filter(Boolean).join(" \u2022 "):`${M(Q)} \u2022 ${Ne}`,Ze=A?r?.digital_control_channel_locked?4:_e?3:1:r?.rtl_active?3:1,we=String(r?.digital_scan_mode||"").toLowerCase()==="single_system",Xe=we?"HOLD":"SCAN",Qe=oe(()=>{if(ye!=="favorites")return"Full Database";let v=Array.isArray(o.favorites)?o.favorites:[];if(v.length===0)return"Favorites";let D=v.filter(ee=>!!ee?.enabled);if(D.length===0)return"Favorites";let R=D.find(ee=>{let Ce=String(ee?.type||"").trim().toLowerCase();return A?Ce==="digital":Ce==="analog"})||D[0];return String(R?.label||"").trim()||"Favorites"},[ye,o.favorites,A]),Re=async()=>{try{await t()}catch{}},et=async()=>{try{await a()}catch{}},tt=async()=>{try{await n()}catch{}},at=async(v,D)=>{if(v==="info"){w(D==="system"?`System: ${M(he)}`:D==="department"?`Department: ${M(Y)}`:`Channel: ${M(Ee)} (${M(Ae)})`),P("");return}if(v==="advanced"){w("Advanced options are still being wired in HP3."),P("");return}if(v==="prev"){w("Previous-channel stepping is not wired yet in HP3."),P("");return}if(v==="fave"){P(""),i(c.FAVORITES);return}if(!A){w("Switch Audio Source to Digital for HOLD/NEXT/AVOID controls."),P("");return}v==="hold"?await Re():v==="next"?await et():v==="avoid"&&await tt(),P("")},rt=oe(()=>[{id:"squelch",label:"Squelch",onClick:()=>w("Squelch is currently managed from SB3 analog controls.")},{id:"range",label:St(o.range_miles),onClick:()=>i(c.RANGE)},{id:"atten",label:"Atten",onClick:()=>w("Attenuation toggle is not wired yet in HP3.")},{id:"gps",label:"GPS",onClick:()=>i(c.LOCATION)},{id:"help",label:"Help",onClick:()=>i(c.MENU)}],[o.range_miles,i]),nt={system:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],department:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],channel:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"hold",label:"Hold"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"},{id:"fave",label:"Fave"}]};return l.createElement("section",{className:"screen main-screen hp2-main"},l.createElement("div",{className:"hp2-radio-bar"},l.createElement("div",{className:"hp2-radio-buttons"},rt.map(v=>l.createElement("button",{key:v.id,type:"button",className:"hp2-radio-btn",onClick:v.onClick,disabled:p},v.label))),l.createElement("div",{className:"hp2-status-icons"},l.createElement("span",{className:`hp2-icon ${we?"on":""}`},Xe),l.createElement("span",{className:"hp2-icon"},"SIG ",ht(Ze)),l.createElement("span",{className:"hp2-icon"},A?"DIG":"ANA"))),l.createElement("div",{className:"hp2-lines"},l.createElement("div",{className:"hp2-line"},l.createElement("div",{className:"hp2-line-label"},"System / Favorite List"),l.createElement("div",{className:"hp2-line-body"},l.createElement("div",{className:"hp2-line-primary"},M(he)),l.createElement("div",{className:"hp2-line-secondary"},Qe)),l.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>P(v=>v==="system"?"":"system"),disabled:p},"<")),l.createElement("div",{className:"hp2-line"},l.createElement("div",{className:"hp2-line-label"},"Department"),l.createElement("div",{className:"hp2-line-body"},l.createElement("div",{className:"hp2-line-primary"},M(Y)),l.createElement("div",{className:"hp2-line-secondary"},A?`Profile: ${M(r?.digital_profile)}`:`Profile: ${M(Z)}`)),l.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>P(v=>v==="department"?"":"department"),disabled:p},"<")),l.createElement("div",{className:"hp2-line channel"},l.createElement("div",{className:"hp2-line-label"},"Channel"),l.createElement("div",{className:"hp2-line-body"},l.createElement("div",{className:"hp2-line-primary"},M(Ee)),l.createElement("div",{className:"hp2-line-secondary"},M(Ae))),l.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>P(v=>v==="channel"?"":"channel"),disabled:p},"<"))),O?l.createElement("div",{className:"hp2-submenu-popup"},nt[O]?.map(v=>l.createElement("button",{key:v.id,type:"button",className:"hp2-submenu-btn",onClick:()=>at(v.id,O),disabled:p},v.label))):null,l.createElement("div",{className:"hp2-feature-bar"},l.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>i(c.MENU),disabled:p},"Menu"),l.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>w("Replay is not wired yet in HP3."),disabled:p},"Replay"),l.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>w("Recording controls are not wired yet in HP3."),disabled:p},"Record"),l.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>u(v=>!v),disabled:p},f?"Unmute":"Mute")),l.createElement("div",{className:"hp2-web-audio"},l.createElement("div",{className:"hp2-audio-head"},l.createElement("div",{className:"muted"},"Web Audio Stream"),$?l.createElement("a",{href:`/stream/${$}`,target:"_blank",rel:"noreferrer"},"Open"):null),l.createElement("div",{className:"hp2-source-switch"},l.createElement(E,{variant:y==="analog"?"primary":"secondary",onClick:()=>h("analog"),disabled:!m||p},"Analog"),l.createElement(E,{variant:y==="digital"?"primary":"secondary",onClick:()=>h("digital"),disabled:!x||p},"Digital")),l.createElement("div",{className:"muted hp2-audio-meta"},"Source: ",A?"Digital":"Analog"," (",$||"no mount",")"),l.createElement("audio",{controls:!0,preload:"none",muted:f,className:"hp2-audio-player",src:$?`/stream/${$}`:"/stream/"})),_?l.createElement("div",{className:"message"},_):null,A?null:l.createElement("div",{className:"muted"},"HOLD/NEXT/AVOID actions require Digital source."),s?l.createElement("div",{className:"error"},s):null,g?l.createElement("div",{className:"message"},g):null)}import j from"https://esm.sh/react@18";import z from"https://esm.sh/react@18";function I({title:e,subtitle:t="",showBack:a=!1,onBack:n}){return z.createElement("div",{className:"header"},z.createElement("div",null,z.createElement("h1",null,e),t?z.createElement("div",{className:"sub"},t):null),a?z.createElement("button",{type:"button",className:"btn btn-secondary",onClick:n},"Back"):null)}var xt=[{id:c.LOCATION,label:"Set Your Location"},{id:c.SERVICE_TYPES,label:"Select Service Types"},{id:c.RANGE,label:"Set Range"},{id:c.FAVORITES,label:"Manage Favorites"},{id:c.AVOID,label:"Avoid Options"},{id:c.MODE_SELECTION,label:"Mode Selection"}];function se(){let{navigate:e,state:t}=k();return j.createElement("section",{className:"screen menu"},j.createElement(I,{title:"Menu",showBack:!0,onBack:()=>e(c.MAIN)}),j.createElement("div",{className:"menu-list"},xt.map(a=>j.createElement(E,{key:a.id,variant:"secondary",className:"menu-item",onClick:()=>e(a.id),disabled:t.working},a.label))),t.error?j.createElement("div",{className:"error"},t.error):null)}import C,{useEffect as Et,useState as q}from"https://esm.sh/react@18";function Ve(e){if(e===""||e===null||e===void 0)return null;let t=Number(e);return Number.isFinite(t)?t:NaN}function le(){let{state:e,saveHpState:t,navigate:a}=k(),{hpState:n,working:i}=e,[o,r]=q(""),[p,s]=q(""),[g,N]=q(""),[d,m]=q(!0),[x,T]=q("");return Et(()=>{r(n.zip||n.postal_code||""),s(n.lat!==void 0&&n.lat!==null?String(n.lat):n.latitude!==void 0&&n.latitude!==null?String(n.latitude):""),N(n.lon!==void 0&&n.lon!==null?String(n.lon):n.longitude!==void 0&&n.longitude!==null?String(n.longitude):""),m(n.use_location!==!1)},[n]),C.createElement("section",{className:"screen location-screen"},C.createElement(I,{title:"Location",showBack:!0,onBack:()=>a(c.MENU)}),C.createElement("div",{className:"list"},C.createElement("label",null,C.createElement("div",{className:"muted"},"ZIP"),C.createElement("input",{className:"input",value:o,onChange:h=>r(h.target.value.trim()),placeholder:"37201"})),C.createElement("label",null,C.createElement("div",{className:"muted"},"Latitude"),C.createElement("input",{className:"input",value:p,onChange:h=>s(h.target.value),placeholder:"36.12"})),C.createElement("label",null,C.createElement("div",{className:"muted"},"Longitude"),C.createElement("input",{className:"input",value:g,onChange:h=>N(h.target.value),placeholder:"-86.67"})),C.createElement("label",{className:"row"},C.createElement("span",null,"Use location for scanning"),C.createElement("input",{type:"checkbox",checked:d,onChange:h=>m(h.target.checked)}))),C.createElement("div",{className:"button-row"},C.createElement(E,{onClick:async()=>{if(T(""),o&&!/^\d{5}(-\d{4})?$/.test(o)){T("ZIP must be 5 digits or ZIP+4.");return}let h=Ve(p),O=Ve(g);if(Number.isNaN(h)||Number.isNaN(O)){T("Latitude and longitude must be valid numbers.");return}if(h!==null&&(h<-90||h>90)){T("Latitude must be between -90 and 90.");return}if(O!==null&&(O<-180||O>180)){T("Longitude must be between -180 and 180.");return}try{await t({zip:o,lat:h,lon:O,use_location:d}),a(c.MENU)}catch{}},disabled:i},"Save")),x?C.createElement("div",{className:"error"},x):null,e.error?C.createElement("div",{className:"error"},e.error):null)}import F,{useEffect as At,useMemo as wt,useState as Ct}from"https://esm.sh/react@18";function de(){let{state:e,saveHpState:t,navigate:a}=k(),{hpState:n,serviceTypes:i,working:o}=e,r=wt(()=>i.filter(d=>d.enabled_by_default).map(d=>Number(d.service_tag)),[i]),[p,s]=Ct([]);At(()=>{let d=Array.isArray(n.enabled_service_tags)?n.enabled_service_tags.map(Number):r;s(Array.from(new Set(d)).filter(m=>Number.isFinite(m)))},[n.enabled_service_tags,r]);let g=d=>{s(m=>m.includes(d)?m.filter(x=>x!==d):[...m,d])},N=async()=>{try{await t({enabled_service_tags:[...p].sort((d,m)=>d-m)}),a(c.MENU)}catch{}};return F.createElement("section",{className:"screen service-types-screen"},F.createElement(I,{title:"Service Types",showBack:!0,onBack:()=>a(c.MENU)}),F.createElement("div",{className:"checkbox-list"},i.map(d=>{let m=Number(d.service_tag),x=p.includes(m);return F.createElement("label",{key:m,className:"row card"},F.createElement("span",null,d.name),F.createElement("input",{type:"checkbox",checked:x,onChange:()=>g(m)}))})),F.createElement("div",{className:"button-row"},F.createElement(E,{onClick:N,disabled:o},"Save")),e.error?F.createElement("div",{className:"error"},e.error):null)}import U,{useEffect as Tt,useState as kt}from"https://esm.sh/react@18";function pe(){let{state:e,saveHpState:t,navigate:a}=k(),{hpState:n,working:i}=e,[o,r]=kt(15);Tt(()=>{let s=Number(n.range_miles);r(Number.isFinite(s)?s:15)},[n.range_miles]);let p=async()=>{try{await t({range_miles:o}),a(c.MENU)}catch{}};return U.createElement("section",{className:"screen range-screen"},U.createElement(I,{title:"Range",showBack:!0,onBack:()=>a(c.MENU)}),U.createElement("div",{className:"card"},U.createElement("div",{className:"row"},U.createElement("span",null,"Range Miles"),U.createElement("strong",null,o.toFixed(1))),U.createElement("input",{className:"range",type:"range",min:"0",max:"30",step:"0.5",value:o,onChange:s=>r(Number(s.target.value))})),U.createElement("div",{className:"button-row"},U.createElement(E,{onClick:p,disabled:i},"Save")),e.error?U.createElement("div",{className:"error"},e.error):null)}import b,{useEffect as It,useMemo as ze,useState as Ot}from"https://esm.sh/react@18";function Lt(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach((n,i)=>{if(!n||typeof n!="object")return;let o=String(n.id||"").trim(),r=o?o.split(":"):[],p=String(n.type||n.kind||"").trim().toLowerCase(),s=String(n.target||"").trim().toLowerCase(),g=String(n.profile_id||n.profileId||n.profile||"").trim();if(!g&&r.length>0&&(r[0].toLowerCase()==="digital"&&r.length>=2?(p="digital",g=r.slice(1).join(":").trim()):r[0].toLowerCase()==="analog"&&r.length>=3&&(p="analog",s=String(r[1]||"").trim().toLowerCase(),g=r.slice(2).join(":").trim())),!p&&s&&(p="analog"),p==="digital"&&(s=""),p!=="digital"&&p!=="analog"||p==="analog"&&s!=="airband"&&s!=="ground"||!g)return;let N=p==="digital"?`digital:${g}`:`analog:${s}:${g}`;a.has(N)||(a.add(N),t.push({id:N,type:p,target:s,profile_id:g,label:String(n.label||n.name||g),enabled:n.enabled===!0,_index:i}))}),t}function Mt(e){return{analog_airband:e.filter(t=>t.type==="analog"&&t.target==="airband").sort((t,a)=>t._index-a._index),analog_ground:e.filter(t=>t.type==="analog"&&t.target==="ground").sort((t,a)=>t._index-a._index),digital:e.filter(t=>t.type==="digital").sort((t,a)=>t._index-a._index)}}function ce(){let{state:e,saveHpState:t,navigate:a}=k(),{hpState:n,working:i}=e,o=ze(()=>Array.isArray(n.favorites)?n.favorites:Array.isArray(n.favorites_list)?n.favorites_list:[],[n.favorites,n.favorites_list]),[r,p]=Ot([]),s=ze(()=>Mt(r),[r]);It(()=>{p(Lt(o))},[o]);let g=(d,m)=>{p(x=>x.map(T=>(T.type==="digital"?"digital":`analog_${T.target}`)!==d?T:{...T,enabled:T.profile_id===m}))},N=async()=>{try{await t({favorites:r}),a(c.MENU)}catch{}};return b.createElement("section",{className:"screen favorites-screen"},b.createElement(I,{title:"Favorites",showBack:!0,onBack:()=>a(c.MENU)}),r.length===0?b.createElement("div",{className:"muted"},"No favorites in current state."):b.createElement("div",{className:"list"},b.createElement("div",{className:"card"},b.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Analog Airband"),s.analog_airband.length===0?b.createElement("div",{className:"muted"},"No airband profiles found."):s.analog_airband.map(d=>b.createElement("label",{key:d.id,className:"row",style:{marginBottom:"6px"}},b.createElement("span",null,d.label),b.createElement("input",{type:"radio",name:"favorites-analog-airband",checked:d.enabled,onChange:()=>g("analog_airband",d.profile_id)})))),b.createElement("div",{className:"card"},b.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Analog Ground"),s.analog_ground.length===0?b.createElement("div",{className:"muted"},"No ground profiles found."):s.analog_ground.map(d=>b.createElement("label",{key:d.id,className:"row",style:{marginBottom:"6px"}},b.createElement("span",null,d.label),b.createElement("input",{type:"radio",name:"favorites-analog-ground",checked:d.enabled,onChange:()=>g("analog_ground",d.profile_id)})))),b.createElement("div",{className:"card"},b.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Digital"),s.digital.length===0?b.createElement("div",{className:"muted"},"No digital profiles found."):s.digital.map(d=>b.createElement("label",{key:d.id,className:"row",style:{marginBottom:"6px"}},b.createElement("span",null,d.label),b.createElement("input",{type:"radio",name:"favorites-digital",checked:d.enabled,onChange:()=>g("digital",d.profile_id)}))))),b.createElement("div",{className:"muted",style:{marginTop:"8px"}},"Saving favorites sets the active analog/digital profiles for HP3 playback."),b.createElement("div",{className:"button-row"},b.createElement(E,{onClick:N,disabled:i},"Save")),e.error?b.createElement("div",{className:"error"},e.error):null)}import S,{useEffect as Ht,useMemo as je,useState as Pt}from"https://esm.sh/react@18";function Bt(e){return Array.isArray(e)?e.map((t,a)=>t&&typeof t=="object"?{id:String(t.id??`${t.type||"item"}-${a}`),label:String(t.label||t.alpha_tag||t.name||`Avoid ${a+1}`),type:String(t.type||"item"),source:"persistent"}:{id:`item-${a}`,label:String(t),type:"item",source:"persistent"}):[]}function Dt(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach(n=>{let i=String(n||"").trim();!i||a.has(i)||(a.add(i),t.push({id:`runtime:${i}`,label:i,type:"system",token:i,source:"runtime"}))}),t}function ue(){let{state:e,saveHpState:t,avoidCurrent:a,clearHpAvoids:n,removeHpAvoid:i,navigate:o}=k(),{hpState:r,hpAvoids:p,working:s}=e,g=je(()=>Array.isArray(r.avoid_list)?r.avoid_list:Array.isArray(r.avoids)?r.avoids:Array.isArray(r.avoid)?r.avoid:[],[r.avoid_list,r.avoids,r.avoid]),[N,d]=Pt([]);Ht(()=>{d(Bt(g))},[g]);let m=je(()=>Dt(p),[p]),x=async(y=N)=>{try{await t({avoid_list:y})}catch{}},T=async()=>{try{await a()}catch{}};return S.createElement("section",{className:"screen avoid-screen"},S.createElement(I,{title:"Avoid",showBack:!0,onBack:()=>o(c.MENU)}),S.createElement("div",{className:"list"},S.createElement("div",{className:"card"},S.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Runtime Avoids (HP Scan Pool)"),m.length===0?S.createElement("div",{className:"muted"},"No runtime HP avoids."):m.map(y=>S.createElement("div",{key:y.id,className:"row",style:{marginBottom:"6px"}},S.createElement("div",null,S.createElement("div",null,y.label),S.createElement("div",{className:"muted"},y.type)),S.createElement(E,{variant:"danger",onClick:()=>i(y.token),disabled:s},"Remove")))),S.createElement("div",{className:"card"},S.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Persistent Avoids (State)"),N.length===0?S.createElement("div",{className:"muted"},"No persistent avoids in current state."):N.map(y=>S.createElement("div",{key:y.id,className:"row",style:{marginBottom:"6px"}},S.createElement("div",null,S.createElement("div",null,y.label),S.createElement("div",{className:"muted"},y.type)),S.createElement(E,{variant:"danger",onClick:()=>{let h=N.filter(O=>O.id!==y.id);d(h),x(h)},disabled:s},"Remove"))))),S.createElement("div",{className:"button-row"},S.createElement(E,{onClick:T,disabled:s},"Avoid Current"),S.createElement(E,{variant:"secondary",onClick:async()=>{d([]),await x([]),await n()},disabled:s},"Clear All"),S.createElement(E,{onClick:()=>x(),disabled:s},"Save")),e.error?S.createElement("div",{className:"error"},e.error):null)}import H,{useEffect as Ut,useState as $t}from"https://esm.sh/react@18";function me(){let{state:e,setMode:t,navigate:a}=k(),[n,i]=$t("hp");return Ut(()=>{i(e.mode||"hp")},[e.mode]),H.createElement("section",{className:"screen mode-selection-screen"},H.createElement(I,{title:"Mode Selection",showBack:!0,onBack:()=>a(c.MENU)}),H.createElement("div",{className:"list"},H.createElement("label",{className:"row card"},H.createElement("span",null,"HP Mode"),H.createElement("input",{type:"radio",name:"scan-mode",value:"hp",checked:n==="hp",onChange:r=>i(r.target.value)})),H.createElement("label",{className:"row card"},H.createElement("span",null,"Expert Mode"),H.createElement("input",{type:"radio",name:"scan-mode",value:"expert",checked:n==="expert",onChange:r=>i(r.target.value)}))),H.createElement("div",{className:"button-row"},H.createElement(E,{onClick:async()=>{try{await t(n),a(c.MENU)}catch{}},disabled:e.working},"Save")),e.error?H.createElement("div",{className:"error"},e.error):null)}import Ft from"https://esm.sh/react@18";function fe({label:e="Loading..."}){return Ft.createElement("div",{className:"loading"},e)}function ge(){let{state:e}=k();if(e.loading)return G.createElement(fe,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case c.MENU:return G.createElement(se,null);case c.LOCATION:return G.createElement(le,null);case c.SERVICE_TYPES:return G.createElement(de,null);case c.RANGE:return G.createElement(pe,null);case c.FAVORITES:return G.createElement(ce,null);case c.AVOID:return G.createElement(ue,null);case c.MODE_SELECTION:return G.createElement(me,null);case c.MAIN:default:return G.createElement(ie,null)}}var Gt=`
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
`;function ve(){return J.createElement(Fe,null,J.createElement("div",{className:"app-shell"},J.createElement("style",null,Gt),J.createElement(ge,null)))}var qe=document.getElementById("root");if(!qe)throw new Error("Missing #root mount element");Vt(qe).render(be.createElement(be.StrictMode,null,be.createElement(ve,null)));
