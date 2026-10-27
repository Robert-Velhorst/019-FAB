import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
assert.equal(process.env.GITHUB_ACTIONS,'true');
assert.equal(process.env.GITHUB_REPOSITORY,'Robert-Velhorst/019-FAB');
assert.equal(process.env.RUNNER_ENVIRONMENT,'github-hosted');
assert.equal(process.platform,'linux');
assert.equal(process.versions.node,'26.12.1');
const output=path.resolve('FAB_NATIVE_INSTALL_EVIDENCE/demo-browser');fs.mkdirSync(output,{recursive:true});
const chrome=execFileSync('which',['google-chrome'],{encoding:'utf8'}).trim();assert.ok(path.isAbsolute(chrome));
const next=path.resolve('node_modules/next/dist/bin/next');
const origin='http://127.0.0.1:4173';
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'fab-typography-browser-'));fs.chmodSync(profile,0o700);
const children=[];const logs={};const errors=[];const browserLog=[];const pages=[];let ws;
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function start(label,command,args){
  const child=spawn(command,args,{cwd:process.cwd(),env:{...process.env,NEXT_TELEMETRY_DISABLED:'1'},detached:true,stdio:['ignore','pipe','pipe']});
  children.push(child);logs[label]='';child.on('error',e=>{logs[label]+=String(e);});
  for(const stream of [child.stdout,child.stderr])stream.on('data',data=>{logs[label]=(logs[label]+data.toString()).slice(-512000);});
  return child;
}
const receipt={routes:pages,original_SDK_helpers_or_commands_changed:false,server_origin:origin,Chrome_sandbox_disabled:false,real_user_data_or_operator_acceptance:false,all_five_original_routes_verified:false};
try{
  start('next',process.execPath,[next,'start','demo','--hostname','127.0.0.1','--port','4173']);
  let ready=false;
  for(let i=0;i<120;i++){try{if((await fetch(origin)).status===200){ready=true;break;}}catch{}await sleep(250);}
  assert.ok(ready,'Owned loopback Next server did not become ready.');
  const chromeArgs=['--headless=new','--no-first-run','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'];
  assert.ok(!chromeArgs.some(a=>a.includes('no-sandbox')||a.includes('disable-web-security')||a.includes('ignore-certificate')));
  start('chrome',chrome,chromeArgs);
  let endpoint;
  for(let i=0;i<120;i++){endpoint=logs.chrome.match(/DevTools listening on (ws:\/\/127\.0\.0\.1:\d+\/[^\s]+)/)?.[1];if(endpoint)break;await sleep(250);}
  assert.ok(endpoint,'Sandboxed installed Chrome did not expose its loopback CDP endpoint.');
  ws=new WebSocket(endpoint);await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
  let id=0;const pending=new Map();
  ws.addEventListener('message',event=>{
    const data=JSON.parse(event.data);
    if(data.id){const entry=pending.get(data.id);if(!entry)return;pending.delete(data.id);clearTimeout(entry.timer);data.error?entry.reject(new Error(JSON.stringify(data.error))):entry.resolve(data.result);}
    else if(data.method==='Runtime.exceptionThrown')errors.push(data.params);
    else if(data.method==='Log.entryAdded')browserLog.push(data.params.entry);
  });
  function call(method,params={},sessionId){return new Promise((resolve,reject)=>{const current=++id;const timer=setTimeout(()=>{pending.delete(current);reject(new Error(`CDP timeout: ${method}`));},30000);pending.set(current,{resolve,reject,timer});ws.send(JSON.stringify({id:current,method,params,...(sessionId?{sessionId}:{})}));});}
  receipt.actualBrowser=await call('Browser.getVersion');receipt.chromeArguments=chromeArgs;receipt.launcherSha256=createHash('sha256').update(fs.readFileSync(chrome)).digest('hex');
  for(const route of ['/','/dark','/themes','/variants','/list-items']){
    const response=await fetch(origin+route);assert.equal(response.status,200,route);const html=await response.text();
    const target=await call('Target.createTarget',{url:'about:blank'});const attachment=await call('Target.attachToTarget',{targetId:target.targetId,flatten:true});const session=attachment.sessionId;
    await call('Page.enable',{},session);await call('Runtime.enable',{},session);await call('Log.enable',{},session);
    await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false},session);
    await call('Page.navigate',{url:origin+route},session);
    let observed;
    for(let attempt=0;attempt<120;attempt++){
      const result=await call('Runtime.evaluate',{expression:`(() => { const prose=document.querySelector('.prose'); const headings=prose?[...prose.querySelectorAll('h1,h2,h3')]:[]; const heading=headings.find(node=>!node.closest('.not-prose')); if(document.readyState!=='complete'||!prose||!heading)return null; const style=getComputedStyle(heading),base=getComputedStyle(prose),bounds=heading.getBoundingClientRect(); return {url:location.href,title:document.title,proseCount:document.querySelectorAll('.prose').length,headingText:heading.textContent,excludedNotProseHeadings:headings.filter(node=>node.closest('.not-prose')).length,headingFontSize:style.fontSize,containerFontSize:base.fontSize,headingColor:style.color,headingWidth:bounds.width,headingHeight:bounds.height}; })()`,returnByValue:true},session);
      assert.equal(result.exceptionDetails,undefined);observed=result.result.value;if(observed)break;await sleep(250);
    }
    assert.ok(observed,route+': original Typography content did not render.');assert.equal(observed.url,origin+route);
    const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false,fromSurface:true},session);const bytes=Buffer.from(shot.data,'base64');const name=route==='/'?'index':route.slice(1);fs.writeFileSync(path.join(output,name+'.png'),bytes);
    pages.push({route,status:response.status,htmlSha256:createHash('sha256').update(html).digest('hex'),observed,screenshot:name+'.png',screenshotSha256:createHash('sha256').update(bytes).digest('hex')});
    fs.writeFileSync(path.join(output,name+'-server.html'),html);
    const dom=await call('Runtime.evaluate',{expression:'document.documentElement.outerHTML',returnByValue:true},session);
    fs.writeFileSync(path.join(output,name+'-browser.html'),dom.result.value);
    const styled=observed.headingWidth>0&&observed.headingHeight>0&&parseFloat(observed.headingFontSize)>parseFloat(observed.containerFontSize);
    pages[pages.length-1].headingStylingPass=styled;
    await call('Target.closeTarget',{targetId:target.targetId});
  }
  assert.ok(pages.every(page=>page.headingStylingPass),'Computed Typography heading styling is absent on at least one route.');
  assert.equal(errors.length,0,'Uncaught browser JavaScript exceptions remain.');receipt.all_five_original_routes_verified=true;
}catch(error){receipt.failure=String(error);process.exitCode=1;}
finally{
  ws?.close();
  for(const child of children.reverse()){if(child.pid>1){try{process.kill(-child.pid,'SIGTERM');}catch(error){if(error.code!=='ESRCH')throw error;}}}
  await sleep(500);
  for(const [name,log] of Object.entries(logs))fs.writeFileSync(path.join(output,name+'.log'),log);
  receipt.uncaughtBrowserExceptions=errors;receipt.allBrowserLogEntries=browserLog;
  fs.writeFileSync(path.join(output,'summary.json'),JSON.stringify(receipt,null,2)+'\n');
  assert.ok(path.resolve(profile).startsWith(path.resolve(os.tmpdir())+path.sep)&&path.basename(profile).startsWith('fab-typography-browser-'));
  fs.rmSync(profile,{recursive:true,force:true});
  console.log(JSON.stringify({routes:pages.length,allFivePass:receipt.all_five_original_routes_verified,actualBrowser:receipt.actualBrowser?.product,failure:receipt.failure,ChromeSandboxDisabled:false}));
}
