'use strict';
// 账号。密钥只留在这台设备，不进服务器。未登录的收藏和记录不会自动上传。
(()=>{
const ACCOUNT_KEYS=['fav','sel','rec','recT','ang','adult','ageOk','lang','tl','qh','wbMode','wbChar','wbCharTxt','wbStyle','wbBm','wbPl','wbKeepBg','wbNeutral','wbSel','wbCfg'];
const PW={user:null,csrf:'',mem:{},expect:/(?:^|; )pw_in=1(?:;|$)/.test(document.cookie),readyDone:false,https:location.protocol==='https:'};
window.PW=PW;window.PW_EXPECT=PW.expect;
const $ =s=>document.querySelector(s);
function parse(k){try{return JSON.parse(localStorage[k]||'null')}catch(e){return null}}
function deviceKey(){if(!PW.user)return '';try{return localStorage['pw.k.'+PW.user.id]||''}catch(e){return ''}}
function putDeviceKey(key){if(!PW.user)return;try{if(key)localStorage['pw.k.'+PW.user.id]=key;else localStorage.removeItem('pw.k.'+PW.user.id)}catch(e){}}
PW.deviceKey=deviceKey;
PW.get=function(k){
  if(PW.user){
    if(k==='wbCfg'){const c=Object.assign({prov:'openai',base:'',key:'',model:'',field:'image[]',proto:'edits',proxy:false},PW.mem.wbCfg||{});c.key=deviceKey();return c}
    return PW.mem[k]===undefined?null:PW.mem[k];
  }
  if(PW.expect&&!PW.readyDone)return null;
  return parse(k);
};
PW.set=function(k,v){
  if(PW.user&&ACCOUNT_KEYS.includes(k)){
    if(k==='wbCfg'){const key=v&&v.key||'';const rest=Object.assign({},v||{});delete rest.key;PW.mem.wbCfg=rest;putDeviceKey(key)}
    else PW.mem[k]=v;
    schedule();
    return;
  }
  try{localStorage[k]=JSON.stringify(v)}catch(e){}
};
let timer=0;
function schedule(){clearTimeout(timer);timer=setTimeout(flush,250)}
async function api(method,url,body,type){
  const headers={'X-CSRF-Token':PW.csrf||''};
  let payload;
  if(body instanceof Blob){headers['Content-Type']=type||body.type||'application/octet-stream';payload=body}
  else if(body!=null){headers['Content-Type']='application/json';payload=JSON.stringify(body)}
  const r=await fetch(url,{method,credentials:'include',headers,body:payload});
  const j=await r.json().catch(()=>null);
  if(!r.ok){const e=Error((j&&j.error&&j.error.message)||('HTTP '+r.status));e.status=r.status;throw e}
  return j;
}
PW.api=api;
async function flush(){
  if(!PW.user)return;
  const state={};
  ACCOUNT_KEYS.forEach(k=>{if(PW.mem[k]!==undefined)state[k]=k==='wbCfg'?Object.assign({},PW.mem[k]):PW.mem[k]});
  if(state.wbCfg)delete state.wbCfg.key;
  try{await api('PUT','/api/me/state',{state})}catch(e){}
}
PW.flush=flush;
async function wipeCaches(){
  if(window.caches){try{const ks=await caches.keys();await Promise.all(ks.map(k=>caches.delete(k)))}catch(e){}}
}
PW.putBlob=blob=>api('POST','/api/me/blobs',blob,blob.type||'image/jpeg');
PW.getBlob=async id=>{const r=await fetch('/api/me/blobs/'+id,{credentials:'include'});if(!r.ok)throw Error('图片不存在');return r.blob()};
function anonPack(){
  const fav=parse('fav')||[],rec=parse('rec')||[],sel=parse('sel')||[];
  return {fav,rec,sel,has:fav.length||rec.length||sel.length};
}
function askImport(){
  const box=$('#acctImport');if(!box)return Promise.resolve(false);
  box.hidden=false;
  return new Promise(resolve=>{
    const done=v=>{box.hidden=true;resolve(v)};
    $('#acctImpY').onclick=()=>done(true);
    $('#acctImpN').onclick=()=>done(false);
  });
}
async function importAnon(){
  const pack=anonPack();
  const state=Object.assign({},PW.mem);
  state.fav=[...new Set([].concat(state.fav||[],pack.fav))];
  state.rec=[...new Set([].concat(pack.rec,state.rec||[]))].slice(0,100);
  state.sel=state.sel&&state.sel.length?state.sel:pack.sel;
  PW.mem=state;await flush();
  try{
    const db=await new Promise((res,rej)=>{const q=indexedDB.open('lk-studio',1);q.onsuccess=()=>res(q.result);q.onerror=()=>rej(q.error)});
    const rows=await new Promise((res,rej)=>{const t=db.transaction('h','readonly');const q=t.objectStore('h').getAll();q.onsuccess=()=>res(q.result||[]);q.onerror=()=>rej(q.error)});
    for(const rec of rows){
      if(!rec||!rec.after)continue;
      const after=(await PW.putBlob(rec.after)).id;
      const before=rec.before?(await PW.putBlob(rec.before)).id:null;
      await api('POST','/api/me/history',{t:rec.t,prov:rec.prov,model:rec.model,prompt:rec.prompt,orig:rec.orig,neutral:!!rec.neutral,mode:rec.mode,label:rec.label,after,before});
    }
    db.close();
    indexedDB.deleteDatabase('lk-studio');
  }catch(e){}
  ['fav','rec','recT','sel'].forEach(k=>{try{localStorage.removeItem(k)}catch(err){}});
}
async function boot(){
  let info={secure:PW.https,httpsOrigin:''};
  try{info=await fetch('/api/auth/info',{credentials:'include'}).then(r=>r.json())}catch(e){}
  PW.info=info;
  if(PW.https){
    try{
      const me=await api('GET','/api/auth/me');
      PW.user={id:me.id,username:me.username};PW.csrf=me.csrf;
      const st=await api('GET','/api/me/state');
      PW.mem=st.state||{};
    }catch(e){PW.user=null;PW.csrf='';PW.mem={}}
  }
  PW.readyDone=true;
  paint();
  if(PW.user&&sessionStorage.pwJustIn){
    sessionStorage.removeItem('pwJustIn');
    const mark='pw.asked.'+PW.user.id;
    if(!localStorage[mark]&&(anonPack().has||await idbCount())){
      const yes=await askImport();
      if(yes)await importAnon();
      try{localStorage[mark]='1'}catch(err){}
      if(yes){location.reload();return}
    }
  }
}
function idbCount(){return new Promise(res=>{try{const q=indexedDB.open('lk-studio');q.onsuccess=()=>{const db=q.result;if(!db.objectStoreNames.contains('h')){db.close();return res(0)}const t=db.transaction('h','readonly');const c=t.objectStore('h').count();c.onsuccess=()=>{db.close();res(c.result||0)};c.onerror=()=>res(0)};q.onerror=()=>res(0)}catch(e){res(0)}})}
PW.ready=boot();
function paint(){
  const b=$('#acctBtn');if(!b)return;
  b.textContent=PW.user?PW.user.username:'登录';
  b.classList.toggle('on',!!PW.user);
}
function msg(t,bad){const m=$('#acctMsg');if(!m)return;m.textContent=t||'';m.className='fine'+(bad?' bad':t?' good':'')}
function openBox(){
  const box=$('#acctBox');if(!box)return;
  box.hidden=false;
  const insecure=!PW.https;
  $('#acctForm').hidden=insecure||!!PW.user;
  $('#acctIn').hidden=!insecure;
  $('#acctWho').hidden=!PW.user||insecure;
  if(insecure){$('#acctInP').textContent=PW.info&&PW.info.httpsOrigin?('账号、密码和 API 密钥只能在 '+PW.info.httpsOrigin+' 使用。当前是明文 HTTP，这些接口已关闭。'):'账号、密码和 API 密钥需要 8443 上的受信 HTTPS。证书还没配好时，这些接口拒绝明文提交。'}
  if(PW.user)$('#acctName').textContent=PW.user.username;
  msg('');
}
async function submit(reg){
  if(!PW.https)return msg('明文页面不能提交密码',1);
  const username=$('#acctUser').value.trim(),password=$('#acctPass').value,again=$('#acctPass2').value;
  if(reg&&password!==again)return msg('两次密码不一致',1);
  try{
    await api('POST',reg?'/api/auth/register':'/api/auth/login',{username,password});
    sessionStorage.pwJustIn='1';
    location.reload();
  }catch(e){msg(e.message||'失败',1)}
}
async function logout(){
  try{await flush();await api('POST','/api/auth/logout',{})}catch(e){}
  PW.user=null;PW.csrf='';PW.mem={};
  await wipeCaches();
  location.reload();
}
async function changePassword(){
  const old=$('#acctOld').value,nw=$('#acctNew').value;
  try{await api('POST','/api/auth/password',{old,new:nw});$('#acctOld').value='';$('#acctNew').value='';msg('密码已更新')}
  catch(e){msg(e.message||'失败',1)}
}
document.addEventListener('click',e=>{
  const t=e.target;
  if(t&&t.id==='acctBtn')openBox();
  if(t&&t.id==='acctX')$('#acctBox').hidden=true;
  if(t&&t.id==='acctLogin')submit(false);
  if(t&&t.id==='acctReg')submit(true);
  if(t&&t.id==='acctOut')logout();
  if(t&&t.id==='acctPw')changePassword();
});
addEventListener('storage',e=>{if(e.key==='pw.ping')location.reload()});
PW.ping=()=>{try{localStorage['pw.ping']=String(Date.now())}catch(err){}};
})();
