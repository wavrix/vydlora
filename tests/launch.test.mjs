import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { localAppOrigin, isVydloraRunning, waitForEngine } from '../lib/launch.mjs';

test('launcher limits browser URLs to a valid local port', () => {
  assert.equal(localAppOrigin('5173'), 'http://127.0.0.1:5173');
  for (const value of ['https://elsewhere.example',0,65536,'bad']) assert.throws(()=>localAppOrigin(value),/PORT/);
});

test('launcher waits for engine readiness and reuses an existing app without spawning another engine', async t => {
  let pings=0;
  const server=http.createServer((req,res)=>{
    res.setHeader('content-type','application/json');
    if(req.url==='/__vydlora/connection')return res.end(JSON.stringify({base_url:'http://127.0.0.1:8000',token_set:true,managed:true}));
    if(req.url==='/api/ping') { pings++;return res.end(JSON.stringify({ok:pings>=2})); }
    res.statusCode=404;res.end('{}');
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');
  t.after(()=>{server.closeAllConnections();server.close();});
  const origin=localAppOrigin(server.address().port);
  assert.equal(await isVydloraRunning(origin),true);
  assert.equal(await waitForEngine(origin,2000),true);
  const child=spawn(process.execPath,['scripts/launch.mjs','--no-browser'],{cwd:path.resolve(import.meta.dirname,'..'),windowsHide:true,env:{...process.env,PORT:String(server.address().port)},stdio:['ignore','pipe','pipe']});
  let output='';child.stdout.on('data',chunk=>{output+=chunk;});child.stderr.on('data',chunk=>{output+=chunk;});
  const [exit]=await once(child,'exit');assert.equal(exit,0,output);assert.match(output,/already running/);
});

test('a random local web server or failed request is not treated as a running Vydlora instance', async () => {
  assert.equal(await isVydloraRunning('http://127.0.0.1:1234',async()=>Response.json({ok:true})),false);
  assert.equal(await isVydloraRunning('http://127.0.0.1:1234',async()=>{throw new Error('offline');}),false);
});

test('launcher starts a new local web app when nothing is running', async t => {
  const engine=http.createServer((req,res)=>{res.setHeader('content-type','application/json');res.end('{"ok":true,"app":"Lora 1"}');});
  engine.listen(0,'127.0.0.1');await once(engine,'listening');
  const reservation=http.createServer();reservation.listen(0,'127.0.0.1');await once(reservation,'listening');
  const port=reservation.address().port;await new Promise(resolve=>reservation.close(resolve));
  const origin=localAppOrigin(port);
  const child=spawn(process.execPath,['scripts/launch.mjs','--no-browser'],{cwd:path.resolve(import.meta.dirname,'..'),windowsHide:true,env:{...process.env,PORT:String(port),LORA1_MANAGE_ENGINE:'0',LORA1_ENGINE_URL:localAppOrigin(engine.address().port)},stdio:['ignore','pipe','pipe']});
  let output='';child.stdout.on('data',chunk=>{output+=chunk;});child.stderr.on('data',chunk=>{output+=chunk;});
  t.after(async()=>{if(child.exitCode===null){child.kill();await once(child,'exit');}engine.closeAllConnections();engine.close();});
  let ready=false;
  for(let i=0;i<80;i++) { if(await isVydloraRunning(origin)){ready=true;break;}if(child.exitCode!==null)break;await new Promise(resolve=>setTimeout(resolve,50)); }
  assert.equal(ready,true,output);
  assert.equal(await waitForEngine(origin,2000),true);
  assert.equal((await fetch(origin+'/')).status,200);
});
