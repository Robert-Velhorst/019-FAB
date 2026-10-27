"""Owned hosted-worker installer; normal dependency scripts run without network access."""
import errno,hashlib,json,os,pathlib,re,socket,subprocess,sys
sdk=pathlib.Path.cwd();repo=pathlib.Path(subprocess.check_output(['git','rev-parse','--show-toplevel'],text=True).strip())
assert os.environ['GITHUB_ACTIONS']=='true' and os.environ['GITHUB_REPOSITORY']=='Robert-Velhorst/019-FAB'
assert os.environ['RUNNER_ENVIRONMENT']=='github-hosted' and sys.platform=='linux'
output=sdk/'FAB_NATIVE_INSTALL_EVIDENCE';output.mkdir(exist_ok=True)
def write(name,value):(output/name).write_text(json.dumps(value,indent=2)+'\n')
def digest(data):return hashlib.sha256(data).hexdigest()
def snapshot():
    rows={}
    for row in subprocess.check_output(['git','ls-tree','-rz','HEAD'],cwd=repo).split(b'\0'):
        if not row:continue
        left,name=row.split(b'\t');mode,kind,blob=left.decode().split();assert kind=='blob'
        name=name.decode();p=repo/name;data=os.readlink(p).encode() if mode=='120000' else p.read_bytes()
        assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==blob,name
        rows[name]={'mode':mode,'blob':blob,'sha256':digest(data)}
    return {'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),'tree':subprocess.check_output(['git','rev-parse','HEAD^{tree}'],cwd=repo,text=True).strip(),'payloads':rows}
status=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
namespace=os.readlink('/proc/self/ns/net');host_namespace=os.environ['FAB_HOST_NETWORK_NAMESPACE']
assert namespace!=host_namespace and os.getuid()==int(os.environ['FAB_EXPECTED_UID'])!=0 and os.getgid()==int(os.environ['FAB_EXPECTED_GID'])
assert os.getgroups()==[] and status['NoNewPrivs'].strip()=='1'
assert all(int(status[name].strip(),16)==0 for name in ['CapInh','CapPrm','CapEff','CapBnd','CapAmb'])
interfaces=[line.split(':',1)[0].strip() for line in pathlib.Path('/proc/net/dev').read_text().splitlines() if ':' in line];assert interfaces==['lo']
try:
    with socket.create_connection(('1.1.1.1',443),timeout=3):raise AssertionError('Installer unexpectedly has external network access.')
except OSError as error:
    assert error.errno in {errno.ENETUNREACH,errno.EHOSTUNREACH,errno.EACCES,errno.EPERM},str(error)
    network_error={'errno':error.errno,'name':errno.errorcode[error.errno]}
assert os.environ.get('npm_config_build_from_source') is None
expected={'engineStrict':True,'strictPeerDependencies':True,'minimumReleaseAge':1440,'minimumReleaseAgeStrict':True,'strictDepBuilds':True,'verifyStoreIntegrity':True,'dangerouslyAllowAllBuilds':False,'sideEffectsCache':False,'allowBuilds':{'@parcel/watcher@2.6.0':True,'unrs-resolver@1.12.2':True}}
config={}
for key,value in expected.items():
    raw=subprocess.check_output(['pnpm','config','get',key,'--json'],text=True).strip();config[key]=json.loads(raw);assert config[key]==value,(key,config[key])
raw=subprocess.check_output(['pnpm','config','get','permissions','--json'],text=True).strip();assert raw in {'','undefined','null','{}'}
before=snapshot();write('source-before.json',before)
lock=sdk/'pnpm-lock.yaml';lock_before=digest(lock.read_bytes())
receipt={'source_head':before['head'],'source_tree':before['tree'],'uid':os.getuid(),'gid':os.getgid(),'groups':os.getgroups(),'namespace':namespace,'host_namespace':host_namespace,'interfaces':interfaces,'NoNewPrivs':True,'zero_effective_inherited_permitted_bounding_ambient_capabilities':True,'negative_external_network_probe':network_error,'effective_policy':config,'lock_before_sha256':lock_before,'install_accepted':False,'normal_lock_resolve_and_fetch_precede_this_phase':True,'original_publisher_case_or_build_acceptance':False}
write('install.json',receipt)
try:
    command=['pnpm','install','--offline','--frozen-lockfile','--strict-peer-dependencies']
    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    (output/'install.log').write_bytes(result.stdout);receipt.update(command=command,exit_code=result.returncode)
    print(result.stdout.decode(errors='replace')[-18000:])
    assert result.returncode==0,'Strict offline installation failed; full log retained.'
    assert digest(lock.read_bytes())==lock_before
    probe=r'''
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { createRequire } = require('node:module');
assert.equal(process.versions.node, '26.12.1');
assert.equal(process.platform, 'linux'); assert.equal(process.arch, 'x64');
const store = path.resolve('node_modules/.pnpm');
const packages = [];
for (const [name, version, prefix] of [['@parcel/watcher','2.6.0','@parcel+watcher@2.6.0'], ['unrs-resolver','1.12.2','unrs-resolver@1.12.2']]) {
  const candidates=fs.readdirSync(store).filter(n=>n===prefix || n.startsWith(prefix+'(') || n.startsWith(prefix+'_'));
  assert.equal(candidates.length,1,name);
  const manifestPath=path.join(store,candidates[0],'node_modules',name,'package.json');
  const manifest=JSON.parse(fs.readFileSync(manifestPath,'utf8'));
  assert.equal(manifest.name,name); assert.equal(manifest.version,version);
  createRequire(manifestPath)(name);
  packages.push({name,version,manifestPath,manifestSha256:crypto.createHash('sha256').update(fs.readFileSync(manifestPath)).digest('hex')});
}
const nativePaths=[...new Set(fs.readFileSync('/proc/self/maps','utf8').split('\n').map(l=>l.match(/\s(\/[^\n]+\.node)$/)?.[1]).filter(Boolean))];
assert.ok(nativePaths.some(n=>n.includes('watcher') && n.endsWith('.node')));
assert.ok(nativePaths.some(n=>n.includes('resolver') && n.endsWith('.node')));
const native=nativePaths.map(filename=>{const manifestPath=path.join(path.dirname(filename),'package.json');const manifest=JSON.parse(fs.readFileSync(manifestPath,'utf8'));return {filename,name:manifest.name,version:manifest.version,sha256:crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex')};});
console.log(JSON.stringify({actualNode:process.versions.node,packages,native}));
'''
    native=json.loads(subprocess.check_output(['node','-e',probe],text=True));write('actual-native-load.json',native)
    inventory=[]
    for directory in sorted((sdk/'node_modules/.pnpm').iterdir()):
        modules=directory/'node_modules'
        if not modules.is_dir():continue
        for candidate in modules.iterdir():
            children=list(candidate.iterdir()) if candidate.name.startswith('@') and candidate.is_dir() else [candidate]
            for item in children:
                manifest=item/'package.json'
                if not manifest.is_file():continue
                data=json.loads(manifest.read_text());inventory.append({'name':data['name'],'version':data['version'],'path':str(item.relative_to(sdk))})
    write('installed-inventory.json',inventory)
    prerelease=[r for r in inventory if re.match(r'^\d+\.\d+\.\d+-',r['version'])];write('installed-prereleases.json',prerelease)
    receipt.update(install_accepted=True,actual_native_loaded=True,installed_inventory_rows=len(inventory),installed_prerelease_rows=len(prerelease),lock_after_sha256=digest(lock.read_bytes()))
finally:
    after=snapshot();write('source-after.json',after);assert after==before
    receipt['all_tracked_source_payloads_unchanged']=len(before['payloads']);write('install.json',receipt)
