"""Owned hosted qualification. Original SDK cases and commands remain unchanged."""
import base64,collections,concurrent.futures,datetime,hashlib,io,json,os,pathlib,re,subprocess,tarfile,urllib.parse,urllib.request
assert os.environ.get('GITHUB_REPOSITORY')=='Robert-Velhorst/019-FAB'
owner=pathlib.Path.cwd();variant=os.environ['DEEPMERGE_VARIANT'];assert variant in ['original','candidate']
descriptor=json.loads((owner/'.github/qualification/deepmerge-own-properties.json').read_bytes())
output=owner/'deepmerge-evidence';output.mkdir(exist_ok=True)
sdk=output/'sdk';sdk.mkdir(exist_ok=True)
def save(name,data):
    (output/name).write_bytes((json.dumps(data,indent=2)+'\n').encode('utf-8'))
def fetch(url,limit):
    with urllib.request.urlopen(url,timeout=60) as response:data=response.read(limit+1)
    assert len(data)<=limit
    return data
def source_snapshot():
    entries={}
    for raw in subprocess.check_output(['git','ls-files','-s','-z'],cwd=owner).split(b'\0'):
        if not raw:continue
        left,name=raw.split(b'\t');mode,blob,stage=left.decode().split();assert stage=='0'
        path=owner/name.decode();payload=os.readlink(path).encode() if mode=='120000' else path.read_bytes()
        assert hashlib.sha1(b'blob '+str(len(payload)).encode()+b'\0'+payload).hexdigest()==blob
        entries[name.decode()]={'mode':mode,'blob':blob,'sha256':hashlib.sha256(payload).hexdigest()}
    return {'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=owner,text=True).strip(),'tree':subprocess.check_output(['git','rev-parse','HEAD^{tree}'],cwd=owner,text=True).strip(),'payloads':entries}
before=source_snapshot();save('owner-source-before.json',before)
url=f'https://codeload.github.com/{descriptor["publisher_repository"]}/tar.gz/{descriptor["publisher_commit"]}'
archive=fetch(url,8*1024*1024);expected={r['path']:r for r in descriptor['publisher_payloads']};actual={}
with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as stream:
    for member in stream.getmembers():
        assert member.isfile() or member.isdir()
        if not member.isfile():continue
        parts=pathlib.PurePosixPath(member.name).parts;assert len(parts)>1 and '..' not in parts
        name='/'.join(parts[1:]);assert name in expected and name not in actual
        payload=stream.extractfile(member).read();row=expected[name]
        assert len(payload)==row['bytes'] and hashlib.sha256(payload).hexdigest()==row['sha256']
        assert hashlib.sha1(b'blob '+str(len(payload)).encode()+b'\0'+payload).hexdigest()==row['git_blob']
        target=sdk/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload);actual[name]=row
assert set(actual)==set(expected) and len(actual)==18
original_manifest=json.loads((sdk/'package.json').read_bytes())
assert original_manifest['scripts']==descriptor['original_scripts']==descriptor['manifest']['scripts']
(sdk/'package.json').write_bytes((json.dumps(descriptor['manifest'],indent=2)+'\n').encode())
(sdk/'rollup.config.js').write_bytes(descriptor['rollup_config'].encode())
if variant=='candidate':
    candidate=descriptor['candidate_index'].encode();assert hashlib.sha256(candidate).hexdigest()==descriptor['candidate_index_sha256']
    (sdk/'index.js').write_bytes(candidate)
else:assert hashlib.sha256((sdk/'index.js').read_bytes()).hexdigest()==descriptor['original_index_sha256']
sdk_before={name:hashlib.sha256((sdk/name).read_bytes()).hexdigest() for name in expected if name!='package-lock.json'}
save('SDK-source-before.json',sdk_before)
parent_sources={}
for parent in descriptor.get('verified_source_parents',[]):
    parent_root=sdk/'vendor'/parent['name'];parent_root.mkdir(parents=True,exist_ok=True)
    parent_expected={r['path']:r for r in parent['source_payloads']};parent_actual={}
    parent_url=f'https://codeload.github.com/{parent["repository"]}/tar.gz/{parent["commit"]}'
    parent_archive=fetch(parent_url,8*1024*1024)
    with tarfile.open(fileobj=io.BytesIO(parent_archive),mode='r:gz') as stream:
        for member in stream.getmembers():
            assert member.isfile() or member.isdir()
            if not member.isfile():continue
            parts=pathlib.PurePosixPath(member.name).parts;assert len(parts)>1 and '..' not in parts
            name='/'.join(parts[1:]);assert name in parent_expected and name not in parent_actual
            payload=stream.extractfile(member).read();row=parent_expected[name]
            assert len(payload)==row['bytes'] and hashlib.sha1(b'blob '+str(len(payload)).encode()+b'\0'+payload).hexdigest()==row['git_blob']
            target=parent_root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload)
            target.chmod(0o755 if row['mode']=='100755' else 0o644)
            parent_actual[name]={'git_blob':row['git_blob'],'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)}
    assert set(parent_actual)==set(parent_expected)
    assert json.loads((parent_root/'package.json').read_bytes())==parent['original_manifest']
    (parent_root/'package.json').write_bytes((json.dumps(parent['ported_manifest'],indent=2)+'\n').encode())
    parent_sources[parent['name']]={'repository':parent['repository'],'commit':parent['commit'],'source_archive_sha256':hashlib.sha256(parent_archive).hexdigest(),'original_payloads':parent_actual,'ported_manifest':parent['ported_manifest'],'source_payload_hashes':{n:hashlib.sha256((parent_root/n).read_bytes()).hexdigest() for n in parent_expected}}
save('verified-source-parents-before.json',parent_sources)
save('input.json',{'variant':variant,'source_archive_url':url,'source_archive_sha256':hashlib.sha256(archive).hexdigest(),'publisher_payloads_verified':actual,'original_scripts':original_manifest['scripts'],'node':subprocess.check_output(['node','--version'],text=True).strip(),'npm':subprocess.check_output(['npm','--version'],text=True).strip(),'ported_manifest':descriptor['manifest'],'production_paths_changed':descriptor['production_changes']})
commands=[]
def run(label,argv):
    with (output/(label+'.log')).open('wb') as stream:
        result=subprocess.run(argv,cwd=sdk,stdout=stream,stderr=subprocess.STDOUT)
    commands.append({'label':label,'argv':argv,'exit_code':result.returncode})
    save('commands.json',commands)
    return result.returncode
assert subprocess.check_output(['npm','config','get','engine-strict'],text=True).strip()=='true'
assert subprocess.check_output(['npm','config','get','strict-peer-deps'],text=True).strip()=='true'
install=run('normal-install',['npm','install'])
if install==0:
    run('owned-regressions',['node',str(owner/'.github/scripts/deepmerge_owned_regressions.cjs')])
    (output/'owned-regression-results.json').write_bytes((sdk/'owned-regression-results.json').read_bytes())
    run('original-complete-test-command',['npm','test'])
    run('original-declaration-command',['npm','run','test:typescript'])
    run('original-build-command',['npm','run','build'])
    run('original-size-command',['npm','run','size'])
    run('complete-unfiltered-audit',['npm','audit','--json'])
    lock=json.loads((sdk/'package-lock.json').read_bytes())
    prereleases=[{'path':name,'name':row.get('name'),'version':row.get('version')} for name,row in lock['packages'].items() if row.get('version') and re.search(r'^\d+\.\d+\.\d+-',row['version'])]
    save('installed-prerelease-lock-rows.json',prereleases)
    run('actual-installed-graph',['npm','ls','--all','--json'])
    selected=collections.defaultdict(set)
    source_graph=[]
    for name,row in lock['packages'].items():
        if not name:continue
        if row.get('link'):
            assert row['resolved'] in ['vendor/jsmd','vendor/v8-argv'];source_graph.append({'path':name,**row});continue
        if name in ['vendor/jsmd','vendor/v8-argv']:
            parent=next(p for p in descriptor['verified_source_parents'] if name=='vendor/'+p['name'])
            assert row['version']==parent['ported_manifest']['version'];source_graph.append({'path':name,**row});continue
        assert 'node_modules/' in name and row.get('version'),name
        selected[name.rsplit('node_modules/',1)[1]].add(row['version'])
    def public(name):
        url='https://registry.npmjs.org/'+urllib.parse.quote(name,safe='')
        try:
            raw=fetch(url,64*1024*1024);data=json.loads(raw)
            stable=[v for v,row in data['versions'].items() if re.fullmatch(r'\d+\.\d+\.\d+',v) and not row.get('deprecated')]
            highest=max(stable,key=lambda v:tuple(map(int,v.split('.')))) if stable else None
            return {'name':name,'source':url,'selected':sorted(selected[name]),'highest_public_non_deprecated_stable':highest,'highest_stable_met':highest is not None and selected[name]=={highest},'response_sha256':hashlib.sha256(raw).hexdigest(),'query':'ok'}
        except Exception as error:return {'name':name,'selected':sorted(selected[name]),'query':'error','error':str(error),'highest_stable_met':False}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:public_rows=list(pool.map(public,sorted(selected)))
    save('verified-source-parent-installed-lock-records.json',source_graph)
    save('complete-public-graph-comparison.json',{'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'namespace_count':len(public_rows),'query_errors':sum(r['query']!='ok' for r in public_rows),'gap_count':sum(not r['highest_stable_met'] for r in public_rows),'packages':public_rows})
    (output/'generated-package-lock.json').write_bytes((sdk/'package-lock.json').read_bytes())
    save('normal-install-manifest.json',json.loads((sdk/'package.json').read_bytes()))
unchanged={}
for name,row in expected.items():
    if name in ['index.js','package.json','rollup.config.js','package-lock.json']:continue
    assert hashlib.sha256((sdk/name).read_bytes()).hexdigest()==row['sha256'],name
    unchanged[name]=row
save('original-protected-source-after.json',unchanged)
assert json.loads((sdk/'package.json').read_bytes())['scripts']==descriptor['original_scripts']
sdk_after={name:hashlib.sha256((sdk/name).read_bytes()).hexdigest() for name in expected if name!='package-lock.json'}
save('SDK-source-after.json',sdk_after);assert sdk_before==sdk_after
parent_after={}
for parent in descriptor.get('verified_source_parents',[]):
    parent_root=sdk/'vendor'/parent['name']
    parent_after[parent['name']]={r['path']:hashlib.sha256((parent_root/r['path']).read_bytes()).hexdigest() for r in parent['source_payloads']}
    assert parent_after[parent['name']]==parent_sources[parent['name']]['source_payload_hashes']
save('verified-source-parents-after.json',parent_after)
after=source_snapshot();save('owner-source-after.json',after);assert before==after
save('terminal.json',{'variant':variant,'commands':commands,'all_original_case_helper_assertion_fixture_selector_timeout_command_bytes_unchanged':True,'all_owner_source_unchanged':True,'no_application_parent_adoption':True,'complete_qualification':install==0 and all(r['exit_code']==0 for r in commands) and not prereleases and all(r['highest_stable_met'] for r in public_rows)})
assert install==0 and all(r['exit_code']==0 for r in commands),'Original complete commands, owned security regressions, or raw audit failed; all failures retained.'
assert not prereleases,'Prerelease rows remain; no exclusions.'
assert all(r['highest_stable_met'] for r in public_rows),'Complete public graph has version gaps or query errors; all rows retained.'
