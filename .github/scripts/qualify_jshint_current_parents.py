"""Owned hosted source/API comparison plus mandatory original SDK gates."""
import collections,concurrent.futures,datetime,hashlib,io,json,os,pathlib,re,subprocess,tarfile,tempfile,urllib.parse,urllib.request
assert os.environ.get('GITHUB_REPOSITORY')=='Robert-Velhorst/019-FAB'
owner=pathlib.Path.cwd();profile=os.environ['JSHINT_PROFILE'];assert profile in ['original-runtime','candidate-runtime','candidate-full-SDK']
out=owner/'jshint-parent-evidence';out.mkdir(exist_ok=True)
def save(name,data):(out/name).write_bytes((json.dumps(data,indent=2)+'\n').encode())
def snapshot():
    result={}
    for record in subprocess.check_output(['git','ls-files','-s','-z'],cwd=owner).split(b'\0'):
        if not record:continue
        left,name=record.split(b'\t');mode,blob,stage=left.decode().split();assert stage=='0'
        p=owner/name.decode();raw=os.readlink(p).encode() if mode=='120000' else p.read_bytes()
        assert hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()==blob
        result[name.decode()]={'mode':mode,'blob':blob,'sha256':hashlib.sha256(raw).hexdigest()}
    return {'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'tree':subprocess.check_output(['git','rev-parse','HEAD^{tree}'],text=True).strip(),'payloads':result}
before=snapshot();save('owner-source-before.json',before)
d=json.loads((owner/'.github/qualification/jshint-current-parents.json').read_bytes());assert d['commit']=='6d06f8f8fccc0b89a54bfe58af83a54bc1fae414'
sdk=pathlib.Path(tempfile.mkdtemp(prefix='owned-jshint-'))/'sdk';sdk.mkdir()
with urllib.request.urlopen('https://codeload.github.com/jshint/jshint/tar.gz/'+d['commit'],timeout=60) as stream:archive=stream.read(48*1024*1024+1)
assert len(archive)<=48*1024*1024;expected={r['path']:r for r in d['source_payloads']};actual={}
with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as tar:
    for member in tar.getmembers():
        assert member.isfile() or member.isdir()
        if not member.isfile():continue
        parts=pathlib.PurePosixPath(member.name).parts;assert len(parts)>1 and '..' not in parts
        name='/'.join(parts[1:]);assert name in expected and name not in actual
        raw=tar.extractfile(member).read();r=expected[name];assert hashlib.sha256(raw).hexdigest()==r['sha256'] and hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()==r['git_blob']
        p=sdk/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw);actual[name]=r
assert actual.keys()==expected.keys() and len(actual)==212
original_manifest=json.loads((sdk/'package.json').read_bytes());manifest=d['candidate_manifest'].copy()
assert manifest['scripts']==original_manifest['scripts']==d['original_scripts']
if profile!='candidate-full-SDK':manifest.pop('devDependencies')
(sdk/'package.json').write_bytes((json.dumps(manifest,indent=2)+'\n').encode())
if profile!='original-runtime':
    raw=d['candidate_cli'].encode();assert hashlib.sha256(raw).hexdigest()==d['candidate_cli_sha256'];(sdk/'src/cli.js').write_bytes(raw)
(out/'publisher-original-package-lock.json').write_bytes((sdk/'package-lock.json').read_bytes())
# Preserve the immutable publisher lock, then resolve the deliberately ported
# manifests normally. Reusing its 2020 selections leaves fixable transitive
# vulnerabilities despite current direct declarations.
(sdk/'package-lock.json').rename(sdk.parent/'publisher-original-package-lock.json')
initial={n:hashlib.sha256((sdk/n).read_bytes()).hexdigest() for n in expected if n!='package-lock.json'};save('SDK-source-before.json',initial)
commands=[]
def run(label,args,cwd=sdk):
    with (out/(label+'.log')).open('wb') as stream:r=subprocess.run(args,cwd=cwd,stdout=stream,stderr=subprocess.STDOUT)
    commands.append({'label':label,'argv':args,'exit_code':r.returncode});save('commands.json',commands);return r.returncode
metadata=run('normal-strict-metadata',['npm','install','--package-lock-only','--ignore-scripts','--strict-peer-deps'])
lockpath=sdk/'package-lock.json';rows=[];prereleases=[];comparison=[];installed=False
if metadata==0 and lockpath.is_file():
    lock=json.loads(lockpath.read_bytes())
    for name,r in lock['packages'].items():
        if not name:continue
        assert 'node_modules/' in name and not r.get('link') and r.get('version')
        rows.append({'path':name,'name':name.rsplit('node_modules/',1)[1],'version':r['version']})
    prereleases=[r for r in rows if re.search(r'^\d+\.\d+\.\d+-',r['version'])];save('prerelease-lock-records.json',prereleases)
    (out/'generated-package-lock.json').write_bytes(lockpath.read_bytes())
    if not prereleases:installed=run('normal-frozen-install',['npm','ci','--strict-peer-deps'])==0
if installed:
    run('complete-installed-graph',['npm','ls','--all','--json']);run('complete-unfiltered-audit',['npm','audit','--json'])
    run('owned-API-regressions',['node',str(owner/'.github/scripts/jshint_owned_api_cases.cjs')])
    if (sdk/'owned-api-case-results.json').exists():(out/'owned-api-case-results.json').write_bytes((sdk/'owned-api-case-results.json').read_bytes())
    if profile=='candidate-full-SDK':
        # Restore Git metadata for the unchanged publisher submodule command.
        initialized=run('owned-Git-init',['git','init'])==0
        fetched=initialized and run('owned-immutable-Git-fetch',['git','fetch','--depth=1','https://github.com/jshint/jshint.git',d['commit']])==0
        indexed=fetched and run('owned-original-index',['git','read-tree','FETCH_HEAD'])==0
        if indexed:run('owned-original-HEAD',['git','update-ref','HEAD','FETCH_HEAD'])
        run('original-immutable-submodule-command',['npm','run','fetch-test262'])
        for name in ['test','test-all','test-node','test-cli','test-regression','test-unit','test-browser','test-262','build','coverage']:
            run('original-'+name,['npm','test'] if name=='test' else ['npm','run',name])
selected=collections.defaultdict(set)
for r in rows:selected[r['name']].add(r['version'])
def public(name):
    try:
        url='https://registry.npmjs.org/'+urllib.parse.quote(name,safe='')
        with urllib.request.urlopen(url,timeout=45) as stream:raw=stream.read(48*1024*1024+1)
        assert len(raw)<=48*1024*1024;m=json.loads(raw);versions=[v for v,r in m['versions'].items() if re.fullmatch(r'\d+\.\d+\.\d+',v) and not r.get('deprecated')]
        highest=max(versions,key=lambda x:tuple(map(int,x.split('.')))) if versions else None
        return {'name':name,'selected':sorted(selected[name]),'highest_public_nonretired_stable':highest,'highest_stable_met':selected[name]=={highest},'query':'ok','metadata_sha256':hashlib.sha256(raw).hexdigest()}
    except Exception as e:return {'name':name,'query':'error','error':str(e),'highest_stable_met':False}
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:comparison=list(pool.map(public,sorted(selected)))
save('complete-public-graph-comparison.json',comparison)
final={n:hashlib.sha256((sdk/n).read_bytes()).hexdigest() for n in expected if n!='package-lock.json'};save('SDK-source-after.json',final);assert initial==final
protected={n:r for n,r in expected.items() if n not in ['package.json','src/cli.js','package-lock.json']}
assert len(protected)==209 and all(final[n]==r['sha256'] for n,r in protected.items());save('original-protected-payloads.json',protected)
after=snapshot();save('owner-source-after.json',after);assert before==after
retired=[r['name'] for r in d['current_declarations'] if r['entire_stable_namespace_retired']]
summary={'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'profile':profile,'source_payloads_verified':212,'original_test_payloads_unchanged':156,'original_cases_helpers_assertions_fixtures_commands_changed':False,'original_lock_retained_separately_and_normal_generated_lock_reported':True,'metadata_exit':metadata,'lock_present':lockpath.is_file(),'installed':installed,'prereleases':prereleases,'retired_SDK_namespaces':retired,'public_namespaces':len(comparison),'public_version_gaps':sum(not r['highest_stable_met'] for r in comparison),'metadata_query_errors':sum(r['query']!='ok' for r in comparison),'commands':commands,'mutable_external_website_integration_source_review_still_required':profile=='candidate-full-SDK','whole_original_SDK_or_application_qualified':False,'application_parent_adoption_merge_deployment_data_change':False}
save('qualification-summary.json',summary)
assert installed and not prereleases and all(c['exit_code']==0 for c in commands),'Original SDK, API, strict metadata or raw audit failed; no suppressions.'
assert all(r['highest_stable_met'] for r in comparison),'Full selected graph still has current-version gaps.'
if profile=='candidate-full-SDK':
    assert not retired,'Retired original SDK namespaces remain a fatal whole-SDK gate.'
    raise AssertionError('Mutable external website integration source review remains required; whole-SDK qualification is not established.')
