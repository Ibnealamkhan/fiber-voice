// Fiber Voice desktop UI: recents/missed log, dialer, in-call controls, diagnostics.
(()=>{
const $=id=>document.getElementById(id);
const LIVE=new Set(['dialing','ringing','early_media','incoming','answering','active','ending']);
const KEYS=[['1',''],['2','ABC'],['3','DEF'],['4','GHI'],['5','JKL'],['6','MNO'],['7','PQRS'],['8','TUV'],['9','WXYZ'],['*',''],['0','+'],['#','']];
const ui={view:'calls',filter:'all',query:'',history:[],missed:0,selected:null,ready:false,call:null,reg:null,started:false,local:{},system:{},contactsStatus:'unavailable',blocked:[],dnd:{},people:[],numbers:{},favorites:[],contactSel:null,contactQuery:''};
let peer,stream,sessionId,timer,currentCallId,connectionMode,starting=false,generation=0,polling=false,toastTimer;

const text=(id,value)=>{$(id).textContent=value;};
function toast(message){
  if(!message)return;$('toast').textContent=message;$('toast').hidden=false;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('toast').hidden=true;},5000);
}
async function api(path,options={}){
  const response=await fetch(path,{...options,headers:options.body?{'Content-Type':'application/json'}:{},signal:AbortSignal.timeout(options.timeout||15000)});
  if(!response.ok){let message=`Request failed (${response.status})`;try{const t=await response.text();try{message=JSON.parse(t).error||t;}catch{message=t||message;}}catch{}throw new Error(message);}
  return response.status===204?{}:response.json();
}
const clean=value=>String(value||'').replace(/[^\d+*#]/g,'');
function formatNumber(raw){
  const n=String(raw||''),d=n.replace(/\D/g,'');
  if(/^(91)?[6-9]\d{9}$/.test(d)&&(n.startsWith('+')||d.length===12))return `+91 ${d.slice(-10,-5)} ${d.slice(-5)}`;
  if(/^[6-9]\d{9}$/.test(d))return `${d.slice(0,5)} ${d.slice(5)}`;
  if(/^0[6-9]\d{9}$/.test(d))return `+91 ${d.slice(1,6)} ${d.slice(6)}`;
  return n||'Unknown';
}
/* ---------- Contact names (saved in the app, or from the Mac's Contacts) ---------- */
function contactKey(number){const d=String(number||'').replace(/\D/g,'');return d.length>=10?d.slice(-10):d;}
function nameFor(number){const k=contactKey(number);return k?ui.local[k]||ui.system[k]||null:null;}
function label(number){return nameFor(number)||formatNumber(number);}
async function loadContacts(){try{const r=await api('/api/contacts');ui.local=r.contacts||{};ui.numbers=r.numbers||{};ui.favorites=r.favorites||[];renderNames();}catch{}}
function renderNames(){
  renderHistory();numberChanged();if(ui.view==='contacts')renderContacts();
  if(ui.call)renderCall({available:ui.ready,call:ui.call});
}
function setSystemContacts(names,status,people){
  ui.system=names&&typeof names==='object'?names:{};ui.contactsStatus=status||'unavailable';
  ui.people=Array.isArray(people)?people:[];
  renderContactsBanner();renderNames();return true;
}
const store={get(k){try{return localStorage.getItem(k);}catch{return null;}},set(k,v){try{localStorage.setItem(k,v);}catch{}}};
function renderContactsBanner(){
  $('contacts-banner').hidden=!(ui.contactsStatus==='notDetermined'&&store.get('contactsBannerDismissed')!=='1'&&window.webkit?.messageHandlers?.fiber);
}
$('contacts-allow').addEventListener('click',()=>{window.webkit?.messageHandlers?.fiber?.postMessage({type:'requestContacts'});$('contacts-banner').hidden=true;});
$('contacts-dismiss').addEventListener('click',()=>{store.set('contactsBannerDismissed','1');renderContactsBanner();});
let editingNumber=null;
function addContact(){
  editingNumber=null;
  text('name-title','New contact');text('name-number','Saved in Fiber Voice on this Mac.');
  $('name-input').value='';$('name-phone').value=$('number').value||'';$('name-phone').hidden=false;
  $('name-source').hidden=true;$('name-remove').hidden=true;
  $('name-dialog').showModal();$('name-input').focus();
}
function editName(number){
  $('name-phone').hidden=true;
  editingNumber=number;const k=contactKey(number);
  const saved=ui.local[k],fromMac=ui.system[k];
  text('name-title',saved||fromMac?'Edit name':'Add name');
  text('name-number',formatNumber(number));
  $('name-input').value=saved||fromMac||'';
  $('name-source').hidden=!fromMac||Boolean(saved);
  text('name-source',fromMac?`From your Mac’s Contacts. Saving here changes the name only in Fiber Voice.`:'');
  $('name-remove').hidden=!saved;
  $('name-dialog').showModal();$('name-input').select();
}
async function saveName(name){
  const adding=editingNumber===null,number=adding?$('name-phone').value.trim():editingNumber;
  if(adding&&!name.trim()){toast('Enter a name.');return;}
  if(adding&&!/^\+?[0-9]{3,15}$/.test(number.replace(/[ ()-]/g,''))){toast('Enter a valid phone number.');return;}
  try{
    const r=await api('/api/contacts',{method:'PUT',body:JSON.stringify({number,name})});
    if(r.name){ui.local[r.key]=r.name;ui.numbers[r.key]=ui.numbers[r.key]||clean(number);}
    else{delete ui.local[r.key];if(!ui.favorites.includes(r.key))delete ui.numbers[r.key];}
    $('name-dialog').close();
    if(adding){ui.contactSel=`app:${r.key}`;showView('contacts');}
    renderNames();
  }catch(e){toast(e.message);}
}
$('name-form').addEventListener('submit',e=>{e.preventDefault();saveName($('name-input').value);});
$('name-remove').addEventListener('click',()=>saveName(''));
$('name-cancel').addEventListener('click',()=>$('name-dialog').close());

function buildKeypad(el,onPress){
  el.replaceChildren(...KEYS.map(([d,l])=>{const b=document.createElement('button');b.className='key';b.type='button';b.dataset.digit=d;b.setAttribute('aria-label',d);b.innerHTML=`${d}<small>${l||'&nbsp;'}</small>`;b.addEventListener('click',()=>onPress(d));return b;}));
}
function flashKey(digit){
  document.querySelectorAll(`.key[data-digit="${CSS.escape(digit)}"]`).forEach(k=>{k.classList.add('pressed');setTimeout(()=>k.classList.remove('pressed'),120);});
}

/* ---------- Line / registration ---------- */
function renderLineStatus(){
  const state=ui.reg||{},ok=state.registered===true,dnd=ui.dnd.active===true;
  $('line-status').className='line-status '+(ok?(dnd?'dnd':'ok'):state.error?'bad':'');
  text('line-status-text',ok?(dnd?'Do Not Disturb':'Ready for calls'):state.state==='needs_authorization'?'Not set up':'Connecting…');
}
function renderRegistration(state){
  if(!state)return;ui.reg=state;
  const ok=state.registered===true;
  renderLineStatus();
  box.title=ok?'':state.error||'';
  text('registration-state',ok?`Registered · SIP ${state.sipCode}`:state.error||'Not registered');
  $('registration-state').className=ok?'ok':'warn';
  if(state.transport)text('transport',state.transport);
  if(state.number&&$('line-number').textContent==='—')text('line-number',formatNumber(state.number));
  $('registration-retry').hidden=ok;
  if(state.calling)renderCall(state.calling);
}
async function refreshStatus(){
  $('refresh').disabled=true;
  try{
    const s=await api('/api/status',{timeout:20000});
    if(s.account?.number)text('line-number',`+91 ${s.account.number}`);
    text('account-status',s.account?.number?`${s.account.mode||'Fiber'} · detected`:s.account?.error||'Unavailable');
    text('ccs',s.reachability?.CCSReachability||s.reachability?.error||'Unknown');
    text('pcscf',s.reachability?.PcscfReachability||s.reachability?.error||'Unknown');
    text('codecs',s.sip?.audioCodecs?.join(' / ')||'Not advertised');
    renderRegistration(s.registration);if(s.calling)renderCall(s.calling);
    const blockers=s.blockers?.length?s.blockers:['SIP signaling and two-way AMR / AMR-WB audio are ready.'];
    $('blockers').replaceChildren(...blockers.map(m=>{const li=document.createElement('li');li.textContent=m;return li;}));
  }catch(e){text('account-status',e.message);}
  finally{$('refresh').disabled=false;}
}

/* ---------- Call log ---------- */
const RESULT={answered:'',missed:'Missed',blocked:'Blocked',silenced:'Silenced',unavailable:'Unreachable',invalid:'Invalid number',declined:'Declined',busy:'Busy',no_answer:'No answer',cancelled:'Cancelled',failed:'Failed'};
function duration(s){if(!s)return '';const m=Math.floor(s/60),r=s%60;return m?`${m}m ${String(r).padStart(2,'0')}s`:`${r}s`;}
function dayLabel(ts){
  const d=new Date(ts*1000),today=new Date();today.setHours(0,0,0,0);
  const diff=Math.round((today-new Date(d.getFullYear(),d.getMonth(),d.getDate()))/86400000);
  if(diff===0)return 'Today';if(diff===1)return 'Yesterday';
  if(diff<7)return d.toLocaleDateString(undefined,{weekday:'long'});
  return d.toLocaleDateString(undefined,{day:'numeric',month:'short',year:d.getFullYear()===today.getFullYear()?undefined:'numeric'});
}
function renderHistory(){
  const q=clean(ui.query),words=ui.query.trim().toLowerCase();
  const items=ui.history.filter(e=>(ui.filter!=='missed'||e.result==='missed'||e.result==='silenced')&&(!words||q&&clean(e.number).includes(q)||(nameFor(e.number)||'').toLowerCase().includes(words)));
  const missedView=ui.filter==='missed';
  text('list-title',missedView?'Missed calls':'Recents');
  text('list-subtitle',missedView?'Calls you didn’t answer':'Your landline calls on this Mac');
  document.querySelectorAll('.segmented button').forEach(b=>b.classList.toggle('active',b.dataset.filter===ui.filter));
  $('missed-badge').hidden=!ui.missed;text('missed-badge',ui.missed>99?'99+':String(ui.missed));
  $('clear-log').hidden=!ui.history.length||missedView;
  $('list-empty').hidden=items.length>0;
  text('empty-title',words?'No matching calls':missedView?'No missed calls':'No calls yet');
  text('empty-text',words?'Try a different name or number.':missedView?'You’re all caught up.':'Calls you make and receive on this Mac appear here.');
  const nodes=[];let last=null;
  for(const e of items){
    const label=dayLabel(e.startedAt);
    if(label!==last){const g=document.createElement('div');g.className='group';g.textContent=label;nodes.push(g);last=label;}
    const row=document.createElement('div');
    const isBlocked=blockedKey(e.number);
    row.className='call-row'+(e.result==='missed'?' missed':'')+(e.result==='silenced'?' silenced':'')+(e.result==='blocked'?' blocked':'')+(['busy','no_answer','cancelled','failed','declined','unavailable','invalid'].includes(e.result)?' failed':'')+(ui.selected===e.id?' selected':'');
    row.setAttribute('role','listitem');row.tabIndex=0;row.dataset.id=e.id;
    const dir=e.direction==='incoming'?'Incoming':'Outgoing';
    const outcome={missed:'Missed call',blocked:'Blocked call',silenced:'Silenced · Do Not Disturb'}[e.result]||(RESULT[e.result]?`${dir} · ${RESULT[e.result]}`:dir);
    const meta=[outcome,duration(e.duration)].filter(Boolean).join(' · ');
    const icon={missed:'i-missed',blocked:'i-block',silenced:'i-moon'}[e.result]||(e.direction==='incoming'?'i-in':'i-out');
    const time=new Date(e.startedAt*1000).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
    row.innerHTML=`<span class="call-icon"><svg><use href="#${icon}"/></svg></span>
      <div class="call-main"><div class="call-number"></div><div class="call-meta"></div></div>
      <span class="call-time"></span>
      <span class="row-btns"><button class="icon-btn call" data-act="call" aria-label="Call back"><svg><use href="#i-phone"/></svg></button><button class="icon-btn" data-act="name" aria-label="Edit name"><svg><use href="#i-edit"/></svg></button><button class="icon-btn" data-act="block" aria-label="${isBlocked?'Unblock number':'Block number'}" title="${isBlocked?'Unblock':'Block'}"><svg><use href="#i-block"/></svg></button><button class="icon-btn" data-act="delete" aria-label="Remove from log"><svg><use href="#i-trash"/></svg></button></span>`;
    const name=nameFor(e.number);
    row.querySelector('.call-number').textContent=name||formatNumber(e.number);
    row.querySelector('.call-number').title=formatNumber(e.number);
    if(isBlocked&&e.result!=='blocked'){const tag=document.createElement('span');tag.className='tag';tag.textContent='Blocked';row.querySelector('.call-number').append(tag);}
    row.querySelector('.call-meta').textContent=name?`${formatNumber(e.number)} · ${meta}`:meta;
    row.querySelector('.call-time').textContent=time;
    if(e.detail)row.title=e.detail;
    nodes.push(row);
  }
  $('call-list').replaceChildren(...nodes);
}
async function loadHistory(){
  try{const h=await api('/api/history?limit=300');ui.history=h.entries;ui.missed=h.missedUnseen;renderHistory();if(ui.view==='contacts')renderCard();}catch{}
}
async function markMissedSeen(){
  if(!ui.missed)return;ui.missed=0;renderHistory();
  try{await api('/api/history/seen',{method:'POST'});}catch{}
}
$('call-list').addEventListener('click',async e=>{
  const row=e.target.closest('.call-row');if(!row)return;
  const entry=ui.history.find(x=>x.id===row.dataset.id);if(!entry)return;
  const act=e.target.closest('[data-act]')?.dataset.act;
  if(act==='name'){editName(entry.number);return;}
  if(act==='block'){toggleBlock(entry.number);return;}
  if(act==='delete'){
    try{await api(`/api/history/${encodeURIComponent(entry.id)}`,{method:'DELETE'});ui.history=ui.history.filter(x=>x.id!==entry.id);renderHistory();}catch(err){toast(err.message);}
    return;
  }
  ui.selected=entry.id;setNumber(entry.number);renderHistory();
  if(act==='call')startCall();
});
$('call-list').addEventListener('dblclick',e=>{const row=e.target.closest('.call-row');if(row&&!e.target.closest('[data-act]'))startCall();});
$('call-list').addEventListener('keydown',e=>{const row=e.target.closest('.call-row');if(row&&e.key==='Enter'){row.click();startCall();}});
$('clear-log').addEventListener('click',async()=>{
  if(!confirm('Clear the whole call log on this Mac?'))return;
  try{await api('/api/history',{method:'DELETE'});ui.history=[];ui.missed=0;renderHistory();}catch(e){toast(e.message);}
});
$('search').addEventListener('input',e=>{ui.query=e.target.value;renderHistory();});

/* ---------- Navigation ---------- */
function showView(view,filter){
  ui.view=view;if(filter)ui.filter=filter;
  ['calls','contacts','dnd','blocked','connection'].forEach(v=>{$(`view-${v}`).hidden=view!==v;});
  document.querySelectorAll('.nav-item[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view&&(view!=='calls'||b.dataset.filter===ui.filter)));
  if(view==='calls'){renderHistory();if(ui.filter==='missed')markMissedSeen();}
  if(view==='connection')refreshStatus();
  if(view==='dnd'||view==='blocked')loadScreening();
  if(view==='contacts')renderContacts();
}

/* ---------- Contacts list, favourites and contact card ---------- */
function initials(name){
  const parts=String(name||'').replace(/[^\p{L}\p{N} ]/gu,'').trim().split(/\s+/).filter(Boolean);
  return (parts.length>1?parts[0][0]+parts[parts.length-1][0]:(parts[0]||'#').slice(0,2)).toUpperCase();
}
function avatarColor(seed){let h=0;for(const c of String(seed))h=(h*31+c.charCodeAt(0))%360;return `hsl(${h} 38% 46%)`;}
function avatar(el,name){el.textContent=initials(name);el.style.background=avatarColor(name);}
function buildContacts(){
  const byKey=new Map(),list=[];
  for(const p of ui.people){
    const numbers=(p.numbers||[]).map(n=>({label:n.label||'phone',number:n.number,key:contactKey(n.number)})).filter(n=>n.key);
    if(!numbers.length)continue;
    const c={id:`mac:${p.id}`,macId:p.id,name:p.name,source:'mac',numbers};
    list.push(c);numbers.forEach(n=>{if(!byKey.has(n.key))byKey.set(n.key,c);});
  }
  for(const [key,name] of Object.entries(ui.local)){
    if(byKey.has(key))continue;
    const c={id:`app:${key}`,name,source:'app',numbers:[{label:'phone',number:ui.numbers[key]||key,key}]};
    list.push(c);byKey.set(key,c);
  }
  list.sort((a,b)=>a.name.localeCompare(b.name,undefined,{sensitivity:'base'}));
  return {list,byKey};
}
function isFavorite(number){return ui.favorites.includes(contactKey(number));}
function contactRow(c,sub,fav){
  const b=document.createElement('button');b.type='button';b.className='contact-row'+(ui.contactSel===c.id?' selected':'');
  b.setAttribute('role','option');b.dataset.id=c.id;
  b.innerHTML=`<span class="initials"></span><span class="who"><strong></strong><small></small></span>${fav?'<svg class="fav-mark"><use href="#i-star"/></svg>':''}`;
  avatar(b.querySelector('.initials'),c.name);
  b.querySelector('strong').textContent=c.name;b.querySelector('small').textContent=sub;
  return b;
}
function renderContacts(){
  const {list,byKey}=buildContacts();ui.contactIndex=new Map(list.map(c=>[c.id,c]));
  const words=ui.contactQuery.trim().toLowerCase(),digits=clean(ui.contactQuery);
  const nodes=[];
  if(!words){
    const favs=ui.favorites.map(key=>{
      const c=byKey.get(key)||{id:`app:${key}`,name:formatNumber(ui.numbers[key]||key),source:'app',numbers:[{label:'phone',number:ui.numbers[key]||key,key}]};
      if(!ui.contactIndex.has(c.id))ui.contactIndex.set(c.id,c);
      const n=c.numbers.find(x=>x.key===key)||c.numbers[0];
      return contactRow(c,`${n.label} · ${formatNumber(n.number)}`,true);
    });
    if(favs.length){const g=document.createElement('div');g.className='group';g.textContent='Favourites';nodes.push(g,...favs);}
  }
  let last=null;
  for(const c of list){
    if(words&&!c.name.toLowerCase().includes(words)&&!(digits&&c.numbers.some(n=>clean(n.number).includes(digits))))continue;
    const letter=/^\p{L}/u.test(c.name)?c.name[0].toLocaleUpperCase():'#';
    if(!words&&letter!==last){const g=document.createElement('div');g.className='group';g.textContent=letter;nodes.push(g);last=letter;}
    nodes.push(contactRow(c,c.numbers.length>1?`${c.numbers.length} numbers`:formatNumber(c.numbers[0].number),false));
  }
  $('contact-list').replaceChildren(...nodes);
  const none=!nodes.length;
  $('contacts-empty').hidden=!none;
  text('contacts-empty-title',words?'No matches':'No contacts yet');
  text('contacts-empty-text',words?'Try a different name or number.':'Add someone with the + button, or name a number from Recents.');
  const native=Boolean(window.webkit?.messageHandlers?.fiber);
  const st=ui.contactsStatus;
  $('contacts-access').hidden=!native||!(st==='notDetermined'||st==='denied');
  text('contacts-access-text',st==='denied'?'Fiber Voice can’t see your Mac’s Contacts. You can allow it in System Settings.':'Show the people in your Mac’s Contacts here.');
  text('contacts-access-btn',st==='denied'?'Open Settings':'Allow access');
  renderCard();
}
function selectedContact(){return ui.contactSel?ui.contactIndex?.get(ui.contactSel):null;}
function renderCard(){
  const c=selectedContact();
  $('card-empty').hidden=Boolean(c);$('card-body').hidden=!c;$('contacts').classList.toggle('show-card',Boolean(c));
  if(!c)return;
  const primary=c.numbers[0];
  avatar($('card-avatar'),c.name);text('card-name',c.name);
  text('card-source',c.source==='mac'?'From your Mac’s Contacts':'Saved in Fiber Voice');
  const fav=isFavorite(primary.number);
  $('card-fav').setAttribute('aria-pressed',String(fav));
  $('card-fav').querySelector('use').setAttribute('href',fav?'#i-star':'#i-star-outline');
  const blocked=Boolean(blockedKey(primary.number));
  $('card-block').querySelector('span').textContent=blocked?'Unblock':'Block';
  $('card-block').classList.toggle('danger',!blocked);
  $('card-edit').querySelector('span').textContent=c.source==='mac'&&window.webkit?.messageHandlers?.fiber?'Edit in Contacts':'Edit';
  $('card-delete').hidden=c.source!=='app'||!ui.local[primary.key];
  $('card-numbers').replaceChildren(...c.numbers.map(n=>{
    const row=document.createElement('div');row.className='number-row';
    const star=isFavorite(n.number);
    row.innerHTML=`<div class="what"><small></small><strong></strong></div>
      <button class="icon-btn star" aria-pressed="${star}" aria-label="${star?'Remove from favourites':'Add to favourites'}" title="Favourite"><svg><use href="#${star?'i-star':'i-star-outline'}"/></svg></button>
      <button class="icon-btn call" aria-label="Call"><svg><use href="#i-phone"/></svg></button>`;
    row.querySelector('small').textContent=n.label+(blockedKey(n.number)?' · blocked':'');
    row.querySelector('strong').textContent=formatNumber(n.number);
    row.querySelector('.star').addEventListener('click',()=>setFavorite(n.number,!star));
    row.querySelector('.call').addEventListener('click',()=>callNumber(n.number));
    return row;
  }));
  const keys=new Set(c.numbers.map(n=>n.key));
  const calls=ui.history.filter(e=>keys.has(contactKey(e.number))).slice(0,8);
  if(!calls.length){const d=document.createElement('div');d.className='none';d.textContent='No calls with this contact on this Mac yet.';$('card-calls').replaceChildren(d);}
  else $('card-calls').replaceChildren(...calls.map(e=>{
    const row=document.createElement('div');row.className='log-row'+(e.result==='missed'?' missed':'');
    const icon={missed:'i-missed',blocked:'i-block',silenced:'i-moon'}[e.result]||(e.direction==='incoming'?'i-in':'i-out');
    const outcome={missed:'Missed',blocked:'Blocked',silenced:'Silenced',answered:e.direction==='incoming'?'Incoming':'Outgoing'}[e.result]||`${e.direction==='incoming'?'Incoming':'Outgoing'} · ${RESULT[e.result]||e.result}`;
    row.innerHTML=`<svg><use href="#${icon}"/></svg><span class="outcome"></span><span class="when"></span>`;
    row.querySelector('.outcome').textContent=[outcome,duration(e.duration)].filter(Boolean).join(' · ');
    row.querySelector('.when').textContent=`${dayLabel(e.startedAt)}, ${new Date(e.startedAt*1000).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'})}`;
    return row;
  }));
}
function callNumber(number){if(ui.call&&LIVE.has(ui.call.state))return;setNumber(clean(number));startCall();}
async function setFavorite(number,on){
  try{const r=await api('/api/favorites',{method:'PUT',body:JSON.stringify({number,favorite:on})});ui.favorites=r.favorites;
    const k=contactKey(number);if(on)ui.numbers[k]=ui.numbers[k]||clean(number);renderContacts();}
  catch(e){toast(e.message);}
}
$('contact-list').addEventListener('click',e=>{const row=e.target.closest('.contact-row');if(!row)return;ui.contactSel=row.dataset.id;renderContacts();});
$('contact-list').addEventListener('dblclick',e=>{const row=e.target.closest('.contact-row');const c=row&&ui.contactIndex.get(row.dataset.id);if(c)callNumber(c.numbers[0].number);});
$('contact-search').addEventListener('input',e=>{ui.contactQuery=e.target.value;renderContacts();});
$('contact-add').addEventListener('click',addContact);
$('card-back').addEventListener('click',()=>{ui.contactSel=null;renderContacts();});
$('card-call').addEventListener('click',()=>{const c=selectedContact();if(c)callNumber(c.numbers[0].number);});
$('card-fav').addEventListener('click',()=>{const c=selectedContact();if(c)setFavorite(c.numbers[0].number,!isFavorite(c.numbers[0].number));});
$('card-block').addEventListener('click',()=>{const c=selectedContact();if(c)toggleBlock(c.numbers[0].number);});
$('card-edit').addEventListener('click',()=>{
  const c=selectedContact();if(!c)return;
  if(c.source==='mac'&&window.webkit?.messageHandlers?.fiber)window.webkit.messageHandlers.fiber.postMessage({type:'openContact',id:c.macId});
  else editName(c.numbers[0].number);
});
$('card-delete').addEventListener('click',async()=>{
  const c=selectedContact();if(!c||!confirm(`Delete ${c.name} from Fiber Voice?`))return;
  editingNumber=c.numbers[0].number;ui.contactSel=null;await saveName('');
});
$('contacts-access-btn').addEventListener('click',()=>{
  window.webkit?.messageHandlers?.fiber?.postMessage({type:ui.contactsStatus==='denied'?'openContactsSettings':'requestContacts'});
});

/* ---------- Block list & Do Not Disturb ---------- */
function blockedKey(number){const k=contactKey(number);return k&&ui.blocked.some(b=>b.key===k)?k:null;}
async function loadScreening(){
  try{const r=await api('/api/screening');ui.blocked=r.blocked;ui.dnd=r.dnd;renderScreening();}catch{}
}
function applyScreening(r){if(r.blocked){ui.blocked=r.blocked;ui.dnd=r.dnd||ui.dnd;}else ui.dnd=r;renderScreening();}
function clock(ts){return new Date(ts*1000).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});}
function dndSummary(){
  const d=ui.dnd;if(!d.active)return d.scheduleEnabled?`Off · scheduled ${d.start}–${d.end}`:'Off';
  if(d.activeReason==='manual')return 'On';
  const until=new Date(d.activeUntil*1000),today=new Date();
  const sameDay=until.toDateString()===today.toDateString();
  return `On until ${sameDay?'':'tomorrow '}${clock(d.activeUntil)}${d.activeReason==='schedule'?' (scheduled)':''}`;
}
function renderScreening(){
  const d=ui.dnd,on=d.active===true;
  $('dnd-quick').setAttribute('aria-pressed',String(on));text('dnd-quick-sub',dndSummary());
  $('dnd-switch').setAttribute('aria-checked',String(on));text('dnd-status',dndSummary());
  $('dnd-schedule').setAttribute('aria-checked',String(Boolean(d.scheduleEnabled)));
  $('dnd-repeat').setAttribute('aria-checked',String(d.repeatCallers!==false));
  if(d.start&&document.activeElement!==$('dnd-start'))$('dnd-start').value=d.start;
  if(d.end&&document.activeElement!==$('dnd-end'))$('dnd-end').value=d.end;
  $('blocked-count').hidden=!ui.blocked.length;text('blocked-count',String(ui.blocked.length));
  renderLineStatus();renderBlocked();if(ui.view==='calls')renderHistory();if(ui.view==='contacts')renderCard();
}
function renderBlocked(){
  $('blocked-empty').hidden=ui.blocked.length>0;
  $('blocked-list').replaceChildren(...ui.blocked.map(b=>{
    const row=document.createElement('div');row.className='call-row blocked';row.setAttribute('role','listitem');
    row.innerHTML=`<span class="call-icon"><svg><use href="#i-block"/></svg></span><div class="call-main"><div class="call-number"></div><div class="call-meta"></div></div><span class="call-time"></span><button class="btn ghost small">Unblock</button>`;
    const name=nameFor(b.number||b.key);
    row.querySelector('.call-number').textContent=name||formatNumber(b.number||b.key);
    row.querySelector('.call-meta').textContent=name?formatNumber(b.number||b.key):'Blocked';
    row.querySelector('.call-time').textContent=b.addedAt?new Date(b.addedAt*1000).toLocaleDateString(undefined,{day:'numeric',month:'short'}):'';
    row.querySelector('button').addEventListener('click',()=>unblock(b.key));
    return row;
  }));
}
async function toggleBlock(number){
  const key=blockedKey(number);
  if(key){await unblock(key);return;}
  if(!confirm(`Block ${label(number)}?\n\nCalls from this number will be rejected on this Mac.`))return;
  try{applyScreening(await api('/api/blocked',{method:'POST',body:JSON.stringify({number})}));toast(`${label(number)} blocked`);}catch(e){toast(e.message);}
}
async function unblock(key){
  try{applyScreening(await api(`/api/blocked/${encodeURIComponent(key)}`,{method:'DELETE'}));}catch(e){toast(e.message);}
}
async function setDnd(changes){
  try{applyScreening(await api('/api/dnd',{method:'PUT',body:JSON.stringify(changes)}));}catch(e){toast(e.message);}
}
function nextMorning(){
  const [h,m]=(ui.dnd.end||'07:00').split(':').map(Number),now=new Date(),t=new Date(now);
  t.setHours(h,m,0,0);if(t<=now)t.setDate(t.getDate()+1);
  return t.getTime()/1000;
}
$('dnd-quick').addEventListener('click',()=>setDnd({enabled:!ui.dnd.active}));
$('dnd-switch').addEventListener('click',()=>setDnd({enabled:!ui.dnd.active}));
document.querySelectorAll('[data-dnd-minutes]').forEach(b=>b.addEventListener('click',()=>setDnd({minutes:Number(b.dataset.dndMinutes)})));
$('dnd-morning').addEventListener('click',()=>setDnd({until:nextMorning()}));
$('dnd-schedule').addEventListener('click',()=>setDnd({scheduleEnabled:!ui.dnd.scheduleEnabled,start:$('dnd-start').value||'22:00',end:$('dnd-end').value||'07:00'}));
$('dnd-repeat').addEventListener('click',()=>setDnd({repeatCallers:ui.dnd.repeatCallers===false}));
['dnd-start','dnd-end'].forEach(id=>$(id).addEventListener('change',()=>{if($(id).value)setDnd({[id==='dnd-start'?'start':'end']:$(id).value});}));
$('block-form').addEventListener('submit',async e=>{
  e.preventDefault();const number=$('block-number').value.trim();if(!number)return;
  try{applyScreening(await api('/api/blocked',{method:'POST',body:JSON.stringify({number})}));$('block-number').value='';}catch(err){toast(err.message);}
});
document.querySelectorAll('.nav-item[data-view]').forEach(b=>b.addEventListener('click',()=>showView(b.dataset.view,b.dataset.filter)));
document.querySelectorAll('.segmented button').forEach(b=>b.addEventListener('click',()=>showView('calls',b.dataset.filter)));

/* ---------- Dialer ---------- */
function setNumber(value){$('number').value=value;numberChanged();}
function numberChanged(){
  $('backspace').hidden=!$('number').value;
  const typed=nameFor($('number').value);
  $('dial-hint').textContent=typed||'Type or paste a number · Return to call';
  $('dial-hint').classList.toggle('named',Boolean(typed));
  $('call-start').disabled=!ui.ready||starting||!clean($('number').value)||Boolean(ui.call&&LIVE.has(ui.call.state));
}
function pressDigit(d){
  flashKey(d);
  if(ui.call?.state==='active'){sendDtmf(d);return;}
  if(ui.call&&LIVE.has(ui.call.state))return;
  setNumber($('number').value+d);
}
async function sendDtmf(digit){
  try{await api(`/api/calls/${ui.call.id}/dtmf`,{method:'POST',body:JSON.stringify({digit})});}catch(e){toast(e.message);}
}
buildKeypad($('keypad'),pressDigit);
buildKeypad($('incall-keypad'),pressDigit);
$('number').addEventListener('input',numberChanged);
$('number').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();startCall();}});
$('backspace').addEventListener('click',()=>{setNumber($('number').value.slice(0,-1));$('number').focus();});
$('call-start').addEventListener('click',()=>startCall());
function startCall(){
  const number=$('number').value;
  if(!ui.ready){toast(ui.reg?.error||'Your landline is not connected yet.');return;}
  if(!/^\+?[0-9]{3,15}$/.test(number.replace(/[ ()-]/g,''))){toast('Enter a valid telephone number.');return;}
  connectAudio('call','/api/calls',{number});
}

// Desktop keyboard: type digits anywhere, Return calls, Escape hangs up.
document.addEventListener('keydown',e=>{
  if(document.body.classList.contains('needs-setup')||e.metaKey||e.ctrlKey||e.altKey||$('name-dialog').open)return;
  const typing=e.target.closest('input,textarea,[contenteditable]');
  const live=ui.call&&LIVE.has(ui.call.state);
  if(e.key==='Escape'){if(live)$('call-end').click();else if(!typing)setNumber('');return;}
  if(typing)return;
  if(/^[0-9*#+]$/.test(e.key)){e.preventDefault();if(e.key==='+'){if(!live)setNumber($('number').value+'+');}else pressDigit(e.key);}
  else if(e.key==='Backspace'&&!live){e.preventDefault();setNumber($('number').value.slice(0,-1));}
  else if(e.key==='Enter'&&!live&&$('number').value){e.preventDefault();startCall();}
});
document.addEventListener('paste',e=>{
  if(e.target.closest('input,textarea')||document.body.classList.contains('needs-setup')||$('name-dialog').open)return;
  const value=(e.clipboardData?.getData('text')||'').replace(/[^\d+]/g,'');
  if(value&&!(ui.call&&LIVE.has(ui.call.state))){e.preventDefault();setNumber(value);}
});

/* ---------- In-call panel ---------- */
const LABELS={dialing:'Calling…',ringing:'Ringing…',early_media:'Connecting…',incoming:'Incoming call',answering:'Connecting…',active:'Connected',ending:'Ending…',ended:'Call ended',cancelled:'Call cancelled',declined:'Call declined',failed:'Call failed'};
function renderCall(state){
  const was=ui.call;
  ui.ready=state.available===true;ui.call=state.call;
  const c=ui.call,live=Boolean(c&&LIVE.has(c.state));
  if(was&&LIVE.has(was.state)&&!live)loadHistory();
  $('dialer').hidden=live;$('incall').hidden=!live;
  numberChanged();
  $('audio-start').disabled=live||starting||Boolean(peer);
  if(!c||!live){
    text('call-state',!ui.ready?(ui.reg?.state==='needs_authorization'?'Set up your landline to call.':ui.reg?.error?'Not connected · '+ui.reg.error:'Connecting to your landline…'):c&&c.error&&Date.now()/1000-(c.endedAt||0)<20?`${LABELS[c.state]||c.state} · ${c.error}`:'');
    $('call-state').className='status-line center'+(c?.error&&!ui.ready?' error':'');
    if(!live&&connectionMode==='call'&&c&&currentCallId===c.id)stopAudio(false);
    return;
  }
  const incoming=c.state==='incoming';
  $('incall').classList.toggle('ringing',incoming||c.state==='ringing'||c.state==='dialing');
  const callerName=nameFor(c.number);
  text('incall-number',callerName||formatNumber(c.number));
  $('incall-sub').hidden=!callerName;text('incall-sub',callerName?formatNumber(c.number):'');
  text('incall-state',(LABELS[c.state]||c.state)+(c.error?` · ${c.error}`:''));
  if(c.state==='active'&&c.connectedAt){const s=Math.max(0,Math.floor(Date.now()/1000-c.connectedAt));text('incall-timer',`${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`);}
  else text('incall-timer','');
  $('call-answer').hidden=!incoming;$('call-answer').disabled=starting||Boolean(peer);
  $('call-end').setAttribute('aria-label',incoming?'Decline':'Hang up');
  $('incall-controls').hidden=incoming;
  $('toggle-keypad').disabled=c.state!=='active';
  if(c.state!=='active'){$('incall-keypad').hidden=true;$('toggle-keypad').setAttribute('aria-pressed','false');}
  const m=c.media;
  text('call-media',m?.codec?`${m.codec} · ${m.packetization||''}`:'');
}
$('call-answer').addEventListener('click',()=>{if(ui.call)connectAudio('call',`/api/calls/${ui.call.id}/answer`);});
$('call-end').addEventListener('click',async()=>{
  const id=ui.call?.id,bound=currentCallId;
  await stopAudio();
  if(id&&id!==bound)try{await api(`/api/calls/${encodeURIComponent(id)}`,{method:'DELETE'});}catch{}
});
$('call-mute').addEventListener('click',()=>{
  const muted=$('call-mute').getAttribute('aria-pressed')!=='true';
  stream?.getAudioTracks().forEach(t=>{t.enabled=!muted;});
  $('call-mute').setAttribute('aria-pressed',String(muted));
  $('call-mute').querySelector('use').setAttribute('href',muted?'#i-mic-off':'#i-mic');
  $('call-mute').querySelector('span').textContent=muted?'Unmute':'Mute';
});
$('toggle-keypad').addEventListener('click',()=>{
  const show=$('incall-keypad').hidden;$('incall-keypad').hidden=!show;$('toggle-keypad').setAttribute('aria-pressed',String(show));
});
function resetMute(){
  $('call-mute').setAttribute('aria-pressed','false');$('call-mute').querySelector('use').setAttribute('href','#i-mic');$('call-mute').querySelector('span').textContent='Mute';
}

/* ---------- WebRTC audio (call + loopback test) ---------- */
async function stopAudio(hangup=true,message){
  generation++;clearTimeout(timer);
  const closingPeer=peer,closingSession=sessionId,closingCall=currentCallId,mode=connectionMode;
  peer=null;stream?.getTracks().forEach(t=>t.stop());stream=null;sessionId=null;connectionMode=null;currentCallId=null;
  closingPeer?.close();$('playback').srcObject=null;resetMute();
  $('audio-stop').disabled=true;$('audio-start').disabled=starting||Boolean(ui.call&&LIVE.has(ui.call.state));
  if(mode==='loopback')text('audio-status',message||'Audio test stopped. Microphone released.');
  try{
    if(mode==='loopback'&&closingSession)await fetch(`/api/audio/${encodeURIComponent(closingSession)}`,{method:'DELETE',keepalive:true});
    if(mode==='call'&&closingCall&&hangup)await fetch(`/api/calls/${encodeURIComponent(closingCall)}`,{method:'DELETE',keepalive:true});
  }catch{}
}
async function connectAudio(mode,endpoint,extra={}){
  if(starting)return;
  if(peer)await stopAudio();
  starting=true;const run=++generation;numberChanged();$('audio-start').disabled=true;
  if(mode==='loopback')text('audio-status','Requesting microphone access…');
  try{
    if(!navigator.mediaDevices?.getUserMedia)throw new Error('Microphone access is not available here.');
    const capture=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
    if(run!==generation){capture.getTracks().forEach(t=>t.stop());return;}
    stream=capture;connectionMode=mode;
    const active=new RTCPeerConnection({iceServers:[]});peer=active;
    active.ontrack=async({track,streams})=>{
      if(run!==generation)return;
      $('playback').srcObject=streams[0]||new MediaStream([track]);
      try{await $('playback').play();}catch{toast('Allow audio playback to hear the call.');}
    };
    active.onconnectionstatechange=()=>{
      if(run!==generation)return;
      if(active.connectionState==='connected'&&mode==='loopback')text('audio-status','Live loopback — speak and you should hear yourself.');
      else if(['failed','disconnected'].includes(active.connectionState)){stopAudio(true,'Audio connection ended.');if(mode==='call')toast('Audio connection to the gateway ended.');}
    };
    capture.getTracks().forEach(t=>active.addTrack(t,capture));
    if(mode==='loopback')$('audio-stop').disabled=false;
    await active.setLocalDescription(await active.createOffer());
    if(active.iceGatheringState!=='complete')await new Promise((resolve,reject)=>{
      const listener=()=>{if(active.iceGatheringState==='complete'){clearTimeout(deadline);active.removeEventListener('icegatheringstatechange',listener);resolve();}};
      const deadline=setTimeout(()=>{active.removeEventListener('icegatheringstatechange',listener);reject(new Error('Audio negotiation timed out'));},10000);
      active.addEventListener('icegatheringstatechange',listener);listener();
    });
    if(run!==generation)return;
    const answer=await api(endpoint,{method:'POST',body:JSON.stringify({...active.localDescription.toJSON(),...extra})});
    if(run!==generation){await fetch(answer.callId?`/api/calls/${answer.callId}`:`/api/audio/${answer.sessionId}`,{method:'DELETE'});return;}
    if(mode==='call')currentCallId=answer.callId;else sessionId=answer.sessionId;
    await active.setRemoteDescription({sdp:answer.sdp,type:answer.type});
    if(mode==='loopback')timer=setTimeout(()=>stopAudio(true,'Two-minute audio test complete.'),115000);
    pollCall();
  }catch(e){
    if(run===generation){
      const message=e.name==='NotAllowedError'?'Microphone access was denied. Allow it in System Settings ▸ Privacy & Security ▸ Microphone.':e.message;
      await stopAudio(true,message);toast(message);
    }
  }finally{starting=false;numberChanged();if(ui.call)renderCall({available:ui.ready,call:ui.call});}
}
$('audio-start').addEventListener('click',()=>connectAudio('loopback','/api/audio/offer'));
$('audio-stop').addEventListener('click',()=>stopAudio());
$('registration-retry').addEventListener('click',async()=>{
  $('registration-retry').disabled=true;
  try{const r=await fetch('/api/registration/retry',{method:'POST',signal:AbortSignal.timeout(45000)});renderRegistration(await r.json());}
  catch(e){toast(e.message);}finally{$('registration-retry').disabled=false;}
});
$('refresh').addEventListener('click',refreshStatus);
$('logout').addEventListener('click',()=>window.fiberVoice?.logout());

/* ---------- Polling ---------- */
async function pollCall(){
  if(polling)return;polling=true;
  try{renderCall(await api('/api/calls/current',{timeout:4000}));}catch{}finally{polling=false;}
}
async function pollRegistration(){try{renderRegistration(await api('/api/registration/status',{timeout:5000}));}catch{}}
setInterval(()=>{if(ui.started&&(!document.hidden||ui.call&&LIVE.has(ui.call.state)))pollCall();},1000);
setInterval(()=>{if(ui.started&&!document.hidden)pollRegistration();},10000);
setInterval(()=>{if(ui.started&&!document.hidden){loadHistory();loadScreening();}},20000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&ui.started){pollCall();loadHistory();loadScreening();pollRegistration();}});
window.addEventListener('pagehide',()=>stopAudio());

function start(){
  $('app').hidden=false;ui.started=true;
  showView(ui.view,ui.filter);pollRegistration();pollCall();loadHistory();loadContacts();loadScreening();refreshStatus();renderContactsBanner();
}
function stop(){ui.started=false;stopAudio();$('app').hidden=true;ui.history=[];ui.missed=0;ui.local={};ui.blocked=[];ui.dnd={};ui.numbers={};ui.favorites=[];ui.contactSel=null;text('line-number','—');}
// Hooks for the native Mac shell (click-to-call links, notification actions, menu bar).
function dial(number){
  const value=String(number||'').replace(/[^\d+]/g,'').slice(0,16);
  if(!value||ui.call&&LIVE.has(ui.call.state))return false;
  if(ui.started)showView('calls',ui.filter);
  setNumber(value);$('number').focus();
  return true;
}
async function answer(){
  if(ui.call?.state!=='incoming')try{renderCall(await api('/api/calls/current',{timeout:4000}));}catch{}
  if(ui.call?.state==='incoming'&&!peer&&!starting){$('call-answer').click();return true;}
  return false;
}
function focusDialer(){if(ui.started)showView('calls',ui.filter);$('number').focus();return true;}
function decline(){if(ui.call&&LIVE.has(ui.call.state)){$('call-end').click();return true;}return false;}
window.fiberApp={start,stop,dial,answer,decline,focusDialer,setSystemContacts,editName,refreshScreening:loadScreening,showContacts:()=>showView('contacts'),addContact,showDnd:()=>showView('dnd'),showBlocked:()=>showView('blocked'),showMissed:()=>showView('calls','missed'),showRecents:()=>showView('calls','all'),showConnection:()=>showView('connection'),get started(){return ui.started;}};
})();
