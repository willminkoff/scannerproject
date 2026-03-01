import Ie from"https://esm.sh/react@18";import{createRoot as ca}from"https://esm.sh/react-dom@18/client";import ie from"https://esm.sh/react@18";import Lt,{createContext as Mt,useCallback as H,useContext as Ot,useEffect as ue,useMemo as Pt,useRef as Dt,useReducer as Ht}from"https://esm.sh/react@18";var Ct={"Content-Type":"application/json"};async function z(e,{method:t="GET",body:a}={}){let n={method:t,headers:{...Ct}};a!==void 0&&(n.body=JSON.stringify(a));let s=await fetch(e,n),i=await s.text(),r={};try{r=i?JSON.parse(i):{}}catch{r={raw:i}}if(!s.ok){let u=r?.error||`Request failed (${s.status})`,f=new Error(u);throw f.status=s.status,f.payload=r,f}return r}function de(){return z("/api/hp/state")}function We(e){return z("/api/hp/state",{method:"POST",body:e})}function pe(){return z("/api/hp/service-types")}function ce(){return z("/api/hp/avoids")}function qe(){return z("/api/hp/avoids",{method:"POST",body:{action:"clear"}})}function Ye(e){return z("/api/hp/avoids",{method:"POST",body:{action:"remove",system:e}})}function Ke(){return z("/api/status")}function Ze(e){return z("/api/mode",{method:"POST",body:{mode:e}})}function Je(e={}){return z("/api/hp/hold",{method:"POST",body:e})}function Xe(e={}){return z("/api/hp/next",{method:"POST",body:e})}function Qe(e={}){return z("/api/hp/avoid",{method:"POST",body:e})}var p=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),Ft={hpState:{},serviceTypes:[],liveStatus:{},hpAvoids:[],currentScreen:p.MAIN,mode:"hp",sseConnected:!1,loading:!0,working:!1,error:"",message:""},Bt=["digital_scheduler_active_system","digital_scheduler_active_system_label","digital_scheduler_next_system","digital_scheduler_next_system_label","digital_scheduler_active_department_label","digital_last_label","digital_channel_label","digital_department_label","digital_system_label","digital_last_mode","digital_last_tgid","digital_profile","digital_scan_mode","stream_mount","digital_stream_mount","profile_airband","profile_ground","last_hit_airband_label","last_hit_ground_label"];function Re(e){return e==null?!1:typeof e=="string"?e.trim()!=="":Array.isArray(e)?e.length>0:!0}function ee(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach(n=>{let s=String(n||"").trim();!s||a.has(s)||(a.add(s),t.push(s))}),t}function zt(e,t){let a=t&&typeof t=="object"?t:{},n={...e||{},...a};return Bt.forEach(s=>{!Re(a[s])&&Re(e?.[s])&&(n[s]=e[s])}),n}function $t(e,t){switch(t.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:t.payload.hpState||{},serviceTypes:t.payload.serviceTypes||[],liveStatus:t.payload.liveStatus||{},hpAvoids:t.payload.hpAvoids||[],mode:t.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:t.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!t.payload};case"SET_ERROR":return{...e,error:t.payload||""};case"SET_MESSAGE":return{...e,message:t.payload||""};case"SET_HP_STATE":return{...e,hpState:t.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:t.payload||[]};case"SET_HP_AVOIDS":return{...e,hpAvoids:ee(t.payload)};case"SET_LIVE_STATUS":return{...e,liveStatus:zt(e.liveStatus,t.payload),hpAvoids:Array.isArray(t.payload?.hp_avoids)?ee(t.payload.hp_avoids):e.hpAvoids};case"SET_MODE":return{...e,mode:t.payload||e.mode};case"SET_SSE_CONNECTED":return{...e,sseConnected:!!t.payload};case"NAVIGATE":return{...e,currentScreen:t.payload||p.MAIN};default:return e}}var at=Mt(null);function et(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(a=>({service_tag:Number(a?.service_tag),name:String(a?.name||`Service ${a?.service_tag}`),enabled_by_default:!!a?.enabled_by_default}))}function tt(e){let t=e&&typeof e.state=="object"&&e.state!==null?e.state:{},a=String(e?.mode||"hp").toLowerCase();return{hpState:t,mode:a}}function rt({children:e}){let[t,a]=Ht($t,Ft),n=Dt(!1),s=H(c=>{a({type:"NAVIGATE",payload:c})},[]),i=H(async()=>{let c=await de(),o=tt(c);return a({type:"SET_HP_STATE",payload:o.hpState}),a({type:"SET_MODE",payload:o.mode}),o},[]),r=H(async()=>{let c=await pe(),o=et(c);return a({type:"SET_SERVICE_TYPES",payload:o}),o},[]),u=H(async()=>{let c=await ce(),o=ee(c?.avoids);return a({type:"SET_HP_AVOIDS",payload:o}),o},[]),f=H(async()=>{if(n.current)return null;n.current=!0;try{let c=await Ke();return a({type:"SET_LIVE_STATUS",payload:c||{}}),c}finally{n.current=!1}},[]),w=H(async()=>{a({type:"LOAD_START"});try{let[c,o,m]=await Promise.all([de(),pe(),ce()]),b=tt(c),l=et(o),_=ee(m?.avoids);a({type:"LOAD_SUCCESS",payload:{hpState:b.hpState,mode:b.mode,serviceTypes:l,liveStatus:{},hpAvoids:_}})}catch(c){a({type:"LOAD_ERROR",payload:c.message})}},[]);ue(()=>{w()},[w]),ue(()=>{let c=setInterval(()=>{f().catch(()=>{})},t.sseConnected?1e4:5e3);return()=>clearInterval(c)},[f,t.sseConnected]),ue(()=>{if(typeof EventSource>"u")return;let c=!1,o=null,m=null,b=()=>{c||(o=new EventSource("/api/stream"),o.onopen=()=>{a({type:"SET_SSE_CONNECTED",payload:!0})},o.addEventListener("status",l=>{try{let _=JSON.parse(l?.data||"{}");a({type:"SET_LIVE_STATUS",payload:_})}catch{}}),o.onerror=()=>{a({type:"SET_SSE_CONNECTED",payload:!1}),o&&(o.close(),o=null),c||(m=setTimeout(b,2e3))})};return b(),()=>{c=!0,a({type:"SET_SSE_CONNECTED",payload:!1}),m&&clearTimeout(m),o&&o.close()}},[]);let S=H(async c=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let o=c&&typeof c=="object"?{...c}:{},m=await We(o),b=m?.state&&typeof m.state=="object"?{...t.hpState,...m.state}:{...t.hpState,...o};return a({type:"SET_HP_STATE",payload:b}),a({type:"SET_MESSAGE",payload:"State saved"}),m}catch(o){throw a({type:"SET_ERROR",payload:o.message}),o}finally{a({type:"SET_WORKING",payload:!1})}},[t.hpState]),L=H(async c=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let o=await Ze(c),m=String(o?.mode||c||"hp").toLowerCase();return a({type:"SET_MODE",payload:m}),a({type:"SET_MESSAGE",payload:`Mode set to ${m}`}),o}catch(o){throw a({type:"SET_ERROR",payload:o.message}),o}finally{a({type:"SET_WORKING",payload:!1})}},[]),y=H(async(c,o)=>{a({type:"SET_WORKING",payload:!0}),a({type:"SET_ERROR",payload:""});try{let m=await c();return Array.isArray(m?.avoids)&&a({type:"SET_HP_AVOIDS",payload:m.avoids}),o&&a({type:"SET_MESSAGE",payload:o}),await i(),await f(),m}catch(m){throw a({type:"SET_ERROR",payload:m.message}),m}finally{a({type:"SET_WORKING",payload:!1})}},[i,f]),N=H(async()=>y(()=>Je(),"Hold command sent"),[y]),k=H(async()=>y(()=>Xe(),"Next command sent"),[y]),h=H(async(c={})=>y(()=>Qe(c),"Avoid command sent"),[y]),g=H(async()=>y(()=>qe(),"Runtime avoids cleared"),[y]),T=H(async c=>y(()=>Ye(c),"Avoid removed"),[y]),C=Pt(()=>({state:t,dispatch:a,navigate:s,refreshAll:w,refreshHpState:i,refreshServiceTypes:r,refreshHpAvoids:u,refreshStatus:f,saveHpState:S,setMode:L,holdScan:N,nextScan:k,avoidCurrent:h,clearHpAvoids:g,removeHpAvoid:T,SCREENS:p}),[t,s,w,i,r,u,f,S,L,N,k,h,g,T]);return Lt.createElement(at.Provider,{value:C},e)}function P(){let e=Ot(at);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import G from"https://esm.sh/react@18";import d,{useEffect as me,useMemo as it,useState as Y}from"https://esm.sh/react@18";import Ut from"https://esm.sh/react@18";function D({children:e,onClick:t,type:a="button",variant:n="primary",className:s="",disabled:i=!1}){return Ut.createElement("button",{type:a,className:`btn ${n==="secondary"?"btn-secondary":n==="danger"?"btn-danger":""} ${s}`.trim(),onClick:t,disabled:i},e)}var nt=["|","/","-","\\"],Gt=6e3;function F(e){return e==null||e===""?"--":String(e)}function Vt(e){let t=Number(e);return!Number.isFinite(t)||t<=0?0:t<1e10?t*1e3:t}function jt(e){let t=Math.max(0,Math.min(4,Number(e)||0));return`${"|".repeat(t)}${".".repeat(4-t)}`}function Wt(e){let t=Number(e);return Number.isFinite(t)?Number.isInteger(t)?`Range ${t}`:`Range ${t.toFixed(1)}`:"Range"}function qt(e,t){let a=t==="ground"?e?.profile_ground:e?.profile_airband,n=t==="ground"?e?.profiles_ground:e?.profiles_airband,s=Array.isArray(n)?n:[],i=String(a||"").trim();if(!i)return"";let r=s.find(f=>String(f?.id||"").trim().toLowerCase()===i.toLowerCase());return String(r?.label||"").trim()||i}function Yt(e,t){let a=String(t||"").trim(),n=String(e||"").trim();if(!n&&!a)return{system:"--",department:"--",channel:"--"};let s=[" | "," - "," / "," \u2014 "," \u2013 ","::"];for(let i of s){if(!n.includes(i))continue;let r=n.split(i).map(u=>String(u||"").trim()).filter(Boolean);if(r.length>=3)return{system:r[0],department:r[1],channel:r.slice(2).join(" / ")};if(r.length===2)return{system:a||r[0],department:r[0],channel:r[1]}}return{system:a||n||"--",department:n||a||"--",channel:n||"--"}}function fe(){let{state:e,holdScan:t,nextScan:a,avoidCurrent:n,navigate:s}=P(),{hpState:i,liveStatus:r,working:u,error:f,message:w}=e,S=String(r?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),L=String(r?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),y=!!S,N=!!L,k=(e.mode==="hp"||e.mode==="expert")&&N?"digital":"analog",[h,g]=Y(k),[T,C]=Y(""),[c,o]=Y(!1),[m,b]=Y(""),[l,_]=Y(0);me(()=>{if(h==="digital"&&!N){g(y?"analog":"digital");return}h==="analog"&&!y&&N&&g("digital")},[y,N,h]),me(()=>{!f&&!w||b("")},[f,w]),me(()=>{let v=setInterval(()=>{_(W=>(W+1)%nt.length)},320);return()=>clearInterval(v)},[]);let x=h==="digital"?L||S:S||L,E=h==="digital"&&N,V=String(i.mode||"full_database").trim().toLowerCase(),X=nt[l]||"|",ut=String(r?.profile_airband||"").trim(),mt=qt(r,"airband")||ut||"Analog",ft=r?.last_hit_airband_label||r?.last_hit_ground_label||r?.last_hit||"",Q=Yt(ft,mt),Le=E?r?.digital_scheduler_active_system_label||r?.digital_system_label||r?.digital_scheduler_active_system||i.system_name||i.system:Q.system,ne=E?r?.digital_department_label||r?.digital_scheduler_active_department_label||i.department_name||i.department||r?.digital_last_label:Q.department||i.department_name||i.department,Me=Vt(r?.digital_last_time),bt=Me>0?Math.max(0,Date.now()-Me):Number.POSITIVE_INFINITY,gt=!!(r?.digital_channel_label||r?.digital_last_label||r?.digital_last_tgid),Oe=E&&gt&&bt<=Gt,Pe=E&&Oe?r?.digital_last_tgid??i.tgid??i.talkgroup_id:"--",oe=E?(()=>{let v=Number(r?.digital_preflight?.playlist_frequency_hz?.[0]||r?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(v)&&v>0?(v/1e6).toFixed(4):i.frequency??i.freq})():r?.last_hit_airband||r?.last_hit_ground||r?.last_hit||"--",De=!!(r?.digital_control_channel_metric_ready??r?.digital_control_decode_available),He=E?r?.digital_control_channel_locked?"Locked":De?"Decoding":i.signal??i.signal_strength:r?.rtl_active?"Active":"Idle",Fe=E?r?.digital_channel_label||r?.digital_last_label||i.channel_name||i.channel||ne:Q.channel||ne,se=E&&(r?.digital_last_mode||i.service_type||i.service)||"",ht=E?Fe:Q.channel||Fe,vt=E?[F(se||"Digital"),Pe!=="--"?`TGID ${F(Pe)}`:"",oe!=="--"?`${F(oe)} MHz`:"",He].filter(Boolean).join(" \u2022 "):`${F(oe)} \u2022 ${He}`,yt=E?r?.digital_control_channel_locked?4:De?3:1:r?.rtl_active?3:1,Be=String(r?.digital_scan_mode||"").toLowerCase()==="single_system",ze=Be?"HOLD":"SCAN",R=E&&ze==="SCAN"&&!Oe,$e=it(()=>{if(V!=="favorites")return"Full Database";let v=String(i.favorites_name||"").trim()||"My Favorites",W=Array.isArray(i.favorites)?i.favorites:[],Tt=v.toLowerCase(),je=W.find(le=>!le||typeof le!="object"?!1:String(le.label||"").trim().toLowerCase()===Tt);return(Array.isArray(je?.custom_favorites)?je.custom_favorites:Array.isArray(i.custom_favorites)?i.custom_favorites:[]).length===0?`${v} (empty)`:v},[V,i.custom_favorites,i.favorites,i.favorites_name]),St=E?se?`Service: ${F(se)}`:"Service: Digital":V==="favorites"?`List: ${$e}`:"Full Database",Ue=R?`Scanning ${X}`:ne,_t=R?`Service: Digital ${X}`:St,Ge=R?`Scanning ${X}`:ht,Ve=R?`Digital \u2022 Awaiting activity ${X}`:vt,Nt=async()=>{try{await t()}catch{}},xt=async()=>{try{await a()}catch{}},Et=async()=>{try{await n()}catch{}},At=async(v,W)=>{if(v==="info"){b(W==="system"?`System: ${F(Le)}`:W==="department"?`Department: ${F(Ue)}`:`Channel: ${F(Ge)} (${F(Ve)})`),C("");return}if(v==="advanced"){b("Advanced options are still being wired in HP3."),C("");return}if(v==="prev"){b("Previous-channel stepping is not wired yet in HP3."),C("");return}if(v==="fave"){C(""),s(p.FAVORITES);return}if(!E){b("Switch Audio Source to Digital for HOLD/NEXT/AVOID controls."),C("");return}v==="hold"?await Nt():v==="next"?await xt():v==="avoid"&&await Et(),C("")},kt=it(()=>[{id:"squelch",label:"Squelch",onClick:()=>b("Squelch is currently managed from SB3 analog controls.")},{id:"range",label:Wt(i.range_miles),onClick:()=>s(p.RANGE)},{id:"atten",label:"Atten",onClick:()=>b("Attenuation toggle is not wired yet in HP3.")},{id:"gps",label:"GPS",onClick:()=>s(p.LOCATION)},{id:"help",label:"Help",onClick:()=>s(p.MENU)}],[i.range_miles,s]),wt={system:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],department:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],channel:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"hold",label:"Hold"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"},{id:"fave",label:"Fave"}]};return d.createElement("section",{className:"screen main-screen hp2-main"},d.createElement("div",{className:"hp2-radio-bar"},d.createElement("div",{className:"hp2-radio-buttons"},kt.map(v=>d.createElement("button",{key:v.id,type:"button",className:"hp2-radio-btn",onClick:v.onClick,disabled:u},v.label))),d.createElement("div",{className:"hp2-status-icons"},d.createElement("span",{className:`hp2-icon ${Be?"on":""}`},ze),d.createElement("span",{className:"hp2-icon"},"SIG ",jt(yt)),d.createElement("span",{className:"hp2-icon"},E?"DIG":"ANA"))),d.createElement("div",{className:"hp2-lines"},d.createElement("div",{className:"hp2-line"},d.createElement("div",{className:"hp2-line-label"},"System / Favorite List"),d.createElement("div",{className:"hp2-line-body"},d.createElement("div",{className:"hp2-line-primary"},F(Le)),d.createElement("div",{className:"hp2-line-secondary"},$e)),d.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>C(v=>v==="system"?"":"system"),disabled:u},"<")),d.createElement("div",{className:"hp2-line"},d.createElement("div",{className:"hp2-line-label"},"Department"),d.createElement("div",{className:"hp2-line-body"},d.createElement("div",{className:"hp2-line-primary"},F(Ue)),d.createElement("div",{className:"hp2-line-secondary"},_t)),d.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>C(v=>v==="department"?"":"department"),disabled:u},"<")),d.createElement("div",{className:"hp2-line channel"},d.createElement("div",{className:"hp2-line-label"},"Channel"),d.createElement("div",{className:"hp2-line-body"},d.createElement("div",{className:"hp2-line-primary"},F(Ge)),d.createElement("div",{className:"hp2-line-secondary"},F(Ve))),d.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>C(v=>v==="channel"?"":"channel"),disabled:u},"<"))),T?d.createElement("div",{className:"hp2-submenu-popup"},wt[T]?.map(v=>d.createElement("button",{key:v.id,type:"button",className:"hp2-submenu-btn",onClick:()=>At(v.id,T),disabled:u},v.label))):null,d.createElement("div",{className:"hp2-feature-bar"},d.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>s(p.MENU),disabled:u},"Menu"),d.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>b("Replay is not wired yet in HP3."),disabled:u},"Replay"),d.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>b("Recording controls are not wired yet in HP3."),disabled:u},"Record"),d.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>o(v=>!v),disabled:u},c?"Unmute":"Mute")),d.createElement("div",{className:"hp2-web-audio"},d.createElement("div",{className:"hp2-audio-head"},d.createElement("div",{className:"muted"},"Web Audio Stream"),x?d.createElement("a",{href:`/stream/${x}`,target:"_blank",rel:"noreferrer"},"Open"):null),d.createElement("div",{className:"hp2-source-switch"},d.createElement(D,{variant:h==="analog"?"primary":"secondary",onClick:()=>g("analog"),disabled:!y||u},"Analog"),d.createElement(D,{variant:h==="digital"?"primary":"secondary",onClick:()=>g("digital"),disabled:!N||u},"Digital")),d.createElement("div",{className:"muted hp2-audio-meta"},"Source: ",E?"Digital":"Analog"," (",x||"no mount",")"),d.createElement("audio",{controls:!0,preload:"none",muted:c,className:"hp2-audio-player",src:x?`/stream/${x}`:"/stream/"})),m?d.createElement("div",{className:"message"},m):null,E?null:d.createElement("div",{className:"muted"},"HOLD/NEXT/AVOID actions require Digital source."),f?d.createElement("div",{className:"error"},f):null,w?d.createElement("div",{className:"message"},w):null)}import Z from"https://esm.sh/react@18";import K from"https://esm.sh/react@18";function $({title:e,subtitle:t="",showBack:a=!1,onBack:n}){return K.createElement("div",{className:"header"},K.createElement("div",null,K.createElement("h1",null,e),t?K.createElement("div",{className:"sub"},t):null),a?K.createElement("button",{type:"button",className:"btn btn-secondary",onClick:n},"Back"):null)}var Kt=[{id:p.LOCATION,label:"Set Your Location"},{id:p.SERVICE_TYPES,label:"Select Service Types"},{id:p.RANGE,label:"Set Range"},{id:p.FAVORITES,label:"Manage Favorites"},{id:p.AVOID,label:"Avoid Options"},{id:p.MODE_SELECTION,label:"Mode Selection"}];function be(){let{navigate:e,state:t}=P();return Z.createElement("section",{className:"screen menu"},Z.createElement($,{title:"Menu",showBack:!0,onBack:()=>e(p.MAIN)}),Z.createElement("div",{className:"menu-list"},Kt.map(a=>Z.createElement(D,{key:a.id,variant:"secondary",className:"menu-item",onClick:()=>e(a.id),disabled:t.working},a.label))),t.error?Z.createElement("div",{className:"error"},t.error):null)}import O,{useEffect as Zt,useState as J}from"https://esm.sh/react@18";function ot(e){if(e===""||e===null||e===void 0)return null;let t=Number(e);return Number.isFinite(t)?t:NaN}function ge(){let{state:e,saveHpState:t,navigate:a}=P(),{hpState:n,working:s}=e,[i,r]=J(""),[u,f]=J(""),[w,S]=J(""),[L,y]=J(!0),[N,k]=J("");return Zt(()=>{r(n.zip||n.postal_code||""),f(n.lat!==void 0&&n.lat!==null?String(n.lat):n.latitude!==void 0&&n.latitude!==null?String(n.latitude):""),S(n.lon!==void 0&&n.lon!==null?String(n.lon):n.longitude!==void 0&&n.longitude!==null?String(n.longitude):""),y(n.use_location!==!1)},[n]),O.createElement("section",{className:"screen location-screen"},O.createElement($,{title:"Location",showBack:!0,onBack:()=>a(p.MENU)}),O.createElement("div",{className:"list"},O.createElement("label",null,O.createElement("div",{className:"muted"},"ZIP"),O.createElement("input",{className:"input",value:i,onChange:g=>r(g.target.value.trim()),placeholder:"37201"})),O.createElement("label",null,O.createElement("div",{className:"muted"},"Latitude"),O.createElement("input",{className:"input",value:u,onChange:g=>f(g.target.value),placeholder:"36.12"})),O.createElement("label",null,O.createElement("div",{className:"muted"},"Longitude"),O.createElement("input",{className:"input",value:w,onChange:g=>S(g.target.value),placeholder:"-86.67"})),O.createElement("label",{className:"row"},O.createElement("span",null,"Use location for scanning"),O.createElement("input",{type:"checkbox",checked:L,onChange:g=>y(g.target.checked)}))),O.createElement("div",{className:"button-row"},O.createElement(D,{onClick:async()=>{if(k(""),i&&!/^\d{5}(-\d{4})?$/.test(i)){k("ZIP must be 5 digits or ZIP+4.");return}let g=ot(u),T=ot(w);if(Number.isNaN(g)||Number.isNaN(T)){k("Latitude and longitude must be valid numbers.");return}if(g===null!=(T===null)){k("Enter both latitude and longitude, or leave both blank.");return}if(g!==null&&(g<-90||g>90)){k("Latitude must be between -90 and 90.");return}if(T!==null&&(T<-180||T>180)){k("Longitude must be between -180 and 180.");return}try{let C={zip:i,use_location:L};g!==null&&T!==null?(C.lat=g,C.lon=T):i&&(C.resolve_zip=!0),await t(C),a(p.MENU)}catch{}},disabled:s},"Save")),N?O.createElement("div",{className:"error"},N):null,e.error?O.createElement("div",{className:"error"},e.error):null)}import I,{useEffect as st,useMemo as he,useState as te}from"https://esm.sh/react@18";var ve=8,Jt=8;function lt(e){let t=Number(e);return Number.isFinite(t)?t:0}function ye(){let{state:e,saveHpState:t,navigate:a}=P(),{hpState:n,serviceTypes:s,working:i}=e,r=he(()=>s.filter(o=>o.enabled_by_default).map(o=>Number(o.service_tag)),[s]),u=he(()=>[...s].sort((o,m)=>{let b=lt(o.service_tag),l=lt(m.service_tag);return b!==l?b-l:String(o.name||"").localeCompare(String(m.name||""))}),[s]),[f,w]=te([]),[S,L]=te(0),[y,N]=te(""),[k,h]=te("");st(()=>{let o=Array.isArray(n.enabled_service_tags)?n.enabled_service_tags.map(Number):r;w(Array.from(new Set(o)).filter(m=>Number.isFinite(m)&&m>0))},[n.enabled_service_tags,r]);let g=Math.max(1,Math.ceil(u.length/ve));st(()=>{S>=g&&L(Math.max(0,g-1))},[S,g]);let T=he(()=>{let o=S*ve,b=[...u.slice(o,o+ve)];for(;b.length<Jt;)b.push(null);return b},[u,S]),C=o=>{h(""),N(""),w(m=>m.includes(o)?m.filter(b=>b!==o):[...m,o])},c=async o=>{h(""),N("");try{await t({enabled_service_tags:[...f].sort((m,b)=>m-b)}),typeof o=="function"?o():N("Service types saved.")}catch(m){h(m?.message||"Failed to save service types.")}};return I.createElement("section",{className:"screen hp2-picker service-types-screen"},I.createElement("div",{className:"hp2-picker-top"},I.createElement("div",{className:"hp2-picker-title"},"Select Service Types"),I.createElement("div",{className:"hp2-picker-top-right"},I.createElement("span",{className:"hp2-picker-help"},"Help"),I.createElement("span",{className:"hp2-picker-status"},"L"),I.createElement("span",{className:"hp2-picker-status"},"SIG"),I.createElement("span",{className:"hp2-picker-status"},"BAT"))),I.createElement("div",{className:"hp2-picker-grid"},T.map((o,m)=>{if(!o)return I.createElement("div",{key:`empty-${m}`,className:"hp2-picker-tile hp2-picker-tile-empty"});let b=Number(o.service_tag),l=f.includes(b);return I.createElement("button",{key:`${b}-${o.name}`,type:"button",className:`hp2-picker-tile ${l?"active":""}`,onClick:()=>C(b),disabled:i},o.name)})),I.createElement("div",{className:"hp2-picker-bottom hp2-picker-bottom-5"},I.createElement("button",{type:"button",className:"hp2-picker-btn listen",onClick:()=>c(()=>a(p.MAIN)),disabled:i},"Listen"),I.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>a(p.MENU),disabled:i},"Back"),I.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>c(()=>a(p.MENU)),disabled:i},"Accept"),I.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>L(o=>Math.max(0,o-1)),disabled:i||S<=0},"^"),I.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>L(o=>Math.min(g-1,o+1)),disabled:i||S>=g-1},"v")),I.createElement("div",{className:"muted hp2-picker-page"},"Page ",S+1," / ",g),y?I.createElement("div",{className:"message"},y):null,k?I.createElement("div",{className:"error"},k):null,e.error?I.createElement("div",{className:"error"},e.error):null)}import U,{useEffect as Xt,useState as Qt}from"https://esm.sh/react@18";function Se(){let{state:e,saveHpState:t,navigate:a}=P(),{hpState:n,working:s}=e,[i,r]=Qt(15);Xt(()=>{let f=Number(n.range_miles);r(Number.isFinite(f)?f:15)},[n.range_miles]);let u=async()=>{try{await t({range_miles:i}),a(p.MENU)}catch{}};return U.createElement("section",{className:"screen range-screen"},U.createElement($,{title:"Range",showBack:!0,onBack:()=>a(p.MENU)}),U.createElement("div",{className:"card"},U.createElement("div",{className:"row"},U.createElement("span",null,"Range Miles"),U.createElement("strong",null,i.toFixed(1))),U.createElement("input",{className:"range",type:"range",min:"0",max:"30",step:"0.5",value:i,onChange:f=>r(Number(f.target.value))})),U.createElement("div",{className:"button-row"},U.createElement(D,{onClick:u,disabled:s},"Save")),e.error?U.createElement("div",{className:"error"},e.error):null)}import M,{useEffect as dt,useMemo as _e,useState as ae}from"https://esm.sh/react@18";
var re=8,Rt=8,q="action:full_database",Ne="action:create_list";
function favName(e,t="My Favorites"){return String(e||"").trim()||t}
function xe(e){return`list:${favName(e).toLowerCase()}`}
function favSlug(e){return String(e||"").trim().toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"")}
function favPageSlots(e,t){let a=t*re,n=[...e.slice(a,a+re)];for(;n.length<Rt;)n.push(null);return n}
function favNormalizeCustomEntries(e){
  let t=[];
  if(!Array.isArray(e))return t;
  for(let a of e){
    if(!a||typeof a!="object")continue;
    let n=String(a.kind||"").trim().toLowerCase();
    if(n!=="trunked"&&n!=="conventional")continue;
    let s={
      id:String(a.id||"").trim(),
      kind:n,
      system_name:String(a.system_name||"").trim(),
      department_name:String(a.department_name||"").trim(),
      alpha_tag:String(a.alpha_tag||a.channel_name||"").trim(),
      service_tag:Number.isFinite(Number(a.service_tag))?Number(a.service_tag):0,
      mode:String(a.mode||"").trim(),
    };
    if(n==="trunked"){
      let i=String(a.talkgroup||a.tgid||"").trim();
      if(!/^\d+$/.test(i))continue;
      let r=[],u=new Set,f=Array.isArray(a.control_channels)?a.control_channels:[];
      for(let w of f){
        let S=Number(w);
        if(!Number.isFinite(S)||S<=0)continue;
        let L=Math.round(S*1e6)/1e6;
        if(u.has(L))continue;
        u.add(L),r.push(L)
      }
      if(!r.length&&!s.system_name)continue;
      s.talkgroup=Number(i),s.control_channels=r.sort((w,S)=>w-S)
    }else{
      let i=Number(a.frequency);
      if(!Number.isFinite(i)||i<=0)continue;
      s.frequency=Math.round(i*1e6)/1e6
    }
    if(!s.id){
      if(n==="trunked")s.id=`tgid:${s.system_name||"sys"}:${s.talkgroup}`;
      else s.id=`freq:${s.system_name||"sys"}:${s.frequency}`
    }
    t.push(s)
  }
  return t
}
function favBuildListModel(e){
  let t=e&&typeof e=="object"?e:{},a=[],n=new Set,s={};
  let i=(r,u)=>{
    let f=favName(r,"");
    if(!f)return;
    let w=f.toLowerCase();
    n.has(w)||(n.add(w),a.push(f));
    let S=favNormalizeCustomEntries(u);
    Array.isArray(s[w])?S.length&&(s[w]=favMergeCustomChannels(s[w],S)):s[w]=S
  };
  (Array.isArray(t.favorites)?t.favorites:[]).forEach(r=>{
    if(!r||typeof r!="object")return;
    i(r.label||r.name,r.custom_favorites)
  });
  let r=favName(t.favorites_name||"My Favorites");
  i(r,t.custom_favorites);
  a.length||(a.push("My Favorites"),s["my favorites"]=favNormalizeCustomEntries(t.custom_favorites));
  for(let u of a){
    let f=u.toLowerCase();
    Array.isArray(s[f])||(s[f]=[])
  }
  return{names:a,customByName:s,activeName:r}
}
function favBuildFavoritesPayload(e,t,a){
  let n=favName(t).toLowerCase();
  return e.map((s,i)=>{
    let r=favName(s,`List ${i+1}`),u=r.toLowerCase(),f=favSlug(r)||String(i+1);
    return{
      id:`fav-${f}`,
      type:"list",
      target:"favorites",
      profile_id:"",
      label:r,
      enabled:u===n,
      custom_favorites:Array.isArray(a[u])?favNormalizeCustomEntries(a[u]):[]
    }
  })
}
function favMergeCustomChannels(e,t){
  let a=new Map;
  for(let n of favNormalizeCustomEntries(e||[]))a.set(String(n.id),n);
  for(let n of favNormalizeCustomEntries(t||[]))a.set(String(n.id),n);
  return Array.from(a.values()).sort((n,s)=>{
    let i=String(n.kind||"").localeCompare(String(s.kind||""));
    if(i!==0)return i;
    i=String(n.system_name||"").localeCompare(String(s.system_name||""));
    if(i!==0)return i;
    i=String(n.department_name||"").localeCompare(String(s.department_name||""));
    if(i!==0)return i;
    if(n.kind==="trunked"||s.kind==="trunked")return Number(n.talkgroup||0)-Number(s.talkgroup||0);
    return Number(n.frequency||0)-Number(s.frequency||0)
  })
}
async function favWizardFetch(e,t={}){
  let a=new URLSearchParams;
  Object.entries(t||{}).forEach(([n,s])=>{
    if(s===void 0||s===null||s==="")return;
    a.set(n,String(s))
  });
  let n=a.toString();
  return z(`${e}${n?`?${n}`:""}`)
}
function favReviewLines(e){
  if(!e||typeof e!="object")return[""];
  let t=String(e.system_name||"System").trim(),a=String(e.department_name||"").trim(),n=String(e.alpha_tag||"").trim();
  if(e.kind==="trunked"){
    let s=`TG ${e.talkgroup||"--"}`;
    return[t||"Trunked",a||"Department",n?`${s} ${n}`:s]
  }
  let s=Number(e.frequency),i=Number.isFinite(s)&&s>0?`${s.toFixed(4)} MHz`:"Frequency";
  return[t||"Conventional",a||"Department",n||i]
}
function favChannelLines(e){
  if(!e||typeof e!="object")return[""];
  let t=String(e.alpha_tag||"").trim(),a=String(e.department_name||e.system_name||"").trim();
  if(String(e.kind||"").toLowerCase()==="trunked"){
    let n=`TG ${e.talkgroup||"--"}`;
    return[t||n,a||n,n]
  }
  let n=Number(e.frequency),s=Number.isFinite(n)&&n>0?`${n.toFixed(4)} MHz`:"Frequency";
  return[t||s,a||s,s]
}
function Ee(){
  let{state:e,saveHpState:t,navigate:a}=P(),{hpState:n,working:s}=e,[i,r]=ae(q),[u,f]=ae(0),[w,S]=ae(""),[L,y]=ae(""),[N,k]=ae("manage"),[h,g]=ae(""),[T,C]=ae(""),[c,o]=ae(!1),[m,b]=ae([]),[l,_]=ae([]),[x,E]=ae([]),[V,X]=ae([]),[ut,mt]=ae([]),[ft,Q]=ae([]),[Le,ne]=ae([]),[Me,bt]=ae(1),[gt,Oe]=ae(0),[Pe,oe]=ae(0),[De,He]=ae("digital"),[Fe,se]=ae(null),[ht,vt]=ae("");
  let yt=_e(()=>favBuildListModel(n),[n.favorites,n.favorites_name,n.custom_favorites]),Be=yt.names,ze=yt.customByName,R=String(n.mode||"").trim().toLowerCase()==="favorites"?"favorites":"full_database",$e=favName(n.favorites_name||yt.activeName||Be[0]||"My Favorites"),St=_e(()=>{let d=Be.map(v=>({id:xe(v),label:v,kind:"list"})),W=[{id:q,label:"Select Database to Monitor",kind:"action",multiline:!0}];return d[0]&&W.push(d[0]),W.push({id:Ne,label:"Create New List",kind:"action"}),d[1]&&W.push(d[1]),d.slice(2).forEach(v=>W.push(v)),W},[Be]),Ue=_e(()=>{let d=favName(h||$e||Be[0]||"My Favorites").toLowerCase();return Array.isArray(ze[d])?ze[d]:[]},[h,$e,Be,ze]),_t=_e(()=>{let d=Array.isArray(ut)?ut:[];if(!ht)return d;let v=String(ht).trim().toLowerCase();return d.filter(W=>String(W?.department_name||W?.system_name||"").trim().toLowerCase()===v)},[ut,ht]),Ge=_e(()=>new Set(ft.map(d=>String(d))),[ft]);
  dt(()=>{if(N!=="manage")return;let d=R==="favorites"?xe($e):q;St.some(v=>v.id===d)||(d=St[0]?.id||q),r(d),f(Math.floor(Math.max(0,St.findIndex(v=>v.id===d))/re)),g($e)},[N,R,$e,St]);
  dt(()=>{f(0)},[N]);
  dt(()=>{if(N!=="review")return;Ue.some(d=>String(d.id)===String(T))||C(Ue[0]?String(Ue[0].id):"")},[N,Ue,T]);
  let favItemsForView=_e(()=>{if(N==="manage")return St;if(N==="review")return Ue;if(N==="country")return m;if(N==="state")return l;if(N==="county")return x;if(N==="system")return V;if(N==="department")return Le.map(d=>({id:`dept:${d}`,name:d,label:d}));if(N==="channel")return _t;return[]},[N,St,Ue,m,l,x,V,Le,_t]),favPageCount=Math.max(1,Math.ceil(favItemsForView.length/re));
  dt(()=>{u>=favPageCount&&f(Math.max(0,favPageCount-1))},[u,favPageCount]);
  let favTiles=_e(()=>favPageSlots(favItemsForView,u),[favItemsForView,u]);
  let favSaveModel=async({mode:d=R,favoritesName:v=$e,names:W=Be,customMap:Tt=ze}={})=>{
    let je=Array.isArray(W)&&W.length?W.map(le=>favName(le,"")).filter(Boolean):["My Favorites"];
    je.length||(je=["My Favorites"]);
    let K=favName(v||je[0]||"My Favorites"),Kt={};
    Object.entries(Tt||{}).forEach(([le,ot])=>{Kt[String(le||"").toLowerCase()]=favNormalizeCustomEntries(ot)}),je.forEach(le=>{let ot=le.toLowerCase();Array.isArray(Kt[ot])||(Kt[ot]=[])});
    let Z=favBuildFavoritesPayload(je,K,Kt),J=Array.isArray(Kt[K.toLowerCase()])?Kt[K.toLowerCase()]:[];
    await t({mode:d,favorites_name:K,favorites:Z,custom_favorites:J})
  };
  let favSetFullDatabase=async()=>{y(""),S("");try{await favSaveModel({mode:"full_database",favoritesName:$e}),r(q),S("Monitoring Full Database.")}catch(d){y(d?.message||"Failed to switch to Full Database.")}};
  let favSelectList=async(d,{goMain:v=!1}={})=>{let W=favName(d);y(""),S("");try{await favSaveModel({mode:"favorites",favoritesName:W}),r(xe(W)),g(W),S(`Selected favorites list: ${W}`),v&&a(p.MAIN)}catch(Tt){y(Tt?.message||"Failed to select favorites list.")}};
  let favCreateList=async()=>{let d=window.prompt("New favorites list name","New List");if(d===null)return;let v=favName(d,"");if(!v){y("List name is required.");return}if(Be.some(W=>favName(W).toLowerCase()===v.toLowerCase())){y("That list name already exists.");return}let W=[...Be,v],Tt={...ze,[v.toLowerCase()]:[]};y(""),S("");try{await favSaveModel({mode:"favorites",favoritesName:v,names:W,customMap:Tt}),r(xe(v)),g(v),S(`Created favorites list: ${v}`)}catch(je){y(je?.message||"Failed to create favorites list.")}};
  let favOpenReview=()=>{let d=St.find(v=>v.id===i);if(!d||d.kind!=="list"){y("Select a favorites list first.");return}y(""),S(""),g(favName(d.label)),k("review")};
  let favDeleteSelected=async()=>{if(!T){y("Select a channel/TG to remove.");return}let d=favName(h||$e||Be[0]||"My Favorites"),v=d.toLowerCase(),W=Ue.filter(Tt=>String(Tt.id)!==String(T)),Tt={...ze,[v]:W};y(""),S("");try{await favSaveModel({customMap:Tt}),S(`Removed entry from ${d}.`),C(W[0]?String(W[0].id):"")}catch(je){y(je?.message||"Failed to remove entry.")}};
  let favLoadCountries=async()=>{o(!0),y(""),S("");try{let d=await favWizardFetch("/api/hp/favorites-wizard/countries"),v=Array.isArray(d?.countries)?d.countries:[];b(v);let W=Number(v[0]?.country_id||1)||1;bt(W),_([]),E([]),X([]),mt([]),Q([]),ne([]),Oe(0),oe(0),se(null),vt(""),k("country")}catch(d){y(d?.message||"Failed to load countries.")}finally{o(!1)}};
  let favLoadStates=async d=>{let v=Number(d||0);if(!v)return;o(!0),y(""),S("");try{let W=await favWizardFetch("/api/hp/favorites-wizard/states",{country_id:v}),Tt=Array.isArray(W?.states)?W.states:[];_(Tt),Oe(Number(Tt[0]?.state_id||0)||0),E([]),X([]),mt([]),Q([]),ne([]),oe(0),se(null),vt(""),k("state")}catch(W){y(W?.message||"Failed to load states.")}finally{o(!1)}};
  let favLoadCounties=async d=>{let v=Number(d||0);if(!v)return;o(!0),y(""),S("");try{let W=await favWizardFetch("/api/hp/favorites-wizard/counties",{state_id:v}),Tt=Array.isArray(W?.counties)?W.counties:[];E(Tt),oe(Number(Tt[0]?.county_id||0)||0),X([]),mt([]),Q([]),ne([]),se(null),vt(""),k("county")}catch(W){y(W?.message||"Failed to load counties.")}finally{o(!1)}};
  let favLoadSystems=async(d,v,W)=>{let Tt=Number(d||0),je=Number(v||0),le=String(W||"digital").toLowerCase()==="analog"?"analog":"digital";if(!Tt)return;o(!0),y(""),S("");try{let ot=await favWizardFetch("/api/hp/favorites-wizard/systems",{state_id:Tt,county_id:je,system_type:le}),Z=Array.isArray(ot?.systems)?ot.systems:[];X(Z),se(null),mt([]),Q([]),ne([]),vt(""),k("system")}catch(ot){y(ot?.message||"Failed to load systems.")}finally{o(!1)}};
  let favLoadChannelsForSystem=async d=>{if(!d)return;let v=String(d.id??d.key??"").trim();if(!v){y("Invalid system selection.");return}o(!0),y(""),S("");try{let W=await favWizardFetch("/api/hp/favorites-wizard/channels",{system_type:De,system_id:v,limit:5000}),Tt=Array.isArray(W?.channels)?W.channels:[];mt(Tt),Q([]),se(d);let je=[],le=new Set;for(let ot of Tt){let Z=favName(ot?.department_name||ot?.system_name||"Department","");if(!Z)continue;let J=Z.toLowerCase();le.has(J)||(le.add(J),je.push(Z))}ne(je),je.length<=1?(vt(je[0]||""),k("channel")):(vt(""),k("department"))}catch(W){y(W?.message||"Failed to load channels.")}finally{o(!1)}};
  let favAddSelectedChannels=async()=>{let d=favName(h||$e||Be[0]||"My Favorites"),v=d.toLowerCase(),W=_t.filter(je=>Ge.has(String(je.id)));if(!W.length){y("Select at least one channel/TG to add.");return}if(!window.confirm(`Add ${W.length} selected channel${W.length===1?"":"s"} to ${d}?`))return;let Tt=favMergeCustomChannels(Array.isArray(ze[v])?ze[v]:[],W),je={...ze,[v]:Tt};y(""),S("");try{await favSaveModel({customMap:je}),S(`Added ${W.length} channel${W.length===1?"":"s"} to ${d}.`),k("review")}catch(le){y(le?.message||"Failed to add channels.")}};
  let favHandleManageTile=async d=>{if(!d||s||c)return;if(d.id===Ne){await favCreateList();return}r(d.id),d.kind==="list"&&(g(favName(d.label)),S("Press Rev/Edit to edit this list."),y(""))};
  let favHandleListen=async()=>{if(N==="manage"){let d=St.find(v=>v.id===i)||null;if(!d){a(p.MAIN);return}if(d.id===q){await favSetFullDatabase(),a(p.MAIN);return}if(d.id===Ne){await favCreateList();return}if(d.kind==="list"){await favSelectList(d.label,{goMain:!0});return}a(p.MAIN);return}a(p.MAIN)};
  let favHandleBack=()=>{y(""),S("");if(N==="manage"){a(p.MENU);return}if(N==="review"){k("manage");return}if(N==="country"){k("review");return}if(N==="state"){k("country");return}if(N==="county"){k("state");return}if(N==="system"){k("county");return}if(N==="department"){k("system");return}if(N==="channel"){k(Le.length>1?"department":"system");return}k("manage")};
  let favHandleTile=async d=>{if(!d||s||c)return;y(""),S("");try{if(N==="manage"){await favHandleManageTile(d);return}if(N==="review"){C(String(d.id||""));return}if(N==="country"){let v=Number(d.country_id||0)||1;bt(v),await favLoadStates(v);return}if(N==="state"){let v=Number(d.state_id||0);v>0&&(Oe(v),await favLoadCounties(v));return}if(N==="county"){let v=Number(d.county_id||0)||0;oe(v),await favLoadSystems(gt,v,De);return}if(N==="system"){await favLoadChannelsForSystem(d);return}if(N==="department"){let v=favName(d.name||d.label||"","");if(!v)return;vt(v),k("channel");return}if(N==="channel"){let v=String(d.id||"");if(!v)return;Q(W=>W.includes(v)?W.filter(Tt=>Tt!==v):[...W,v]);return}}catch(v){y(v?.message||"Action failed.")}};
  let favTileSelected=d=>{if(!d)return!1;if(N==="manage")return d.id===i;if(N==="review")return String(d.id||"")===String(T||"");if(N==="country")return Number(d.country_id||0)===Number(Me||0);if(N==="state")return Number(d.state_id||0)===Number(gt||0);if(N==="county")return Number(d.county_id||0)===Number(Pe||0);if(N==="system")return String(d.id??d.key??"")===String(Fe?.id??Fe?.key??"");if(N==="department")return String(d.name||d.label||"").trim().toLowerCase()===String(ht||"").trim().toLowerCase();if(N==="channel")return Ge.has(String(d.id||""));return!1};
  let favTileLines=d=>{if(!d)return[""];if(N==="manage")return[String(d.label||"")];if(N==="review")return favReviewLines(d);if(N==="system")return[String(d.name||d.key||"System"),String(d.protocol||d.category||De||"").toUpperCase()];if(N==="channel")return favChannelLines(d);if(N==="country")return[String(d.name||"Country")];if(N==="state")return[String(d.name||"State"),String(d.abbr||"")];if(N==="county")return[String(d.name||"County")];if(N==="department")return[String(d.name||d.label||"Department")];return[String(d.label||d.name||"")]};
  let favTitle=N==="manage"?"Manage Favorites Lists":N==="review"?"Review / Edit Channels":N==="country"?"Select Country":N==="state"?"Select State":N==="county"?"Select County":N==="system"?"Select a System":N==="department"?"Select Department":"Select Channel",favDisabled=!!(s||c);
  return M.createElement("section",{className:"screen hp2-picker favorites-screen"},M.createElement("div",{className:"hp2-picker-top"},M.createElement("div",{className:"hp2-picker-title"},favTitle),M.createElement("div",{className:"hp2-picker-top-right"},M.createElement("span",{className:"hp2-picker-help"},"Help"),M.createElement("span",{className:"hp2-picker-status"},"L"),M.createElement("span",{className:"hp2-picker-status"},"SIG"),M.createElement("span",{className:"hp2-picker-status"},"BAT"))),M.createElement("div",{className:"hp2-picker-grid"},favTiles.map((d,v)=>{if(!d)return M.createElement("div",{key:`empty-${v}`,className:"hp2-picker-tile hp2-picker-tile-empty"});let W=favTileLines(d).filter(Boolean),Tt=favTileSelected(d),je=W.length>1||!!d.multiline;return M.createElement("button",{key:String(d.id??d.key??d.label??`row-${v}`),type:"button",className:`hp2-picker-tile ${Tt?"active":""} ${je?"multiline fav-tile-stack":""}`,onClick:()=>favHandleTile(d),disabled:favDisabled},je?W.map((le,ot)=>M.createElement("div",{key:`${String(d.id??v)}-${ot}`,className:ot===0?"fav-line-main":"fav-line-sub"},le)):W[0])})),N==="manage"?M.createElement("div",{className:"hp2-picker-bottom hp2-picker-bottom-5"},M.createElement("button",{type:"button",className:"hp2-picker-btn listen",onClick:favHandleListen,disabled:favDisabled},"Listen"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:favHandleBack,disabled:favDisabled},"Back"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:favOpenReview,disabled:favDisabled},"Rev/Edit"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>f(d=>Math.max(0,d-1)),disabled:favDisabled||u<=0},"^"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>f(d=>Math.min(favPageCount-1,d+1)),disabled:favDisabled||u>=favPageCount-1},"v")):N==="review"?M.createElement("div",{className:"hp2-picker-bottom hp2-picker-bottom-6"},M.createElement("button",{type:"button",className:"hp2-picker-btn listen",onClick:favHandleListen,disabled:favDisabled},"Listen"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:favHandleBack,disabled:favDisabled},"Back"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:favLoadCountries,disabled:favDisabled},"Add Channel"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:favDeleteSelected,disabled:favDisabled||!T},"Delete"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>f(d=>Math.max(0,d-1)),disabled:favDisabled||u<=0},"^"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>f(d=>Math.min(favPageCount-1,d+1)),disabled:favDisabled||u>=favPageCount-1},"v")):N==="system"?M.createElement("div",{className:"hp2-picker-bottom hp2-picker-bottom-5"},M.createElement("button",{type:"button",className:"hp2-picker-btn listen",onClick:favHandleListen,disabled:favDisabled},"Listen"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:favHandleBack,disabled:favDisabled},"Back"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:async()=>{let d=De==="digital"?"analog":"digital";He(d),await favLoadSystems(gt,Pe,d)},disabled:favDisabled||!gt},De==="digital"?"Type: TG":"Type: CH"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>f(d=>Math.max(0,d-1)),disabled:favDisabled||u<=0},"^"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>f(d=>Math.min(favPageCount-1,d+1)),disabled:favDisabled||u>=favPageCount-1},"v")):N==="channel"?M.createElement("div",{className:"hp2-picker-bottom hp2-picker-bottom-5"},M.createElement("button",{type:"button",className:"hp2-picker-btn listen",onClick:favHandleListen,disabled:favDisabled},"Listen"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:favHandleBack,disabled:favDisabled},"Back"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:favAddSelectedChannels,disabled:favDisabled||ft.length<=0},"Add Channel"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>f(d=>Math.max(0,d-1)),disabled:favDisabled||u<=0},"^"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>f(d=>Math.min(favPageCount-1,d+1)),disabled:favDisabled||u>=favPageCount-1},"v")):M.createElement("div",{className:"hp2-picker-bottom hp2-picker-bottom-4"},M.createElement("button",{type:"button",className:"hp2-picker-btn listen",onClick:favHandleListen,disabled:favDisabled},"Listen"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:favHandleBack,disabled:favDisabled},"Back"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>f(d=>Math.max(0,d-1)),disabled:favDisabled||u<=0},"^"),M.createElement("button",{type:"button",className:"hp2-picker-btn",onClick:()=>f(d=>Math.min(favPageCount-1,d+1)),disabled:favDisabled||u>=favPageCount-1},"v")),M.createElement("div",{className:"muted hp2-picker-page"},"Page ",u+1," / ",favPageCount),N==="review"?M.createElement("div",{className:"muted fav-wizard-note"},"List: ",favName(h||$e||"My Favorites")):null,N==="system"?M.createElement("div",{className:"muted fav-wizard-note"},"System Type: ",De==="digital"?"Digital (Talkgroups)":"Analog (Conventional Channels)" ):null,N==="channel"?M.createElement("div",{className:"muted fav-wizard-note"},"Selected: ",ft.length):null,w?M.createElement("div",{className:"message"},w):null,L?M.createElement("div",{className:"error"},L):null,e.error?M.createElement("div",{className:"error"},e.error):null)}

import A,{useEffect as ra,useMemo as pt,useState as ia}from"https://esm.sh/react@18";function na(e){return Array.isArray(e)?e.map((t,a)=>t&&typeof t=="object"?{id:String(t.id??`${t.type||"item"}-${a}`),label:String(t.label||t.alpha_tag||t.name||`Avoid ${a+1}`),type:String(t.type||"item"),source:"persistent"}:{id:`item-${a}`,label:String(t),type:"item",source:"persistent"}):[]}function oa(e){if(!Array.isArray(e))return[];let t=[],a=new Set;return e.forEach(n=>{let s=String(n||"").trim();!s||a.has(s)||(a.add(s),t.push({id:`runtime:${s}`,label:s,type:"system",token:s,source:"runtime"}))}),t}function Ae(){let{state:e,saveHpState:t,avoidCurrent:a,clearHpAvoids:n,removeHpAvoid:s,navigate:i}=P(),{hpState:r,hpAvoids:u,working:f}=e,w=pt(()=>Array.isArray(r.avoid_list)?r.avoid_list:Array.isArray(r.avoids)?r.avoids:Array.isArray(r.avoid)?r.avoid:[],[r.avoid_list,r.avoids,r.avoid]),[S,L]=ia([]);ra(()=>{L(na(w))},[w]);let y=pt(()=>oa(u),[u]),N=async(h=S)=>{try{await t({avoid_list:h})}catch{}},k=async()=>{try{await a()}catch{}};return A.createElement("section",{className:"screen avoid-screen"},A.createElement($,{title:"Avoid",showBack:!0,onBack:()=>i(p.MENU)}),A.createElement("div",{className:"list"},A.createElement("div",{className:"card"},A.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Runtime Avoids (HP Scan Pool)"),y.length===0?A.createElement("div",{className:"muted"},"No runtime HP avoids."):y.map(h=>A.createElement("div",{key:h.id,className:"row",style:{marginBottom:"6px"}},A.createElement("div",null,A.createElement("div",null,h.label),A.createElement("div",{className:"muted"},h.type)),A.createElement(D,{variant:"danger",onClick:()=>s(h.token),disabled:f},"Remove")))),A.createElement("div",{className:"card"},A.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Persistent Avoids (State)"),S.length===0?A.createElement("div",{className:"muted"},"No persistent avoids in current state."):S.map(h=>A.createElement("div",{key:h.id,className:"row",style:{marginBottom:"6px"}},A.createElement("div",null,A.createElement("div",null,h.label),A.createElement("div",{className:"muted"},h.type)),A.createElement(D,{variant:"danger",onClick:()=>{let g=S.filter(T=>T.id!==h.id);L(g),N(g)},disabled:f},"Remove"))))),A.createElement("div",{className:"button-row"},A.createElement(D,{onClick:k,disabled:f},"Avoid Current"),A.createElement(D,{variant:"secondary",onClick:async()=>{L([]),await N([]),await n()},disabled:f},"Clear All"),A.createElement(D,{onClick:()=>N(),disabled:f},"Save")),e.error?A.createElement("div",{className:"error"},e.error):null)}import B,{useEffect as sa,useState as la}from"https://esm.sh/react@18";function ke(){let{state:e,setMode:t,navigate:a}=P(),[n,s]=la("hp");return sa(()=>{s(e.mode||"hp")},[e.mode]),B.createElement("section",{className:"screen mode-selection-screen"},B.createElement($,{title:"Mode Selection",showBack:!0,onBack:()=>a(p.MENU)}),B.createElement("div",{className:"list"},B.createElement("label",{className:"row card"},B.createElement("span",null,"HP Mode"),B.createElement("input",{type:"radio",name:"scan-mode",value:"hp",checked:n==="hp",onChange:r=>s(r.target.value)})),B.createElement("label",{className:"row card"},B.createElement("span",null,"Expert Mode"),B.createElement("input",{type:"radio",name:"scan-mode",value:"expert",checked:n==="expert",onChange:r=>s(r.target.value)}))),B.createElement("div",{className:"button-row"},B.createElement(D,{onClick:async()=>{try{await t(n),a(p.MENU)}catch{}},disabled:e.working},"Save")),e.error?B.createElement("div",{className:"error"},e.error):null)}import da from"https://esm.sh/react@18";function we({label:e="Loading..."}){return da.createElement("div",{className:"loading"},e)}function Te(){let{state:e}=P();if(e.loading)return G.createElement(we,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case p.MENU:return G.createElement(be,null);case p.LOCATION:return G.createElement(ge,null);case p.SERVICE_TYPES:return G.createElement(ye,null);case p.RANGE:return G.createElement(Se,null);case p.FAVORITES:return G.createElement(Ee,null);case p.AVOID:return G.createElement(Ae,null);case p.MODE_SELECTION:return G.createElement(ke,null);case p.MAIN:default:return G.createElement(fe,null)}}var pa=`
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: Tahoma, Verdana, sans-serif;
    background:
      radial-gradient(1200px 460px at 50% -20%, rgba(247, 170, 71, 0.13), transparent 65%),
      linear-gradient(180deg, #15110f 0%, #0f1319 100%);
    color: #e9eef5;
  }
  .device-stage {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 14px;
  }
  .device-shell {
    width: min(1020px, 100%);
    border-radius: 24px;
    border: 1px solid #3d332d;
    background:
      linear-gradient(180deg, #3a3431 0%, #2a2523 42%, #221e1d 100%);
    box-shadow:
      0 30px 65px rgba(0, 0, 0, 0.55),
      inset 0 1px 0 rgba(255, 255, 255, 0.05),
      inset 0 -6px 18px rgba(0, 0, 0, 0.45);
    padding: 14px;
  }
  .device-brand {
    text-align: center;
    color: #f5f4ef;
    font-weight: 700;
    font-size: clamp(1.7rem, 4.8vw, 3rem);
    letter-spacing: 0.02em;
    text-shadow: 0 1px 0 rgba(0, 0, 0, 0.4);
    margin-bottom: 8px;
  }
  .device-body {
    display: grid;
    grid-template-columns: clamp(110px, 24vw, 230px) minmax(0, 1fr);
    gap: 12px;
    align-items: stretch;
  }
  .device-speaker {
    position: relative;
    border-radius: 16px;
    border: 1px solid #4b3d35;
    background: linear-gradient(180deg, #2f2925 0%, #262120 100%);
    overflow: hidden;
    height: 100%;
    box-shadow:
      inset 0 1px 0 rgba(255, 255, 255, 0.04),
      inset 0 -8px 14px rgba(0, 0, 0, 0.33);
  }
  .device-speaker::before {
    content: "";
    position: absolute;
    inset: 12px;
    border-radius: 10px;
    border: 1px solid #3f342d;
    background:
      radial-gradient(circle at 4px 4px, #100f10 1.8px, transparent 1.9px),
      linear-gradient(180deg, #1f1a18 0%, #171414 100%);
    background-size: 12px 12px, auto;
  }
  .device-bottom-label {
    margin-top: 8px;
    text-align: center;
    color: #f09b5f;
    font-weight: 700;
    font-size: 1.05rem;
    letter-spacing: 0.11em;
    white-space: nowrap;
    opacity: 0.88;
    pointer-events: none;
  }
  .device-charge {
    position: absolute;
    left: 16px;
    bottom: 14px;
    color: #e7cbc1;
    font-size: 0.95rem;
    letter-spacing: 0.08em;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    opacity: 0.92;
    pointer-events: none;
  }
  .device-charge::before {
    content: "";
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: #e74a36;
    box-shadow: 0 0 9px rgba(231, 74, 54, 0.65);
  }
  .device-bezel {
    border-radius: 16px;
    border: 1px solid #4f433b;
    background: linear-gradient(180deg, #2d2826 0%, #252120 100%);
    padding: 10px;
    box-shadow:
      inset 0 1px 0 rgba(255, 255, 255, 0.04),
      inset 0 -9px 14px rgba(0, 0, 0, 0.42);
  }
  .device-screen {
    width: 100%;
    aspect-ratio: 3 / 2;
    border-radius: 13px;
    border: 1px solid #161a24;
    background: radial-gradient(120% 115% at 0% 0%, #1f2a3b 0%, #111722 55%, #0b1018 100%);
    box-shadow:
      inset 0 0 0 1px rgba(202, 222, 255, 0.05),
      0 10px 22px rgba(0, 0, 0, 0.42);
    overflow: hidden;
  }
  .app-shell {
    width: 100%;
    height: 100%;
    padding: 10px;
    overflow: auto;
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
    line-height: 1.05;
    white-space: normal;
    overflow-wrap: anywhere;
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
    line-height: 1.05;
    white-space: normal;
    overflow-wrap: anywhere;
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
    line-height: 1.05;
    white-space: normal;
    overflow-wrap: anywhere;
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
    line-height: 1.06;
    cursor: pointer;
    white-space: normal;
    overflow: auto;
    overflow-wrap: anywhere;
    word-break: break-word;
    text-overflow: clip;
    scrollbar-width: thin;
    scrollbar-color: #6f819b #1a2230;
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
  .hp2-picker-bottom-6 {
    grid-template-columns: 1.2fr 1fr 1fr 1fr 0.8fr 0.8fr;
  }
  .hp2-picker-btn {
    border: 0;
    min-height: 46px;
    background: #2b3749;
    color: #dce9fb;
    font-size: 0.9rem;
    font-weight: 700;
    line-height: 1.05;
    white-space: normal;
    overflow: auto;
    overflow-wrap: anywhere;
    word-break: break-word;
    text-align: center;
    padding: 4px 6px;
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
  .favorites-screen .hp2-picker-tile.fav-tile-stack {
    white-space: normal;
    line-height: 1.05;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    justify-content: center;
    gap: 2px;
  }
  .favorites-screen .fav-line-main {
    color: #ffe2a2;
    font-size: 0.81rem;
    width: 100%;
    line-height: 1.08;
    white-space: normal;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .favorites-screen .fav-line-sub {
    color: #c9d8ee;
    font-size: 0.7rem;
    width: 100%;
    line-height: 1.06;
    white-space: normal;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .favorites-screen .fav-wizard-note {
    padding: 6px 12px 4px;
    font-size: 0.78rem;
  }
  @media (max-width: 520px) {
    .device-stage {
      padding: 6px;
    }
    .device-shell {
      border-radius: 18px;
      padding: 9px;
    }
    .device-body {
      gap: 8px;
      grid-template-columns: 88px minmax(0, 1fr);
    }
    .device-speaker {
      border-radius: 12px;
    }
    .device-speaker::before {
      inset: 9px 8px;
    }
    .device-bottom-label {
      font-size: 1.05rem;
      letter-spacing: 0.11em;
    }
    .device-charge {
      left: 8px;
      bottom: 9px;
      font-size: 0.72rem;
      gap: 5px;
    }
    .device-charge::before {
      width: 7px;
      height: 7px;
    }
    .device-bezel {
      border-radius: 12px;
      padding: 6px;
    }
    .device-screen {
      border-radius: 10px;
    }
    .app-shell {
      padding: 6px;
      overflow: auto;
    }
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
    .hp2-picker-bottom-6 {
      grid-template-columns: 1.15fr 1fr 1fr 1fr 0.8fr 0.8fr;
    }
    .favorites-screen .hp2-picker-tile {
      min-height: 58px;
      font-size: 0.78rem;
    }
  }
`;
function Ce(){
  return ie.createElement(
    rt,
    null,
    ie.createElement(
      "div",
      {className:"device-stage"},
      ie.createElement("style",null,pa),
      ie.createElement(
        "div",
        {className:"device-shell"},
        ie.createElement("div",{className:"device-brand","aria-hidden":"true"},"Uniden"),
        ie.createElement(
          "div",
          {className:"device-body"},
          ie.createElement(
            "aside",
            {className:"device-speaker","aria-hidden":"true"},
            ie.createElement("div",{className:"device-charge"},"CHARGE")
          ),
          ie.createElement(
            "div",
            {className:"device-bezel"},
            ie.createElement(
              "div",
              {className:"device-screen"},
              ie.createElement(
                "div",
                {className:"app-shell"},
                ie.createElement(Te,null)
              )
            )
          )
        ),
        ie.createElement("div",{className:"device-bottom-label","aria-hidden":"true"},"HOMEPATROL-3")
      )
    )
  )
}
var ct=document.getElementById("root");if(!ct)throw new Error("Missing #root mount element");ca(ct).render(Ie.createElement(Ie.StrictMode,null,Ie.createElement(Ce,null)));
