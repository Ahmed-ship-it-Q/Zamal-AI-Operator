'use strict';
// Fixed synthetic fixture only. No user URLs, scripts, paths, profiles or credentials.
const {chromium}=require('playwright');
const http=require('node:http'),crypto=require('node:crypto'),readline=require('node:readline');
const sha=x=>crypto.createHash('sha256').update(x).digest('hex');
const upload=Buffer.from('Zamal synthetic upload fixture. No candidate information.\n');
const fields={name:'Synthetic Applicant',email:'fixture@example.invalid',role:'fixture'};
const contract=sha(JSON.stringify({fields,upload_sha256:sha(upload),fixture:'job-browser-v1'}));
let browser,server,allowed=false,submissions=0;
const lines=readline.createInterface({input:process.stdin});
const queue=[],waiters=[];
lines.on('line',line=>{if(line.length>4096)process.exit(2);const resolve=waiters.shift();if(resolve)resolve(line);else queue.push(line);});
lines.on('close',()=>{for(const resolve of waiters.splice(0))resolve('{}');});
const next=()=>queue.length?Promise.resolve(queue.shift()):new Promise(resolve=>waiters.push(resolve));
const emit=x=>process.stdout.write(JSON.stringify(x)+'\n');
(async()=>{
 try{
  const request=JSON.parse(await next());
  if(Object.keys(request).length!==1||!['prepare','submit'].includes(request.operation))throw Error();
  server=http.createServer((req,res)=>{
   res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'none'; form-action 'self'; frame-ancestors 'none'");
   if(req.method==='GET'&&req.url==='/'){res.end('<title>Local test</title><a href="/application">Open fixture</a>');return;}
   if(req.method==='GET'&&req.url==='/application'){
    res.end('<title>Synthetic application fixture</title><h1>LOCAL TEST ONLY</h1><form method="post" enctype="multipart/form-data" action="/submit"><label>Name<input name="name"></label><label>Email<input name="email" type="email"></label><select name="role"><option value="fixture">Fixture role</option></select><input type="file" name="attachment"><button type="submit">Perform local test action</button></form>');return;
   }
   if(req.method==='POST'&&req.url==='/submit'&&allowed&&submissions===0){
    let size=0,chunks=[];req.on('data',b=>{size+=b.length;if(size>32768)req.destroy();else chunks.push(b);});
    req.on('end',()=>{const body=Buffer.concat(chunks);if(!body.includes(upload)||!Object.values(fields).every(v=>body.includes(Buffer.from(v)))){res.writeHead(422);res.end('Invalid fixture');return;}submissions++;allowed=false;res.end('<title>Confirmed</title><h1 id="confirmation">Local fixture accepted</h1>');});return;
   }
   res.writeHead(403);res.end('Denied');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin='http://127.0.0.1:'+server.address().port;
  browser=await chromium.launch({channel:'chrome',headless:true,chromiumSandbox:true,timeout:15000,env:{PATH:'/usr/bin:/bin',HOME:process.env.HOME,TMPDIR:process.env.TMPDIR,LANG:'en_US.UTF-8'}});
  const context=await browser.newContext({acceptDownloads:false,serviceWorkers:'block'});
  await context.route('**/*',route=>{
   const r=route.request(),u=new URL(r.url());
   if(u.origin!==origin||!['/','/application','/submit'].includes(u.pathname)||!['GET','POST'].includes(r.method())||(r.method()==='POST'&&(!allowed||u.pathname!=='/submit')))return route.abort('blockedbyclient');
   return route.continue();
  });
  const page=await context.newPage();page.setDefaultTimeout(10000);
  await page.goto(origin);await page.getByRole('link').click();
  const detected=await page.locator('input,select').evaluateAll(elements=>elements.map(e=>({name:e.name,type:e.type})));
  if(detected.length!==4)throw Error();
  await page.locator('[name=name]').fill(fields.name);await page.locator('[name=email]').fill(fields.email);
  await page.locator('[name=role]').selectOption(fields.role);
  await page.locator('[name=attachment]').setInputFiles({name:'synthetic.txt',mimeType:'text/plain',buffer:upload});
  for(const [key,value] of Object.entries(fields))if(await page.locator('[name='+key+']').inputValue()!==value)throw Error();
  if(await page.locator('[name=attachment]').evaluate(e=>e.files[0].size)!==upload.length)throw Error();
  const before=await page.screenshot();
  emit({phase:'PREPARED',contract,fields:detected,upload_sha256:sha(upload),navigated:true,filled:true,submissions,screenshot_sha256:sha(before)});
  if(request.operation==='submit'){
   const decision=JSON.parse(await next());
   if(Object.keys(decision).length!==2||decision.execute!==true||decision.contract!==contract)throw Error();
   allowed=true;
   await Promise.all([page.waitForResponse(r=>r.url()===origin+'/submit'&&r.request().method()==='POST',{timeout:15000}),page.locator('button[type=submit]').click()]);
   if(await page.locator('#confirmation').textContent()!=='Local fixture accepted'||submissions!==1)throw Error();
   emit({phase:'VERIFIED',contract,submissions,confirmation:'Local fixture accepted',screenshot_sha256:sha(await page.screenshot()),synthetic_only:true});
  }
 }catch{emit({phase:'FAILED',reason:'browser_fixture_unavailable_or_contract_denied'});process.exitCode=1;}
 finally{if(browser)await browser.close();if(server)await new Promise(resolve=>server.close(resolve));lines.close();}
})().then(()=>process.exit(process.exitCode||0));
