import me from"https://esm.sh/react@18";import{createRoot as Mt}from"https://esm.sh/react-dom@18/client";import W from"https://esm.sh/react@18";import tt,{createContext as at,useCallback as O,useContext as rt,useEffect as te,useMemo as ot,useReducer as nt}from"https://esm.sh/react@18";var Re={"Content-Type":"application/json"};async function P(e,{method:t="GET",body:a}={}){let o={method:t,headers:{...Re}};a!==void 0&&(o.body=JSON.stringify(a));let n=await fetch(e,o),s=await n.text(),r={};try{r=s?JSON.parse(s):{}}catch{r={raw:s}}if(!n.ok){let l=r?.error||`Request failed (${n.status})`,p=new Error(l);throw p.status=n.status,p.payload=r,p}return r}function X(){return P("/api/hp/state")}function xe(e){return P("/api/hp/state",{method:"POST",body:e})}function Q(){return P("/api/hp/service-types")}function R(){return P("/api/hp/avoids")}function Ee(){return P("/api/hp/avoids",{method:"POST",body:{action:"clear"}})}function Ae(e){return P("/api/hp/avoids",{method:"POST",body:{action:"remove",system:e}})}function ee(){return P("/api/status")}function we(e){return P("/api/mode",{method:"POST",body:{mode:e}})}function Te(e={}){return P("/api/hp/hold",{method:"POST",body:e})}function Ce(e={}){return P("/api/hp/next",{method:"POST",body:e})}function ke(e={}){return P("/api/hp/avoid",{method:"POST",body:e})}var c=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),it={hpState:{},serviceTypes:[],liveStatus:{},hpAvoids:[],currentScreen:c.MAIN,mode:"hp",sseConnected:!1,loading:!0,working:!1,error:"",message:""},st=["digital_scheduler_active_system","digital_scheduler_next_system","digital_last_label","digital_last_mode","digital_last_tgid","digital_profile","digital_scan_mode","stream_mount","digital_stream_mount","profile_airband","profile_ground","last_hit_airband_label","last_hit_ground_label"];function Ie(e){return e==null?!1:typeof e=="string"?e.trim()!=="":Array.isArray(e)?e.length>0:!0}function Y(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach(o=>{let n=String(o||"").trim();!n||a.has(n)||(a.add(n),t.push(n))}),t}function dt(e,t){let a=t&&typeof t=="object"?t:{},o={...e||{},...a};return st.forEach(n=>{!Ie(a[n])&&Ie(e?.[n])&&(o[n]=e[n])}),o}function lt(e,t){switch(t.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:t.payload.hpState||{},serviceTypes:t.payload.serviceTypes||[],liveStatus:t.payload.liveStatus||{},hpAvoids:t.payload.hpAvoids||[],mode:t.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:t.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!t.payload};case"SET_ERROR":return{...e,error:t.payload||""};case"SET_MESSAGE":return{...e,message:t.payload||""};case"SET_HP_STATE":return{...e,hpState:t.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:t.payload||[]};case"SET_HP_AVOIDS":return{...e,hpAvoids:Y(t.payload)};case"SET_LIVE_STATUS":return{...e,liveStatus:dt(e.liveStatus,t.payload),hpAvoids:Array.isArray(t.payload?.hp_avoids)?Y(t.payload.hp_avoids):e.hpAvoids};case"SET_MODE":return{...e,mode:t.payload||e.mode};case"SET_SSE_CONNECTED":return{...e,sseConnected:!!t.payload};case"NAVIGATE":return{...e,currentScreen:t.payload||c.MAIN};default:return e}}var Le=at(null);function Oe(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(a=>({service_tag:Number(a?.service_tag),name:String(a?.name||`Service ${a?.service_tag}`),enabled_by_default:!!a?.enabled_by_default}))}function Me(e){let t=e&&typeof e.state=="object"&&e.state!==null?e.state:{},a=String(e?.mode||"hp").toLowerCase();return{hpState:t,mode:a}}function He({children:e}){let[t,a]=nt(lt,it),o=O(u=>{a({type:"NAVIGATE",payload:u})},[]),n=O(async()=>{let u=await X(),m=Me(u);return a({type:"SET_HP_STATE",payload:m.hpState}),a({type:"SET_MODE",payload:m.mode}),m},[]),s=O(async()=>{let u=await Q(),m=Oe(u);return a({type:"SET_SERVICE_TYPES",payload:m}),m},[]),r=O(async()=>{let u=await R(),m=Y(u?.avoids);return a({type:"SET_HP_AVOIDS",payload:m}),m},[]),l=O(async()=>{let u=await ee();return a({type:"SET_LIVE_STATUS",payload:u||{}}),u},[]),p=O(async()=>{a({type:"LOAD_START"});try{let[u,m,x]=await Promise.all([X(),Q(),R()]),B={};try{B=await ee()}catch{B={}}let k=Me(u),U=Oe(m),A=Y(x?.avoids);a({type:"LOAD_SUCCESS",payload:{hpState:k.hpState,mode:k.mode,serviceTypes:U,liveStatus:B,hpAvoids:A}})}catch(u){a({type:"LOAD_ERROR",payload:u.message})}},[]);te(()=>{p()},[p]),te(()=>{let u=setInterval(()=>{l().catch(()=>{})},t.sseConnected?1e4:2500);return()=>clearInterval(u)},[l,t.sseConnected]),te(()=>{if(typeof EventSource>"u")return;let u=!1,m=null,x=null,B=()=>{u||(m=new EventSource("/api/stream"),m.onopen=()=>{a({type:"SET_SSE_CONNECTED",payload:!0})},m.addEventListener("status",k=>{try{let U=JSON.parse(k?.data||"{}");a({type:"SET_LIVE_STATUS",payload:U})}catch{}}),m.onerror=()=>{a({type:"SET_SSE_CONNECTED",payload:!1}),m&&(m.close(),m=null),u||(x=setTimeout(B,2e3))})};return B(),()=>{u=!0,a({type:"SET_SSE_CONNECTED",payload:!1}),x&&clearTimeout(x),m&&m.close()}},[]);let v=O(async u=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let m={...t.hpState,...u},x=await xe(m),B=x?.state&&typeof x.state=="object"?{...t.hpState,...x.state}:m;return a({type:"SET_HP_STATE",payload:B}),a({type:"SET_MESSAGE",payload:"State saved"}),x}catch(m){throw a({type:"SET_ERROR",payload:m.message}),m}finally{a({type:"SET_WORKING",payload:!1})}},[t.hpState]),N=O(async u=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let m=await we(u),x=String(m?.mode||u||"hp").toLowerCase();return a({type:"SET_MODE",payload:x}),a({type:"SET_MESSAGE",payload:`Mode set to ${x}`}),m}catch(m){throw a({type:"SET_ERROR",payload:m.message}),m}finally{a({type:"SET_WORKING",payload:!1})}},[]),i=O(async(u,m)=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let x=await u();return Array.isArray(x?.avoids)&&a({type:"SET_HP_AVOIDS",payload:x.avoids}),m&&a({type:"SET_MESSAGE",payload:m}),await n(),await l(),x}catch(x){throw a({type:"SET_ERROR",payload:x.message}),x}finally{a({type:"SET_WORKING",payload:!1})}},[n,l]),g=O(async()=>i(()=>Te(),"Hold command sent"),[i]),_=O(async()=>i(()=>Ce(),"Next command sent"),[i]),T=O(async(u={})=>i(()=>ke(u),"Avoid command sent"),[i]),y=O(async()=>i(()=>Ee(),"Runtime avoids cleared"),[i]),h=O(async u=>i(()=>Ae(u),"Avoid removed"),[i]),L=ot(()=>({state:t,dispatch:a,navigate:o,refreshAll:p,refreshHpState:n,refreshServiceTypes:s,refreshHpAvoids:r,refreshStatus:l,saveHpState:v,setMode:N,holdScan:g,nextScan:_,avoidCurrent:T,clearHpAvoids:y,removeHpAvoid:h,SCREENS:c}),[t,o,p,n,s,r,l,v,N,g,_,T,y,h]);return tt.createElement(Le.Provider,{value:L},e)}function C(){let e=rt(Le);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import G from"https://esm.sh/react@18";import d,{useEffect as Be,useMemo as Pe,useState as K}from"https://esm.sh/react@18";import pt from"https://esm.sh/react@18";function E({children:e,onClick:t,type:a="button",variant:o="primary",className:n="",disabled:s=!1}){return pt.createElement("button",{type:a,className:`btn ${o==="secondary"?"btn-secondary":o==="danger"?"btn-danger":""} ${n}`.trim(),onClick:t,disabled:s},e)}function M(e){return e==null||e===""?"--":String(e)}function ct(e){let t=Math.max(0,Math.min(4,Number(e)||0));return`${"|".repeat(t)}${".".repeat(4-t)}`}function ut(e){let t=Number(e);return Number.isFinite(t)?Number.isInteger(t)?`Range ${t}`:`Range ${t.toFixed(1)}`:"Range"}function ae(){let{state:e,holdScan:t,nextScan:a,avoidCurrent:o,navigate:n}=C(),{hpState:s,liveStatus:r,working:l,error:p,message:v}=e,N=String(r?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),i=String(r?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),g=!!N,_=!!i,T=(e.mode==="hp"||e.mode==="expert")&&_?"digital":"analog",[y,h]=K(T),[L,u]=K(""),[m,x]=K(!1),[B,k]=K("");Be(()=>{if(y==="digital"&&!_){h(g?"analog":"digital");return}y==="analog"&&!g&&_&&h("digital")},[g,_,y]),Be(()=>{!p&&!v||k("")},[p,v]);let U=y==="digital"?i||N:N||i,A=y==="digital"&&_,fe=String(s.mode||"full_database").trim().toLowerCase(),ve=A?r?.digital_scheduler_active_system||r?.digital_profile||s.system_name||s.system:r?.profile_airband||"Airband",F=A?r?.digital_department_label||s.department_name||s.department||r?.digital_profile||r?.digital_last_label:r?.last_hit_airband_label||r?.last_hit_ground_label||r?.last_hit||s.department_name||s.department,ge=A?r?.digital_last_tgid??s.tgid??s.talkgroup_id:"--",J=A?(()=>{let f=Number(r?.digital_preflight?.playlist_frequency_hz?.[0]||r?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(f)&&f>0?(f/1e6).toFixed(4):s.frequency??s.freq})():r?.last_hit_airband||r?.last_hit_ground||r?.last_hit||"--",be=!!(r?.digital_control_channel_metric_ready??r?.digital_control_decode_available),ye=A?r?.digital_control_channel_locked?"Locked":be?"Decoding":s.signal??s.signal_strength:r?.rtl_active?"Active":"Idle",Ve=A&&(r?.digital_last_label||s.channel_name||s.channel)||F,Fe=A&&(r?.digital_last_mode||s.service_type||s.service)||"",he=A?Ve:F,Se=A?[M(Fe||"Digital"),ge!=="--"?`TGID ${M(ge)}`:"",J!=="--"?`${M(J)} MHz`:"",ye].filter(Boolean).join(" \u2022 "):`${M(J)} \u2022 ${ye}`,ze=A?r?.digital_control_channel_locked?4:be?3:1:r?.rtl_active?3:1,Ne=String(r?.digital_scan_mode||"").toLowerCase()==="single_system",je=Ne?"HOLD":"SCAN",qe=Pe(()=>{if(fe!=="favorites")return"Full Database";let f=Array.isArray(s.favorites)?s.favorites:[];if(f.length===0)return"Favorites";let V=f.filter(Z=>!!Z?.enabled);if(V.length===0)return"Favorites";let Qe=V.find(Z=>{let _e=String(Z?.type||"").trim().toLowerCase();return A?_e==="digital":_e==="analog"})||V[0];return String(Qe?.label||"").trim()||"Favorites"},[fe,s.favorites,A]),Ye=async()=>{try{await t()}catch{}},Ke=async()=>{try{await a()}catch{}},We=async()=>{try{await o()}catch{}},Je=async(f,V)=>{if(f==="info"){k(V==="system"?`System: ${M(ve)}`:V==="department"?`Department: ${M(F)}`:`Channel: ${M(he)} (${M(Se)})`),u("");return}if(f==="advanced"){k("Advanced options are still being wired in HP3."),u("");return}if(f==="prev"){k("Previous-channel stepping is not wired yet in HP3."),u("");return}if(f==="fave"){u(""),n(c.FAVORITES);return}if(!A){k("Switch Audio Source to Digital for HOLD/NEXT/AVOID controls."),u("");return}f==="hold"?await Ye():f==="next"?await Ke():f==="avoid"&&await We(),u("")},Ze=Pe(()=>[{id:"squelch",label:"Squelch",onClick:()=>k("Squelch is currently managed from SB3 analog controls.")},{id:"range",label:ut(s.range_miles),onClick:()=>n(c.RANGE)},{id:"atten",label:"Atten",onClick:()=>k("Attenuation toggle is not wired yet in HP3.")},{id:"gps",label:"GPS",onClick:()=>n(c.LOCATION)},{id:"help",label:"Help",onClick:()=>n(c.MENU)}],[s.range_miles,n]),Xe={system:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],department:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],channel:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"hold",label:"Hold"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"},{id:"fave",label:"Fave"}]};return d.createElement("section",{className:"screen main-screen hp2-main"},d.createElement("div",{className:"hp2-radio-bar"},d.createElement("div",{className:"hp2-radio-buttons"},Ze.map(f=>d.createElement("button",{key:f.id,type:"button",className:"hp2-radio-btn",onClick:f.onClick,disabled:l},f.label))),d.createElement("div",{className:"hp2-status-icons"},d.createElement("span",{className:`hp2-icon ${Ne?"on":""}`},je),d.createElement("span",{className:"hp2-icon"},"SIG ",ct(ze)),d.createElement("span",{className:"hp2-icon"},A?"DIG":"ANA"))),d.createElement("div",{className:"hp2-lines"},d.createElement("div",{className:"hp2-line"},d.createElement("div",{className:"hp2-line-label"},"System / Favorite List"),d.createElement("div",{className:"hp2-line-body"},d.createElement("div",{className:"hp2-line-primary"},M(ve)),d.createElement("div",{className:"hp2-line-secondary"},qe)),d.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>u(f=>f==="system"?"":"system"),disabled:l},"<")),d.createElement("div",{className:"hp2-line"},d.createElement("div",{className:"hp2-line-label"},"Department"),d.createElement("div",{className:"hp2-line-body"},d.createElement("div",{className:"hp2-line-primary"},M(F)),d.createElement("div",{className:"hp2-line-secondary"},A?`Profile: ${M(r?.digital_profile)}`:`Source: ${M(r?.profile_airband||"Airband")}`)),d.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>u(f=>f==="department"?"":"department"),disabled:l},"<")),d.createElement("div",{className:"hp2-line channel"},d.createElement("div",{className:"hp2-line-label"},"Channel"),d.createElement("div",{className:"hp2-line-body"},d.createElement("div",{className:"hp2-line-primary"},M(he)),d.createElement("div",{className:"hp2-line-secondary"},M(Se))),d.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>u(f=>f==="channel"?"":"channel"),disabled:l},"<"))),L?d.createElement("div",{className:"hp2-submenu-popup"},Xe[L]?.map(f=>d.createElement("button",{key:f.id,type:"button",className:"hp2-submenu-btn",onClick:()=>Je(f.id,L),disabled:l},f.label))):null,d.createElement("div",{className:"hp2-feature-bar"},d.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>n(c.MENU),disabled:l},"Menu"),d.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>k("Replay is not wired yet in HP3."),disabled:l},"Replay"),d.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>k("Recording controls are not wired yet in HP3."),disabled:l},"Record"),d.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>x(f=>!f),disabled:l},m?"Unmute":"Mute")),d.createElement("div",{className:"hp2-web-audio"},d.createElement("div",{className:"hp2-audio-head"},d.createElement("div",{className:"muted"},"Web Audio Stream"),U?d.createElement("a",{href:`/stream/${U}`,target:"_blank",rel:"noreferrer"},"Open"):null),d.createElement("div",{className:"hp2-source-switch"},d.createElement(E,{variant:y==="analog"?"primary":"secondary",onClick:()=>h("analog"),disabled:!g||l},"Analog"),d.createElement(E,{variant:y==="digital"?"primary":"secondary",onClick:()=>h("digital"),disabled:!_||l},"Digital")),d.createElement("div",{className:"muted hp2-audio-meta"},"Source: ",A?"Digital":"Analog"," (",U||"no mount",")"),d.createElement("audio",{controls:!0,preload:"none",muted:m,className:"hp2-audio-player",src:U?`/stream/${U}`:"/stream/"})),B?d.createElement("div",{className:"message"},B):null,A?null:d.createElement("div",{className:"muted"},"HOLD/NEXT/AVOID actions require Digital source."),p?d.createElement("div",{className:"error"},p):null,v?d.createElement("div",{className:"message"},v):null)}import j from"https://esm.sh/react@18";import z from"https://esm.sh/react@18";function I({title:e,subtitle:t="",showBack:a=!1,onBack:o}){return z.createElement("div",{className:"header"},z.createElement("div",null,z.createElement("h1",null,e),t?z.createElement("div",{className:"sub"},t):null),a?z.createElement("button",{type:"button",className:"btn btn-secondary",onClick:o},"Back"):null)}var mt=[{id:c.LOCATION,label:"Set Your Location"},{id:c.SERVICE_TYPES,label:"Select Service Types"},{id:c.RANGE,label:"Set Range"},{id:c.FAVORITES,label:"Manage Favorites"},{id:c.AVOID,label:"Avoid Options"},{id:c.MODE_SELECTION,label:"Mode Selection"}];function re(){let{navigate:e,state:t}=C();return j.createElement("section",{className:"screen menu"},j.createElement(I,{title:"Menu",showBack:!0,onBack:()=>e(c.MAIN)}),j.createElement("div",{className:"menu-list"},mt.map(a=>j.createElement(E,{key:a.id,variant:"secondary",className:"menu-item",onClick:()=>e(a.id),disabled:t.working},a.label))),t.error?j.createElement("div",{className:"error"},t.error):null)}import w,{useEffect as ft,useState as q}from"https://esm.sh/react@18";function De(e){if(e===""||e===null||e===void 0)return null;let t=Number(e);return Number.isFinite(t)?t:NaN}function oe(){let{state:e,saveHpState:t,navigate:a}=C(),{hpState:o,working:n}=e,[s,r]=q(""),[l,p]=q(""),[v,N]=q(""),[i,g]=q(!0),[_,T]=q("");return ft(()=>{r(o.zip||o.postal_code||""),p(o.lat!==void 0&&o.lat!==null?String(o.lat):o.latitude!==void 0&&o.latitude!==null?String(o.latitude):""),N(o.lon!==void 0&&o.lon!==null?String(o.lon):o.longitude!==void 0&&o.longitude!==null?String(o.longitude):""),g(o.use_location!==!1)},[o]),w.createElement("section",{className:"screen location-screen"},w.createElement(I,{title:"Location",showBack:!0,onBack:()=>a(c.MENU)}),w.createElement("div",{className:"list"},w.createElement("label",null,w.createElement("div",{className:"muted"},"ZIP"),w.createElement("input",{className:"input",value:s,onChange:h=>r(h.target.value.trim()),placeholder:"37201"})),w.createElement("label",null,w.createElement("div",{className:"muted"},"Latitude"),w.createElement("input",{className:"input",value:l,onChange:h=>p(h.target.value),placeholder:"36.12"})),w.createElement("label",null,w.createElement("div",{className:"muted"},"Longitude"),w.createElement("input",{className:"input",value:v,onChange:h=>N(h.target.value),placeholder:"-86.67"})),w.createElement("label",{className:"row"},w.createElement("span",null,"Use location for scanning"),w.createElement("input",{type:"checkbox",checked:i,onChange:h=>g(h.target.checked)}))),w.createElement("div",{className:"button-row"},w.createElement(E,{onClick:async()=>{if(T(""),s&&!/^\d{5}(-\d{4})?$/.test(s)){T("ZIP must be 5 digits or ZIP+4.");return}let h=De(l),L=De(v);if(Number.isNaN(h)||Number.isNaN(L)){T("Latitude and longitude must be valid numbers.");return}if(h!==null&&(h<-90||h>90)){T("Latitude must be between -90 and 90.");return}if(L!==null&&(L<-180||L>180)){T("Longitude must be between -180 and 180.");return}try{await t({zip:s,lat:h,lon:L,use_location:i}),a(c.MENU)}catch{}},disabled:n},"Save")),_?w.createElement("div",{className:"error"},_):null,e.error?w.createElement("div",{className:"error"},e.error):null)}import $,{useEffect as vt,useMemo as gt,useState as bt}from"https://esm.sh/react@18";function ne(){let{state:e,saveHpState:t,navigate:a}=C(),{hpState:o,serviceTypes:n,working:s}=e,r=gt(()=>n.filter(i=>i.enabled_by_default).map(i=>Number(i.service_tag)),[n]),[l,p]=bt([]);vt(()=>{let i=Array.isArray(o.enabled_service_tags)?o.enabled_service_tags.map(Number):r;p(Array.from(new Set(i)).filter(g=>Number.isFinite(g)))},[o.enabled_service_tags,r]);let v=i=>{p(g=>g.includes(i)?g.filter(_=>_!==i):[...g,i])},N=async()=>{try{await t({enabled_service_tags:[...l].sort((i,g)=>i-g)}),a(c.MENU)}catch{}};return $.createElement("section",{className:"screen service-types-screen"},$.createElement(I,{title:"Service Types",showBack:!0,onBack:()=>a(c.MENU)}),$.createElement("div",{className:"checkbox-list"},n.map(i=>{let g=Number(i.service_tag),_=l.includes(g);return $.createElement("label",{key:g,className:"row card"},$.createElement("span",null,i.name),$.createElement("input",{type:"checkbox",checked:_,onChange:()=>v(g)}))})),$.createElement("div",{className:"button-row"},$.createElement(E,{onClick:N,disabled:s},"Save")),e.error?$.createElement("div",{className:"error"},e.error):null)}import D,{useEffect as yt,useState as ht}from"https://esm.sh/react@18";function ie(){let{state:e,saveHpState:t,navigate:a}=C(),{hpState:o,working:n}=e,[s,r]=ht(15);yt(()=>{let p=Number(o.range_miles);r(Number.isFinite(p)?p:15)},[o.range_miles]);let l=async()=>{try{await t({range_miles:s}),a(c.MENU)}catch{}};return D.createElement("section",{className:"screen range-screen"},D.createElement(I,{title:"Range",showBack:!0,onBack:()=>a(c.MENU)}),D.createElement("div",{className:"card"},D.createElement("div",{className:"row"},D.createElement("span",null,"Range Miles"),D.createElement("strong",null,s.toFixed(1))),D.createElement("input",{className:"range",type:"range",min:"0",max:"30",step:"0.5",value:s,onChange:p=>r(Number(p.target.value))})),D.createElement("div",{className:"button-row"},D.createElement(E,{onClick:l,disabled:n},"Save")),e.error?D.createElement("div",{className:"error"},e.error):null)}import b,{useEffect as St,useMemo as Ue,useState as Nt}from"https://esm.sh/react@18";function _t(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach((o,n)=>{if(!o||typeof o!="object")return;let s=String(o.id||"").trim(),r=s?s.split(":"):[],l=String(o.type||o.kind||"").trim().toLowerCase(),p=String(o.target||"").trim().toLowerCase(),v=String(o.profile_id||o.profileId||o.profile||"").trim();if(!v&&r.length>0&&(r[0].toLowerCase()==="digital"&&r.length>=2?(l="digital",v=r.slice(1).join(":").trim()):r[0].toLowerCase()==="analog"&&r.length>=3&&(l="analog",p=String(r[1]||"").trim().toLowerCase(),v=r.slice(2).join(":").trim())),!l&&p&&(l="analog"),l==="digital"&&(p=""),l!=="digital"&&l!=="analog"||l==="analog"&&p!=="airband"&&p!=="ground"||!v)return;let N=l==="digital"?`digital:${v}`:`analog:${p}:${v}`;a.has(N)||(a.add(N),t.push({id:N,type:l,target:p,profile_id:v,label:String(o.label||o.name||v),enabled:o.enabled===!0,_index:n}))}),t}function xt(e){return{analog_airband:e.filter(t=>t.type==="analog"&&t.target==="airband").sort((t,a)=>t._index-a._index),analog_ground:e.filter(t=>t.type==="analog"&&t.target==="ground").sort((t,a)=>t._index-a._index),digital:e.filter(t=>t.type==="digital").sort((t,a)=>t._index-a._index)}}function se(){let{state:e,saveHpState:t,navigate:a}=C(),{hpState:o,working:n}=e,s=Ue(()=>Array.isArray(o.favorites)?o.favorites:Array.isArray(o.favorites_list)?o.favorites_list:[],[o.favorites,o.favorites_list]),[r,l]=Nt([]),p=Ue(()=>xt(r),[r]);St(()=>{l(_t(s))},[s]);let v=(i,g)=>{l(_=>_.map(T=>(T.type==="digital"?"digital":`analog_${T.target}`)!==i?T:{...T,enabled:T.profile_id===g}))},N=async()=>{try{await t({favorites:r}),a(c.MENU)}catch{}};return b.createElement("section",{className:"screen favorites-screen"},b.createElement(I,{title:"Favorites",showBack:!0,onBack:()=>a(c.MENU)}),r.length===0?b.createElement("div",{className:"muted"},"No favorites in current state."):b.createElement("div",{className:"list"},b.createElement("div",{className:"card"},b.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Analog Airband"),p.analog_airband.length===0?b.createElement("div",{className:"muted"},"No airband profiles found."):p.analog_airband.map(i=>b.createElement("label",{key:i.id,className:"row",style:{marginBottom:"6px"}},b.createElement("span",null,i.label),b.createElement("input",{type:"radio",name:"favorites-analog-airband",checked:i.enabled,onChange:()=>v("analog_airband",i.profile_id)})))),b.createElement("div",{className:"card"},b.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Analog Ground"),p.analog_ground.length===0?b.createElement("div",{className:"muted"},"No ground profiles found."):p.analog_ground.map(i=>b.createElement("label",{key:i.id,className:"row",style:{marginBottom:"6px"}},b.createElement("span",null,i.label),b.createElement("input",{type:"radio",name:"favorites-analog-ground",checked:i.enabled,onChange:()=>v("analog_ground",i.profile_id)})))),b.createElement("div",{className:"card"},b.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Digital"),p.digital.length===0?b.createElement("div",{className:"muted"},"No digital profiles found."):p.digital.map(i=>b.createElement("label",{key:i.id,className:"row",style:{marginBottom:"6px"}},b.createElement("span",null,i.label),b.createElement("input",{type:"radio",name:"favorites-digital",checked:i.enabled,onChange:()=>v("digital",i.profile_id)}))))),b.createElement("div",{className:"muted",style:{marginTop:"8px"}},"Saving favorites sets the active analog/digital profiles for HP3 playback."),b.createElement("div",{className:"button-row"},b.createElement(E,{onClick:N,disabled:n},"Save")),e.error?b.createElement("div",{className:"error"},e.error):null)}import S,{useEffect as Et,useMemo as $e,useState as At}from"https://esm.sh/react@18";function wt(e){return Array.isArray(e)?e.map((t,a)=>t&&typeof t=="object"?{id:String(t.id??`${t.type||"item"}-${a}`),label:String(t.label||t.alpha_tag||t.name||`Avoid ${a+1}`),type:String(t.type||"item"),source:"persistent"}:{id:`item-${a}`,label:String(t),type:"item",source:"persistent"}):[]}function Tt(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach(o=>{let n=String(o||"").trim();!n||a.has(n)||(a.add(n),t.push({id:`runtime:${n}`,label:n,type:"system",token:n,source:"runtime"}))}),t}function de(){let{state:e,saveHpState:t,avoidCurrent:a,clearHpAvoids:o,removeHpAvoid:n,navigate:s}=C(),{hpState:r,hpAvoids:l,working:p}=e,v=$e(()=>Array.isArray(r.avoid_list)?r.avoid_list:Array.isArray(r.avoids)?r.avoids:Array.isArray(r.avoid)?r.avoid:[],[r.avoid_list,r.avoids,r.avoid]),[N,i]=At([]);Et(()=>{i(wt(v))},[v]);let g=$e(()=>Tt(l),[l]),_=async(y=N)=>{try{await t({avoid_list:y})}catch{}},T=async()=>{try{await a()}catch{}};return S.createElement("section",{className:"screen avoid-screen"},S.createElement(I,{title:"Avoid",showBack:!0,onBack:()=>s(c.MENU)}),S.createElement("div",{className:"list"},S.createElement("div",{className:"card"},S.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Runtime Avoids (HP Scan Pool)"),g.length===0?S.createElement("div",{className:"muted"},"No runtime HP avoids."):g.map(y=>S.createElement("div",{key:y.id,className:"row",style:{marginBottom:"6px"}},S.createElement("div",null,S.createElement("div",null,y.label),S.createElement("div",{className:"muted"},y.type)),S.createElement(E,{variant:"danger",onClick:()=>n(y.token),disabled:p},"Remove")))),S.createElement("div",{className:"card"},S.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Persistent Avoids (State)"),N.length===0?S.createElement("div",{className:"muted"},"No persistent avoids in current state."):N.map(y=>S.createElement("div",{key:y.id,className:"row",style:{marginBottom:"6px"}},S.createElement("div",null,S.createElement("div",null,y.label),S.createElement("div",{className:"muted"},y.type)),S.createElement(E,{variant:"danger",onClick:()=>{let h=N.filter(L=>L.id!==y.id);i(h),_(h)},disabled:p},"Remove"))))),S.createElement("div",{className:"button-row"},S.createElement(E,{onClick:T,disabled:p},"Avoid Current"),S.createElement(E,{variant:"secondary",onClick:async()=>{i([]),await _([]),await o()},disabled:p},"Clear All"),S.createElement(E,{onClick:()=>_(),disabled:p},"Save")),e.error?S.createElement("div",{className:"error"},e.error):null)}import H,{useEffect as Ct,useState as kt}from"https://esm.sh/react@18";function le(){let{state:e,setMode:t,navigate:a}=C(),[o,n]=kt("hp");return Ct(()=>{n(e.mode||"hp")},[e.mode]),H.createElement("section",{className:"screen mode-selection-screen"},H.createElement(I,{title:"Mode Selection",showBack:!0,onBack:()=>a(c.MENU)}),H.createElement("div",{className:"list"},H.createElement("label",{className:"row card"},H.createElement("span",null,"HP Mode"),H.createElement("input",{type:"radio",name:"scan-mode",value:"hp",checked:o==="hp",onChange:r=>n(r.target.value)})),H.createElement("label",{className:"row card"},H.createElement("span",null,"Expert Mode"),H.createElement("input",{type:"radio",name:"scan-mode",value:"expert",checked:o==="expert",onChange:r=>n(r.target.value)}))),H.createElement("div",{className:"button-row"},H.createElement(E,{onClick:async()=>{try{await t(o),a(c.MENU)}catch{}},disabled:e.working},"Save")),e.error?H.createElement("div",{className:"error"},e.error):null)}import It from"https://esm.sh/react@18";function pe({label:e="Loading..."}){return It.createElement("div",{className:"loading"},e)}function ce(){let{state:e}=C();if(e.loading)return G.createElement(pe,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case c.MENU:return G.createElement(re,null);case c.LOCATION:return G.createElement(oe,null);case c.SERVICE_TYPES:return G.createElement(ne,null);case c.RANGE:return G.createElement(ie,null);case c.FAVORITES:return G.createElement(se,null);case c.AVOID:return G.createElement(de,null);case c.MODE_SELECTION:return G.createElement(le,null);case c.MAIN:default:return G.createElement(ae,null)}}var Ot=`
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
`;function ue(){return W.createElement(He,null,W.createElement("div",{className:"app-shell"},W.createElement("style",null,Ot),W.createElement(ce,null)))}var Ge=document.getElementById("root");if(!Ge)throw new Error("Missing #root mount element");Mt(Ge).render(me.createElement(me.StrictMode,null,me.createElement(ue,null)));
