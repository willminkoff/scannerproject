import se from"https://esm.sh/react@18";import{createRoot as _a}from"https://esm.sh/react-dom@18/client";import q from"https://esm.sh/react@18";import Ke,{createContext as Ze,useCallback as O,useContext as Je,useEffect as Ne,useMemo as Xe,useReducer as Qe}from"https://esm.sh/react@18";var We={"Content-Type":"application/json"};async function P(e,{method:a="GET",body:t}={}){let n={method:a,headers:{...We}};t!==void 0&&(n.body=JSON.stringify(t));let l=await fetch(e,n),o=await l.text(),r={};try{r=o?JSON.parse(o):{}}catch{r={raw:o}}if(!l.ok){let s=r?.error||`Request failed (${l.status})`,u=new Error(s);throw u.status=l.status,u.payload=r,u}return r}function Y(){return P("/api/hp/state")}function be(e){return P("/api/hp/state",{method:"POST",body:e})}function K(){return P("/api/hp/service-types")}function Z(){return P("/api/status")}function ve(e){return P("/api/mode",{method:"POST",body:{mode:e}})}function he(e={}){return P("/api/hp/hold",{method:"POST",body:e})}function ye(e={}){return P("/api/hp/next",{method:"POST",body:e})}function Se(e={}){return P("/api/hp/avoid",{method:"POST",body:e})}var p=Object.freeze({MAIN:"MAIN",MENU:"MENU",LOCATION:"LOCATION",SERVICE_TYPES:"SERVICE_TYPES",RANGE:"RANGE",FAVORITES:"FAVORITES",AVOID:"AVOID",MODE_SELECTION:"MODE_SELECTION"}),Re={hpState:{},serviceTypes:[],liveStatus:{},currentScreen:p.MAIN,mode:"hp",loading:!0,working:!1,error:"",message:""};function ea(e,a){switch(a.type){case"LOAD_START":return{...e,loading:!0,error:""};case"LOAD_SUCCESS":return{...e,loading:!1,error:"",hpState:a.payload.hpState||{},serviceTypes:a.payload.serviceTypes||[],liveStatus:a.payload.liveStatus||{},mode:a.payload.mode||e.mode};case"LOAD_ERROR":return{...e,loading:!1,error:a.payload||"Load failed"};case"SET_WORKING":return{...e,working:!!a.payload};case"SET_ERROR":return{...e,error:a.payload||""};case"SET_MESSAGE":return{...e,message:a.payload||""};case"SET_HP_STATE":return{...e,hpState:a.payload||{}};case"SET_SERVICE_TYPES":return{...e,serviceTypes:a.payload||[]};case"SET_LIVE_STATUS":return{...e,liveStatus:a.payload||{}};case"SET_MODE":return{...e,mode:a.payload||e.mode};case"NAVIGATE":return{...e,currentScreen:a.payload||p.MAIN};default:return e}}var Ee=Ze(null);function xe(e){return(Array.isArray(e?.service_types)?e.service_types:[]).map(t=>({service_tag:Number(t?.service_tag),name:String(t?.name||`Service ${t?.service_tag}`),enabled_by_default:!!t?.enabled_by_default}))}function _e(e){let a=e&&typeof e.state=="object"&&e.state!==null?e.state:{},t=String(e?.mode||"hp").toLowerCase();return{hpState:a,mode:t}}function Ae({children:e}){let[a,t]=Qe(ea,Re),n=O(m=>{t({type:"NAVIGATE",payload:m})},[]),l=O(async()=>{let m=await Y(),c=_e(m);return t({type:"SET_HP_STATE",payload:c.hpState}),t({type:"SET_MODE",payload:c.mode}),c},[]),o=O(async()=>{let m=await K(),c=xe(m);return t({type:"SET_SERVICE_TYPES",payload:c}),c},[]),r=O(async()=>{let m=await Z();return t({type:"SET_LIVE_STATUS",payload:m||{}}),m},[]),s=O(async()=>{t({type:"LOAD_START"});try{let[m,c]=await Promise.all([Y(),K()]),v={};try{v=await Z()}catch{v={}}let k=_e(m),V=xe(c);t({type:"LOAD_SUCCESS",payload:{hpState:k.hpState,mode:k.mode,serviceTypes:V,liveStatus:v}})}catch(m){t({type:"LOAD_ERROR",payload:m.message})}},[]);Ne(()=>{s()},[s]),Ne(()=>{let m=setInterval(()=>{r().catch(()=>{})},2500);return()=>clearInterval(m)},[r]);let u=O(async m=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let c={...a.hpState,...m},v=await be(c),k=v?.state&&typeof v.state=="object"?{...a.hpState,...v.state}:c;return t({type:"SET_HP_STATE",payload:k}),t({type:"SET_MESSAGE",payload:"State saved"}),v}catch(c){throw t({type:"SET_ERROR",payload:c.message}),c}finally{t({type:"SET_WORKING",payload:!1})}},[a.hpState]),b=O(async m=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let c=await ve(m),v=String(c?.mode||m||"hp").toLowerCase();return t({type:"SET_MODE",payload:v}),t({type:"SET_MESSAGE",payload:`Mode set to ${v}`}),c}catch(c){throw t({type:"SET_ERROR",payload:c.message}),c}finally{t({type:"SET_WORKING",payload:!1})}},[]),y=O(async(m,c)=>{t({type:"SET_WORKING",payload:!0}),t({type:"SET_ERROR",payload:""});try{let v=await m();return c&&t({type:"SET_MESSAGE",payload:c}),await l(),await r(),v}catch(v){throw t({type:"SET_ERROR",payload:v.message}),v}finally{t({type:"SET_WORKING",payload:!1})}},[l,r]),d=O(async()=>y(()=>he(),"Hold command sent"),[y]),g=O(async()=>y(()=>ye(),"Next command sent"),[y]),S=O(async(m={})=>y(()=>Se(m),"Avoid command sent"),[y]),E=Xe(()=>({state:a,dispatch:t,navigate:n,refreshAll:s,refreshHpState:l,refreshServiceTypes:o,refreshStatus:r,saveHpState:u,setMode:b,holdScan:d,nextScan:g,avoidCurrent:S,SCREENS:p}),[a,n,s,l,o,r,u,b,d,g,S]);return Ke.createElement(Ee.Provider,{value:E},e)}function _(){let e=Je(Ee);if(!e)throw new Error("useUI must be used inside UIProvider");return e}import H from"https://esm.sh/react@18";import i,{useEffect as we,useMemo as ke,useState as j}from"https://esm.sh/react@18";import aa from"https://esm.sh/react@18";function N({children:e,onClick:a,type:t="button",variant:n="primary",className:l="",disabled:o=!1}){return aa.createElement("button",{type:t,className:`btn ${n==="secondary"?"btn-secondary":n==="danger"?"btn-danger":""} ${l}`.trim(),onClick:a,disabled:o},e)}function T(e){return e==null||e===""?"--":String(e)}function ta(e){let a=Math.max(0,Math.min(4,Number(e)||0));return`${"|".repeat(a)}${".".repeat(4-a)}`}function ra(e){let a=Number(e);return Number.isFinite(a)?Number.isInteger(a)?`Range ${a}`:`Range ${a.toFixed(1)}`:"Range"}function J(){let{state:e,holdScan:a,nextScan:t,avoidCurrent:n,navigate:l}=_(),{hpState:o,liveStatus:r,working:s,error:u,message:b}=e,y=String(r?.stream_mount||"ANALOG.mp3").trim().replace(/^\//,""),d=String(r?.digital_stream_mount||"DIGITAL.mp3").trim().replace(/^\//,""),g=!!y,S=!!d,E=(e.mode==="hp"||e.mode==="expert")&&S?"digital":"analog",[m,c]=j(E),[v,k]=j(""),[V,Me]=j(!1),[le,M]=j("");we(()=>{if(m==="digital"&&!S){c(g?"analog":"digital");return}m==="analog"&&!g&&S&&c("digital")},[g,S,m]),we(()=>{!u&&!b||M("")},[u,b]);let D=m==="digital"?d||y:y||d,A=m==="digital"&&S,de=String(o.mode||"full_database").trim().toLowerCase(),pe=A?r?.digital_scheduler_active_system||r?.digital_profile||o.system_name||o.system:r?.profile_airband||"Airband",$=A?r?.digital_last_label||o.department_name||o.department:r?.last_hit_airband_label||r?.last_hit_ground_label||r?.last_hit||o.department_name||o.department,Oe=A?r?.digital_last_tgid??o.tgid??o.talkgroup_id:"--",Le=A?(()=>{let f=Number(r?.digital_preflight?.playlist_frequency_hz?.[0]||r?.digital_playlist_frequency_hz?.[0]||0);return Number.isFinite(f)&&f>0?(f/1e6).toFixed(4):o.frequency??o.freq})():r?.last_hit_airband||r?.last_hit_ground||r?.last_hit||"--",ce=A?r?.digital_control_channel_locked?"Locked":r?.digital_control_decode_available?"Decoding":o.signal??o.signal_strength:r?.rtl_active?"Active":"Idle",Be=A&&(r?.digital_last_label||o.channel_name||o.channel)||$,He=A&&(r?.digital_last_mode||o.service_type||o.service)||"",ue=A?Be:$,me=A?`${T(He||"Digital")} \u2022 ${T(Oe)} \u2022 ${ce}`:`${T(Le)} \u2022 ${ce}`,Pe=A?r?.digital_control_channel_locked?4:r?.digital_control_decode_available?3:1:r?.rtl_active?3:1,ge=String(r?.digital_scan_mode||"").toLowerCase()==="single_system",Ue=ge?"HOLD":"SCAN",De=ke(()=>{if(de!=="favorites")return"Full Database";let f=Array.isArray(o.favorites)?o.favorites:[];if(f.length===0)return"Favorites";let U=f.filter(W=>!!W?.enabled);if(U.length===0)return"Favorites";let qe=U.find(W=>{let fe=String(W?.type||"").trim().toLowerCase();return A?fe==="digital":fe==="analog"})||U[0];return String(qe?.label||"").trim()||"Favorites"},[de,o.favorites,A]),$e=async()=>{try{await a()}catch{}},Fe=async()=>{try{await t()}catch{}},Ge=async()=>{try{await n()}catch{}},ze=async(f,U)=>{if(f==="info"){M(U==="system"?`System: ${T(pe)}`:U==="department"?`Department: ${T($)}`:`Channel: ${T(ue)} (${T(me)})`),k("");return}if(f==="advanced"){M("Advanced options are still being wired in HP3."),k("");return}if(f==="prev"){M("Previous-channel stepping is not wired yet in HP3."),k("");return}if(f==="fave"){k(""),l(p.FAVORITES);return}if(!A){M("Switch Audio Source to Digital for HOLD/NEXT/AVOID controls."),k("");return}f==="hold"?await $e():f==="next"?await Fe():f==="avoid"&&await Ge(),k("")},Ve=ke(()=>[{id:"squelch",label:"Squelch",onClick:()=>M("Squelch is currently managed from SB3 analog controls.")},{id:"range",label:ra(o.range_miles),onClick:()=>l(p.RANGE)},{id:"atten",label:"Atten",onClick:()=>M("Attenuation toggle is not wired yet in HP3.")},{id:"gps",label:"GPS",onClick:()=>l(p.LOCATION)},{id:"help",label:"Help",onClick:()=>l(p.MENU)}],[o.range_miles,l]),je={system:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],department:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"}],channel:[{id:"info",label:"Info"},{id:"advanced",label:"Advanced"},{id:"prev",label:"Prev"},{id:"hold",label:"Hold"},{id:"next",label:"Next"},{id:"avoid",label:"Avoid"},{id:"fave",label:"Fave"}]};return i.createElement("section",{className:"screen main-screen hp2-main"},i.createElement("div",{className:"hp2-radio-bar"},i.createElement("div",{className:"hp2-radio-buttons"},Ve.map(f=>i.createElement("button",{key:f.id,type:"button",className:"hp2-radio-btn",onClick:f.onClick,disabled:s},f.label))),i.createElement("div",{className:"hp2-status-icons"},i.createElement("span",{className:`hp2-icon ${ge?"on":""}`},Ue),i.createElement("span",{className:"hp2-icon"},"SIG ",ta(Pe)),i.createElement("span",{className:"hp2-icon"},A?"DIG":"ANA"))),i.createElement("div",{className:"hp2-lines"},i.createElement("div",{className:"hp2-line"},i.createElement("div",{className:"hp2-line-label"},"System / Favorite List"),i.createElement("div",{className:"hp2-line-body"},i.createElement("div",{className:"hp2-line-primary"},T(pe)),i.createElement("div",{className:"hp2-line-secondary"},De)),i.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>k(f=>f==="system"?"":"system"),disabled:s},"<")),i.createElement("div",{className:"hp2-line"},i.createElement("div",{className:"hp2-line-label"},"Department"),i.createElement("div",{className:"hp2-line-body"},i.createElement("div",{className:"hp2-line-primary"},T($)),i.createElement("div",{className:"hp2-line-secondary"},"Service: ",T(o.mode))),i.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>k(f=>f==="department"?"":"department"),disabled:s},"<")),i.createElement("div",{className:"hp2-line channel"},i.createElement("div",{className:"hp2-line-label"},"Channel"),i.createElement("div",{className:"hp2-line-body"},i.createElement("div",{className:"hp2-line-primary"},T(ue)),i.createElement("div",{className:"hp2-line-secondary"},T(me))),i.createElement("button",{type:"button",className:"hp2-subtab",onClick:()=>k(f=>f==="channel"?"":"channel"),disabled:s},"<"))),v?i.createElement("div",{className:"hp2-submenu-popup"},je[v]?.map(f=>i.createElement("button",{key:f.id,type:"button",className:"hp2-submenu-btn",onClick:()=>ze(f.id,v),disabled:s},f.label))):null,i.createElement("div",{className:"hp2-feature-bar"},i.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>l(p.MENU),disabled:s},"Menu"),i.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>M("Replay is not wired yet in HP3."),disabled:s},"Replay"),i.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>M("Recording controls are not wired yet in HP3."),disabled:s},"Record"),i.createElement("button",{type:"button",className:"hp2-feature-btn",onClick:()=>Me(f=>!f),disabled:s},V?"Unmute":"Mute")),i.createElement("div",{className:"hp2-web-audio"},i.createElement("div",{className:"hp2-audio-head"},i.createElement("div",{className:"muted"},"Web Audio Stream"),D?i.createElement("a",{href:`/stream/${D}`,target:"_blank",rel:"noreferrer"},"Open"):null),i.createElement("div",{className:"hp2-source-switch"},i.createElement(N,{variant:m==="analog"?"primary":"secondary",onClick:()=>c("analog"),disabled:!g||s},"Analog"),i.createElement(N,{variant:m==="digital"?"primary":"secondary",onClick:()=>c("digital"),disabled:!S||s},"Digital")),i.createElement("div",{className:"muted hp2-audio-meta"},"Source: ",A?"Digital":"Analog"," (",D||"no mount",")"),i.createElement("audio",{controls:!0,preload:"none",muted:V,className:"hp2-audio-player",src:D?`/stream/${D}`:"/stream/"})),le?i.createElement("div",{className:"message"},le):null,A?null:i.createElement("div",{className:"muted"},"HOLD/NEXT/AVOID actions require Digital source."),u?i.createElement("div",{className:"error"},u):null,b?i.createElement("div",{className:"message"},b):null)}import G from"https://esm.sh/react@18";import F from"https://esm.sh/react@18";function w({title:e,subtitle:a="",showBack:t=!1,onBack:n}){return F.createElement("div",{className:"header"},F.createElement("div",null,F.createElement("h1",null,e),a?F.createElement("div",{className:"sub"},a):null),t?F.createElement("button",{type:"button",className:"btn btn-secondary",onClick:n},"Back"):null)}var na=[{id:p.LOCATION,label:"Set Your Location"},{id:p.SERVICE_TYPES,label:"Select Service Types"},{id:p.RANGE,label:"Set Range"},{id:p.FAVORITES,label:"Manage Favorites"},{id:p.AVOID,label:"Avoid Options"},{id:p.MODE_SELECTION,label:"Mode Selection"}];function X(){let{navigate:e,state:a}=_();return G.createElement("section",{className:"screen menu"},G.createElement(w,{title:"Menu",showBack:!0,onBack:()=>e(p.MAIN)}),G.createElement("div",{className:"menu-list"},na.map(t=>G.createElement(N,{key:t.id,variant:"secondary",className:"menu-item",onClick:()=>e(t.id),disabled:a.working},t.label))),a.error?G.createElement("div",{className:"error"},a.error):null)}import x,{useEffect as oa,useState as z}from"https://esm.sh/react@18";function Ce(e){if(e===""||e===null||e===void 0)return null;let a=Number(e);return Number.isFinite(a)?a:NaN}function Q(){let{state:e,saveHpState:a,navigate:t}=_(),{hpState:n,working:l}=e,[o,r]=z(""),[s,u]=z(""),[b,y]=z(""),[d,g]=z(!0),[S,E]=z("");return oa(()=>{r(n.zip||n.postal_code||""),u(n.lat!==void 0&&n.lat!==null?String(n.lat):n.latitude!==void 0&&n.latitude!==null?String(n.latitude):""),y(n.lon!==void 0&&n.lon!==null?String(n.lon):n.longitude!==void 0&&n.longitude!==null?String(n.longitude):""),g(n.use_location!==!1)},[n]),x.createElement("section",{className:"screen location-screen"},x.createElement(w,{title:"Location",showBack:!0,onBack:()=>t(p.MENU)}),x.createElement("div",{className:"list"},x.createElement("label",null,x.createElement("div",{className:"muted"},"ZIP"),x.createElement("input",{className:"input",value:o,onChange:c=>r(c.target.value.trim()),placeholder:"37201"})),x.createElement("label",null,x.createElement("div",{className:"muted"},"Latitude"),x.createElement("input",{className:"input",value:s,onChange:c=>u(c.target.value),placeholder:"36.12"})),x.createElement("label",null,x.createElement("div",{className:"muted"},"Longitude"),x.createElement("input",{className:"input",value:b,onChange:c=>y(c.target.value),placeholder:"-86.67"})),x.createElement("label",{className:"row"},x.createElement("span",null,"Use location for scanning"),x.createElement("input",{type:"checkbox",checked:d,onChange:c=>g(c.target.checked)}))),x.createElement("div",{className:"button-row"},x.createElement(N,{onClick:async()=>{if(E(""),o&&!/^\d{5}(-\d{4})?$/.test(o)){E("ZIP must be 5 digits or ZIP+4.");return}let c=Ce(s),v=Ce(b);if(Number.isNaN(c)||Number.isNaN(v)){E("Latitude and longitude must be valid numbers.");return}if(c!==null&&(c<-90||c>90)){E("Latitude must be between -90 and 90.");return}if(v!==null&&(v<-180||v>180)){E("Longitude must be between -180 and 180.");return}try{await a({zip:o,lat:c,lon:v,use_location:d}),t(p.MENU)}catch{}},disabled:l},"Save")),S?x.createElement("div",{className:"error"},S):null,e.error?x.createElement("div",{className:"error"},e.error):null)}import B,{useEffect as ia,useMemo as sa,useState as la}from"https://esm.sh/react@18";function R(){let{state:e,saveHpState:a,navigate:t}=_(),{hpState:n,serviceTypes:l,working:o}=e,r=sa(()=>l.filter(d=>d.enabled_by_default).map(d=>Number(d.service_tag)),[l]),[s,u]=la([]);ia(()=>{let d=Array.isArray(n.enabled_service_tags)?n.enabled_service_tags.map(Number):r;u(Array.from(new Set(d)).filter(g=>Number.isFinite(g)))},[n.enabled_service_tags,r]);let b=d=>{u(g=>g.includes(d)?g.filter(S=>S!==d):[...g,d])},y=async()=>{try{await a({enabled_service_tags:[...s].sort((d,g)=>d-g)}),t(p.MENU)}catch{}};return B.createElement("section",{className:"screen service-types-screen"},B.createElement(w,{title:"Service Types",showBack:!0,onBack:()=>t(p.MENU)}),B.createElement("div",{className:"checkbox-list"},l.map(d=>{let g=Number(d.service_tag),S=s.includes(g);return B.createElement("label",{key:g,className:"row card"},B.createElement("span",null,d.name),B.createElement("input",{type:"checkbox",checked:S,onChange:()=>b(g)}))})),B.createElement("div",{className:"button-row"},B.createElement(N,{onClick:y,disabled:o},"Save")),e.error?B.createElement("div",{className:"error"},e.error):null)}import L,{useEffect as da,useState as pa}from"https://esm.sh/react@18";function ee(){let{state:e,saveHpState:a,navigate:t}=_(),{hpState:n,working:l}=e,[o,r]=pa(15);da(()=>{let u=Number(n.range_miles);r(Number.isFinite(u)?u:15)},[n.range_miles]);let s=async()=>{try{await a({range_miles:o}),t(p.MENU)}catch{}};return L.createElement("section",{className:"screen range-screen"},L.createElement(w,{title:"Range",showBack:!0,onBack:()=>t(p.MENU)}),L.createElement("div",{className:"card"},L.createElement("div",{className:"row"},L.createElement("span",null,"Range Miles"),L.createElement("strong",null,o.toFixed(1))),L.createElement("input",{className:"range",type:"range",min:"0",max:"30",step:"0.5",value:o,onChange:u=>r(Number(u.target.value))})),L.createElement("div",{className:"button-row"},L.createElement(N,{onClick:s,disabled:l},"Save")),e.error?L.createElement("div",{className:"error"},e.error):null)}import h,{useEffect as ca,useMemo as Te,useState as ua}from"https://esm.sh/react@18";function ma(e){if(!Array.isArray(e))return[];let a=[],t=new Set;return e.forEach((n,l)=>{if(!n||typeof n!="object")return;let o=String(n.id||"").trim(),r=o?o.split(":"):[],s=String(n.type||n.kind||"").trim().toLowerCase(),u=String(n.target||"").trim().toLowerCase(),b=String(n.profile_id||n.profileId||n.profile||"").trim();if(!b&&r.length>0&&(r[0].toLowerCase()==="digital"&&r.length>=2?(s="digital",b=r.slice(1).join(":").trim()):r[0].toLowerCase()==="analog"&&r.length>=3&&(s="analog",u=String(r[1]||"").trim().toLowerCase(),b=r.slice(2).join(":").trim())),!s&&u&&(s="analog"),s==="digital"&&(u=""),s!=="digital"&&s!=="analog"||s==="analog"&&u!=="airband"&&u!=="ground"||!b)return;let y=s==="digital"?`digital:${b}`:`analog:${u}:${b}`;t.has(y)||(t.add(y),a.push({id:y,type:s,target:u,profile_id:b,label:String(n.label||n.name||b),enabled:n.enabled===!0,_index:l}))}),a}function ga(e){return{analog_airband:e.filter(a=>a.type==="analog"&&a.target==="airband").sort((a,t)=>a._index-t._index),analog_ground:e.filter(a=>a.type==="analog"&&a.target==="ground").sort((a,t)=>a._index-t._index),digital:e.filter(a=>a.type==="digital").sort((a,t)=>a._index-t._index)}}function ae(){let{state:e,saveHpState:a,navigate:t}=_(),{hpState:n,working:l}=e,o=Te(()=>Array.isArray(n.favorites)?n.favorites:Array.isArray(n.favorites_list)?n.favorites_list:[],[n.favorites,n.favorites_list]),[r,s]=ua([]),u=Te(()=>ga(r),[r]);ca(()=>{s(ma(o))},[o]);let b=(d,g)=>{s(S=>S.map(E=>(E.type==="digital"?"digital":`analog_${E.target}`)!==d?E:{...E,enabled:E.profile_id===g}))},y=async()=>{try{await a({favorites:r}),t(p.MENU)}catch{}};return h.createElement("section",{className:"screen favorites-screen"},h.createElement(w,{title:"Favorites",showBack:!0,onBack:()=>t(p.MENU)}),r.length===0?h.createElement("div",{className:"muted"},"No favorites in current state."):h.createElement("div",{className:"list"},h.createElement("div",{className:"card"},h.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Analog Airband"),u.analog_airband.length===0?h.createElement("div",{className:"muted"},"No airband profiles found."):u.analog_airband.map(d=>h.createElement("label",{key:d.id,className:"row",style:{marginBottom:"6px"}},h.createElement("span",null,d.label),h.createElement("input",{type:"radio",name:"favorites-analog-airband",checked:d.enabled,onChange:()=>b("analog_airband",d.profile_id)})))),h.createElement("div",{className:"card"},h.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Analog Ground"),u.analog_ground.length===0?h.createElement("div",{className:"muted"},"No ground profiles found."):u.analog_ground.map(d=>h.createElement("label",{key:d.id,className:"row",style:{marginBottom:"6px"}},h.createElement("span",null,d.label),h.createElement("input",{type:"radio",name:"favorites-analog-ground",checked:d.enabled,onChange:()=>b("analog_ground",d.profile_id)})))),h.createElement("div",{className:"card"},h.createElement("div",{className:"muted",style:{marginBottom:"8px"}},"Digital"),u.digital.length===0?h.createElement("div",{className:"muted"},"No digital profiles found."):u.digital.map(d=>h.createElement("label",{key:d.id,className:"row",style:{marginBottom:"6px"}},h.createElement("span",null,d.label),h.createElement("input",{type:"radio",name:"favorites-digital",checked:d.enabled,onChange:()=>b("digital",d.profile_id)}))))),h.createElement("div",{className:"muted",style:{marginTop:"8px"}},"Saving favorites sets the active analog/digital profiles for HP3 playback."),h.createElement("div",{className:"button-row"},h.createElement(N,{onClick:y,disabled:l},"Save")),e.error?h.createElement("div",{className:"error"},e.error):null)}import C,{useEffect as fa,useMemo as ba,useState as va}from"https://esm.sh/react@18";function ha(e){return Array.isArray(e)?e.map((a,t)=>a&&typeof a=="object"?{id:a.id??`${a.type||"item"}-${t}`,label:String(a.label||a.alpha_tag||a.name||`Avoid ${t+1}`),type:String(a.type||"item")}:{id:`item-${t}`,label:String(a),type:"item"}):[]}function te(){let{state:e,saveHpState:a,avoidCurrent:t,navigate:n}=_(),{hpState:l,working:o}=e,r=ba(()=>Array.isArray(l.avoid_list)?l.avoid_list:Array.isArray(l.avoids)?l.avoids:Array.isArray(l.avoid)?l.avoid:[],[l.avoid_list,l.avoids,l.avoid]),[s,u]=va([]);fa(()=>{u(ha(r))},[r]);let b=()=>{u([])},y=async(g=s)=>{try{await a({avoid_list:g})}catch{}},d=async()=>{try{await t()}catch{}};return C.createElement("section",{className:"screen avoid-screen"},C.createElement(w,{title:"Avoid",showBack:!0,onBack:()=>n(p.MENU)}),s.length===0?C.createElement("div",{className:"muted"},"No avoided items in current state."):C.createElement("div",{className:"list"},s.map(g=>C.createElement("div",{key:g.id,className:"row card"},C.createElement("div",null,C.createElement("div",null,g.label),C.createElement("div",{className:"muted"},g.type)),C.createElement(N,{variant:"danger",onClick:()=>{let S=s.filter(E=>E.id!==g.id);u(S),y(S)},disabled:o},"Remove")))),C.createElement("div",{className:"button-row"},C.createElement(N,{onClick:d,disabled:o},"Avoid Current"),C.createElement(N,{variant:"secondary",onClick:()=>{b(),y([])},disabled:o},"Clear"),C.createElement(N,{onClick:()=>y(),disabled:o},"Save")),e.error?C.createElement("div",{className:"error"},e.error):null)}import I,{useEffect as ya,useState as Sa}from"https://esm.sh/react@18";function re(){let{state:e,setMode:a,navigate:t}=_(),[n,l]=Sa("hp");return ya(()=>{l(e.mode||"hp")},[e.mode]),I.createElement("section",{className:"screen mode-selection-screen"},I.createElement(w,{title:"Mode Selection",showBack:!0,onBack:()=>t(p.MENU)}),I.createElement("div",{className:"list"},I.createElement("label",{className:"row card"},I.createElement("span",null,"HP Mode"),I.createElement("input",{type:"radio",name:"scan-mode",value:"hp",checked:n==="hp",onChange:r=>l(r.target.value)})),I.createElement("label",{className:"row card"},I.createElement("span",null,"Expert Mode"),I.createElement("input",{type:"radio",name:"scan-mode",value:"expert",checked:n==="expert",onChange:r=>l(r.target.value)}))),I.createElement("div",{className:"button-row"},I.createElement(N,{onClick:async()=>{try{await a(n),t(p.MENU)}catch{}},disabled:e.working},"Save")),e.error?I.createElement("div",{className:"error"},e.error):null)}import Na from"https://esm.sh/react@18";function ne({label:e="Loading..."}){return Na.createElement("div",{className:"loading"},e)}function oe(){let{state:e}=_();if(e.loading)return H.createElement(ne,{label:"Loading HomePatrol state..."});switch(e.currentScreen){case p.MENU:return H.createElement(X,null);case p.LOCATION:return H.createElement(Q,null);case p.SERVICE_TYPES:return H.createElement(R,null);case p.RANGE:return H.createElement(ee,null);case p.FAVORITES:return H.createElement(ae,null);case p.AVOID:return H.createElement(te,null);case p.MODE_SELECTION:return H.createElement(re,null);case p.MAIN:default:return H.createElement(J,null)}}var xa=`
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
`;function ie(){return q.createElement(Ae,null,q.createElement("div",{className:"app-shell"},q.createElement("style",null,xa),q.createElement(oe,null)))}var Ie=document.getElementById("root");if(!Ie)throw new Error("Missing #root mount element");_a(Ie).render(se.createElement(se.StrictMode,null,se.createElement(ie,null)));
