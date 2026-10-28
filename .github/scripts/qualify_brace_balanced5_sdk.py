"""Owned hosted complete original Brace Expansion SDK comparison."""
import collections,concurrent.futures,datetime,hashlib,io,json,os,pathlib,re,subprocess,tarfile,tempfile,urllib.parse,urllib.request
assert os.environ.get('GITHUB_REPOSITORY')=='Robert-Velhorst/019-FAB'
owner=pathlib.Path.cwd();profile=os.environ['BRACE_PROFILE'];assert profile in ['original-balanced4','candidate-balanced5']
out=owner/'brace-balanced5-evidence';out.mkdir(exist_ok=True)
def save(name,data):(out/name).write_bytes((json.dumps(data,indent=2)+'\n').encode())
def owner_snapshot():
    result={}
    for record in subprocess.check_output(['git','ls-files','-s','-z'],cwd=owner).split(b'\0'):
        if not record:continue
        left,name=record.split(b'\t');mode,sha,stage=left.decode().split();assert stage=='0'
        p=owner/name.decode();raw=os.readlink(p).encode() if mode=='120000' else p.read_bytes();assert hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()==sha
        result[name.decode()]={'mode':mode,'blob':sha,'sha256':hashlib.sha256(raw).hexdigest()}
    return {'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'tree':subprocess.check_output(['git','rev-parse','HEAD^{tree}'],text=True).strip(),'payloads':result}
before=owner_snapshot();save('owner-source-before.json',before)
d=json.loads((owner/'.github/qualification/brace-balanced5-original-sdk.json').read_bytes());assert d['commit']=='f3410159d768f56c9d9f4511d3e1b46425fc1099'
sdk=pathlib.Path(tempfile.mkdtemp(prefix='owned-brace-balanced5-'))/'sdk';sdk.mkdir()
with urllib.request.urlopen('https://codeload.github.com/juliangruber/brace-expansion/tar.gz/'+d['commit'],timeout=60) as stream:archive=stream.read(16*1024*1024+1)
assert len(archive)<=16*1024*1024;expected={r['path']:r for r in d['source_payloads']};actual={}
with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as tar:
    for member in tar.getmembers():
        assert member.isdir() or member.isfile()
        if not member.isfile():continue
        parts=pathlib.PurePosixPath(member.name).parts;assert len(parts)>1 and '..' not in parts
        name='/'.join(parts[1:]);assert name in expected and name not in actual
        raw=tar.extractfile(member).read();r=expected[name];assert hashlib.sha256(raw).hexdigest()==r['sha256'] and hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()==r['git_blob']
        p=sdk/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw);actual[name]=r
assert actual.keys()==expected.keys() and len(actual)==19
original_manifest=json.loads((sdk/'package.json').read_bytes());manifest=json.loads(json.dumps(d['candidate_manifest']))
assert original_manifest['scripts']==manifest['scripts']==d['original_scripts']
if profile=='original-balanced4':manifest['dependencies']['balanced-match']='4.0.4'
(sdk/'package.json').write_bytes((json.dumps(manifest,indent=2)+'\n').encode())
rawlock=(sdk/'package-lock.json').read_bytes();save('publisher-source-input.json',{'profile':profile,'source_payloads':actual,'archive_sha256':hashlib.sha256(archive).hexdigest(),'original_scripts':original_manifest['scripts'],'candidate_manifest':manifest,'actual_node':subprocess.check_output(['node','--version'],text=True).strip(),'actual_npm':subprocess.check_output(['npm','--version'],text=True).strip()})
(out/'publisher-original-package-lock.json').write_bytes(rawlock);(sdk/'package-lock.json').rename(sdk.parent/'publisher-original-package-lock.json')
sdk_before={n:hashlib.sha256((sdk/n).read_bytes()).hexdigest() for n in expected if n!='package-lock.json'};save('SDK-source-before.json',sdk_before)
commands=[]
def run(label,args):
    with (out/(label+'.log')).open('wb') as stream:r=subprocess.run(args,cwd=sdk,stdout=stream,stderr=subprocess.STDOUT)
    commands.append({'label':label,'argv':args,'exit_code':r.returncode});save('commands.json',commands);return r.returncode
metadata=run('normal-strict-metadata',['npm','install','--package-lock-only','--ignore-scripts','--strict-peer-deps']);rows=[];prereleases=[];comparison=[];installed=False;case_exit=None;pack_records=[]
if metadata==0 and (sdk/'package-lock.json').is_file():
    lock=json.loads((sdk/'package-lock.json').read_bytes());(out/'generated-package-lock.json').write_bytes((sdk/'package-lock.json').read_bytes())
    for name,r in lock['packages'].items():
        if not name:continue
        assert 'node_modules/' in name and not r.get('link') and r.get('version')
        rows.append({'path':name,'name':name.rsplit('node_modules/',1)[1],'version':r['version']})
    prereleases=[r for r in rows if re.search(r'^\d+\.\d+\.\d+-',r['version'])];save('prerelease-lock-records.json',prereleases)
    if not prereleases:installed=run('normal-frozen-install-and-original-prepare',['npm','ci','--strict-peer-deps'])==0
if installed:
    case_exit=run('original-complete-test-command',['npm','test'])
    run('original-prepare-command',['npm','run','prepare'])
    run('complete-unfiltered-audit',['npm','audit','--json']);run('complete-installed-graph',['npm','ls','--all','--json'])
    if run('owned-pack-built-original-SDK',['npm','pack','--json','--pack-destination',str(out)])==0:
        filename=out/('brace-expansion-'+manifest['version']+'.tgz');raw=filename.read_bytes();seen=set()
        with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tar:
            for member in tar.getmembers():
                if not member.isfile():continue
                parts=pathlib.PurePosixPath(member.name).parts;assert parts[0]=='package' and '..' not in parts;name='/'.join(parts[1:]);assert name not in seen;seen.add(name)
                payload=tar.extractfile(member).read();assert payload==(sdk/name).read_bytes();pack_records.append({'path':name,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()})
        save('private-pack-source-reconciliation.json',{'filename':filename.name,'sha256':hashlib.sha256(raw).hexdigest(),'all_payloads_match_built_SDK':True,'payloads':pack_records,'external_publication':False})
selected=collections.defaultdict(set)
for r in rows:selected[r['name']].add(r['version'])
def public(name):
    try:
        with urllib.request.urlopen('https://registry.npmjs.org/'+urllib.parse.quote(name,safe=''),timeout=45) as stream:raw=stream.read(48*1024*1024+1)
        assert len(raw)<=48*1024*1024;m=json.loads(raw);versions=[v for v,r in m['versions'].items() if re.fullmatch(r'\d+\.\d+\.\d+',v) and not r.get('deprecated')]
        highest=max(versions,key=lambda x:tuple(map(int,x.split('.')))) if versions else None
        return {'name':name,'selected':sorted(selected[name]),'highest_public_nonretired_stable':highest,'highest_stable_met':selected[name]=={highest},'query':'ok','metadata_sha256':hashlib.sha256(raw).hexdigest()}
    except Exception as e:return {'name':name,'selected':sorted(selected[name]),'query':'error','error':str(e),'highest_stable_met':False}
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:comparison=list(pool.map(public,sorted(selected)))
save('complete-public-graph-comparison.json',comparison)
sdk_after={n:hashlib.sha256((sdk/n).read_bytes()).hexdigest() for n in expected if n!='package-lock.json'};save('SDK-source-after.json',sdk_after)
changes=[{'path':n,'before':sdk_before[n],'after':sdk_after[n]} for n in sdk_before if sdk_before[n]!=sdk_after[n]];save('original-source-build-side-effects.json',changes)
protected={n:r for n,r in expected.items() if n!='package.json' and n!='package-lock.json'}
cases={n:r for n,r in protected.items() if n.startswith('test/')};assert len(cases)==4
case_payloads_unchanged=all(sdk_after[n]==r['sha256'] for n,r in cases.items());save('original-case-helper-fixture-payloads-after.json',cases)
after=owner_snapshot();save('owner-source-after.json',after);assert before==after
summary={'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'profile':profile,'source_payloads_verified':19,'original_case_helper_fixture_payloads':4,'original_case_helper_fixture_payloads_unchanged':case_payloads_unchanged,'original_scripts_unchanged':json.loads((sdk/'package.json').read_bytes())['scripts']==d['original_scripts'],'metadata_exit':metadata,'installed':installed,'original_npm_test_exit':case_exit,'prereleases':prereleases,'public_namespaces':len(comparison),'public_version_gaps':sum(not r['highest_stable_met'] for r in comparison),'public_metadata_query_errors':sum(r['query']!='ok' for r in comparison),'source_build_side_effects':changes,'private_pack_payloads':len(pack_records),'commands':commands,'whole_original_SDK_qualified':False,'actual_parent_or_application_adoption':False}
save('qualification-summary.json',summary)
assert case_payloads_unchanged and not changes,'Original source/build helper mutations remain a required gate.'
assert installed and not prereleases and all(r['exit_code']==0 for r in commands),'Complete original commands or raw audit failed; no exclusions.'
assert all(r['highest_stable_met'] for r in comparison),'Complete original SDK current-version gaps remain a fatal qualification gate.'
