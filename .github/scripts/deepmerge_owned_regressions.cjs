'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const merge = require(path.join(process.cwd(), 'index.js'));
const records = [];
const marker = 'owned-deepmerge-prototype-canary';
function ownProto() {
  const value = { ordinary: 1 };
  Object.defineProperty(value, '__proto__', { value: { canary: marker }, enumerable: true, writable: true, configurable: true });
  return value;
}
function nullProto() {
  const value = Object.create(null);
  value.__proto__ = { canary: marker };
  value.ordinary = 1;
  return value;
}
function safe(value, requireOwnProperty = true) {
  assert.equal(Object.getPrototypeOf(value), Object.prototype);
  assert.equal(value.canary, undefined);
  assert.equal(Object.prototype.canary, undefined);
  const descriptor = Object.getOwnPropertyDescriptor(value, '__proto__');
  if (!descriptor) { assert.equal(requireOwnProperty, false); return; }
  assert.deepEqual({writable:descriptor.writable,enumerable:descriptor.enumerable,configurable:descriptor.configurable}, {writable:true,enumerable:true,configurable:true});
  assert.equal(descriptor.value.canary, marker);
}
function test(name, category, callback) {
  try { callback(); records.push({ name, category, pass: true }); }
  catch(error) { records.push({ name, category, pass: false, error: error.message }); }
}
for (const [name, create] of [['own-enumerable-target',ownProto],['null-prototype-target',nullProto]]) {
  test(name+'-copy','prototype',()=>safe(merge(create(), {})));
  test(name+'-nested','prototype',()=>safe(merge({nested:create()}, {nested:{extra:2}}).nested));
  test(name+'-array','positive',()=>safe(merge({items:[create()]},{items:[]}).items[0],false));
  test(name+'-merge-all','positive',()=>safe(merge.all([create(),{extra:2}]),false));
}
test('null-prototype-source','prototype',()=>safe(merge(Object.create(null),nullProto())));
test('source-and-target-own-proto','prototype',()=>safe(merge(ownProto(),ownProto())));
test('ordinary-recursive-merge','positive',()=>assert.deepEqual(merge({a:{b:1}},{a:{c:2}}),{a:{b:1,c:2}}));
test('array-concatenation','positive',()=>assert.deepEqual(merge({a:[1]},{a:[2]}),{a:[1,2]}));
test('symbol-own-data-property','positive',()=>{const key=Symbol('owned');const result=merge({[key]:1},{[key]:2});assert.equal(result[key],2);assert.deepEqual(Object.getOwnPropertyDescriptor(result,key),{value:2,writable:true,enumerable:true,configurable:true});});
test('custom-merge-hook','positive',()=>assert.deepEqual(merge({name:{value:'first'}},{name:{value:'second'}},{customMerge:key=>key==='name'?(a,b)=>({value:a.value+'/'+b.value}):undefined}),{name:{value:'first/second'}}));
test('clone-false-reference','positive',()=>{const child={x:1};assert.equal(merge({},{child},{clone:false}).child,child);});
test('existing-plain-target-prototype-policy','positive',()=>{const result=merge({},JSON.parse('{"__proto__":{"canary":"owned-deepmerge-prototype-canary"}}'));assert.equal(Object.hasOwn(result,'__proto__'),false);assert.equal(Object.getPrototypeOf(result),Object.prototype);assert.equal(result.canary,undefined);});
test('inputs-preserved','positive',()=>{const a={x:{v:1}},b={x:{w:2}};merge(a,b);assert.deepEqual(a,{x:{v:1}});assert.deepEqual(b,{x:{w:2}});});
test('function-custom-array-merge','positive',()=>assert.deepEqual(merge({x:[1]},{x:[2]},{arrayMerge:(a,b)=>b}),{x:[2]}));
fs.writeFileSync('owned-regression-results.json',JSON.stringify({cases:records,passed:records.filter(r=>r.pass).length,failed:records.filter(r=>!r.pass).length},null,2)+'\n');
process.exitCode = records.some(r=>!r.pass) ? 1 : 0;
