import Zt from"https://esm.sh/react@18";import{createRoot as Xt}from"https://esm.sh/react-dom@18/client";import"https://esm.sh/react@18";import{createContext as gt,useCallback as L,useContext as ft,useEffect as de,useMemo as ht,useRef as bt,useReducer as yt}from"https://esm.sh/react@18";var mt={"Content-Type":"application/json"};async function D(e,{method:a="GET",body:t}={}){let r={method:a,headers:{...mt}};t!==void 0&&(r.body=JSON.stringify(t));let i=await fetch(e,r),s=await i.text(),n={};try{n=s?JSON.parse(s):{}}catch{n={raw:s}}if(!i.ok){let c=n?.error||`Request failed (${i.status})`,p=new Error(c);throw p.status=i.status,p.payload=n,p}return n}function ie(){return D("/api/hp/state")}function Oe(e){return D("/api/hp/state",{method:"POST",body:e})}function se(){return D("/api/hp/service-types")}function le(){return D("/api/hp/avoids")}function Le(){return D("/api/hp/avoids",{method:"POST",body:{action:"clear"}})}function He(e){return D("/api/hp/avoids",{method:"POST",body:{action:"remove",system:e}})}function Be(){return D("/api/status")}function Pe(e){return D("/api/mode",{method:"POST",body:{mode:e}})}function De(e={}){return D("/api/hp/hold",{method:"POST",body:e})}function Fe(e={}){return D("/api/hp/next",{method:"POST",body:e})}function Ue(e={}){return D("/api/hp/avoid",{method:"POST",body:e})}import{jsx as At}from"https://esm.sh/react@18/jsx-runtime";var d=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),St={hpState:{},serviceTypes:[],liveStatus:{},hpAvoids:[],currentScreen:d.MAIN,mode:"hp",sseConnected:!1,loading:!0,working:!1,error:"",message:""},_t=["digital_scheduler_active_system","digital_scheduler_active_system_label","digital_scheduler_next_system","digital_scheduler_next_system_label","digital_scheduler_active_department_label","digital_last_label","digital_channel_label","digital_department_label","digital_system_label","digital_last_mode","digital_last_tgid","digital_profile","digital_scan_mode","stream_mount","digital_stream_mount","profile_airband","profile_ground","last_hit_airband_label","last_hit_ground_label"];function $e(e){return e==null?!1:typeof e=="string"?e.trim()!=="":Array.isArray(e)?e.length>0:!0}function X(e){if(!Array.isArray(e))return[];let a=[],t=new Set;return e.forEach(r=>{let i=String(r||"").trim();!i||t.has(i)||(t.add(i),a.push(i))}),a}function Nt(e,a){let t=a&&typeof a=="object"?a:{},r={...e||{},...t};return _t.forEach(i=>{!$e(t[i])&&$e(e?.[i])&&(r[i]=e[i])}),r}function Et(e,a){switch(a.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:a.payload.hpState||{},serviceTypes:a.payload.serviceTypes||[],liveStatus:a.payload.liveStatus||{},hpAvoids:a.payload.hpAvoids||[],mode:a.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:a.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!a.payload};case"SET_ERROR":return{...e,error:a.payload||""};case"SET_MESSAGE":return{...e,message:a.payload||""};case"SET_HP_STATE":return{...e,hpState:a.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:a.payload||[]};case"SET_HP_AVOIDS":return{...e,hpAvoids:X(a.payload)};case"SET_LIVE_STATUS":return{...e,liveStatus:Nt(e.liveStatus,a.payload),hpAvoids:Array.isArray(a.payload?.hp_avoids)?X(a.payload.hp_avoids):e.hpAvoids};case"SET_MODE":return{...e,mode:a.payload||e.mode};case"SET_SSE_CONNECTED":return{...e,sseConnected:!!a.payload};case"NAVIGATE":return{...e,currentScreen:a.payload||d.MAIN};default:return e}}var Ge=gt(null);function ze(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(t=>({service_tag:Number(t?.service_tag),name:String(t?.name||`Service ${t?.service_tag}`),enabled_by_default:!!t?.enabled_by_default}))}function Ve(e){let a=e&&typeof e.state=="object"&&e.state!==null?e.state:{},t=String(e?.mode||"hp").toLowerCase();return{hpState:a,mode:t}}function qe({children:e}){let[a,t]=yt(Et,St),r=bt(!1),i=L(v=>{t({type:"NAVIGATE",payload:v})},[]),s=L(async()=>{let v=await ie(),m=Ve(v);return t({type:"SET_HP_STATE",payload:m.hpState}),t({type:"SET_MODE",payload:m.mode}),m},[]),n=L(async()=>{let v=await se(),m=ze(v);return t({type:"SET_SERVICE_TYPES",payload:m}),m},[]),c=L(async()=>{let v=await le(),m=X(v?.avoids);return t({type:"SET_HP_AVOIDS",payload:m}),m},[]),p=L(async()=>{if(r.current)return null;r.current=!0;try{let v=await Be();return t({type:"SET_LIVE_STATUS",payload:v||{}}),v}finally{r.current=!1}},[]),b=L(async()=>{t({type:"LOAD_START"});try{let[v,m,x]=await Promise.all([ie(),se(),le()]),o=Ve(v),g=ze(m),k=X(x?.avoids);t({type:"LOAD_SUCCESS",payload:{hpState:o.hpState,mode:o.mode,serviceTypes:g,liveStatus:{},hpAvoids:k}})}catch(v){t({type:"LOAD_ERROR",payload:v.message})}},[]);de(()=>{b()},[b]),de(()=>{let v=setInterval(()=>{p().catch(()=>{})},a.sseConnected?1e4:5e3);return()=>clearInterval(v)},[p,a.sseConnected]),de(()=>{if(typeof EventSource>"u")return;let v=!1,m=null,x=null,o=()=>{v||(m=new EventSource("/api/stream"),m.onopen=()=>{t({type:"SET_SSE_CONNECTED",payload:!0})},m.addEventListener("status",g=>{try{let k=JSON.parse(g?.data||"{}");t({type:"SET_LIVE_STATUS",payload:k})}catch{}}),m.onerror=()=>{t({type:"SET_SSE_CONNECTED",payload:!1}),m&&(m.close(),m=null),v||(x=setTimeout(o,2e3))})};return o(),()=>{v=!0,t({type:"SET_SSE_CONNECTED",payload:!1}),x&&clearTimeout(x),m&&m.close()}},[]);let N=L(async v=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let m={...a.hpState,...v},x=await Oe(m),o=x?.state&&typeof x.state=="object"?{...a.hpState,...x.state}:m;return t({type:"SET_HP_STATE",payload:o}),t({type:"SET_MESSAGE",payload:"State saved"}),x}catch(m){throw t({type:"SET_ERROR",payload:m.message}),m}finally{t({type:"SET_WORKING",payload:!1})}},[a.hpState]),l=L(async v=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let m=await Pe(v),x=String(m?.mode||v||"hp").toLowerCase();return t({type:"SET_MODE",payload:x}),t({type:"SET_MESSAGE",payload:`Mode set to ${x}`}),m}catch(m){throw t({type:"SET_ERROR",payload:m.message}),m}finally{t({type:"SET_WORKING",payload:!1})}},[]),u=L(async(v,m)=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let x=await v();return Array.isArray(x?.avoids)&&t({type:"SET_HP_AVOIDS",payload:x.avoids}),m&&t({type:"SET_MESSAGE",payload:m}),await s(),await p(),x}catch(x){throw t({type:"SET_ERROR",payload:x.message}),x}finally{t({type:"SET_WORKING",payload:!1})}},[s,p]),S=L(async()=>u(()=>De(),"Hold command sent"),[u]),T=L(async()=>u(()=>Fe(),"Next command sent"),[u]),A=L(async(v={})=>u(()=>Ue(v),"Avoid command sent"),[u]),E=L(async()=>u(()=>Le(),"Runtime avoids cleared"),[u]),w=L(async v=>u(()=>He(v),"Avoid removed"),[u]),H=ht(()=>({state:a,dispatch:t,navigate:i,refreshAll:b,refreshHpState:s,refreshServiceTypes:n,refreshHpAvoids:c,refreshStatus:p,saveHpState:N,setMode:l,holdScan:S,nextScan:T,avoidCurrent:A,clearHpAvoids:E,removeHpAvoid:w,SCREENS:d}),[a,i,b,s,n,c,p,N,l,S,T,A,E,w]);return At(Ge.Provider,{value:H,children:e})}function C(){let e=ft(Ge);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import"https://esm.sh/react@18";import{useEffect as Ye,useMemo as Ke,useState as j}from"https://esm.sh/react@18";import"https://esm.sh/react@18";import{jsx as xt}from"https://esm.sh/react@18/jsx-runtime";function _({children:e,onClick:a,type:t="button",variant:r="primary",className:i="",disabled:s=!1}){return xt("button",{type:t,className:`btn ${r==="secondary"?"btn-secondary":r==="danger"?"btn-danger":""} ${i}`.trim(),onClick:a,disabled:s,children:e})}import{jsx as f,jsxs as R}from"https://esm.sh/react@18/jsx-runtime";function B(e){return e==null||e===""?"--":String(e)}function kt(e){let a=Math.max(0,Math.min(4,Number(e)||0));return`${"|".repeat(a)}${".".repeat(4-a)}`}function Ct(e){let a=Number(e);return Number.isFinite(a)?Number.isInteger(a)?`Range ${a}`:`Range ${a.toFixed(1)}`:"Range"}function Tt(e,a){let t=a==="ground"?e?.profile_ground:e?.profile_airband,r=a==="ground"?e?.profiles_ground:e?.profiles_airband,i=Array.isArray(r)?r:[],s=String(t||"").trim();if(!s)return"";let n=i.find(p=>String(p?.id||"").trim().toLowerCase()===s.toLowerCase());return String(n?.label||"").trim()||s}function wt(e,a){let t=String(a||"").trim(),r=String(e||"").trim();if(!r&&!t)return{system:"--",department:"--",channel:"--"};let i=[" | "," - "," / "," \u2014 "," \u2013 ","::"];for(let s of i){if(!r.includes(s))continue;let n=r.split(s).map(c=>String(c||"").trim()).filter(Boolean);if(n.length>=3)return{system:n[0],department:n[1],channel:n.slice(2).join(" / ")};if(n.length===2)return{system:t||n[0],department:n[0],channel:n[1]}}return{system:t||r||"--",department:r||t||"--",channel:r||"--"}}function ce(){let{state:e,holdScan:a,nextScan:t,avoidCurrent:r,navigate:i}=C(),{hpState:s,liveStatus:n,working:c,error:p,message:b}=e,N=String(n?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),l=String(n?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),u=!!N,S=!!l,T=(e.mode==="hp"||e.mode==="expert")&&S?"digital":"analog",[A,E]=j(T),[w,H]=j(""),[v,m]=j(!1),[x,o]=j("");Ye(()=>{if(A==="digital"&&!S){E(u?"analog":"digital");return}A==="analog"&&!u&&S&&E("digital")},[u,S,A]),Ye(()=>{!p&&!b||o("")},[p,b]);let g=A==="digital"?l||N:N||l,k=A==="digital"&&S,z=String(s.mode||"full_database").trim().toLowerCase(),ae=String(n?.profile_airband||"").trim(),at=Tt(n,"airband")||ae||"Analog",nt=n?.last_hit_airband_label||n?.last_hit_ground_label||n?.last_hit||"",J=wt(nt,at),Ae=k?n?.digital_scheduler_active_system_label||n?.digital_system_label||n?.digital_scheduler_active_system||s.system_name||s.system:J.system,Z=k?n?.digital_department_label||n?.digital_scheduler_active_department_label||s.department_name||s.department||n?.digital_last_label:J.department||s.department_name||s.department,xe=k?n?.digital_last_tgid??s.tgid??s.talkgroup_id:"--",ne=k?(()=>{let h=Number(n?.digital_preflight?.playlist_frequency_hz?.[0]||n?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(h)&&h>0?(h/1e6).toFixed(4):s.frequency??s.freq})():n?.last_hit_airband||n?.last_hit_ground||n?.last_hit||"--",ke=!!(n?.digital_control_channel_metric_ready??n?.digital_control_decode_available),Ce=k?n?.digital_control_channel_locked?"Locked":ke?"Decoding":s.signal??s.signal_strength:n?.rtl_active?"Active":"Idle",Te=k?n?.digital_channel_label||n?.digital_last_label||s.channel_name||s.channel||Z:J.channel||Z,re=k&&(n?.digital_last_mode||s.service_type||s.service)||"",we=k?Te:J.channel||Te,Re=k?[B(re||"Digital"),xe!=="--"?`TGID ${B(xe)}`:"",ne!=="--"?`${B(ne)} MHz`:"",Ce].filter(Boolean).join(" \u2022 "):`${B(ne)} \u2022 ${Ce}`,rt=k?n?.digital_control_channel_locked?4:ke?3:1:n?.rtl_active?3:1,Ie=String(n?.digital_scan_mode||"").toLowerCase()==="single_system",ot=Ie?"HOLD":"SCAN",Me=Ke(()=>{if(z!=="favorites")return"Full Database";let h=String(s.favorites_name||"").trim()||"My Favorites";return(Array.isArray(s.custom_favorites)?s.custom_favorites:[]).length===0?`${h} (empty)`:h},[z,s.custom_favorites,s.favorites_name]),it=k?re?`Service: ${B(re)}`:"Service: Digital":z==="favorites"?`List: ${Me}`:"Full Database",st=async()=>{try{await a()}catch{}},lt=async()=>{try{await t()}catch{}},dt=async()=>{try{await r()}catch{}},ct=async(h,oe)=>{if(h==="info"){o(oe==="system"?`System: ${B(Ae)}`:oe==="department"?`Department: ${B(Z)}`:`Channel: ${B(we)} (${B(Re)})`),H("");return}if(h==="advanced"){o("Advanced options are still being wired in HP3."),H("");return}if(h==="prev"){o("Previous-channel stepping is not wired yet in HP3."),H("");return}if(h==="fave"){H(""),i(d.FAVORITES);return}if(!k){o("Switch Audio Source to Digital for HOLD/NEXT/AVOID controls."),H("");return}h==="hold"?await st():h==="next"?await lt():h==="avoid"&&await dt(),H("")},pt=Ke(()=>[{id:"squelch",label:"Squelch",onClick:()=>o("Squelch is currently managed from SB3 analog controls.")},{id:"range",label:Ct(s.range_miles),onClick:()=>i(d.RANGE)},{id:"atten",label:"Atten",onClick:()=>o("Attenuation toggle is not wired yet in HP3.")},{id:"gps",label:"GPS",onClick:()=>i(d.LOCATION)},{id:"help",label:"Help",onClick:()=>i(d.MENU)}],[s.range_miles,i]),ut={system:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],department:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],channel:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"hold",label:"Hold"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"},{id:"fave",label:"Fave"}]};return R("section",{className:"screen main-screen hp2-main",children:[R("div",{className:"hp2-radio-bar",children:[f("div",{className:"hp2-radio-buttons",children:pt.map(h=>f("button",{type:"button",className:"hp2-radio-btn",onClick:h.onClick,disabled:c,children:h.label},h.id))}),R("div",{className:"hp2-status-icons",children:[f("span",{className:`hp2-icon ${Ie?"on":""}`,children:ot}),R("span",{className:"hp2-icon",children:["SIG ",kt(rt)]}),f("span",{className:"hp2-icon",children:k?"DIG":"ANA"})]})]}),R("div",{className:"hp2-lines",children:[R("div",{className:"hp2-line",children:[f("div",{className:"hp2-line-label",children:"System / Favorite List"}),R("div",{className:"hp2-line-body",children:[f("div",{className:"hp2-line-primary",children:B(Ae)}),f("div",{className:"hp2-line-secondary",children:Me})]}),f("button",{type:"button",className:"hp2-subtab",onClick:()=>H(h=>h==="system"?"":"system"),disabled:c,children:"<"})]}),R("div",{className:"hp2-line",children:[f("div",{className:"hp2-line-label",children:"Department"}),R("div",{className:"hp2-line-body",children:[f("div",{className:"hp2-line-primary",children:B(Z)}),f("div",{className:"hp2-line-secondary",children:it})]}),f("button",{type:"button",className:"hp2-subtab",onClick:()=>H(h=>h==="department"?"":"department"),disabled:c,children:"<"})]}),R("div",{className:"hp2-line channel",children:[f("div",{className:"hp2-line-label",children:"Channel"}),R("div",{className:"hp2-line-body",children:[f("div",{className:"hp2-line-primary",children:B(we)}),f("div",{className:"hp2-line-secondary",children:B(Re)})]}),f("button",{type:"button",className:"hp2-subtab",onClick:()=>H(h=>h==="channel"?"":"channel"),disabled:c,children:"<"})]})]}),w?f("div",{className:"hp2-submenu-popup",children:ut[w]?.map(h=>f("button",{type:"button",className:"hp2-submenu-btn",onClick:()=>ct(h.id,w),disabled:c,children:h.label},h.id))}):null,R("div",{className:"hp2-feature-bar",children:[f("button",{type:"button",className:"hp2-feature-btn",onClick:()=>i(d.MENU),disabled:c,children:"Menu"}),f("button",{type:"button",className:"hp2-feature-btn",onClick:()=>o("Replay is not wired yet in HP3."),disabled:c,children:"Replay"}),f("button",{type:"button",className:"hp2-feature-btn",onClick:()=>o("Recording controls are not wired yet in HP3."),disabled:c,children:"Record"}),f("button",{type:"button",className:"hp2-feature-btn",onClick:()=>m(h=>!h),disabled:c,children:v?"Unmute":"Mute"})]}),R("div",{className:"hp2-web-audio",children:[R("div",{className:"hp2-audio-head",children:[f("div",{className:"muted",children:"Web Audio Stream"}),g?f("a",{href:`/stream/${g}`,target:"_blank",rel:"noreferrer",children:"Open"}):null]}),R("div",{className:"hp2-source-switch",children:[f(_,{variant:A==="analog"?"primary":"secondary",onClick:()=>E("analog"),disabled:!u||c,children:"Analog"}),f(_,{variant:A==="digital"?"primary":"secondary",onClick:()=>E("digital"),disabled:!S||c,children:"Digital"})]}),R("div",{className:"muted hp2-audio-meta",children:["Source: ",k?"Digital":"Analog"," (",g||"no mount",")"]}),f("audio",{controls:!0,preload:"none",muted:v,className:"hp2-audio-player",src:g?`/stream/${g}`:"/stream/"})]}),x?f("div",{className:"message",children:x}):null,k?null:f("div",{className:"muted",children:"HOLD/NEXT/AVOID actions require Digital source."}),p?f("div",{className:"error",children:p}):null,b?f("div",{className:"message",children:b}):null]})}import"https://esm.sh/react@18";import"https://esm.sh/react@18";import{jsx as pe,jsxs as We}from"https://esm.sh/react@18/jsx-runtime";function I({title:e,subtitle:a="",showBack:t=!1,onBack:r}){return We("div",{className:"header",children:[We("div",{children:[pe("h1",{children:e}),a?pe("div",{className:"sub",children:a}):null]}),t?pe("button",{type:"button",className:"btn btn-secondary",onClick:r,children:"Back"}):null]})}import{jsx as Q,jsxs as It}from"https://esm.sh/react@18/jsx-runtime";var Rt=[{id:d.LOCATION,label:"Set Your Location"},{id:d.SERVICE_TYPES,label:"Select Service Types"},{id:d.RANGE,label:"Set Range"},{id:d.FAVORITES,label:"Manage Favorites"},{id:d.AVOID,label:"Avoid Options"},{id:d.MODE_SELECTION,label:"Mode Selection"}];function ue(){let{navigate:e,state:a}=C();return It("section",{className:"screen menu",children:[Q(I,{title:"Menu",showBack:!0,onBack:()=>e(d.MAIN)}),Q("div",{className:"menu-list",children:Rt.map(t=>Q(_,{variant:"secondary",className:"menu-item",onClick:()=>e(t.id),disabled:a.working,children:t.label},t.id))}),a.error?Q("div",{className:"error",children:a.error}):null]})}import{useEffect as Mt,useState as K}from"https://esm.sh/react@18";import{jsx as P,jsxs as q}from"https://esm.sh/react@18/jsx-runtime";function Je(e){if(e===""||e===null||e===void 0)return null;let a=Number(e);return Number.isFinite(a)?a:NaN}function me(){let{state:e,saveHpState:a,navigate:t}=C(),{hpState:r,working:i}=e,[s,n]=K(""),[c,p]=K(""),[b,N]=K(""),[l,u]=K(!0),[S,T]=K("");return Mt(()=>{n(r.zip||r.postal_code||""),p(r.lat!==void 0&&r.lat!==null?String(r.lat):r.latitude!==void 0&&r.latitude!==null?String(r.latitude):""),N(r.lon!==void 0&&r.lon!==null?String(r.lon):r.longitude!==void 0&&r.longitude!==null?String(r.longitude):""),u(r.use_location!==!1)},[r]),q("section",{className:"screen location-screen",children:[P(I,{title:"Location",showBack:!0,onBack:()=>t(d.MENU)}),q("div",{className:"list",children:[q("label",{children:[P("div",{className:"muted",children:"ZIP"}),P("input",{className:"input",value:s,onChange:E=>n(E.target.value.trim()),placeholder:"37201"})]}),q("label",{children:[P("div",{className:"muted",children:"Latitude"}),P("input",{className:"input",value:c,onChange:E=>p(E.target.value),placeholder:"36.12"})]}),q("label",{children:[P("div",{className:"muted",children:"Longitude"}),P("input",{className:"input",value:b,onChange:E=>N(E.target.value),placeholder:"-86.67"})]}),q("label",{className:"row",children:[P("span",{children:"Use location for scanning"}),P("input",{type:"checkbox",checked:l,onChange:E=>u(E.target.checked)})]})]}),P("div",{className:"button-row",children:P(_,{onClick:async()=>{if(T(""),s&&!/^\d{5}(-\d{4})?$/.test(s)){T("ZIP must be 5 digits or ZIP+4.");return}let E=Je(c),w=Je(b);if(Number.isNaN(E)||Number.isNaN(w)){T("Latitude and longitude must be valid numbers.");return}if(E!==null&&(E<-90||E>90)){T("Latitude must be between -90 and 90.");return}if(w!==null&&(w<-180||w>180)){T("Longitude must be between -180 and 180.");return}try{await a({zip:s,lat:E,lon:w,use_location:l}),t(d.MENU)}catch{}},disabled:i,children:"Save"})}),S?P("div",{className:"error",children:S}):null,e.error?P("div",{className:"error",children:e.error}):null]})}import{useEffect as Ot,useMemo as Lt,useState as Ht}from"https://esm.sh/react@18";import{jsx as V,jsxs as Ze}from"https://esm.sh/react@18/jsx-runtime";function ve(){let{state:e,saveHpState:a,navigate:t}=C(),{hpState:r,serviceTypes:i,working:s}=e,n=Lt(()=>i.filter(l=>l.enabled_by_default).map(l=>Number(l.service_tag)),[i]),[c,p]=Ht([]);Ot(()=>{let l=Array.isArray(r.enabled_service_tags)?r.enabled_service_tags.map(Number):n;p(Array.from(new Set(l)).filter(u=>Number.isFinite(u)))},[r.enabled_service_tags,n]);let b=l=>{p(u=>u.includes(l)?u.filter(S=>S!==l):[...u,l])},N=async()=>{try{await a({enabled_service_tags:[...c].sort((l,u)=>l-u)}),t(d.MENU)}catch{}};return Ze("section",{className:"screen service-types-screen",children:[V(I,{title:"Service Types",showBack:!0,onBack:()=>t(d.MENU)}),V("div",{className:"checkbox-list",children:i.map(l=>{let u=Number(l.service_tag),S=c.includes(u);return Ze("label",{className:"row card",children:[V("span",{children:l.name}),V("input",{type:"checkbox",checked:S,onChange:()=>b(u)})]},u)})}),V("div",{className:"button-row",children:V(_,{onClick:N,disabled:s,children:"Save"})}),e.error?V("div",{className:"error",children:e.error}):null]})}import{useEffect as Bt,useState as Pt}from"https://esm.sh/react@18";import{jsx as G,jsxs as ge}from"https://esm.sh/react@18/jsx-runtime";function fe(){let{state:e,saveHpState:a,navigate:t}=C(),{hpState:r,working:i}=e,[s,n]=Pt(15);Bt(()=>{let p=Number(r.range_miles);n(Number.isFinite(p)?p:15)},[r.range_miles]);let c=async()=>{try{await a({range_miles:s}),t(d.MENU)}catch{}};return ge("section",{className:"screen range-screen",children:[G(I,{title:"Range",showBack:!0,onBack:()=>t(d.MENU)}),ge("div",{className:"card",children:[ge("div",{className:"row",children:[G("span",{children:"Range Miles"}),G("strong",{children:s.toFixed(1)})]}),G("input",{className:"range",type:"range",min:"0",max:"30",step:"0.5",value:s,onChange:p=>n(Number(p.target.value))})]}),G("div",{className:"button-row",children:G(_,{onClick:c,disabled:i,children:"Save"})}),e.error?G("div",{className:"error",children:e.error}):null]})}import{useEffect as Dt,useMemo as Xe,useState as W}from"https://esm.sh/react@18";import{jsx as y,jsxs as M}from"https://esm.sh/react@18/jsx-runtime";function ee(e){let a=Number(String(e||"").trim());return Number.isFinite(a)?a:null}function Y(e){let a=Number.parseInt(String(e||"").trim(),10);return Number.isFinite(a)?a:null}function Ft(e){let a=String(e||"").split(/[,\s]+/).map(i=>i.trim()).filter(Boolean),t=new Set,r=[];return a.forEach(i=>{let s=ee(i);if(s===null||s<=0)return;let n=Number(s.toFixed(6));t.has(n)||(t.add(n),r.push(n))}),r.sort((i,s)=>i-s)}function Ut(e){if(!Array.isArray(e))return[];let a=[];return e.forEach((t,r)=>{if(!t||typeof t!="object")return;let i=String(t.kind||"").trim().toLowerCase();if(i!=="trunked"&&i!=="conventional")return;let s=String(t.id||`fav-${r+1}`).trim()||`fav-${r+1}`;if(i==="trunked"){let c=Y(t.talkgroup||t.tgid),p=Array.isArray(t.control_channels)?t.control_channels.map(b=>ee(b)).filter(b=>b!==null&&b>0).map(b=>Number(b.toFixed(6))):[];if(c===null||c<=0||p.length===0)return;a.push({id:s,kind:"trunked",system_name:String(t.system_name||"").trim(),department_name:String(t.department_name||"").trim(),alpha_tag:String(t.alpha_tag||t.channel_name||"").trim(),talkgroup:String(c),service_tag:Y(t.service_tag)||0,control_channels:Array.from(new Set(p)).sort((b,N)=>b-N)});return}let n=ee(t.frequency);n===null||n<=0||a.push({id:s,kind:"conventional",alpha_tag:String(t.alpha_tag||t.channel_name||"").trim(),frequency:Number(n.toFixed(6)),service_tag:Y(t.service_tag)||0})}),a}function je(e){let a=Math.random().toString(16).slice(2,8);return`${e}-${Date.now()}-${a}`}function he(){let{state:e,saveHpState:a,navigate:t}=C(),{hpState:r,working:i}=e,[s,n]=W("My Favorites"),[c,p]=W([]),[b,N]=W(""),[l,u]=W({system_name:"",department_name:"",alpha_tag:"",talkgroup:"",service_tag:"",control_channels:""}),[S,T]=W({alpha_tag:"",frequency:"",service_tag:""});Dt(()=>{n(String(r.favorites_name||"My Favorites").trim()||"My Favorites"),p(Ut(r.custom_favorites))},[r.favorites_name,r.custom_favorites]);let A=Xe(()=>c.filter(o=>o.kind==="trunked"),[c]),E=Xe(()=>c.filter(o=>o.kind==="conventional"),[c]),w=o=>{p(g=>g.filter(k=>k.id!==o))},H=()=>{N("");let o=Y(l.talkgroup);if(o===null||o<=0){N("Trunked talkgroup must be a positive integer.");return}let g=Ft(l.control_channels);if(g.length===0){N("At least one trunked control channel is required.");return}let k=Y(l.service_tag)||0,z={id:je("trunk"),kind:"trunked",system_name:String(l.system_name||"").trim(),department_name:String(l.department_name||"").trim(),alpha_tag:String(l.alpha_tag||"").trim(),talkgroup:String(o),service_tag:k,control_channels:g};p(ae=>[...ae,z]),u({system_name:l.system_name,department_name:l.department_name,alpha_tag:"",talkgroup:"",service_tag:l.service_tag,control_channels:l.control_channels})},v=()=>{N("");let o=ee(S.frequency);if(o===null||o<=0){N("Conventional frequency must be a positive number.");return}let g=Y(S.service_tag)||0,k={id:je("conv"),kind:"conventional",alpha_tag:String(S.alpha_tag||"").trim(),frequency:Number(o.toFixed(6)),service_tag:g};p(z=>[...z,k]),T({alpha_tag:"",frequency:"",service_tag:S.service_tag})},m=async()=>{N("");let o=String(s||"").trim()||"My Favorites";try{await a({mode:"favorites",favorites_name:o,custom_favorites:c}),t(d.MENU)}catch{}},x=async()=>{N("");try{await a({mode:"full_database"}),t(d.MENU)}catch{}};return M("section",{className:"screen favorites-screen",children:[y(I,{title:"Favorites",showBack:!0,onBack:()=>t(d.MENU)}),M("div",{className:"card",children:[y("div",{className:"muted",style:{marginBottom:"8px"},children:"Favorites List Name"}),y("input",{className:"input",value:s,onChange:o=>n(o.target.value),placeholder:"My Favorites"})]}),M("div",{className:"card",children:[y("div",{className:"muted",style:{marginBottom:"8px"},children:"Add Trunked Favorite"}),y("input",{className:"input",value:l.system_name,onChange:o=>u(g=>({...g,system_name:o.target.value})),placeholder:"System name"}),y("input",{className:"input",value:l.department_name,onChange:o=>u(g=>({...g,department_name:o.target.value})),placeholder:"Department name"}),y("input",{className:"input",value:l.alpha_tag,onChange:o=>u(g=>({...g,alpha_tag:o.target.value})),placeholder:"Channel label (alpha tag)"}),y("input",{className:"input",value:l.talkgroup,onChange:o=>u(g=>({...g,talkgroup:o.target.value})),placeholder:"Talkgroup (decimal)"}),y("input",{className:"input",value:l.control_channels,onChange:o=>u(g=>({...g,control_channels:o.target.value})),placeholder:"Control channels MHz (comma separated)"}),y("input",{className:"input",value:l.service_tag,onChange:o=>u(g=>({...g,service_tag:o.target.value})),placeholder:"Service tag (optional)"}),y("div",{className:"button-row",children:y(_,{onClick:H,disabled:i,children:"Add Trunked"})})]}),M("div",{className:"card",children:[y("div",{className:"muted",style:{marginBottom:"8px"},children:"Add Conventional Favorite"}),y("input",{className:"input",value:S.alpha_tag,onChange:o=>T(g=>({...g,alpha_tag:o.target.value})),placeholder:"Channel label (alpha tag)"}),y("input",{className:"input",value:S.frequency,onChange:o=>T(g=>({...g,frequency:o.target.value})),placeholder:"Frequency MHz"}),y("input",{className:"input",value:S.service_tag,onChange:o=>T(g=>({...g,service_tag:o.target.value})),placeholder:"Service tag (optional)"}),y("div",{className:"button-row",children:y(_,{onClick:v,disabled:i,children:"Add Conventional"})})]}),M("div",{className:"card",children:[M("div",{className:"muted",style:{marginBottom:"8px"},children:["Current Favorites (",c.length,")"]}),c.length===0?y("div",{className:"muted",children:"No custom favorites yet."}):M("div",{className:"list",children:[A.map(o=>M("div",{className:"row",style:{marginBottom:"8px"},children:[M("div",{children:[y("div",{children:y("strong",{children:o.system_name||"Custom Trunked"})}),M("div",{className:"muted",children:[o.department_name||"Department"," - TGID ",o.talkgroup]}),M("div",{className:"muted",children:[o.control_channels.join(", ")," MHz"]})]}),y(_,{variant:"danger",onClick:()=>w(o.id),disabled:i,children:"Remove"})]},o.id)),E.map(o=>M("div",{className:"row",style:{marginBottom:"8px"},children:[M("div",{children:[y("div",{children:y("strong",{children:o.alpha_tag||"Conventional"})}),M("div",{className:"muted",children:[o.frequency.toFixed(4)," MHz",o.service_tag>0?` - Service ${o.service_tag}`:""]})]}),y(_,{variant:"danger",onClick:()=>w(o.id),disabled:i,children:"Remove"})]},o.id))]})]}),M("div",{className:"button-row",children:[y(_,{onClick:m,disabled:i,children:"Save Favorites Mode"}),y(_,{variant:"secondary",onClick:x,disabled:i,children:"Use Full Database"})]}),b?y("div",{className:"error",children:b}):null,e.error?y("div",{className:"error",children:e.error}):null]})}import{useEffect as $t,useMemo as Qe,useState as zt}from"https://esm.sh/react@18";import{jsx as O,jsxs as F}from"https://esm.sh/react@18/jsx-runtime";function Vt(e){return Array.isArray(e)?e.map((a,t)=>a&&typeof a=="object"?{id:String(a.id??`${a.type||"item"}-${t}`),label:String(a.label||a.alpha_tag||a.name||`Avoid ${t+1}`),type:String(a.type||"item"),source:"persistent"}:{id:`item-${t}`,label:String(a),type:"item",source:"persistent"}):[]}function Gt(e){if(!Array.isArray(e))return[];let a=[],t=new Set;return e.forEach(r=>{let i=String(r||"").trim();!i||t.has(i)||(t.add(i),a.push({id:`runtime:${i}`,label:i,type:"system",token:i,source:"runtime"}))}),a}function be(){let{state:e,saveHpState:a,avoidCurrent:t,clearHpAvoids:r,removeHpAvoid:i,navigate:s}=C(),{hpState:n,hpAvoids:c,working:p}=e,b=Qe(()=>Array.isArray(n.avoid_list)?n.avoid_list:Array.isArray(n.avoids)?n.avoids:Array.isArray(n.avoid)?n.avoid:[],[n.avoid_list,n.avoids,n.avoid]),[N,l]=zt([]);$t(()=>{l(Vt(b))},[b]);let u=Qe(()=>Gt(c),[c]),S=async(A=N)=>{try{await a({avoid_list:A})}catch{}},T=async()=>{try{await t()}catch{}};return F("section",{className:"screen avoid-screen",children:[O(I,{title:"Avoid",showBack:!0,onBack:()=>s(d.MENU)}),F("div",{className:"list",children:[F("div",{className:"card",children:[O("div",{className:"muted",style:{marginBottom:"8px"},children:"Runtime Avoids (HP Scan Pool)"}),u.length===0?O("div",{className:"muted",children:"No runtime HP avoids."}):u.map(A=>F("div",{className:"row",style:{marginBottom:"6px"},children:[F("div",{children:[O("div",{children:A.label}),O("div",{className:"muted",children:A.type})]}),O(_,{variant:"danger",onClick:()=>i(A.token),disabled:p,children:"Remove"})]},A.id))]}),F("div",{className:"card",children:[O("div",{className:"muted",style:{marginBottom:"8px"},children:"Persistent Avoids (State)"}),N.length===0?O("div",{className:"muted",children:"No persistent avoids in current state."}):N.map(A=>F("div",{className:"row",style:{marginBottom:"6px"},children:[F("div",{children:[O("div",{children:A.label}),O("div",{className:"muted",children:A.type})]}),O(_,{variant:"danger",onClick:()=>{let E=N.filter(w=>w.id!==A.id);l(E),S(E)},disabled:p,children:"Remove"})]},A.id))]})]}),F("div",{className:"button-row",children:[O(_,{onClick:T,disabled:p,children:"Avoid Current"}),O(_,{variant:"secondary",onClick:async()=>{l([]),await S([]),await r()},disabled:p,children:"Clear All"}),O(_,{onClick:()=>S(),disabled:p,children:"Save"})]}),e.error?O("div",{className:"error",children:e.error}):null]})}import{useEffect as qt,useState as Yt}from"https://esm.sh/react@18";import{jsx as $,jsxs as te}from"https://esm.sh/react@18/jsx-runtime";function ye(){let{state:e,setMode:a,navigate:t}=C(),[r,i]=Yt("hp");return qt(()=>{i(e.mode||"hp")},[e.mode]),te("section",{className:"screen mode-selection-screen",children:[$(I,{title:"Mode Selection",showBack:!0,onBack:()=>t(d.MENU)}),te("div",{className:"list",children:[te("label",{className:"row card",children:[$("span",{children:"HP Mode"}),$("input",{type:"radio",name:"scan-mode",value:"hp",checked:r==="hp",onChange:n=>i(n.target.value)})]}),te("label",{className:"row card",children:[$("span",{children:"Expert Mode"}),$("input",{type:"radio",name:"scan-mode",value:"expert",checked:r==="expert",onChange:n=>i(n.target.value)})]})]}),$("div",{className:"button-row",children:$(_,{onClick:async()=>{try{await a(r),t(d.MENU)}catch{}},disabled:e.working,children:"Save"})}),e.error?$("div",{className:"error",children:e.error}):null]})}import"https://esm.sh/react@18";import{jsx as Kt}from"https://esm.sh/react@18/jsx-runtime";function Se({label:e="Loading..."}){return Kt("div",{className:"loading",children:e})}import{jsx as U}from"https://esm.sh/react@18/jsx-runtime";function _e(){let{state:e}=C();if(e.loading)return U(Se,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case d.MENU:return U(ue,{});case d.LOCATION:return U(me,{});case d.SERVICE_TYPES:return U(ve,{});case d.RANGE:return U(fe,{});case d.FAVORITES:return U(he,{});case d.AVOID:return U(be,{});case d.MODE_SELECTION:return U(ye,{});case d.MAIN:default:return U(ce,{})}}import{jsx as Ne,jsxs as Jt}from"https://esm.sh/react@18/jsx-runtime";var Wt=`
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
`;function Ee(){return Ne(qe,{children:Jt("div",{className:"app-shell",children:[Ne("style",{children:Wt}),Ne(_e,{})]})})}import{jsx as et}from"https://esm.sh/react@18/jsx-runtime";var tt=document.getElementById("root");if(!tt)throw new Error("Missing #root mount element");Xt(tt).render(et(Zt.StrictMode,{children:et(Ee,{})}));
