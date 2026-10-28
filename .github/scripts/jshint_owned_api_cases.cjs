'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const cwd=process.cwd();
fs.writeFileSync('owned-valid.js','var answer = 42;\n');
fs.writeFileSync('owned-invalid.js','var = ;\n');
fs.writeFileSync('owned-browser.js','window.alert("hello");\n');
fs.writeFileSync('owned-config.json','{ // original JSON comment semantics\n "esversion": 11, "maxerr": 23\n}\n');
const cases=[
 ['core-valid',`assert.equal(require('./src/jshint').JSHINT('var answer = 42;',{esversion:11}),true);`],
 ['core-invalid',`const h=require('./src/jshint').JSHINT;assert.equal(h('var = ;'),false);assert.ok(h.errors.length>0);`],
 ['HTML-JavaScript-extraction',`assert.equal(require('./src/cli').extract('<script>var answer = 42;</script>','always').trim(),'var answer = 42;');`],
 ['HTML-nonJavaScript-excluded',`assert.equal(require('./src/cli').extract('<script type="application/json">{}</script>','always').trim(),'');`],
 ['commented-JSON-configuration',`const c=require('./src/cli').loadConfig(require('node:path').resolve('owned-config.json'));assert.equal(c.maxerr,23);assert.equal(c.esversion,11);`],
 ['glob-ignore-invalid-file',`const c=require('./src/cli');const p=require('node:path').resolve('owned-invalid.js');let messages;assert.equal(c.run({args:[p],ignores:[p],config:{esversion:11},reporter:r=>{messages=r;}}),true);assert.deepEqual(messages,[]);`],
 ['glob-configuration-override',`const c=require('./src/cli');let messages;assert.equal(c.run({args:['owned-browser.js'],ignores:[],config:{esversion:11,undef:true,overrides:{'**/owned-browser.js':{browser:true}}},reporter:r=>{messages=r;}}),true);assert.deepEqual(messages,[]);`],
 ['CLI-rejects-invalid-JavaScript',`let messages;assert.equal(require('./src/cli').run({args:['owned-invalid.js'],ignores:[],config:{esversion:11},reporter:r=>{messages=r;}}),false);assert.ok(messages.length>0);`],
];
const results=cases.map(([name,body],position)=>{
 const caseFile=path.join(cwd,`owned-api-case-${position}.cjs`);
 fs.writeFileSync(caseFile,`'use strict';\nconst assert=require('node:assert/strict');${body}\n`);
 const r=spawnSync(process.execPath,[caseFile],{cwd,encoding:'utf8'});
 return {name,pass:r.status===0,status:r.status,signal:r.signal,stdout:r.stdout,stderr:r.stderr,error:r.error?String(r.error):null};
});
fs.writeFileSync('owned-api-case-results.json',JSON.stringify({node:process.version,cases:results,passed:results.filter(x=>x.pass).length,failed:results.filter(x=>!x.pass).length,skipped:0,original_SDK_cases_replaced:false},null,2)+'\n');
process.exitCode=results.some(x=>!x.pass)?1:0;
