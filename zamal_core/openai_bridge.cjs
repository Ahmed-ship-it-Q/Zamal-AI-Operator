'use strict';
// n8n remains credential owner. Plaintext exists only in this short-lived process.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),https=require('node:https');
const ROOT=path.resolve(__dirname,'..');
const MODEL='gpt-4.1-mini-2025-04-14';
const {generationBody:validateRequest}=require('./gemini_bridge.cjs');
const fail=()=>{throw new Error('openai_transport_failed');};
function generationBody(request){
  const validated=validateRequest(request);
  const properties={};
  for(const name of ['context_id','context_hash','stage','summary','proposals','evidence_references','next_action'])
    properties[name]=['proposals','evidence_references'].includes(name)?{type:'array',items:{type:'string'}}:{type:'string'};
  return {model:MODEL,store:false,tools:[],tool_choice:'none',max_output_tokens:2048,
    instructions:validated.systemInstruction.parts[0].text,input:JSON.stringify(request),
    text:{format:{type:'json_schema',name:'operator_draft',strict:true,
      schema:{type:'object',properties,required:Object.keys(properties),additionalProperties:false}}}};
}
function responseText(value){
  if(value.status!=='completed'||value.error||value.incomplete_details||!Array.isArray(value.output)||value.output.length!==1)fail();
  const m=value.output[0];
  if(m.type!=='message'||m.role!=='assistant'||m.status!=='completed'||!Array.isArray(m.content)||m.content.length!==1)fail();
  const p=m.content[0];
  if(p.type!=='output_text'||typeof p.text!=='string'||!p.text||Buffer.byteLength(p.text)>128000)fail();
  return JSON.parse(p.text);
}
function credentialFields(c){
  if((c.url||'https://api.openai.com/v1')!=='https://api.openai.com/v1'||c.header||
    typeof c.apiKey!=='string'||!c.apiKey||/[\r\n]/.test(c.apiKey)||
    (c.organizationId && (typeof c.organizationId!=='string'||!/^org-[A-Za-z0-9_-]+$/.test(c.organizationId))))fail();
  return {key:c.apiKey,organization:c.organizationId||''};
}
function regular(file,secret=false){
  const s=fs.lstatSync(file);
  if(!s.isFile()||s.isSymbolicLink()||s.nlink!==1||(secret&&(s.mode&0o077)))fail();
}
async function configuration(){
  for(const d of ['.n8n-data','.n8n-data/.n8n'])if(fs.lstatSync(path.join(ROOT,d)).isSymbolicLink())fail();
  const settingsFile=path.join(ROOT,'.n8n-data/.n8n/config'),database=path.join(ROOT,'.n8n-data/.n8n/database.sqlite');
  regular(settingsFile,true);regular(database);
  const sqlite=require(path.join(ROOT,'node_modules/sqlite3'));
  const db=await new Promise((resolve,reject)=>{const d=new sqlite.Database(database,sqlite.OPEN_READONLY,e=>e?reject(e):resolve(d));});
  try{
    const rows=await new Promise((resolve,reject)=>db.all('SELECT id,data FROM credentials_entity WHERE type=?',['openAiApi'],(e,r)=>e?reject(e):resolve(r)));
    if(rows.length!==1)fail();
    const row=rows[0];
    if(!Buffer.from(row.data,'base64').subarray(0,8).equals(Buffer.from('Salted__')))fail();
    const raw=fs.readFileSync(settingsFile,'utf8'),settings=JSON.parse(raw);
    if(typeof settings.encryptionKey!=='string'||!settings.encryptionKey)fail();
    const {CipherAes256CBC}=require(path.join(ROOT,'node_modules/n8n/node_modules/n8n-core/dist/encryption/aes-256-cbc.js'));
    const fields=credentialFields(JSON.parse(new CipherAes256CBC().decrypt(row.data,settings.encryptionKey)));
    const identity=crypto.createHash('sha256').update(JSON.stringify([row.id,row.data,raw,MODEL,fs.readFileSync(__filename,'utf8'),fs.readFileSync(path.join(__dirname,'gemini_bridge.cjs'),'utf8')])).digest('hex');
    return {...fields,model:MODEL,reference:'n8n:credential:openAiApi:'+row.id,identity};
  }finally{await new Promise(resolve=>db.close(resolve));}
}
async function main(){
  const cfg=await configuration();
  if(process.argv[2]==='--probe'){process.stdout.write(JSON.stringify({ready:true,model:cfg.model,reference:cfg.reference,identity:cfg.identity}));return;}
  if(process.argv[2]!=='--invoke'||process.argv[3]!==cfg.identity)fail();
  let input='';for await(const chunk of process.stdin){input+=chunk;if(Buffer.byteLength(input)>250000)fail();}
  const body=JSON.stringify(generationBody(JSON.parse(input)));
  const headers={'Content-Type':'application/json','Content-Length':Buffer.byteLength(body),Authorization:'Bearer '+cfg.key};
  if(cfg.organization)headers['OpenAI-Organization']=cfg.organization;
  const result=await new Promise((resolve,reject)=>{
    const req=https.request({hostname:'api.openai.com',port:443,path:'/v1/responses',method:'POST',headers,timeout:45000},res=>{
      if(res.statusCode!==200){res.resume();reject(new Error('provider_http_'+res.statusCode));return;}
      let output='';res.on('data',chunk=>{output+=chunk;if(Buffer.byteLength(output)>512000)res.destroy(new Error('output_limit'));});
      res.on('error',reject);res.on('end',()=>{try{resolve(responseText(JSON.parse(output)));}catch(e){reject(e);}});
    });
    req.on('timeout',()=>req.destroy(new Error('timeout')));req.on('error',reject);req.end(body);
  });
  process.stdout.write(JSON.stringify(result));
}
if(require.main===module)main().catch(error=>{
  const safe=/^provider_http_[0-9]{3}$/.test(error.message)?error.message:'openai_transport_failed';
  process.stdout.write(JSON.stringify({transport_error:safe}));process.exitCode=1;
});
module.exports={generationBody,responseText,credentialFields};
