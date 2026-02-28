import va from"https://esm.sh/react@18";import{createRoot as ya}from"https://esm.sh/react-dom@18/client";import"https://esm.sh/react@18";import{createContext as Dt,useCallback as D,useContext as Ht,useEffect as he,useMemo as Ft,useRef as Bt,useReducer as zt}from"https://esm.sh/react@18";var Ot={"Content-Type":"application/json"};async function B(e,{method:t="GET",body:a}={}){let r={method:t,headers:{...Ot}};a!==void 0&&(r.body=JSON.stringify(a));let s=await fetch(e,r),n=await s.text(),i={};try{i=n?JSON.parse(n):{}}catch{i={raw:n}}if(!s.ok){let p=i?.error||`Request failed (${s.status})`,m=new Error(p);throw m.status=s.status,m.payload=i,m}return i}function ue(){return B("/api/hp/state")}function Ze(e){return B("/api/hp/state",{method:"POST",body:e})}function me(){return B("/api/hp/service-types")}function fe(){return B("/api/hp/avoids")}function Je(){return B("/api/hp/avoids",{method:"POST",body:{action:"clear"}})}function Xe(e){return B("/api/hp/avoids",{method:"POST",body:{action:"remove",system:e}})}function je(){return B("/api/status")}function Qe(e){return B("/api/mode",{method:"POST",body:{mode:e}})}function et(e={}){return B("/api/hp/hold",{method:"POST",body:e})}function tt(e={}){return B("/api/hp/next",{method:"POST",body:e})}function at(e={}){return B("/api/hp/avoid",{method:"POST",body:e})}import{jsx as Wt}from"https://esm.sh/react@18/jsx-runtime";var d=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),$t={hpState:{},serviceTypes:[],liveStatus:{},hpAvoids:[],currentScreen:d.MAIN,mode:"hp",sseConnected:!1,loading:!0,working:!1,error:"",message:""},Ut=["digital_scheduler_active_system","digital_scheduler_active_system_label","digital_scheduler_next_system","digital_scheduler_next_system_label","digital_scheduler_active_department_label","digital_last_label","digital_channel_label","digital_department_label","digital_system_label","digital_last_mode","digital_last_tgid","digital_profile","digital_scan_mode","stream_mount","digital_stream_mount","profile_airband","profile_ground","last_hit_airband_label","last_hit_ground_label"];function it(e){return e==null?!1:typeof e=="string"?e.trim()!=="":Array.isArray(e)?e.length>0:!0}function ie(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach(r=>{let s=String(r||"").trim();!s||a.has(s)||(a.add(s),t.push(s))}),t}function Gt(e,t){let a=t&&typeof t=="object"?t:{},r={...e||{},...a};return Ut.forEach(s=>{!it(a[s])&&it(e?.[s])&&(r[s]=e[s])}),r}function Vt(e,t){switch(t.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:t.payload.hpState||{},serviceTypes:t.payload.serviceTypes||[],liveStatus:t.payload.liveStatus||{},hpAvoids:t.payload.hpAvoids||[],mode:t.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:t.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!t.payload};case"SET_ERROR":return{...e,error:t.payload||""};case"SET_MESSAGE":return{...e,message:t.payload||""};case"SET_HP_STATE":return{...e,hpState:t.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:t.payload||[]};case"SET_HP_AVOIDS":return{...e,hpAvoids:ie(t.payload)};case"SET_LIVE_STATUS":return{...e,liveStatus:Gt(e.liveStatus,t.payload),hpAvoids:Array.isArray(t.payload?.hp_avoids)?ie(t.payload.hp_avoids):e.hpAvoids};case"SET_MODE":return{...e,mode:t.payload||e.mode};case"SET_SSE_CONNECTED":return{...e,sseConnected:!!t.payload};case"NAVIGATE":return{...e,currentScreen:t.payload||d.MAIN};default:return e}}var ot=Dt(null);function rt(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(a=>({service_tag:Number(a?.service_tag),name:String(a?.name||`Service ${a?.service_tag}`),enabled_by_default:!!a?.enabled_by_default}))}function nt(e){let t=e&&typeof e.state=="object"&&e.state!==null?e.state:{},a=String(e?.mode||"hp").toLowerCase();return{hpState:t,mode:a}}function st({children:e}){let[t,a]=zt(Vt,$t),r=Bt(!1),s=D(c=>{a({type:"NAVIGATE",payload:c})},[]),n=D(async()=>{let c=await ue(),o=nt(c);return a({type:"SET_HP_STATE",payload:o.hpState}),a({type:"SET_MODE",payload:o.mode}),o},[]),i=D(async()=>{let c=await me(),o=rt(c);return a({type:"SET_SERVICE_TYPES",payload:o}),o},[]),p=D(async()=>{let c=await fe(),o=ie(c?.avoids);return a({type:"SET_HP_AVOIDS",payload:o}),o},[]),m=D(async()=>{if(r.current)return null;r.current=!0;try{let c=await je();return a({type:"SET_LIVE_STATUS",payload:c||{}}),c}finally{r.current=!1}},[]),A=D(async()=>{a({type:"LOAD_START"});try{let[c,o,u]=await Promise.all([ue(),me(),fe()]),f=nt(c),l=rt(o),_=ie(u?.avoids);a({type:"LOAD_SUCCESS",payload:{hpState:f.hpState,mode:f.mode,serviceTypes:l,liveStatus:{},hpAvoids:_}})}catch(c){a({type:"LOAD_ERROR",payload:c.message})}},[]);he(()=>{A()},[A]),he(()=>{let c=setInterval(()=>{m().catch(()=>{})},t.sseConnected?1e4:5e3);return()=>clearInterval(c)},[m,t.sseConnected]),he(()=>{if(typeof EventSource>"u")return;let c=!1,o=null,u=null,f=()=>{c||(o=new EventSource("/api/stream"),o.onopen=()=>{a({type:"SET_SSE_CONNECTED",payload:!0})},o.addEventListener("status",l=>{try{let _=JSON.parse(l?.data||"{}");a({type:"SET_LIVE_STATUS",payload:_})}catch{}}),o.onerror=()=>{a({type:"SET_SSE_CONNECTED",payload:!1}),o&&(o.close(),o=null),c||(u=setTimeout(f,2e3))})};return f(),()=>{c=!0,a({type:"SET_SSE_CONNECTED",payload:!1}),u&&clearTimeout(u),o&&o.close()}},[]);let S=D(async c=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let o=c&&typeof c=="object"?{...c}:{},u=await Ze(o),f=u?.state&&typeof u.state=="object"?{...t.hpState,...u.state}:{...t.hpState,...o};return a({type:"SET_HP_STATE",payload:f}),a({type:"SET_MESSAGE",payload:"State saved"}),u}catch(o){throw a({type:"SET_ERROR",payload:o.message}),o}finally{a({type:"SET_WORKING",payload:!1})}},[t.hpState]),C=D(async c=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let o=await Qe(c),u=String(o?.mode||c||"hp").toLowerCase();return a({type:"SET_MODE",payload:u}),a({type:"SET_MESSAGE",payload:`Mode set to ${u}`}),o}catch(o){throw a({type:"SET_ERROR",payload:o.message}),o}finally{a({type:"SET_WORKING",payload:!1})}},[]),v=D(async(c,o)=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let u=await c();return Array.isArray(u?.avoids)&&a({type:"SET_HP_AVOIDS",payload:u.avoids}),o&&a({type:"SET_MESSAGE",payload:o}),await n(),await m(),u}catch(u){throw a({type:"SET_ERROR",payload:u.message}),u}finally{a({type:"SET_WORKING",payload:!1})}},[n,m]),N=D(async()=>v(()=>et(),"Hold command sent"),[v]),k=D(async()=>v(()=>tt(),"Next command sent"),[v]),g=D(async(c={})=>v(()=>at(c),"Avoid command sent"),[v]),h=D(async()=>v(()=>Je(),"Runtime avoids cleared"),[v]),w=D(async c=>v(()=>Xe(c),"Avoid removed"),[v]),T=Ft(()=>({state:t,dispatch:a,navigate:s,refreshAll:A,refreshHpState:n,refreshServiceTypes:i,refreshHpAvoids:p,refreshStatus:m,saveHpState:S,setMode:C,holdScan:N,nextScan:k,avoidCurrent:g,clearHpAvoids:h,removeHpAvoid:w,SCREENS:d}),[t,s,A,n,i,p,m,S,C,N,k,g,h,w]);return Wt(ot.Provider,{value:T,children:e})}function I(){let e=Ht(ot);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import"https://esm.sh/react@18";import{useEffect as ge,useMemo as lt,useState as J}from"https://esm.sh/react@18";import"https://esm.sh/react@18";import{jsx as qt}from"https://esm.sh/react@18/jsx-runtime";function M({children:e,onClick:t,type:a="button",variant:r="primary",className:s="",disabled:n=!1}){return qt("button",{type:a,className:`btn ${r==="secondary"?"btn-secondary":r==="danger"?"btn-danger":""} ${s}`.trim(),onClick:t,disabled:n,children:e})}import{jsx as y,jsxs as R}from"https://esm.sh/react@18/jsx-runtime";var dt=["|","/","-","\\"],Yt=6e3;function H(e){return e==null||e===""?"--":String(e)}function Kt(e){let t=Number(e);return!Number.isFinite(t)||t<=0?0:t<1e10?t*1e3:t}function Zt(e){let t=Math.max(0,Math.min(4,Number(e)||0));return`${"|".repeat(t)}${".".repeat(4-t)}`}function Jt(e){let t=Number(e);return Number.isFinite(t)?Number.isInteger(t)?`Range ${t}`:`Range ${t.toFixed(1)}`:"Range"}function Xt(e,t){let a=t==="ground"?e?.profile_ground:e?.profile_airband,r=t==="ground"?e?.profiles_ground:e?.profiles_airband,s=Array.isArray(r)?r:[],n=String(a||"").trim();if(!n)return"";let i=s.find(m=>String(m?.id||"").trim().toLowerCase()===n.toLowerCase());return String(i?.label||"").trim()||n}function jt(e,t){let a=String(t||"").trim(),r=String(e||"").trim();if(!r&&!a)return{system:"--",department:"--",channel:"--"};let s=[" | "," - "," / "," \u2014 "," \u2013 ","::"];for(let n of s){if(!r.includes(n))continue;let i=r.split(n).map(p=>String(p||"").trim()).filter(Boolean);if(i.length>=3)return{system:i[0],department:i[1],channel:i.slice(2).join(" / ")};if(i.length===2)return{system:a||i[0],department:i[0],channel:i[1]}}return{system:a||r||"--",department:r||a||"--",channel:r||"--"}}function be(){let{state:e,holdScan:t,nextScan:a,avoidCurrent:r,navigate:s}=I(),{hpState:n,liveStatus:i,working:p,error:m,message:A}=e,S=String(i?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),C=String(i?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),v=!!S,N=!!C,k=(e.mode==="hp"||e.mode==="expert")&&N?"digital":"analog",[g,h]=J(k),[w,T]=J(""),[c,o]=J(!1),[u,f]=J(""),[l,_]=J(0);ge(()=>{if(g==="digital"&&!N){h(v?"analog":"digital");return}g==="analog"&&!v&&N&&h("digital")},[v,N,g]),ge(()=>{!m&&!A||f("")},[m,A]),ge(()=>{let b=setInterval(()=>{_(Z=>(Z+1)%dt.length)},320);return()=>clearInterval(b)},[]);let x=g==="digital"?C||S:S||C,E=g==="digital"&&N,G=String(n.mode||"full_database").trim().toLowerCase(),ee=dt[l]||"|",vt=String(i?.profile_airband||"").trim(),yt=Xt(i,"airband")||vt||"Analog",St=i?.last_hit_airband_label||i?.last_hit_ground_label||i?.last_hit||"",te=jt(St,yt),De=E?i?.digital_scheduler_active_system_label||i?.digital_system_label||i?.digital_scheduler_active_system||n.system_name||n.system:te.system,de=E?i?.digital_department_label||i?.digital_scheduler_active_department_label||n.department_name||n.department||i?.digital_last_label:te.department||n.department_name||n.department,He=Kt(i?.digital_last_time),_t=He>0?Math.max(0,Date.now()-He):Number.POSITIVE_INFINITY,Nt=!!(i?.digital_channel_label||i?.digital_last_label||i?.digital_last_tgid),Fe=E&&Nt&&_t<=Yt,Be=E&&Fe?i?.digital_last_tgid??n.tgid??n.talkgroup_id:"--",ce=E?(()=>{let b=Number(i?.digital_preflight?.playlist_frequency_hz?.[0]||i?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(b)&&b>0?(b/1e6).toFixed(4):n.frequency??n.freq})():i?.last_hit_airband||i?.last_hit_ground||i?.last_hit||"--",ze=!!(i?.digital_control_channel_metric_ready??i?.digital_control_decode_available),$e=E?i?.digital_control_channel_locked?"Locked":ze?"Decoding":n.signal??n.signal_strength:i?.rtl_active?"Active":"Idle",Ue=E?i?.digital_channel_label||i?.digital_last_label||n.channel_name||n.channel||de:te.channel||de,pe=E&&(i?.digital_last_mode||n.service_type||n.service)||"",xt=E?Ue:te.channel||Ue,Et=E?[H(pe||"Digital"),Be!=="--"?`TGID ${H(Be)}`:"",ce!=="--"?`${H(ce)} MHz`:"",$e].filter(Boolean).join(" \u2022 "):`${H(ce)} \u2022 ${$e}`,kt=E?i?.digital_control_channel_locked?4:ze?3:1:i?.rtl_active?3:1,Ge=String(i?.digital_scan_mode||"").toLowerCase()==="single_system",Ve=Ge?"HOLD":"SCAN",ae=E&&Ve==="SCAN"&&!Fe,We=lt(()=>{if(G!=="favorites")return"Full Database";let b=String(n.favorites_name||"").trim()||"My Favorites";return(Array.isArray(n.custom_favorites)?n.custom_favorites:[]).length===0?`${b} (empty)`:b},[G,n.custom_favorites,n.favorites_name]),At=E?pe?`Service: ${H(pe)}`:"Service: Digital":G==="favorites"?`List: ${We}`:"Full Database",qe=ae?`Scanning ${ee}`:de,wt=ae?`Service: Digital ${ee}`:At,Ye=ae?`Scanning ${ee}`:xt,Ke=ae?`Digital \u2022 Awaiting activity ${ee}`:Et,Tt=async()=>{try{await t()}catch{}},Ct=async()=>{try{await a()}catch{}},It=async()=>{try{await r()}catch{}},Mt=async(b,Z)=>{if(b==="info"){f(Z==="system"?`System: ${H(De)}`:Z==="department"?`Department: ${H(qe)}`:`Channel: ${H(Ye)} (${H(Ke)})`),T("");return}if(b==="advanced"){f("Advanced options are still being wired in HP3."),T("");return}if(b==="prev"){f("Previous-channel stepping is not wired yet in HP3."),T("");return}if(b==="fave"){T(""),s(d.FAVORITES);return}if(!E){f("Switch Audio Source to Digital for HOLD/NEXT/AVOID controls."),T("");return}b==="hold"?await Tt():b==="next"?await Ct():b==="avoid"&&await It(),T("")},Rt=lt(()=>[{id:"squelch",label:"Squelch",onClick:()=>f("Squelch is currently managed from SB3 analog controls.")},{id:"range",label:Jt(n.range_miles),onClick:()=>s(d.RANGE)},{id:"atten",label:"Atten",onClick:()=>f("Attenuation toggle is not wired yet in HP3.")},{id:"gps",label:"GPS",onClick:()=>s(d.LOCATION)},{id:"help",label:"Help",onClick:()=>s(d.MENU)}],[n.range_miles,s]),Lt={system:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],department:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],channel:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"hold",label:"Hold"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"},{id:"fave",label:"Fave"}]};return R("section",{className:"screen main-screen hp2-main",children:[R("div",{className:"hp2-radio-bar",children:[y("div",{className:"hp2-radio-buttons",children:Rt.map(b=>y("button",{type:"button",className:"hp2-radio-btn",onClick:b.onClick,disabled:p,children:b.label},b.id))}),R("div",{className:"hp2-status-icons",children:[y("span",{className:`hp2-icon ${Ge?"on":""}`,children:Ve}),R("span",{className:"hp2-icon",children:["SIG ",Zt(kt)]}),y("span",{className:"hp2-icon",children:E?"DIG":"ANA"})]})]}),R("div",{className:"hp2-lines",children:[R("div",{className:"hp2-line",children:[y("div",{className:"hp2-line-label",children:"System / Favorite List"}),R("div",{className:"hp2-line-body",children:[y("div",{className:"hp2-line-primary",children:H(De)}),y("div",{className:"hp2-line-secondary",children:We})]}),y("button",{type:"button",className:"hp2-subtab",onClick:()=>T(b=>b==="system"?"":"system"),disabled:p,children:"<"})]}),R("div",{className:"hp2-line",children:[y("div",{className:"hp2-line-label",children:"Department"}),R("div",{className:"hp2-line-body",children:[y("div",{className:"hp2-line-primary",children:H(qe)}),y("div",{className:"hp2-line-secondary",children:wt})]}),y("button",{type:"button",className:"hp2-subtab",onClick:()=>T(b=>b==="department"?"":"department"),disabled:p,children:"<"})]}),R("div",{className:"hp2-line channel",children:[y("div",{className:"hp2-line-label",children:"Channel"}),R("div",{className:"hp2-line-body",children:[y("div",{className:"hp2-line-primary",children:H(Ye)}),y("div",{className:"hp2-line-secondary",children:H(Ke)})]}),y("button",{type:"button",className:"hp2-subtab",onClick:()=>T(b=>b==="channel"?"":"channel"),disabled:p,children:"<"})]})]}),w?y("div",{className:"hp2-submenu-popup",children:Lt[w]?.map(b=>y("button",{type:"button",className:"hp2-submenu-btn",onClick:()=>Mt(b.id,w),disabled:p,children:b.label},b.id))}):null,R("div",{className:"hp2-feature-bar",children:[y("button",{type:"button",className:"hp2-feature-btn",onClick:()=>s(d.MENU),disabled:p,children:"Menu"}),y("button",{type:"button",className:"hp2-feature-btn",onClick:()=>f("Replay is not wired yet in HP3."),disabled:p,children:"Replay"}),y("button",{type:"button",className:"hp2-feature-btn",onClick:()=>f("Recording controls are not wired yet in HP3."),disabled:p,children:"Record"}),y("button",{type:"button",className:"hp2-feature-btn",onClick:()=>o(b=>!b),disabled:p,children:c?"Unmute":"Mute"})]}),R("div",{className:"hp2-web-audio",children:[R("div",{className:"hp2-audio-head",children:[y("div",{className:"muted",children:"Web Audio Stream"}),x?y("a",{href:`/stream/${x}`,target:"_blank",rel:"noreferrer",children:"Open"}):null]}),R("div",{className:"hp2-source-switch",children:[y(M,{variant:g==="analog"?"primary":"secondary",onClick:()=>h("analog"),disabled:!v||p,children:"Analog"}),y(M,{variant:g==="digital"?"primary":"secondary",onClick:()=>h("digital"),disabled:!N||p,children:"Digital"})]}),R("div",{className:"muted hp2-audio-meta",children:["Source: ",E?"Digital":"Analog"," (",x||"no mount",")"]}),y("audio",{controls:!0,preload:"none",muted:c,className:"hp2-audio-player",src:x?`/stream/${x}`:"/stream/"})]}),u?y("div",{className:"message",children:u}):null,E?null:y("div",{className:"muted",children:"HOLD/NEXT/AVOID actions require Digital source."}),m?y("div",{className:"error",children:m}):null,A?y("div",{className:"message",children:A}):null]})}import"https://esm.sh/react@18";import"https://esm.sh/react@18";import{jsx as ve,jsxs as ct}from"https://esm.sh/react@18/jsx-runtime";function z({title:e,subtitle:t="",showBack:a=!1,onBack:r}){return ct("div",{className:"header",children:[ct("div",{children:[ve("h1",{children:e}),t?ve("div",{className:"sub",children:t}):null]}),a?ve("button",{type:"button",className:"btn btn-secondary",onClick:r,children:"Back"}):null]})}import{jsx as re,jsxs as ea}from"https://esm.sh/react@18/jsx-runtime";var Qt=[{id:d.LOCATION,label:"Set Your Location"},{id:d.SERVICE_TYPES,label:"Select Service Types"},{id:d.RANGE,label:"Set Range"},{id:d.FAVORITES,label:"Manage Favorites"},{id:d.AVOID,label:"Avoid Options"},{id:d.MODE_SELECTION,label:"Mode Selection"}];function ye(){let{navigate:e,state:t}=I();return ea("section",{className:"screen menu",children:[re(z,{title:"Menu",showBack:!0,onBack:()=>e(d.MAIN)}),re("div",{className:"menu-list",children:Qt.map(a=>re(M,{variant:"secondary",className:"menu-item",onClick:()=>e(a.id),disabled:t.working,children:a.label},a.id))}),t.error?re("div",{className:"error",children:t.error}):null]})}import{useEffect as ta,useState as X}from"https://esm.sh/react@18";import{jsx as F,jsxs as Y}from"https://esm.sh/react@18/jsx-runtime";function pt(e){if(e===""||e===null||e===void 0)return null;let t=Number(e);return Number.isFinite(t)?t:NaN}function Se(){let{state:e,saveHpState:t,navigate:a}=I(),{hpState:r,working:s}=e,[n,i]=X(""),[p,m]=X(""),[A,S]=X(""),[C,v]=X(!0),[N,k]=X("");return ta(()=>{i(r.zip||r.postal_code||""),m(r.lat!==void 0&&r.lat!==null?String(r.lat):r.latitude!==void 0&&r.latitude!==null?String(r.latitude):""),S(r.lon!==void 0&&r.lon!==null?String(r.lon):r.longitude!==void 0&&r.longitude!==null?String(r.longitude):""),v(r.use_location!==!1)},[r]),Y("section",{className:"screen location-screen",children:[F(z,{title:"Location",showBack:!0,onBack:()=>a(d.MENU)}),Y("div",{className:"list",children:[Y("label",{children:[F("div",{className:"muted",children:"ZIP"}),F("input",{className:"input",value:n,onChange:h=>i(h.target.value.trim()),placeholder:"37201"})]}),Y("label",{children:[F("div",{className:"muted",children:"Latitude"}),F("input",{className:"input",value:p,onChange:h=>m(h.target.value),placeholder:"36.12"})]}),Y("label",{children:[F("div",{className:"muted",children:"Longitude"}),F("input",{className:"input",value:A,onChange:h=>S(h.target.value),placeholder:"-86.67"})]}),Y("label",{className:"row",children:[F("span",{children:"Use location for scanning"}),F("input",{type:"checkbox",checked:C,onChange:h=>v(h.target.checked)})]})]}),F("div",{className:"button-row",children:F(M,{onClick:async()=>{if(k(""),n&&!/^\d{5}(-\d{4})?$/.test(n)){k("ZIP must be 5 digits or ZIP+4.");return}let h=pt(p),w=pt(A);if(Number.isNaN(h)||Number.isNaN(w)){k("Latitude and longitude must be valid numbers.");return}if(h===null!=(w===null)){k("Enter both latitude and longitude, or leave both blank.");return}if(h!==null&&(h<-90||h>90)){k("Latitude must be between -90 and 90.");return}if(w!==null&&(w<-180||w>180)){k("Longitude must be between -180 and 180.");return}try{let T={zip:n,use_location:C};h!==null&&w!==null?(T.lat=h,T.lon=w):n&&(T.resolve_zip=!0),await t(T),a(d.MENU)}catch{}},disabled:s,children:"Save"})}),N?F("div",{className:"error",children:N}):null,e.error?F("div",{className:"error",children:e.error}):null]})}import{useEffect as ut,useMemo as _e,useState as ne}from"https://esm.sh/react@18";import{jsx as L,jsxs as j}from"https://esm.sh/react@18/jsx-runtime";var Ne=8,aa=8;function mt(e){let t=Number(e);return Number.isFinite(t)?t:0}function xe(){let{state:e,saveHpState:t,navigate:a}=I(),{hpState:r,serviceTypes:s,working:n}=e,i=_e(()=>s.filter(o=>o.enabled_by_default).map(o=>Number(o.service_tag)),[s]),p=_e(()=>[...s].sort((o,u)=>{let f=mt(o.service_tag),l=mt(u.service_tag);return f!==l?f-l:String(o.name||"").localeCompare(String(u.name||""))}),[s]),[m,A]=ne([]),[S,C]=ne(0),[v,N]=ne(""),[k,g]=ne("");ut(()=>{let o=Array.isArray(r.enabled_service_tags)?r.enabled_service_tags.map(Number):i;A(Array.from(new Set(o)).filter(u=>Number.isFinite(u)&&u>0))},[r.enabled_service_tags,i]);let h=Math.max(1,Math.ceil(p.length/Ne));ut(()=>{S>=h&&C(Math.max(0,h-1))},[S,h]);let w=_e(()=>{let o=S*Ne,f=[...p.slice(o,o+Ne)];for(;f.length<aa;)f.push(null);return f},[p,S]),T=o=>{g(""),N(""),A(u=>u.includes(o)?u.filter(f=>f!==o):[...u,o])},c=async o=>{g(""),N("");try{await t({enabled_service_tags:[...m].sort((u,f)=>u-f)}),typeof o=="function"?o():N("Service types saved.")}catch(u){g(u?.message||"Failed to save service types.")}};return j("section",{className:"screen hp2-picker service-types-screen",children:[j("div",{className:"hp2-picker-top",children:[L("div",{className:"hp2-picker-title",children:"Select Service Types"}),j("div",{className:"hp2-picker-top-right",children:[L("span",{className:"hp2-picker-help",children:"Help"}),L("span",{className:"hp2-picker-status",children:"L"}),L("span",{className:"hp2-picker-status",children:"SIG"}),L("span",{className:"hp2-picker-status",children:"BAT"})]})]}),L("div",{className:"hp2-picker-grid",children:w.map((o,u)=>{if(!o)return L("div",{className:"hp2-picker-tile hp2-picker-tile-empty"},`empty-${u}`);let f=Number(o.service_tag),l=m.includes(f);return L("button",{type:"button",className:`hp2-picker-tile ${l?"active":""}`,onClick:()=>T(f),disabled:n,children:o.name},`${f}-${o.name}`)})}),j("div",{className:"hp2-picker-bottom hp2-picker-bottom-5",children:[L("button",{type:"button",className:"hp2-picker-btn listen",onClick:()=>c(()=>a(d.MAIN)),disabled:n,children:"Listen"}),L("button",{type:"button",className:"hp2-picker-btn",onClick:()=>a(d.MENU),disabled:n,children:"Back"}),L("button",{type:"button",className:"hp2-picker-btn",onClick:()=>c(()=>a(d.MENU)),disabled:n,children:"Accept"}),L("button",{type:"button",className:"hp2-picker-btn",onClick:()=>C(o=>Math.max(0,o-1)),disabled:n||S<=0,children:"^"}),L("button",{type:"button",className:"hp2-picker-btn",onClick:()=>C(o=>Math.min(h-1,o+1)),disabled:n||S>=h-1,children:"v"})]}),j("div",{className:"muted hp2-picker-page",children:["Page ",S+1," / ",h]}),v?L("div",{className:"message",children:v}):null,k?L("div",{className:"error",children:k}):null,e.error?L("div",{className:"error",children:e.error}):null]})}import{useEffect as ia,useState as ra}from"https://esm.sh/react@18";import{jsx as q,jsxs as Ee}from"https://esm.sh/react@18/jsx-runtime";function ke(){let{state:e,saveHpState:t,navigate:a}=I(),{hpState:r,working:s}=e,[n,i]=ra(15);ia(()=>{let m=Number(r.range_miles);i(Number.isFinite(m)?m:15)},[r.range_miles]);let p=async()=>{try{await t({range_miles:n}),a(d.MENU)}catch{}};return Ee("section",{className:"screen range-screen",children:[q(z,{title:"Range",showBack:!0,onBack:()=>a(d.MENU)}),Ee("div",{className:"card",children:[Ee("div",{className:"row",children:[q("span",{children:"Range Miles"}),q("strong",{children:n.toFixed(1)})]}),q("input",{className:"range",type:"range",min:"0",max:"30",step:"0.5",value:n,onChange:m=>i(Number(m.target.value))})]}),q("div",{className:"button-row",children:q(M,{onClick:p,disabled:s,children:"Save"})}),e.error?q("div",{className:"error",children:e.error}):null]})}import{useEffect as ft,useMemo as Ae,useState as oe}from"https://esm.sh/react@18";import{jsx as O,jsxs as Q}from"https://esm.sh/react@18/jsx-runtime";var se=8,na=8,K="action:full_database",we="action:create_list";function V(e,t="My Favorites"){return String(e||"").trim()||t}function Te(e){return`list:${V(e).toLowerCase()}`}function oa(e){let t=[],a=new Set,r=n=>{let i=V(n,"");if(!i)return;let p=i.toLowerCase();a.has(p)||(a.add(p),t.push(i))};return(Array.isArray(e?.favorites)?e.favorites:[]).forEach(n=>{!n||typeof n!="object"||r(n.label)}),r(e?.favorites_name||"My Favorites"),t.length===0&&t.push("My Favorites"),t}function sa(e,t){let a=V(t).toLowerCase();return e.map((r,s)=>{let n=V(r),i=n.toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"");return{id:i?`fav-${i}`:`fav-${s+1}`,type:"list",target:"favorites",profile_id:"",label:n,enabled:n.toLowerCase()===a}})}function la(e,t){let a=t*se,s=[...e.slice(a,a+se)];for(;s.length<na;)s.push(null);return s}function Ce(){let{state:e,saveHpState:t,navigate:a}=I(),{hpState:r,working:s}=e,[n,i]=oe(K),[p,m]=oe(0),[A,S]=oe(""),[C,v]=oe(""),N=Ae(()=>oa(r),[r.favorites,r.favorites_name]),k=Ae(()=>{let l=N.map(x=>({id:Te(x),label:x,kind:"list"})),_=[{id:K,label:"Select Database to Monitor",kind:"action",multiline:!0}];l[0]&&_.push(l[0]),_.push({id:we,label:"Create New List",kind:"action"}),l[1]&&_.push(l[1]);for(let x=2;x<l.length;x+=1)_.push(l[x]);return _},[N]),g=Math.max(1,Math.ceil(k.length/se));ft(()=>{let _=String(r.mode||"").trim().toLowerCase()==="favorites"?Te(r.favorites_name||"My Favorites"):K;i(_);let x=Math.max(0,k.findIndex(E=>E.id===_));m(Math.floor(x/se))},[r.mode,r.favorites_name,k]),ft(()=>{p>=g&&m(Math.max(0,g-1))},[p,g]);let h=Ae(()=>la(k,p),[k,p]),w=async(l,_,x)=>{let E=Array.isArray(x)&&x.length>0?x:N,G=V(_||r.favorites_name||"My Favorites");await t({mode:l,favorites_name:G,favorites:sa(E,G)})},T=async()=>{v(""),S("");try{await w("full_database",r.favorites_name||"My Favorites"),i(K),S("Monitoring Full Database.")}catch(l){v(l?.message||"Failed to switch to Full Database.")}},c=async(l,_)=>{let x=V(l);v(""),S("");try{await w("favorites",x,_),i(Te(x)),S(`Selected favorites list: ${x}`)}catch(E){v(E?.message||"Failed to select favorites list.")}},o=async()=>{let l=window.prompt("New favorites list name","New List");if(l===null)return;let _=V(l,"");if(!_){v("List name is required.");return}let x=_.toLowerCase();if(N.some(G=>V(G).toLowerCase()===x)){v("That list name already exists.");return}let E=[...N,_];await c(_,E)},u=async l=>{if(!(!l||s)){if(l.id===K){await T();return}if(l.id===we){await o();return}l.kind==="list"&&await c(l.label)}},f=async()=>{let l=k.find(_=>_.id===n)||null;if(!l){a(d.MAIN);return}if(l.id===K){await T(),a(d.MAIN);return}if(l.id===we){await o();return}await c(l.label),a(d.MAIN)};return Q("section",{className:"screen hp2-picker favorites-screen",children:[Q("div",{className:"hp2-picker-top",children:[O("div",{className:"hp2-picker-title",children:"Manage Favorites Lists"}),Q("div",{className:"hp2-picker-top-right",children:[O("span",{className:"hp2-picker-help",children:"Help"}),O("span",{className:"hp2-picker-status",children:"L"}),O("span",{className:"hp2-picker-status",children:"SIG"}),O("span",{className:"hp2-picker-status",children:"BAT"})]})]}),O("div",{className:"hp2-picker-grid",children:h.map((l,_)=>{if(!l)return O("div",{className:"hp2-picker-tile hp2-picker-tile-empty"},`empty-${_}`);let x=l.id===n;return O("button",{type:"button",className:`hp2-picker-tile ${x?"active":""} ${l.multiline?"multiline":""}`,onClick:()=>u(l),disabled:s,children:l.label},l.id)})}),Q("div",{className:"hp2-picker-bottom hp2-picker-bottom-4",children:[O("button",{type:"button",className:"hp2-picker-btn listen",onClick:f,disabled:s,children:"Listen"}),O("button",{type:"button",className:"hp2-picker-btn",onClick:()=>a(d.MENU),disabled:s,children:"Back"}),O("button",{type:"button",className:"hp2-picker-btn",onClick:()=>m(l=>Math.max(0,l-1)),disabled:s||p<=0,children:"^"}),O("button",{type:"button",className:"hp2-picker-btn",onClick:()=>m(l=>Math.min(g-1,l+1)),disabled:s||p>=g-1,children:"v"})]}),Q("div",{className:"muted hp2-picker-page",children:["Page ",p+1," / ",g]}),A?O("div",{className:"message",children:A}):null,C?O("div",{className:"error",children:C}):null,e.error?O("div",{className:"error",children:e.error}):null]})}import{useEffect as da,useMemo as ht,useState as ca}from"https://esm.sh/react@18";import{jsx as P,jsxs as $}from"https://esm.sh/react@18/jsx-runtime";function pa(e){return Array.isArray(e)?e.map((t,a)=>t&&typeof t=="object"?{id:String(t.id??`${t.type||"item"}-${a}`),label:String(t.label||t.alpha_tag||t.name||`Avoid ${a+1}`),type:String(t.type||"item"),source:"persistent"}:{id:`item-${a}`,label:String(t),type:"item",source:"persistent"}):[]}function ua(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach(r=>{let s=String(r||"").trim();!s||a.has(s)||(a.add(s),t.push({id:`runtime:${s}`,label:s,type:"system",token:s,source:"runtime"}))}),t}function Ie(){let{state:e,saveHpState:t,avoidCurrent:a,clearHpAvoids:r,removeHpAvoid:s,navigate:n}=I(),{hpState:i,hpAvoids:p,working:m}=e,A=ht(()=>Array.isArray(i.avoid_list)?i.avoid_list:Array.isArray(i.avoids)?i.avoids:Array.isArray(i.avoid)?i.avoid:[],[i.avoid_list,i.avoids,i.avoid]),[S,C]=ca([]);da(()=>{C(pa(A))},[A]);let v=ht(()=>ua(p),[p]),N=async(g=S)=>{try{await t({avoid_list:g})}catch{}},k=async()=>{try{await a()}catch{}};return $("section",{className:"screen avoid-screen",children:[P(z,{title:"Avoid",showBack:!0,onBack:()=>n(d.MENU)}),$("div",{className:"list",children:[$("div",{className:"card",children:[P("div",{className:"muted",style:{marginBottom:"8px"},children:"Runtime Avoids (HP Scan Pool)"}),v.length===0?P("div",{className:"muted",children:"No runtime HP avoids."}):v.map(g=>$("div",{className:"row",style:{marginBottom:"6px"},children:[$("div",{children:[P("div",{children:g.label}),P("div",{className:"muted",children:g.type})]}),P(M,{variant:"danger",onClick:()=>s(g.token),disabled:m,children:"Remove"})]},g.id))]}),$("div",{className:"card",children:[P("div",{className:"muted",style:{marginBottom:"8px"},children:"Persistent Avoids (State)"}),S.length===0?P("div",{className:"muted",children:"No persistent avoids in current state."}):S.map(g=>$("div",{className:"row",style:{marginBottom:"6px"},children:[$("div",{children:[P("div",{children:g.label}),P("div",{className:"muted",children:g.type})]}),P(M,{variant:"danger",onClick:()=>{let h=S.filter(w=>w.id!==g.id);C(h),N(h)},disabled:m,children:"Remove"})]},g.id))]})]}),$("div",{className:"button-row",children:[P(M,{onClick:k,disabled:m,children:"Avoid Current"}),P(M,{variant:"secondary",onClick:async()=>{C([]),await N([]),await r()},disabled:m,children:"Clear All"}),P(M,{onClick:()=>N(),disabled:m,children:"Save"})]}),e.error?P("div",{className:"error",children:e.error}):null]})}import{useEffect as ma,useState as fa}from"https://esm.sh/react@18";import{jsx as W,jsxs as le}from"https://esm.sh/react@18/jsx-runtime";function Me(){let{state:e,setMode:t,navigate:a}=I(),[r,s]=fa("hp");return ma(()=>{s(e.mode||"hp")},[e.mode]),le("section",{className:"screen mode-selection-screen",children:[W(z,{title:"Mode Selection",showBack:!0,onBack:()=>a(d.MENU)}),le("div",{className:"list",children:[le("label",{className:"row card",children:[W("span",{children:"HP Mode"}),W("input",{type:"radio",name:"scan-mode",value:"hp",checked:r==="hp",onChange:i=>s(i.target.value)})]}),le("label",{className:"row card",children:[W("span",{children:"Expert Mode"}),W("input",{type:"radio",name:"scan-mode",value:"expert",checked:r==="expert",onChange:i=>s(i.target.value)})]})]}),W("div",{className:"button-row",children:W(M,{onClick:async()=>{try{await t(r),a(d.MENU)}catch{}},disabled:e.working,children:"Save"})}),e.error?W("div",{className:"error",children:e.error}):null]})}import"https://esm.sh/react@18";import{jsx as ha}from"https://esm.sh/react@18/jsx-runtime";function Re({label:e="Loading..."}){return ha("div",{className:"loading",children:e})}import{jsx as U}from"https://esm.sh/react@18/jsx-runtime";function Le(){let{state:e}=I();if(e.loading)return U(Re,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case d.MENU:return U(ye,{});case d.LOCATION:return U(Se,{});case d.SERVICE_TYPES:return U(xe,{});case d.RANGE:return U(ke,{});case d.FAVORITES:return U(Ce,{});case d.AVOID:return U(Ie,{});case d.MODE_SELECTION:return U(Me,{});case d.MAIN:default:return U(be,{})}}import{jsx as Oe,jsxs as ba}from"https://esm.sh/react@18/jsx-runtime";var ga=`
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
    height: 52px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 3px;
    overflow: hidden;
  }
  .hp2-line-primary {
    font-size: 1.02rem;
    color: #ffb54a;
    line-height: 1.15;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .hp2-line-secondary {
    color: #9fb0c7;
    font-size: 0.78rem;
    line-height: 1.2;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
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

  .hp2-picker {
    padding: 0;
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid #4b535f;
    background: linear-gradient(180deg, #111722 0%, #0d121b 100%);
    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.45);
  }
  .hp2-picker-top {
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: center;
    border-bottom: 1px solid #384355;
    background: linear-gradient(180deg, #28374a 0%, #1e2a39 100%);
  }
  .hp2-picker-title {
    color: #ffcc2b;
    font-size: 2rem;
    line-height: 1;
    font-weight: 700;
    padding: 12px 14px;
    letter-spacing: 0.01em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .hp2-picker-top-right {
    display: grid;
    grid-auto-flow: column;
    align-items: center;
    border-left: 1px solid #415064;
  }
  .hp2-picker-help,
  .hp2-picker-status {
    color: #d8e4f4;
    font-size: 0.86rem;
    font-weight: 700;
    padding: 0 10px;
    height: 100%;
    display: flex;
    align-items: center;
    border-left: 1px solid #415064;
  }
  .hp2-picker-help {
    border-left: 0;
    color: #dce8ff;
  }
  .hp2-picker-grid {
    padding: 10px;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-template-rows: repeat(4, minmax(0, 1fr));
    gap: 8px;
  }
  .hp2-picker-tile {
    min-height: 64px;
    border: 1px solid #3f4b5f;
    border-radius: 8px;
    background: linear-gradient(180deg, #2e3a4d 0%, #1f2836 100%);
    color: #e6f1ff;
    font-size: 0.84rem;
    font-weight: 700;
    text-align: left;
    padding: 9px 10px;
    cursor: pointer;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .hp2-picker-tile.active {
    border-color: #e2ad43;
    color: #fff3cf;
    background: linear-gradient(180deg, #f6ca2e 0%, #de5c20 100%);
  }
  .hp2-picker-tile:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .hp2-picker-tile-empty {
    cursor: default;
    background: linear-gradient(180deg, #1e2633 0%, #171e29 100%);
    border-style: solid;
  }
  .hp2-picker-bottom {
    display: grid;
    gap: 1px;
    background: #39475b;
    border-top: 1px solid #465469;
  }
  .hp2-picker-bottom-5 {
    grid-template-columns: 1.2fr 1fr 1fr 0.8fr 0.8fr;
  }
  .hp2-picker-bottom-4 {
    grid-template-columns: 1.3fr 1fr 0.8fr 0.8fr;
  }
  .hp2-picker-btn {
    border: 0;
    min-height: 46px;
    background: #2b3749;
    color: #dce9fb;
    font-size: 0.9rem;
    font-weight: 700;
    cursor: pointer;
  }
  .hp2-picker-btn.listen {
    color: #ff8d2f;
  }
  .hp2-picker-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .hp2-picker-page {
    padding: 8px 12px 10px;
    font-size: 0.78rem;
  }
  .favorites-screen .hp2-picker-tile {
    min-height: 66px;
    font-size: 0.82rem;
  }
  .favorites-screen .hp2-picker-tile.multiline {
    white-space: normal;
    line-height: 1.05;
  }
  .favorites-screen .hp2-picker-btn {
    min-height: 48px;
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
    .hp2-picker-title {
      font-size: 1.72rem;
      padding: 10px 10px;
    }
    .hp2-picker-help,
    .hp2-picker-status {
      font-size: 0.75rem;
      padding: 0 7px;
    }
    .hp2-picker-grid {
      gap: 6px;
      padding: 8px;
    }
    .hp2-picker-tile {
      min-height: 56px;
      font-size: 0.8rem;
    }
    .hp2-picker-btn {
      min-height: 42px;
      font-size: 0.82rem;
    }
    .favorites-screen .hp2-picker-tile {
      min-height: 58px;
      font-size: 0.78rem;
    }
  }
`;function Pe(){return Oe(st,{children:ba("div",{className:"app-shell",children:[Oe("style",{children:ga}),Oe(Le,{})]})})}import{jsx as gt}from"https://esm.sh/react@18/jsx-runtime";var bt=document.getElementById("root");if(!bt)throw new Error("Missing #root mount element");ya(bt).render(gt(va.StrictMode,{children:gt(Pe,{})}));
