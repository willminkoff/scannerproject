import fa from"https://esm.sh/react@18";import{createRoot as ya}from"https://esm.sh/react-dom@18/client";import"https://esm.sh/react@18";import{createContext as Ot,useCallback as z,useContext as Ft,useEffect as ze,useMemo as Bt,useRef as Ht,useReducer as Pt}from"https://esm.sh/react@18";var Lt={"Content-Type":"application/json"};async function F(e,{method:a="GET",body:t}={}){let o={method:a,headers:{...Lt}};t!==void 0&&(o.body=JSON.stringify(t));let r=await fetch(e,o),s=await r.text(),i={};try{i=s?JSON.parse(s):{}}catch{i={raw:s}}if(!r.ok){let u=i?.error||`Request failed (${r.status})`,c=new Error(u);throw c.status=r.status,c.payload=i,c}return i}function Pe(){return F("/api/hp/state")}function at(e){return F("/api/hp/state",{method:"POST",body:e})}function De(){return F("/api/hp/service-types")}function Ee(e={}){let a=new URLSearchParams;Object.entries(e).forEach(([o,r])=>{r==null||r===""||a.set(o,String(r))});let t=a.toString();return t?`?${t}`:""}function nt(){return F("/api/hp/favorites-wizard/countries")}function rt(e){return F(`/api/hp/favorites-wizard/states${Ee({country_id:e})}`)}function ot(e){return F(`/api/hp/favorites-wizard/counties${Ee({state_id:e})}`)}function it({stateId:e,countyId:a,systemType:t,q:o}){return F(`/api/hp/favorites-wizard/systems${Ee({state_id:e,county_id:a,system_type:t,q:o})}`)}function st({systemType:e,systemId:a,q:t,limit:o=500}){return F(`/api/hp/favorites-wizard/channels${Ee({system_type:e,system_id:a,q:t,limit:o})}`)}function $e(){return F("/api/hp/avoids")}function lt(){return F("/api/hp/avoids",{method:"POST",body:{action:"clear"}})}function dt(e){return F("/api/hp/avoids",{method:"POST",body:{action:"remove",system:e}})}function ct(){return F("/api/status")}function ut(e){return F("/api/mode",{method:"POST",body:{mode:e}})}function pt(e={}){return F("/api/hp/hold",{method:"POST",body:e})}function mt(e={}){return F("/api/hp/next",{method:"POST",body:e})}function vt(e={}){return F("/api/hp/avoid",{method:"POST",body:e})}import{jsx as qt}from"https://esm.sh/react@18/jsx-runtime";var d=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),Dt={hpState:{},serviceTypes:[],liveStatus:{},hpAvoids:[],currentScreen:d.MAIN,mode:"hp",sseConnected:!1,loading:!0,working:!1,error:"",message:""},$t=["digital_scheduler_active_system","digital_scheduler_active_system_label","digital_scheduler_next_system","digital_scheduler_next_system_label","digital_scheduler_active_department_label","digital_last_label","digital_channel_label","digital_department_label","digital_system_label","digital_last_mode","digital_last_tgid","digital_profile","digital_scan_mode","stream_mount","digital_stream_mount","profile_airband","profile_ground","last_hit_airband_label","last_hit_ground_label"];function ft(e){return e==null?!1:typeof e=="string"?e.trim()!=="":Array.isArray(e)?e.length>0:!0}function ke(e){if(!Array.isArray(e))return[];let a=[],t=new Set;return e.forEach(o=>{let r=String(o||"").trim();!r||t.has(r)||(t.add(r),a.push(r))}),a}function zt(e,a){let t=a&&typeof a=="object"?a:{},o={...e||{},...t};return $t.forEach(r=>{!ft(t[r])&&ft(e?.[r])&&(o[r]=e[r])}),o}function Ut(e,a){switch(a.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:a.payload.hpState||{},serviceTypes:a.payload.serviceTypes||[],liveStatus:a.payload.liveStatus||{},hpAvoids:a.payload.hpAvoids||[],mode:a.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:a.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!a.payload};case"SET_ERROR":return{...e,error:a.payload||""};case"SET_MESSAGE":return{...e,message:a.payload||""};case"SET_HP_STATE":return{...e,hpState:a.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:a.payload||[]};case"SET_HP_AVOIDS":return{...e,hpAvoids:ke(a.payload)};case"SET_LIVE_STATUS":return{...e,liveStatus:zt(e.liveStatus,a.payload),hpAvoids:Array.isArray(a.payload?.hp_avoids)?ke(a.payload.hp_avoids):e.hpAvoids};case"SET_MODE":return{...e,mode:a.payload||e.mode};case"SET_SSE_CONNECTED":return{...e,sseConnected:!!a.payload};case"NAVIGATE":return{...e,currentScreen:a.payload||d.MAIN};default:return e}}var bt=Ot(null);function yt(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(t=>({service_tag:Number(t?.service_tag),name:String(t?.name||`Service ${t?.service_tag}`),enabled_by_default:!!t?.enabled_by_default}))}function ht(e){let a=e&&typeof e.state=="object"&&e.state!==null?e.state:{},t=String(e?.mode||"hp").toLowerCase();return{hpState:a,mode:t}}function St({children:e}){let[a,t]=Pt(Ut,Dt),o=Ht(!1),r=z(f=>{t({type:"NAVIGATE",payload:f})},[]),s=z(async()=>{let f=await Pe(),p=ht(f);return t({type:"SET_HP_STATE",payload:p.hpState}),t({type:"SET_MODE",payload:p.mode}),p},[]),i=z(async()=>{let f=await De(),p=yt(f);return t({type:"SET_SERVICE_TYPES",payload:p}),p},[]),u=z(async()=>{let f=await $e(),p=ke(f?.avoids);return t({type:"SET_HP_AVOIDS",payload:p}),p},[]),c=z(async()=>{if(o.current)return null;o.current=!0;try{let f=await ct();return t({type:"SET_LIVE_STATUS",payload:f||{}}),f}finally{o.current=!1}},[]),A=z(async()=>{t({type:"LOAD_START"});try{let[f,p,S]=await Promise.all([Pe(),De(),$e()]),C=ht(f),$=yt(p),R=ke(S?.avoids);t({type:"LOAD_SUCCESS",payload:{hpState:C.hpState,mode:C.mode,serviceTypes:$,liveStatus:{},hpAvoids:R}})}catch(f){t({type:"LOAD_ERROR",payload:f.message})}},[]);ze(()=>{A()},[A]),ze(()=>{let f=setInterval(()=>{c().catch(()=>{})},a.sseConnected?1e4:5e3);return()=>clearInterval(f)},[c,a.sseConnected]),ze(()=>{if(typeof EventSource>"u")return;let f=!1,p=null,S=null,C=()=>{f||(p=new EventSource("/api/stream"),p.onopen=()=>{t({type:"SET_SSE_CONNECTED",payload:!0})},p.addEventListener("status",$=>{try{let R=JSON.parse($?.data||"{}");t({type:"SET_LIVE_STATUS",payload:R})}catch{}}),p.onerror=()=>{t({type:"SET_SSE_CONNECTED",payload:!1}),p&&(p.close(),p=null),f||(S=setTimeout(C,2e3))})};return C(),()=>{f=!0,t({type:"SET_SSE_CONNECTED",payload:!1}),S&&clearTimeout(S),p&&p.close()}},[]);let y=z(async f=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let p={...a.hpState,...f},S=await at(p),C=S?.state&&typeof S.state=="object"?{...a.hpState,...S.state}:p;return t({type:"SET_HP_STATE",payload:C}),t({type:"SET_MESSAGE",payload:"State saved"}),S}catch(p){throw t({type:"SET_ERROR",payload:p.message}),p}finally{t({type:"SET_WORKING",payload:!1})}},[a.hpState]),b=z(async f=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let p=await ut(f),S=String(p?.mode||f||"hp").toLowerCase();return t({type:"SET_MODE",payload:S}),t({type:"SET_MESSAGE",payload:`Mode set to ${S}`}),p}catch(p){throw t({type:"SET_ERROR",payload:p.message}),p}finally{t({type:"SET_WORKING",payload:!1})}},[]),g=z(async(f,p)=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let S=await f();return Array.isArray(S?.avoids)&&t({type:"SET_HP_AVOIDS",payload:S.avoids}),p&&t({type:"SET_MESSAGE",payload:p}),await s(),await c(),S}catch(S){throw t({type:"SET_ERROR",payload:S.message}),S}finally{t({type:"SET_WORKING",payload:!1})}},[s,c]),w=z(async()=>g(()=>pt(),"Hold command sent"),[g]),D=z(async()=>g(()=>mt(),"Next command sent"),[g]),k=z(async(f={})=>g(()=>vt(f),"Avoid command sent"),[g]),E=z(async()=>g(()=>lt(),"Runtime avoids cleared"),[g]),O=z(async f=>g(()=>dt(f),"Avoid removed"),[g]),U=Bt(()=>({state:a,dispatch:t,navigate:r,refreshAll:A,refreshHpState:s,refreshServiceTypes:i,refreshHpAvoids:u,refreshStatus:c,saveHpState:y,setMode:b,holdScan:w,nextScan:D,avoidCurrent:k,clearHpAvoids:E,removeHpAvoid:O,SCREENS:d}),[a,r,A,s,i,u,c,y,b,w,D,k,E,O]);return qt(bt.Provider,{value:U,children:e})}function L(){let e=Ft(bt);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import"https://esm.sh/react@18";import{useEffect as _t,useMemo as Nt,useState as Ce}from"https://esm.sh/react@18";import"https://esm.sh/react@18";import{jsx as Vt}from"https://esm.sh/react@18/jsx-runtime";function N({children:e,onClick:a,type:t="button",variant:o="primary",className:r="",disabled:s=!1}){return Vt("button",{type:t,className:`btn ${o==="secondary"?"btn-secondary":o==="danger"?"btn-danger":""} ${r}`.trim(),onClick:a,disabled:s,children:e})}import{jsx as _,jsxs as B}from"https://esm.sh/react@18/jsx-runtime";function V(e){return e==null||e===""?"--":String(e)}function Gt(e){let a=Math.max(0,Math.min(4,Number(e)||0));return`${"|".repeat(a)}${".".repeat(4-a)}`}function Wt(e){let a=Number(e);return Number.isFinite(a)?Number.isInteger(a)?`Range ${a}`:`Range ${a.toFixed(1)}`:"Range"}function Kt(e,a){let t=a==="ground"?e?.profile_ground:e?.profile_airband,o=a==="ground"?e?.profiles_ground:e?.profiles_airband,r=Array.isArray(o)?o:[],s=String(t||"").trim();if(!s)return"";let i=r.find(c=>String(c?.id||"").trim().toLowerCase()===s.toLowerCase());return String(i?.label||"").trim()||s}function Yt(e,a){let t=String(a||"").trim(),o=String(e||"").trim();if(!o&&!t)return{system:"--",department:"--",channel:"--"};let r=[" | "," - "," / "," \u2014 "," \u2013 ","::"];for(let s of r){if(!o.includes(s))continue;let i=o.split(s).map(u=>String(u||"").trim()).filter(Boolean);if(i.length>=3)return{system:i[0],department:i[1],channel:i.slice(2).join(" / ")};if(i.length===2)return{system:t||i[0],department:i[0],channel:i[1]}}return{system:t||o||"--",department:o||t||"--",channel:o||"--"}}function Ue(){let{state:e,holdScan:a,nextScan:t,avoidCurrent:o,navigate:r}=L(),{hpState:s,liveStatus:i,working:u,error:c,message:A}=e,y=String(i?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),b=String(i?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),g=!!y,w=!!b,D=(e.mode==="hp"||e.mode==="expert")&&w?"digital":"analog",[k,E]=Ce(D),[O,U]=Ce(""),[f,p]=Ce(!1),[S,C]=Ce("");_t(()=>{if(k==="digital"&&!w){E(g?"analog":"digital");return}k==="analog"&&!g&&w&&E("digital")},[g,w,k]),_t(()=>{!c&&!A||C("")},[c,A]);let $=k==="digital"?b||y:y||b,R=k==="digital"&&w,K=String(s.mode||"full_database").trim().toLowerCase(),ge=String(i?.profile_airband||"").trim(),fe=Kt(i,"airband")||ge||"Analog",te=i?.last_hit_airband_label||i?.last_hit_ground_label||i?.last_hit||"",ne=Yt(te,fe),re=R?i?.digital_scheduler_active_system_label||i?.digital_system_label||i?.digital_scheduler_active_system||s.system_name||s.system:ne.system,j=R?i?.digital_department_label||i?.digital_scheduler_active_department_label||s.department_name||s.department||i?.digital_last_label:ne.department||s.department_name||s.department,le=R?i?.digital_last_tgid??s.tgid??s.talkgroup_id:"--",ye=R?(()=>{let h=Number(i?.digital_preflight?.playlist_frequency_hz?.[0]||i?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(h)&&h>0?(h/1e6).toFixed(4):s.frequency??s.freq})():i?.last_hit_airband||i?.last_hit_ground||i?.last_hit||"--",de=!!(i?.digital_control_channel_metric_ready??i?.digital_control_decode_available),Ne=R?i?.digital_control_channel_locked?"Locked":de?"Decoding":s.signal??s.signal_strength:i?.rtl_active?"Active":"Idle",Ae=R?i?.digital_channel_label||i?.digital_last_label||s.channel_name||s.channel||j:ne.channel||j,he=R&&(i?.digital_last_mode||s.service_type||s.service)||"",oe=R?Ae:ne.channel||Ae,W=R?[V(he||"Digital"),le!=="--"?`TGID ${V(le)}`:"",ye!=="--"?`${V(ye)} MHz`:"",Ne].filter(Boolean).join(" \u2022 "):`${V(ye)} \u2022 ${Ne}`,Ie=R?i?.digital_control_channel_locked?4:de?3:1:i?.rtl_active?3:1,q=String(i?.digital_scan_mode||"").toLowerCase()==="single_system",M=q?"HOLD":"SCAN",Q=Nt(()=>{if(K!=="favorites")return"Full Database";let h=String(s.favorites_name||"").trim()||"My Favorites";return(Array.isArray(s.custom_favorites)?s.custom_favorites:[]).length===0?`${h} (empty)`:h},[K,s.custom_favorites,s.favorites_name]),J=R?he?`Service: ${V(he)}`:"Service: Digital":K==="favorites"?`List: ${Q}`:"Full Database",ce=async()=>{try{await a()}catch{}},Me=async()=>{try{await t()}catch{}},Le=async()=>{try{await o()}catch{}},Oe=async(h,be)=>{if(h==="info"){C(be==="system"?`System: ${V(re)}`:be==="department"?`Department: ${V(j)}`:`Channel: ${V(oe)} (${V(W)})`),U("");return}if(h==="advanced"){C("Advanced options are still being wired in HP3."),U("");return}if(h==="prev"){C("Previous-channel stepping is not wired yet in HP3."),U("");return}if(h==="fave"){U(""),r(d.FAVORITES);return}if(!R){C("Switch Audio Source to Digital for HOLD/NEXT/AVOID controls."),U("");return}h==="hold"?await ce():h==="next"?await Me():h==="avoid"&&await Le(),U("")},Fe=Nt(()=>[{id:"squelch",label:"Squelch",onClick:()=>C("Squelch is currently managed from SB3 analog controls.")},{id:"range",label:Wt(s.range_miles),onClick:()=>r(d.RANGE)},{id:"atten",label:"Atten",onClick:()=>C("Attenuation toggle is not wired yet in HP3.")},{id:"gps",label:"GPS",onClick:()=>r(d.LOCATION)},{id:"help",label:"Help",onClick:()=>r(d.MENU)}],[s.range_miles,r]),xe={system:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],department:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],channel:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"hold",label:"Hold"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"},{id:"fave",label:"Fave"}]};return B("section",{className:"screen main-screen hp2-main",children:[B("div",{className:"hp2-radio-bar",children:[_("div",{className:"hp2-radio-buttons",children:Fe.map(h=>_("button",{type:"button",className:"hp2-radio-btn",onClick:h.onClick,disabled:u,children:h.label},h.id))}),B("div",{className:"hp2-status-icons",children:[_("span",{className:`hp2-icon ${q?"on":""}`,children:M}),B("span",{className:"hp2-icon",children:["SIG ",Gt(Ie)]}),_("span",{className:"hp2-icon",children:R?"DIG":"ANA"})]})]}),B("div",{className:"hp2-lines",children:[B("div",{className:"hp2-line",children:[_("div",{className:"hp2-line-label",children:"System / Favorite List"}),B("div",{className:"hp2-line-body",children:[_("div",{className:"hp2-line-primary",children:V(re)}),_("div",{className:"hp2-line-secondary",children:Q})]}),_("button",{type:"button",className:"hp2-subtab",onClick:()=>U(h=>h==="system"?"":"system"),disabled:u,children:"<"})]}),B("div",{className:"hp2-line",children:[_("div",{className:"hp2-line-label",children:"Department"}),B("div",{className:"hp2-line-body",children:[_("div",{className:"hp2-line-primary",children:V(j)}),_("div",{className:"hp2-line-secondary",children:J})]}),_("button",{type:"button",className:"hp2-subtab",onClick:()=>U(h=>h==="department"?"":"department"),disabled:u,children:"<"})]}),B("div",{className:"hp2-line channel",children:[_("div",{className:"hp2-line-label",children:"Channel"}),B("div",{className:"hp2-line-body",children:[_("div",{className:"hp2-line-primary",children:V(oe)}),_("div",{className:"hp2-line-secondary",children:V(W)})]}),_("button",{type:"button",className:"hp2-subtab",onClick:()=>U(h=>h==="channel"?"":"channel"),disabled:u,children:"<"})]})]}),O?_("div",{className:"hp2-submenu-popup",children:xe[O]?.map(h=>_("button",{type:"button",className:"hp2-submenu-btn",onClick:()=>Oe(h.id,O),disabled:u,children:h.label},h.id))}):null,B("div",{className:"hp2-feature-bar",children:[_("button",{type:"button",className:"hp2-feature-btn",onClick:()=>r(d.MENU),disabled:u,children:"Menu"}),_("button",{type:"button",className:"hp2-feature-btn",onClick:()=>C("Replay is not wired yet in HP3."),disabled:u,children:"Replay"}),_("button",{type:"button",className:"hp2-feature-btn",onClick:()=>C("Recording controls are not wired yet in HP3."),disabled:u,children:"Record"}),_("button",{type:"button",className:"hp2-feature-btn",onClick:()=>p(h=>!h),disabled:u,children:f?"Unmute":"Mute"})]}),B("div",{className:"hp2-web-audio",children:[B("div",{className:"hp2-audio-head",children:[_("div",{className:"muted",children:"Web Audio Stream"}),$?_("a",{href:`/stream/${$}`,target:"_blank",rel:"noreferrer",children:"Open"}):null]}),B("div",{className:"hp2-source-switch",children:[_(N,{variant:k==="analog"?"primary":"secondary",onClick:()=>E("analog"),disabled:!g||u,children:"Analog"}),_(N,{variant:k==="digital"?"primary":"secondary",onClick:()=>E("digital"),disabled:!w||u,children:"Digital"})]}),B("div",{className:"muted hp2-audio-meta",children:["Source: ",R?"Digital":"Analog"," (",$||"no mount",")"]}),_("audio",{controls:!0,preload:"none",muted:f,className:"hp2-audio-player",src:$?`/stream/${$}`:"/stream/"})]}),S?_("div",{className:"message",children:S}):null,R?null:_("div",{className:"muted",children:"HOLD/NEXT/AVOID actions require Digital source."}),c?_("div",{className:"error",children:c}):null,A?_("div",{className:"message",children:A}):null]})}import"https://esm.sh/react@18";import"https://esm.sh/react@18";import{jsx as qe,jsxs as At}from"https://esm.sh/react@18/jsx-runtime";function H({title:e,subtitle:a="",showBack:t=!1,onBack:o}){return At("div",{className:"header",children:[At("div",{children:[qe("h1",{children:e}),a?qe("div",{className:"sub",children:a}):null]}),t?qe("button",{type:"button",className:"btn btn-secondary",onClick:o,children:"Back"}):null]})}import{jsx as Te,jsxs as Qt}from"https://esm.sh/react@18/jsx-runtime";var jt=[{id:d.LOCATION,label:"Set Your Location"},{id:d.SERVICE_TYPES,label:"Select Service Types"},{id:d.RANGE,label:"Set Range"},{id:d.FAVORITES,label:"Manage Favorites"},{id:d.AVOID,label:"Avoid Options"},{id:d.MODE_SELECTION,label:"Mode Selection"}];function Ve(){let{navigate:e,state:a}=L();return Qt("section",{className:"screen menu",children:[Te(H,{title:"Menu",showBack:!0,onBack:()=>e(d.MAIN)}),Te("div",{className:"menu-list",children:jt.map(t=>Te(N,{variant:"secondary",className:"menu-item",onClick:()=>e(t.id),disabled:a.working,children:t.label},t.id))}),a.error?Te("div",{className:"error",children:a.error}):null]})}import{useEffect as Jt,useState as Se}from"https://esm.sh/react@18";import{jsx as G,jsxs as pe}from"https://esm.sh/react@18/jsx-runtime";function xt(e){if(e===""||e===null||e===void 0)return null;let a=Number(e);return Number.isFinite(a)?a:NaN}function Ge(){let{state:e,saveHpState:a,navigate:t}=L(),{hpState:o,working:r}=e,[s,i]=Se(""),[u,c]=Se(""),[A,y]=Se(""),[b,g]=Se(!0),[w,D]=Se("");return Jt(()=>{i(o.zip||o.postal_code||""),c(o.lat!==void 0&&o.lat!==null?String(o.lat):o.latitude!==void 0&&o.latitude!==null?String(o.latitude):""),y(o.lon!==void 0&&o.lon!==null?String(o.lon):o.longitude!==void 0&&o.longitude!==null?String(o.longitude):""),g(o.use_location!==!1)},[o]),pe("section",{className:"screen location-screen",children:[G(H,{title:"Location",showBack:!0,onBack:()=>t(d.MENU)}),pe("div",{className:"list",children:[pe("label",{children:[G("div",{className:"muted",children:"ZIP"}),G("input",{className:"input",value:s,onChange:E=>i(E.target.value.trim()),placeholder:"37201"})]}),pe("label",{children:[G("div",{className:"muted",children:"Latitude"}),G("input",{className:"input",value:u,onChange:E=>c(E.target.value),placeholder:"36.12"})]}),pe("label",{children:[G("div",{className:"muted",children:"Longitude"}),G("input",{className:"input",value:A,onChange:E=>y(E.target.value),placeholder:"-86.67"})]}),pe("label",{className:"row",children:[G("span",{children:"Use location for scanning"}),G("input",{type:"checkbox",checked:b,onChange:E=>g(E.target.checked)})]})]}),G("div",{className:"button-row",children:G(N,{onClick:async()=>{if(D(""),s&&!/^\d{5}(-\d{4})?$/.test(s)){D("ZIP must be 5 digits or ZIP+4.");return}let E=xt(u),O=xt(A);if(Number.isNaN(E)||Number.isNaN(O)){D("Latitude and longitude must be valid numbers.");return}if(E!==null&&(E<-90||E>90)){D("Latitude must be between -90 and 90.");return}if(O!==null&&(O<-180||O>180)){D("Longitude must be between -180 and 180.");return}try{await a({zip:s,lat:E,lon:O,use_location:b}),t(d.MENU)}catch{}},disabled:r,children:"Save"})}),w?G("div",{className:"error",children:w}):null,e.error?G("div",{className:"error",children:e.error}):null]})}import{useEffect as Zt,useMemo as Xt,useState as ea}from"https://esm.sh/react@18";import{jsx as ie,jsxs as Et}from"https://esm.sh/react@18/jsx-runtime";function We(){let{state:e,saveHpState:a,navigate:t}=L(),{hpState:o,serviceTypes:r,working:s}=e,i=Xt(()=>r.filter(b=>b.enabled_by_default).map(b=>Number(b.service_tag)),[r]),[u,c]=ea([]);Zt(()=>{let b=Array.isArray(o.enabled_service_tags)?o.enabled_service_tags.map(Number):i;c(Array.from(new Set(b)).filter(g=>Number.isFinite(g)))},[o.enabled_service_tags,i]);let A=b=>{c(g=>g.includes(b)?g.filter(w=>w!==b):[...g,b])},y=async()=>{try{await a({enabled_service_tags:[...u].sort((b,g)=>b-g)}),t(d.MENU)}catch{}};return Et("section",{className:"screen service-types-screen",children:[ie(H,{title:"Service Types",showBack:!0,onBack:()=>t(d.MENU)}),ie("div",{className:"checkbox-list",children:r.map(b=>{let g=Number(b.service_tag),w=u.includes(g);return Et("label",{className:"row card",children:[ie("span",{children:b.name}),ie("input",{type:"checkbox",checked:w,onChange:()=>A(g)})]},g)})}),ie("div",{className:"button-row",children:ie(N,{onClick:y,disabled:s,children:"Save"})}),e.error?ie("div",{className:"error",children:e.error}):null]})}import{useEffect as ta,useState as aa}from"https://esm.sh/react@18";import{jsx as se,jsxs as Ke}from"https://esm.sh/react@18/jsx-runtime";function Ye(){let{state:e,saveHpState:a,navigate:t}=L(),{hpState:o,working:r}=e,[s,i]=aa(15);ta(()=>{let c=Number(o.range_miles);i(Number.isFinite(c)?c:15)},[o.range_miles]);let u=async()=>{try{await a({range_miles:s}),t(d.MENU)}catch{}};return Ke("section",{className:"screen range-screen",children:[se(H,{title:"Range",showBack:!0,onBack:()=>t(d.MENU)}),Ke("div",{className:"card",children:[Ke("div",{className:"row",children:[se("span",{children:"Range Miles"}),se("strong",{children:s.toFixed(1)})]}),se("input",{className:"range",type:"range",min:"0",max:"30",step:"0.5",value:s,onChange:c=>i(Number(c.target.value))})]}),se("div",{className:"button-row",children:se(N,{onClick:u,disabled:r,children:"Save"})}),e.error?se("div",{className:"error",children:e.error}):null]})}import{useEffect as me,useMemo as kt,useState as I}from"https://esm.sh/react@18";import{jsx as l,jsxs as T}from"https://esm.sh/react@18/jsx-runtime";function ve(e){let a=Number(String(e||"").trim());return Number.isFinite(a)?a:null}function Z(e){let a=Number.parseInt(String(e||"").trim(),10);return Number.isFinite(a)?a:null}function na(e){let a=String(e||"").split(/[,\s]+/).map(r=>r.trim()).filter(Boolean),t=new Set,o=[];return a.forEach(r=>{let s=ve(r);if(s===null||s<=0)return;let i=Number(s.toFixed(6));t.has(i)||(t.add(i),o.push(i))}),o.sort((r,s)=>r-s)}function ra(e){if(!Array.isArray(e))return[];let a=[];return e.forEach((t,o)=>{if(!t||typeof t!="object")return;let r=String(t.kind||"").trim().toLowerCase();if(r!=="trunked"&&r!=="conventional")return;let s=String(t.id||`fav-${o+1}`).trim()||`fav-${o+1}`;if(r==="trunked"){let u=Z(t.talkgroup||t.tgid),c=Array.isArray(t.control_channels)?t.control_channels.map(A=>ve(A)).filter(A=>A!==null&&A>0).map(A=>Number(A.toFixed(6))):[];if(u===null||u<=0||c.length===0)return;a.push({id:s,kind:"trunked",system_name:String(t.system_name||"").trim(),department_name:String(t.department_name||"").trim(),alpha_tag:String(t.alpha_tag||t.channel_name||"").trim(),talkgroup:String(u),service_tag:Z(t.service_tag)||0,control_channels:Array.from(new Set(c)).sort((A,y)=>A-y)});return}let i=ve(t.frequency);i===null||i<=0||a.push({id:s,kind:"conventional",alpha_tag:String(t.alpha_tag||t.channel_name||"").trim(),frequency:Number(i.toFixed(6)),service_tag:Z(t.service_tag)||0})}),a}function we(e){let a=Math.random().toString(16).slice(2,8);return`${e}-${Date.now()}-${a}`}function oa(e){if(!e||typeof e!="object")return"";let a=String(e.kind||"").trim().toLowerCase();if(a==="trunked"){let t=Number(e.talkgroup)||0,o=Array.isArray(e.control_channels)?[...new Set(e.control_channels.map(r=>Number(r).toFixed(6)))].sort():[];return`trunked|${t}|${o.join(",")}`}return a==="conventional"?`conventional|${(Number(e.frequency)||0).toFixed(6)}|${String(e.alpha_tag||"").trim().toLowerCase()}`:""}function Ct(e){if(!e||typeof e!="object")return"";let a=String(e.kind||"").trim().toLowerCase();if(a==="trunked"){let t=Number(e.talkgroup)||0,o=Array.isArray(e.control_channels)?[...new Set(e.control_channels.map(r=>Number(r).toFixed(6)))].sort():[];return`trunked|${t}|${o.join(",")}`}return a==="conventional"?`conventional|${(Number(e.frequency)||0).toFixed(6)}|${String(e.alpha_tag||"").trim().toLowerCase()}`:""}function ia(e){if(!e||typeof e!="object")return null;let a=String(e.kind||"").trim().toLowerCase();if(a==="trunked"){let t=Z(e.talkgroup),o=Array.isArray(e.control_channels)?e.control_channels.map(r=>ve(r)).filter(r=>r!==null&&r>0).map(r=>Number(r.toFixed(6))):[];return t===null||t<=0||o.length===0?null:{id:we("trunk"),kind:"trunked",system_name:String(e.system_name||"").trim(),department_name:String(e.department_name||"").trim(),alpha_tag:String(e.alpha_tag||"").trim(),talkgroup:String(t),service_tag:Z(e.service_tag)||0,control_channels:Array.from(new Set(o)).sort((r,s)=>r-s)}}if(a==="conventional"){let t=ve(e.frequency);return t===null||t<=0?null:{id:we("conv"),kind:"conventional",alpha_tag:String(e.alpha_tag||"").trim(),frequency:Number(t.toFixed(6)),service_tag:Z(e.service_tag)||0}}return null}function je(){let{state:e,saveHpState:a,navigate:t}=L(),{hpState:o,working:r}=e,[s,i]=I("My Favorites"),[u,c]=I([]),[A,y]=I(""),[b,g]=I(""),[w,D]=I([]),[k,E]=I([]),[O,U]=I([]),[f,p]=I([]),[S,C]=I([]),[$,R]=I(1),[K,ge]=I(0),[_e,fe]=I(0),[te,ne]=I("digital"),[re,j]=I(""),[le,ye]=I(""),[de,Ne]=I(""),[Ae,he]=I(!1),[oe,W]=I([]),[Ie,q]=I(!1),[M,Q]=I({system_name:"",department_name:"",alpha_tag:"",talkgroup:"",service_tag:"",control_channels:""}),[J,ce]=I({alpha_tag:"",frequency:"",service_tag:""});me(()=>{i(String(o.favorites_name||"My Favorites").trim()||"My Favorites"),c(ra(o.custom_favorites))},[o.favorites_name,o.custom_favorites]),me(()=>{let n=!1;return(async()=>{q(!0);try{let v=await nt();if(n)return;let x=Array.isArray(v?.countries)?v.countries:[];if(D(x),x.length>0){let Y=x.find(ue=>Number(ue.country_id)===1)||x[0];R(Number(Y.country_id)||1)}}catch(v){n||y(v.message||"Failed to load countries.")}finally{n||q(!1)}})(),()=>{n=!0}},[]),me(()=>{if(!$)return;let n=!1;return(async()=>{q(!0),p([]),C([]),j(""),W([]);try{let v=await rt($);if(n)return;let x=Array.isArray(v?.states)?v.states:[];E(x),x.length>0?ge(Number(x[0].state_id)||0):ge(0)}catch(v){n||y(v.message||"Failed to load states.")}finally{n||q(!1)}})(),()=>{n=!0}},[$]),me(()=>{if(!K)return;let n=!1;return(async()=>{q(!0),p([]),C([]),j(""),W([]);try{let v=await ot(K);if(n)return;let x=Array.isArray(v?.counties)?v.counties:[];U(x),x.length>0?fe(Number(x[0].county_id)||0):fe(0)}catch(v){n||y(v.message||"Failed to load counties.")}finally{n||q(!1)}})(),()=>{n=!0}},[K]),me(()=>{if(!K)return;let n=!1,m=setTimeout(async()=>{q(!0);try{let v=await it({stateId:K,countyId:_e,systemType:te,q:le.trim()});if(n)return;let x=Array.isArray(v?.systems)?v.systems:[];if(p(x),x.length>0){let Y=String(x[0].id||"").trim();j(Y)}else j(""),C([]),W([])}catch(v){n||y(v.message||"Failed to load systems.")}finally{n||q(!1)}},250);return()=>{n=!0,clearTimeout(m)}},[K,_e,te,le]),me(()=>{if(!re){C([]),W([]);return}let n=!1,m=setTimeout(async()=>{q(!0);try{let v=await st({systemType:te,systemId:re,q:de.trim(),limit:500});if(n)return;let x=Array.isArray(v?.channels)?v.channels:[];C(x),he(!!v?.truncated),W([])}catch(v){n||y(v.message||"Failed to load channels.")}finally{n||q(!1)}},250);return()=>{n=!0,clearTimeout(m)}},[te,re,de]);let Me=n=>{W(m=>{let v=String(n||"");return m.includes(v)?m.filter(x=>x!==v):[...m,v]})},Le=()=>{y(""),g("");let n=new Set(oe),m=S.filter(v=>n.has(String(v.id||"")));if(m.length===0){y("No channels selected.");return}c(v=>{let x=new Set(v.map(ue=>Ct(ue)).filter(Boolean)),Y=[];return m.forEach(ue=>{let Be=ia(ue);if(!Be)return;let He=Ct(Be)||oa(ue);!He||x.has(He)||(x.add(He),Y.push(Be))}),g(Y.length>0?`Added ${Y.length} channel${Y.length===1?"":"s"} to favorites.`:"All selected channels were already in favorites."),W([]),[...v,...Y]})},Oe=kt(()=>u.filter(n=>n.kind==="trunked"),[u]),Fe=kt(()=>u.filter(n=>n.kind==="conventional"),[u]),xe=n=>{c(m=>m.filter(v=>v.id!==n))},h=()=>{y("");let n=Z(M.talkgroup);if(n===null||n<=0){y("Trunked talkgroup must be a positive integer.");return}let m=na(M.control_channels);if(m.length===0){y("At least one trunked control channel is required.");return}let v=Z(M.service_tag)||0,x={id:we("trunk"),kind:"trunked",system_name:String(M.system_name||"").trim(),department_name:String(M.department_name||"").trim(),alpha_tag:String(M.alpha_tag||"").trim(),talkgroup:String(n),service_tag:v,control_channels:m};c(Y=>[...Y,x]),Q({system_name:M.system_name,department_name:M.department_name,alpha_tag:"",talkgroup:"",service_tag:M.service_tag,control_channels:M.control_channels})},be=()=>{y("");let n=ve(J.frequency);if(n===null||n<=0){y("Conventional frequency must be a positive number.");return}let m=Z(J.service_tag)||0,v={id:we("conv"),kind:"conventional",alpha_tag:String(J.alpha_tag||"").trim(),frequency:Number(n.toFixed(6)),service_tag:m};c(x=>[...x,v]),ce({alpha_tag:"",frequency:"",service_tag:J.service_tag})},It=async()=>{y("");let n=String(s||"").trim()||"My Favorites";try{await a({mode:"favorites",favorites_name:n,custom_favorites:u}),t(d.MENU)}catch{}},Mt=async()=>{y("");try{await a({mode:"full_database"}),t(d.MENU)}catch{}};return T("section",{className:"screen favorites-screen",children:[l(H,{title:"Favorites",showBack:!0,onBack:()=>t(d.MENU)}),T("div",{className:"card",children:[l("div",{className:"muted",style:{marginBottom:"8px"},children:"Favorites List Name"}),l("input",{className:"input",value:s,onChange:n=>i(n.target.value),placeholder:"My Favorites"})]}),T("div",{className:"card",children:[l("div",{className:"muted",style:{marginBottom:"8px"},children:"Favorites Wizard (HP2 style)"}),l("div",{className:"muted",children:"Country"}),l("select",{className:"input",value:$,onChange:n=>R(Number(n.target.value)||1),children:w.map(n=>l("option",{value:n.country_id,children:n.name},n.country_id))}),l("div",{className:"muted",children:"State / Province"}),l("select",{className:"input",value:K,onChange:n=>ge(Number(n.target.value)||0),children:k.map(n=>l("option",{value:n.state_id,children:n.name},n.state_id))}),l("div",{className:"muted",children:"County"}),l("select",{className:"input",value:_e,onChange:n=>fe(Number(n.target.value)||0),children:O.map(n=>l("option",{value:n.county_id,children:n.name},`${n.county_id}:${n.name}`))}),T("div",{className:"row",style:{marginTop:"8px"},children:[T("label",{children:[l("input",{type:"radio",name:"wizard-system-type",value:"digital",checked:te==="digital",onChange:()=>ne("digital")})," ","Digital"]}),T("label",{children:[l("input",{type:"radio",name:"wizard-system-type",value:"analog",checked:te==="analog",onChange:()=>ne("analog")})," ","Analog"]})]}),l("div",{className:"muted",style:{marginTop:"8px"},children:"System Search"}),l("input",{className:"input",value:le,onChange:n=>ye(n.target.value),placeholder:"Filter systems"}),l("div",{className:"muted",children:"System"}),l("select",{className:"input",value:re,onChange:n=>j(String(n.target.value||"")),children:f.map(n=>l("option",{value:n.id,children:n.name},`${n.system_type}:${n.id}`))}),l("div",{className:"muted",style:{marginTop:"8px"},children:"Channel Search"}),l("input",{className:"input",value:de,onChange:n=>Ne(n.target.value),placeholder:"Filter channels / talkgroups"}),T("div",{className:"row",style:{marginTop:"8px"},children:[l(N,{variant:"secondary",onClick:()=>W(S.map(n=>String(n.id||""))),disabled:S.length===0||r,children:"Select All"}),l(N,{variant:"secondary",onClick:()=>W([]),disabled:oe.length===0||r,children:"Clear Selection"}),l(N,{onClick:Le,disabled:oe.length===0||r,children:"Add Selected"})]}),Ae?l("div",{className:"muted",style:{marginTop:"6px"},children:"Showing first 500 channels. Narrow search to see more."}):null,T("div",{className:"muted",style:{marginTop:"6px"},children:["Loaded ",S.length," channel",S.length===1?"":"s","."]}),Ie?l("div",{className:"muted",children:"Loading wizard data\u2026"}):null,T("div",{className:"list",style:{marginTop:"8px",maxHeight:"320px",overflowY:"auto"},children:[S.map(n=>{let m=String(n.id||""),v=oe.includes(m),x=n.kind==="trunked"?`TGID ${n.talkgroup} \u2022 ${n.department_name||"Department"}`:`${Number(n.frequency||0).toFixed(4)} MHz \u2022 ${n.department_name||"Department"}`;return T("label",{className:"row",style:{marginBottom:"6px"},children:[T("span",{children:[l("strong",{children:n.alpha_tag||n.department_name||"Channel"}),l("div",{className:"muted",children:x})]}),l("input",{type:"checkbox",checked:v,onChange:()=>Me(m)})]},m)}),S.length===0?l("div",{className:"muted",children:"No channels found for current selection."}):null]})]}),T("div",{className:"card",children:[l("div",{className:"muted",style:{marginBottom:"8px"},children:"Manual Add (optional)"}),l("div",{className:"muted",style:{marginBottom:"6px"},children:"Trunked"}),l("input",{className:"input",value:M.system_name,onChange:n=>Q(m=>({...m,system_name:n.target.value})),placeholder:"System name"}),l("input",{className:"input",value:M.department_name,onChange:n=>Q(m=>({...m,department_name:n.target.value})),placeholder:"Department name"}),l("input",{className:"input",value:M.alpha_tag,onChange:n=>Q(m=>({...m,alpha_tag:n.target.value})),placeholder:"Channel label (alpha tag)"}),l("input",{className:"input",value:M.talkgroup,onChange:n=>Q(m=>({...m,talkgroup:n.target.value})),placeholder:"Talkgroup (decimal)"}),l("input",{className:"input",value:M.control_channels,onChange:n=>Q(m=>({...m,control_channels:n.target.value})),placeholder:"Control channels MHz (comma separated)"}),l("input",{className:"input",value:M.service_tag,onChange:n=>Q(m=>({...m,service_tag:n.target.value})),placeholder:"Service tag (optional)"}),l("div",{className:"button-row",children:l(N,{onClick:h,disabled:r,children:"Add Trunked"})}),l("div",{className:"muted",style:{marginBottom:"6px",marginTop:"10px"},children:"Conventional"}),l("input",{className:"input",value:J.alpha_tag,onChange:n=>ce(m=>({...m,alpha_tag:n.target.value})),placeholder:"Channel label (alpha tag)"}),l("input",{className:"input",value:J.frequency,onChange:n=>ce(m=>({...m,frequency:n.target.value})),placeholder:"Frequency MHz"}),l("input",{className:"input",value:J.service_tag,onChange:n=>ce(m=>({...m,service_tag:n.target.value})),placeholder:"Service tag (optional)"}),l("div",{className:"button-row",children:l(N,{onClick:be,disabled:r,children:"Add Conventional"})})]}),T("div",{className:"card",children:[T("div",{className:"muted",style:{marginBottom:"8px"},children:["Current Favorites (",u.length,")"]}),u.length===0?l("div",{className:"muted",children:"No custom favorites yet."}):T("div",{className:"list",children:[Oe.map(n=>T("div",{className:"row",style:{marginBottom:"8px"},children:[T("div",{children:[l("div",{children:l("strong",{children:n.system_name||"Custom Trunked"})}),T("div",{className:"muted",children:[n.department_name||"Department"," - TGID ",n.talkgroup]}),T("div",{className:"muted",children:[n.alpha_tag||"Channel"," - ",n.control_channels.join(", ")," MHz"]})]}),l(N,{variant:"danger",onClick:()=>xe(n.id),disabled:r,children:"Remove"})]},n.id)),Fe.map(n=>T("div",{className:"row",style:{marginBottom:"8px"},children:[T("div",{children:[l("div",{children:l("strong",{children:n.alpha_tag||"Conventional"})}),T("div",{className:"muted",children:[n.frequency.toFixed(4)," MHz",n.service_tag>0?` - Service ${n.service_tag}`:""]})]}),l(N,{variant:"danger",onClick:()=>xe(n.id),disabled:r,children:"Remove"})]},n.id))]})]}),T("div",{className:"button-row",children:[l(N,{onClick:It,disabled:r,children:"Save Favorites Mode"}),l(N,{variant:"secondary",onClick:Mt,disabled:r,children:"Use Full Database"})]}),b?l("div",{className:"message",children:b}):null,A?l("div",{className:"error",children:A}):null,e.error?l("div",{className:"error",children:e.error}):null]})}import{useEffect as sa,useMemo as Tt,useState as la}from"https://esm.sh/react@18";import{jsx as P,jsxs as X}from"https://esm.sh/react@18/jsx-runtime";function da(e){return Array.isArray(e)?e.map((a,t)=>a&&typeof a=="object"?{id:String(a.id??`${a.type||"item"}-${t}`),label:String(a.label||a.alpha_tag||a.name||`Avoid ${t+1}`),type:String(a.type||"item"),source:"persistent"}:{id:`item-${t}`,label:String(a),type:"item",source:"persistent"}):[]}function ca(e){if(!Array.isArray(e))return[];let a=[],t=new Set;return e.forEach(o=>{let r=String(o||"").trim();!r||t.has(r)||(t.add(r),a.push({id:`runtime:${r}`,label:r,type:"system",token:r,source:"runtime"}))}),a}function Qe(){let{state:e,saveHpState:a,avoidCurrent:t,clearHpAvoids:o,removeHpAvoid:r,navigate:s}=L(),{hpState:i,hpAvoids:u,working:c}=e,A=Tt(()=>Array.isArray(i.avoid_list)?i.avoid_list:Array.isArray(i.avoids)?i.avoids:Array.isArray(i.avoid)?i.avoid:[],[i.avoid_list,i.avoids,i.avoid]),[y,b]=la([]);sa(()=>{b(da(A))},[A]);let g=Tt(()=>ca(u),[u]),w=async(k=y)=>{try{await a({avoid_list:k})}catch{}},D=async()=>{try{await t()}catch{}};return X("section",{className:"screen avoid-screen",children:[P(H,{title:"Avoid",showBack:!0,onBack:()=>s(d.MENU)}),X("div",{className:"list",children:[X("div",{className:"card",children:[P("div",{className:"muted",style:{marginBottom:"8px"},children:"Runtime Avoids (HP Scan Pool)"}),g.length===0?P("div",{className:"muted",children:"No runtime HP avoids."}):g.map(k=>X("div",{className:"row",style:{marginBottom:"6px"},children:[X("div",{children:[P("div",{children:k.label}),P("div",{className:"muted",children:k.type})]}),P(N,{variant:"danger",onClick:()=>r(k.token),disabled:c,children:"Remove"})]},k.id))]}),X("div",{className:"card",children:[P("div",{className:"muted",style:{marginBottom:"8px"},children:"Persistent Avoids (State)"}),y.length===0?P("div",{className:"muted",children:"No persistent avoids in current state."}):y.map(k=>X("div",{className:"row",style:{marginBottom:"6px"},children:[X("div",{children:[P("div",{children:k.label}),P("div",{className:"muted",children:k.type})]}),P(N,{variant:"danger",onClick:()=>{let E=y.filter(O=>O.id!==k.id);b(E),w(E)},disabled:c,children:"Remove"})]},k.id))]})]}),X("div",{className:"button-row",children:[P(N,{onClick:D,disabled:c,children:"Avoid Current"}),P(N,{variant:"secondary",onClick:async()=>{b([]),await w([]),await o()},disabled:c,children:"Clear All"}),P(N,{onClick:()=>w(),disabled:c,children:"Save"})]}),e.error?P("div",{className:"error",children:e.error}):null]})}import{useEffect as ua,useState as pa}from"https://esm.sh/react@18";import{jsx as ae,jsxs as Re}from"https://esm.sh/react@18/jsx-runtime";function Je(){let{state:e,setMode:a,navigate:t}=L(),[o,r]=pa("hp");return ua(()=>{r(e.mode||"hp")},[e.mode]),Re("section",{className:"screen mode-selection-screen",children:[ae(H,{title:"Mode Selection",showBack:!0,onBack:()=>t(d.MENU)}),Re("div",{className:"list",children:[Re("label",{className:"row card",children:[ae("span",{children:"HP Mode"}),ae("input",{type:"radio",name:"scan-mode",value:"hp",checked:o==="hp",onChange:i=>r(i.target.value)})]}),Re("label",{className:"row card",children:[ae("span",{children:"Expert Mode"}),ae("input",{type:"radio",name:"scan-mode",value:"expert",checked:o==="expert",onChange:i=>r(i.target.value)})]})]}),ae("div",{className:"button-row",children:ae(N,{onClick:async()=>{try{await a(o),t(d.MENU)}catch{}},disabled:e.working,children:"Save"})}),e.error?ae("div",{className:"error",children:e.error}):null]})}import"https://esm.sh/react@18";import{jsx as ma}from"https://esm.sh/react@18/jsx-runtime";function Ze({label:e="Loading..."}){return ma("div",{className:"loading",children:e})}import{jsx as ee}from"https://esm.sh/react@18/jsx-runtime";function Xe(){let{state:e}=L();if(e.loading)return ee(Ze,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case d.MENU:return ee(Ve,{});case d.LOCATION:return ee(Ge,{});case d.SERVICE_TYPES:return ee(We,{});case d.RANGE:return ee(Ye,{});case d.FAVORITES:return ee(je,{});case d.AVOID:return ee(Qe,{});case d.MODE_SELECTION:return ee(Je,{});case d.MAIN:default:return ee(Ue,{})}}import{jsx as et,jsxs as ga}from"https://esm.sh/react@18/jsx-runtime";var va=`
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
`;function tt(){return et(St,{children:ga("div",{className:"app-shell",children:[et("style",{children:va}),et(Xe,{})]})})}import{jsx as wt}from"https://esm.sh/react@18/jsx-runtime";var Rt=document.getElementById("root");if(!Rt)throw new Error("Missing #root mount element");ya(Rt).render(wt(fa.StrictMode,{children:wt(tt,{})}));
