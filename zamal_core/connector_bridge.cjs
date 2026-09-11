'use strict';
// Fixed read-only connector operations. Existing n8n OAuth owns all tokens.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),https=require('node:https');
const ROOT=path.resolve(__dirname,'..');
const TYPES={gmail:'gmailOAuth2',calendar:'googleCalendarOAuth2Api',github:'githubOAuth2Api'};
function fail(){throw Error('connector_unavailable');}
function plan(r){
 if(!r||!Object.hasOwn(TYPES,r.connector)||typeof r.arguments!=='object')fail();const a=r.arguments;
 if(r.connector==='gmail'){
  if(Object.keys(a).some(k=>k!=='query')||typeof(a.query||'')!=='string'||(a.query||'').length>500)fail();
  return {host:'gmail.googleapis.com',path:'/gmail/v1/users/me/messages?'+new URLSearchParams({q:a.query||'in:inbox',maxResults:'10'})};
 }
 if(r.connector==='calendar'){
  if(Object.keys(a).some(k=>!['start','end'].includes(k))||!Number.isFinite(Date.parse(a.start))||!Number.isFinite(Date.parse(a.end))||Date.parse(a.end)<=Date.parse(a.start))fail();
  return {host:'www.googleapis.com',path:'/calendar/v3/calendars/primary/events?'+new URLSearchParams({timeMin:a.start,timeMax:a.end,singleEvents:'true',orderBy:'startTime',maxResults:'50'})};
 }
 if(Object.keys(a).some(k=>!['repository','view'].includes(k))||!['status','issues','pulls','checks','repositories'].includes(a.view))fail();
 if(a.view==='repositories')return {host:'api.github.com',path:'/user/repos?per_page=20&sort=updated'};
 if(!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(a.repository))fail();
 return {host:'api.github.com',path:'/repos/'+a.repository+({status:'',issues:'/issues?per_page=10',pulls:'/pulls?per_page=10',checks:'/actions/runs?per_page=10'}[a.view])};
}
async function configuration(connector){
 if(!Object.hasOwn(TYPES,connector))fail();
 for(const d of ['.n8n-data','.n8n-data/.n8n'])if(fs.lstatSync(path.join(ROOT,d)).isSymbolicLink())fail();
 const sf=path.join(ROOT,'.n8n-data/.n8n/config'),database=path.join(ROOT,'.n8n-data/.n8n/database.sqlite');
 for(const f of [sf,database]){const s=fs.lstatSync(f);if(!s.isFile()||s.isSymbolicLink()||s.nlink!==1||(f===sf&&(s.mode&0o077)))fail();}
 const sqlite=require(path.join(ROOT,'node_modules/sqlite3'));
 const db=await new Promise((resolve,reject)=>{const d=new sqlite.Database(database,sqlite.OPEN_READONLY,e=>e?reject(e):resolve(d));});
 try{
  const rows=await new Promise((resolve,reject)=>db.all('SELECT id,data FROM credentials_entity WHERE type=?',[TYPES[connector]],(e,r)=>e?reject(e):resolve(r)));
  if(rows.length!==1||!Buffer.from(rows[0].data,'base64').subarray(0,8).equals(Buffer.from('Salted__')))fail();
  const raw=fs.readFileSync(sf,'utf8'),settings=JSON.parse(raw);
  const {CipherAes256CBC}=require(path.join(ROOT,'node_modules/n8n/node_modules/n8n-core/dist/encryption/aes-256-cbc.js'));
  const c=JSON.parse(new CipherAes256CBC().decrypt(rows[0].data,settings.encryptionKey));
  const t=c.oauthTokenData;
  if(!t||typeof t.access_token!=='string'||!t.access_token||/[\r\n]/.test(t.access_token)||c.baseUrl||c.url||c.header)fail();
  return {key:t.access_token,reference:'n8n:'+TYPES[connector]+':'+rows[0].id,identity:crypto.createHash('sha256').update(rows[0].data+raw+fs.readFileSync(__filename,'utf8')).digest('hex')};
 }finally{await new Promise(resolve=>db.close(resolve));}
}
function get(host,p,key){return new Promise((resolve,reject)=>{
 const req=https.request({hostname:host,port:443,path:p,method:'GET',headers:{Authorization:'Bearer '+key,'User-Agent':'ZamalOperator/1.0',Accept:'application/json'},timeout:8000},res=>{
  if(res.statusCode!==200){res.resume();reject(Error('connector_http_'+res.statusCode));return;}
  let data='';res.on('data',c=>{data+=c;if(Buffer.byteLength(data)>512000)res.destroy(Error('limit'));});res.on('error',reject);res.on('end',()=>{try{resolve(JSON.parse(data));}catch{reject(Error('invalid_response'));}});
 });req.on('timeout',()=>req.destroy(Error('timeout')));req.on('error',reject);req.end();
});}
async function main(){
 const r=JSON.parse(fs.readFileSync(0,'utf8'));const cfg=await configuration(r.connector);
 if(r.probe===true){process.stdout.write(JSON.stringify({ready:true,reference:cfg.reference,identity:cfg.identity}));return;}
 if(r.expected_identity!==cfg.identity)fail();
 const p=plan(r);const data=await get(p.host,p.path,cfg.key);let result;
 if(r.connector==='gmail'){
  const messages=[];for(const m of (data.messages||[]).slice(0,10)){
   if(!/^[A-Za-z0-9]+$/.test(m.id))fail();const msg=await get(p.host,'/gmail/v1/users/me/messages/'+m.id+'?format=metadata',cfg.key);
   const h=name=>(msg.payload?.headers||[]).find(x=>x.name.toLowerCase()===name)?.value||'';
   messages.push({id:msg.id,thread_id:msg.threadId,subject:h('subject'),sender:h('from'),snippet:msg.snippet||'',date:h('date'),outgoing:(msg.labelIds||[]).includes('SENT')});
  }result={messages};
 }else if(r.connector==='calendar')result={events:(data.items||[]).filter(e=>e.status!=='cancelled').map(e=>({id:e.id,title:e.summary||'(untitled)',start:e.start?.dateTime||e.start?.date,end:e.end?.dateTime||e.end?.date}))};
 else{const fields=['id','full_name','description','default_branch','open_issues_count','updated_at','html_url','number','title','name','state','status','conclusion'];const pick=x=>Object.fromEntries(fields.filter(k=>x[k]!==undefined).map(k=>[k,x[k]]));result={data:Array.isArray(data)?data.map(pick):data.workflow_runs?data.workflow_runs.map(pick):pick(data)};}
 process.stdout.write(JSON.stringify({...result,connection_ref:cfg.reference,retrieved_at:Date.now()/1000}));
}
if(require.main===module)main().catch(()=>{process.stdout.write(JSON.stringify({unavailable:true,code:'connector_auth_or_read_failed'}));process.exitCode=1;});
module.exports={plan};
