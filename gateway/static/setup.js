// First-run setup and logout: trust the router certificate, then authorize via OTP.
(()=>{
const $=id=>document.getElementById(id);
let routerFingerprint=null,busy=false;
const steps=['router','otp','done'];
function show(step){
  const index=steps.indexOf(step);
  steps.forEach((name,i)=>{const li=$(`setup-step-${name}`);li.classList.toggle('current',i===index);li.classList.toggle('done',i<index);});
}
function status(message,error=false){$('setup-status').textContent=message||'';$('setup-status').className='status-line'+(error?' error':'');}
async function api(path,options={}){
  const response=await fetch(path,{...options,headers:options.body?{'Content-Type':'application/json'}:{},signal:AbortSignal.timeout(options.timeout||45000)});
  let data={};try{data=await response.json();}catch{}
  if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);
  return data;
}
async function run(button,task){
  if(busy)return;busy=true;button.disabled=true;
  try{await task();}catch(e){status(e.name==='TimeoutError'?'The router did not answer in time. Try again.':e.message,true);}
  finally{busy=false;button.disabled=false;}
}
function resetForm(){
  routerFingerprint=null;$('router-details').hidden=true;$('setup-trust').hidden=true;
  $('setup-check').textContent='Check router';$('setup-check').className='btn primary';
  $('otp-entry').hidden=true;$('setup-send').hidden=false;status('');
}
function openSetup(state){
  document.body.classList.add('needs-setup');
  $('setup').hidden=false;$('app').hidden=true;
  if(!state.trusted)show('router');
  else{show('otp');if(state.otpPending)showOtp(state.otpTarget);}
}
function showOtp(target){
  $('otp-entry').hidden=false;$('setup-send').hidden=true;
  $('otp-target').textContent=`A code was sent to ${target||'your registered mobile number'}. It can take a minute to arrive.`;
  $('setup-otp').value='';$('setup-otp').focus();
}
function finished(state){
  show('done');
  const r=state.registration||{};
  $('setup-done-note').textContent=r.registered?'This Mac is registered with your landline.':`Authorized. Registration: ${r.error||'connecting…'}`;
  status(r.registered?'Setup complete.':'Setup saved. Registration keeps retrying in the background.',!r.registered&&Boolean(r.error));
  $('setup-finish').focus();
}
$('setup-check').addEventListener('click',()=>run($('setup-check'),async()=>{
  status('Looking for your JioFiber router…');
  const r=await api('/api/setup/router',{timeout:20000});
  routerFingerprint=r.certificateSha256;
  $('router-details').hidden=false;
  $('setup-number').textContent=r.number?`+91 ${r.number}`:'Not reported by router';
  $('setup-issuer').textContent=[r.organization||'Unknown issuer',r.selfSigned?'self-signed':null,r.validUntil?`valid until ${r.validUntil.slice(0,4)}`:null].filter(Boolean).join(' · ');
  $('setup-fingerprint').textContent=r.certificateSha256.match(/.{1,4}/g).join(' ');
  $('setup-ipv6').textContent=r.ipv6?'Available':'Not available — enable IPv6 on your router, then check again';
  $('setup-ipv6').className=r.ipv6?'':'warn';
  $('setup-trust').hidden=false;$('setup-check').textContent='Check again';$('setup-check').className='btn ghost';
  status(r.ipv6?'Router found. Confirm this is your own JioFiber router.':'Router found, but calling needs IPv6 on this network.',!r.ipv6);
}));
$('setup-trust').addEventListener('click',()=>run($('setup-trust'),async()=>{
  if(!routerFingerprint)return;
  await api('/api/setup/trust',{method:'POST',body:JSON.stringify({certificateSha256:routerFingerprint})});
  show('otp');status('Router trusted.');
}));
async function sendCode(button){
  await run(button,async()=>{
    status('Asking the router to authorize this Mac…');
    const r=await api('/api/setup/otp',{method:'POST'});
    if(r.state==='otp_sent'){showOtp(r.otpTarget);status('');}
    else finished(r);
  });
}
$('setup-send').addEventListener('click',()=>sendCode($('setup-send')));
$('setup-resend').addEventListener('click',()=>sendCode($('setup-resend')));
$('setup-verify').addEventListener('click',()=>run($('setup-verify'),async()=>{
  const otp=$('setup-otp').value.trim();
  if(!/^\d{5,6}$/.test(otp)){status('Enter the 5- or 6-digit code you received.',true);return;}
  status('Verifying and registering…');
  finished(await api('/api/setup/verify',{method:'POST',body:JSON.stringify({otp})}));
}));
$('setup-otp').addEventListener('keydown',e=>{if(e.key==='Enter')$('setup-verify').click();});
$('setup-finish').addEventListener('click',()=>{
  document.body.classList.remove('needs-setup');$('setup').hidden=true;
  window.fiberApp?.start();
});

// Log out: deregister, forget router trust, authorization and call log.
async function resetSetup(){
  await api('/api/setup/reset',{method:'POST'});
  window.fiberApp?.stop();resetForm();openSetup({trusted:false});
}
async function logout(){
  if(!confirm('Log out of your landline on this Mac?\n\nThis Mac stops receiving calls, and its call log, saved names and blocked numbers are cleared. Logging back in needs a new code sent to your registered mobile.'))return;
  try{await resetSetup();}catch(e){alert(e.message);}
}
window.fiberVoice={resetSetup,logout,state:()=>api('/api/setup')};

api('/api/setup').then(state=>{if(state.provisioned)window.fiberApp?.start();else openSetup(state);})
  .catch(()=>window.fiberApp?.start());
})();
