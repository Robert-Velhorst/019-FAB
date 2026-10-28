"""Owned hosted compiler/build proof and mandatory complete original SDK gate."""
import collections,concurrent.futures,datetime,hashlib,io,json,os,pathlib,re,subprocess,tarfile,tempfile,urllib.parse,urllib.request
assert os.environ.get('GITHUB_REPOSITORY')=='Robert-Velhorst/019-FAB'
owner=pathlib.Path.cwd();profile=os.environ['TSHY_PROFILE'];assert profile in ['native-builder-runtime','complete-original-SDK']
out=owner/'tshy-stable-native-evidence';out.mkdir(exist_ok=True)
def save(name,value):(out/name).write_bytes((json.dumps(value,indent=2)+'\n').encode())
def snapshot():
    records={}
    for item in subprocess.check_output(['git','ls-files','-s','-z'],cwd=owner).split(b'\0'):
        if not item:continue
        left,name=item.split(b'\t');mode,sha,stage=left.decode().split();assert stage=='0'
        p=owner/name.decode();raw=os.readlink(p).encode() if mode=='120000' else p.read_bytes();assert hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()==sha
        records[name.decode()]={'mode':mode,'blob':sha,'sha256':hashlib.sha256(raw).hexdigest()}
    return {'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'tree':subprocess.check_output(['git','rev-parse','HEAD^{tree}'],text=True).strip(),'payloads':records}
before=snapshot();save('owner-source-before.json',before)
d=json.loads((owner/'.github/qualification/tshy-stable-native-original-sdk.json').read_bytes());assert d['commit']=='b695e201caa7232767ade0cfb5e75f244fd8e41c'
temporary=pathlib.Path(tempfile.mkdtemp(prefix='owned-tshy-native7-'));sdk=temporary/'sdk';sdk.mkdir()
with urllib.request.urlopen('https://codeload.github.com/isaacs/tshy/tar.gz/'+d['commit'],timeout=60) as stream:archive=stream.read(32*1024*1024+1)
assert len(archive)<=32*1024*1024;expected={r['path']:r for r in d['source_payloads']};actual={}
with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as tar:
    for member in tar.getmembers():
        assert member.isfile() or member.isdir()
        if not member.isfile():continue
        parts=pathlib.PurePosixPath(member.name).parts;assert len(parts)>1 and '..' not in parts
        name='/'.join(parts[1:]);assert name in expected and name not in actual
        raw=tar.extractfile(member).read();r=expected[name];assert hashlib.sha256(raw).hexdigest()==r['sha256'] and hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()==r['git_blob']
        p=sdk/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw);actual[name]=r
assert actual.keys()==expected.keys() and len(actual)==174
original_manifest=json.loads((sdk/'package.json').read_bytes());manifest=json.loads(json.dumps(d['candidate_manifest']));assert manifest['scripts']==original_manifest['scripts']==d['original_scripts']
if profile=='native-builder-runtime':manifest['devDependencies']={name:value for name,value in manifest['devDependencies'].items() if name in ['@types/node','esbuild','@owned/native-typescript-cli']}
(sdk/'package.json').write_bytes((json.dumps(manifest,indent=2)+'\n').encode())
for port in d['source_ports']:
    assert hashlib.sha256((sdk/port['path']).read_bytes()).hexdigest()==port['before_sha256'];raw=port['content'].encode();assert hashlib.sha256(raw).hexdigest()==port['sha256'];(sdk/port['path']).write_bytes(raw)
# Explicit source-linked declaration parent; never overwrite shared dependency sources.
nt=json.loads((owner/'.github/qualification/node-types-current-undici-parent.json').read_bytes())
assert nt['public_name']=='@types/node' and nt['public_version']=='26.6.5'
with urllib.request.urlopen(nt['official_tarball'],timeout=60) as stream:ntarchive=stream.read(8*1024*1024+1)
assert len(ntarchive)<=8*1024*1024 and hashlib.sha256(ntarchive).hexdigest()==nt['archive_sha256']
import base64,shutil
assert 'sha512-'+base64.b64encode(hashlib.sha512(ntarchive).digest()).decode()==nt['official_integrity']
ntroot=sdk/'vendor/owned-node-types';ntroot.mkdir(parents=True)
ntexpected={r['path']:r for r in nt['source_payloads']};ntactual={}
with tarfile.open(fileobj=io.BytesIO(ntarchive),mode='r:gz') as tar:
    for member in tar.getmembers():
        assert member.isfile() or member.isdir()
        if not member.isfile():continue
        parts=pathlib.PurePosixPath(member.name).parts;assert len(parts)>1 and '..' not in parts and not member.name.startswith('/')
        name='/'.join(parts[1:]);assert name in ntexpected and name not in ntactual and member.size<4*1024*1024
        raw=tar.extractfile(member).read();assert len(raw)==member.size and hashlib.sha256(raw).hexdigest()==ntexpected[name]['sha256']
        target=ntroot/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw);ntactual[name]=ntexpected[name]
assert ntactual.keys()==ntexpected.keys() and json.loads((ntroot/'package.json').read_bytes())==nt['original_manifest']
(ntroot/'package.json').write_bytes((json.dumps(nt['candidate_manifest'],indent=2)+'\n').encode())
manifest['devDependencies']['@types/node']='file:vendor/owned-node-types'
(sdk/'package.json').write_bytes((json.dumps(manifest,indent=2)+'\n').encode())
ntbefore={n:hashlib.sha256((ntroot/n).read_bytes()).hexdigest() for n in ntexpected};save('Node-types-explicit-parent-source-before.json',{'official_input':nt,'actual':ntbefore})

# Verify complete public parent source/cases, then stage explicit consumer ports.
gp=json.loads((owner/'.github/qualification/tshy-current-glob-parent-ports.json').read_bytes())
gparents={p['name']:p for p in gp['parents']};assert set(gparents)=={'resolve-import','rimraf','sync-content'}
groot={};gbefore={};gsdk={}
for name,p in gparents.items():
    original=p['original_SDK'];expected_source={r['path']:r for r in original['source_payloads']}
    with urllib.request.urlopen('https://codeload.github.com/'+original['repository']+'/tar.gz/'+original['commit'],timeout=60) as stream:raw=stream.read(32*1024*1024+1)
    assert len(raw)<=32*1024*1024 and hashlib.sha256(raw).hexdigest()==original['archive_sha256'];source={}
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tar:
        for member in tar.getmembers():
            if member.isdir():continue
            parts=pathlib.PurePosixPath(member.name).parts;assert len(parts)>1 and '..' not in parts and not member.name.startswith('/')
            rel='/'.join(parts[1:]);assert rel in expected_source and rel not in source and (member.isfile() or member.issym())
            data=member.linkname.encode() if member.issym() else tar.extractfile(member).read()
            assert hashlib.sha256(data).hexdigest()==expected_source[rel]['sha256'] and hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==expected_source[rel]['git_blob']
            source[rel]=expected_source[rel]
    assert source.keys()==expected_source.keys();gsdk[name]={'original_input':original,'verified_source':source,'original_SDK_case_commands_executed':False}
    with urllib.request.urlopen(p['tarball'],timeout=60) as stream:raw=stream.read(8*1024*1024+1)
    assert len(raw)<=8*1024*1024 and hashlib.sha256(raw).hexdigest()==p['archive_sha256']
    assert 'sha512-'+base64.b64encode(hashlib.sha512(raw).digest()).decode()==p['integrity']
    parentroot=sdk/p['vendor_path'];parentroot.mkdir(parents=True);expected_source={r['path']:r for r in p['source_payloads']};source={}
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tar:
        for member in tar.getmembers():
            assert member.isfile() or member.isdir()
            if not member.isfile():continue
            parts=pathlib.PurePosixPath(member.name).parts;assert len(parts)>1 and '..' not in parts and not member.name.startswith('/')
            rel='/'.join(parts[1:]);assert rel in expected_source and rel not in source and member.size<4*1024*1024
            data=tar.extractfile(member).read();assert len(data)==member.size and hashlib.sha256(data).hexdigest()==expected_source[rel]['sha256']
            target=parentroot/rel;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);source[rel]=expected_source[rel]
    assert source.keys()==expected_source.keys() and json.loads((parentroot/'package.json').read_bytes())==p['original_manifest']
    assert p['candidate_manifest'].get('scripts',{})==p['original_manifest'].get('scripts',{})
    (parentroot/'package.json').write_bytes((json.dumps(p['candidate_manifest'],indent=2)+'\n').encode())
    groot[name]=parentroot;gbefore[name]={rel:hashlib.sha256((parentroot/rel).read_bytes()).hexdigest() for rel in expected_source}
    manifest['dependencies'][name]='file:'+p['vendor_path']
(sdk/'package.json').write_bytes((json.dumps(manifest,indent=2)+'\n').encode())
save('Glob-parent-original-SDK-source-and-case-proof.json',gsdk);save('Glob-parent-publisher-source-before.json',gbefore)

cli=sdk/'vendor/owned-native-typescript-cli';cli.mkdir(parents=True)
for r in d['owned_cli_source_files']:
    raw=r['content'].encode();assert hashlib.sha256(raw).hexdigest()==r['sha256'];path=cli/r['path'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
(cli/'bin/tsgo.cjs').chmod(0o755)
owned_before={r['path']:hashlib.sha256((cli/r['path']).read_bytes()).hexdigest() for r in d['owned_cli_source_files']};save('owned-CLI-source-before.json',owned_before)
save('publisher-source-input.json',{'profile':profile,'source_payloads':actual,'archive_sha256':hashlib.sha256(archive).hexdigest(),'original_scripts':original_manifest['scripts'],'ported_manifest':manifest,'source_ports':d['source_ports'],'actual_node':subprocess.check_output(['node','--version'],text=True).strip(),'actual_npm':subprocess.check_output(['npm','--version'],text=True).strip()})
(out/'publisher-original-package-lock.json').write_bytes((sdk/'package-lock.json').read_bytes());(sdk/'package-lock.json').rename(temporary/'publisher-original-package-lock.json')
initial={n:hashlib.sha256((sdk/n).read_bytes()).hexdigest() for n in expected if n!='package-lock.json'};save('SDK-source-before.json',initial)
commands=[]
def run(label,args,cwd=sdk,extra_env=None):
    env=os.environ.copy()
    if extra_env:env.update(extra_env)
    with (out/(label+'.log')).open('wb') as stream:r=subprocess.run(args,cwd=cwd,stdout=stream,stderr=subprocess.STDOUT,env=env)
    commands.append({'label':label,'argv':args,'exit_code':r.returncode});save('commands.json',commands);return r.returncode
metadata=run('normal-strict-metadata',['npm','install','--package-lock-only','--ignore-scripts','--strict-peer-deps']);rows=[];prereleases=[];source_links=[];installed=False;native_proof=None;original_test_exit=None;cases=[]
if metadata==0 and (sdk/'package-lock.json').is_file():
    lock=json.loads((sdk/'package-lock.json').read_bytes());(out/'generated-package-lock.json').write_bytes((sdk/'package-lock.json').read_bytes())
    for name,r in lock['packages'].items():
        if not name:continue
        if r.get('link'):
            assert (name=='node_modules/tshy' and r['resolved'] in ['', '.']) or (name=='node_modules/@owned/native-typescript-cli' and r['resolved']=='vendor/owned-native-typescript-cli') or (name=='node_modules/@types/node' and r['resolved']=='vendor/owned-node-types') or any(name.rsplit('node_modules/',1)[-1]==n and r['resolved']==p['vendor_path'] for n,p in gparents.items()),(name,r)
            source_links.append({'path':name,**r});continue
        if name=='vendor/owned-native-typescript-cli':
            assert r['version']=='1.0.0' and r['dependencies']=={'typescript':'7.0.2'};source_links.append({'path':name,**r});continue
        if name=='vendor/owned-node-types':
            assert r['version']==nt['candidate_manifest']['version'] and r['dependencies']==nt['candidate_manifest']['dependencies'];source_links.append({'path':name,**r});rows.append({'path':name,'name':'@types/node','version':nt['public_version'],'explicit_private_parent_version':r['version']});continue
        if name in {p['vendor_path'] for p in gparents.values()}:
            p=next(p for p in gparents.values() if p['vendor_path']==name);assert r['version']==p['candidate_manifest']['version'] and r['dependencies']==p['candidate_manifest']['dependencies'];source_links.append({'path':name,**r});rows.append({'path':name,'name':p['name'],'version':p['highest_public_nonretired_stable'],'explicit_private_parent_version':r['version']});continue
        assert 'node_modules/' in name and r.get('version'),(name,r)
        rows.append({'path':name,'name':name.rsplit('node_modules/',1)[1],'version':r['version']})
    prereleases=[r for r in rows if re.search(r'^\d+\.\d+\.\d+-',r['version'])];save('prerelease-lock-records.json',prereleases);save('explicit-source-link-records.json',source_links)
    if not prereleases:installed=run('normal-frozen-install-and-unchanged-prepare',['npm','ci','--strict-peer-deps'])==0
if installed:
    native_exit=run('real-stable-native-compiler-version',['node',str(cli/'bin/tsgo.cjs'),'--version'],extra_env={'OWNED_NATIVE_COMPILER_TRACE':'1'})
    text=(out/'real-stable-native-compiler-version.log').read_text();records=[json.loads(line.removeprefix('OWNED_NATIVE_COMPILER ')) for line in text.splitlines() if line.startswith('OWNED_NATIVE_COMPILER ')];assert native_exit==0 and len(records)==1 and re.search(r'^Version 7\.0\.2\s*$',text,re.M)
    native_proof=records[0];assert native_proof['compiler_package']=='typescript' and native_proof['compiler_version']=='7.0.2' and native_proof['argv']==['--version'];assert hashlib.sha256(pathlib.Path(native_proof['compiler_launcher']).read_bytes()).hexdigest()==native_proof['compiler_launcher_sha256'];save('actual-stable-native-compiler-proof.json',native_proof)
    if profile=='complete-original-SDK':
        original_test_exit=run('original-complete-SDK-test',['npm','test'])
        run('original-prepare-command',['npm','run','prepare']);run('original-typedoc-command',['npm','run','typedoc'])
    else:
        tool=sdk/'dist/esm/bin-min.mjs';assert tool.is_file()
        for selector in ['tsc','tsgo']:
            project=temporary/('owned-'+selector);(project/'src').mkdir(parents=True)
            shutil.copytree(ntroot,project/'vendor/owned-node-types')
            package={'name':'owned-native-client','version':'1.0.0','private':True,'type':'module','devDependencies':{'@types/node':manifest['devDependencies']['@types/node'],'typescript':'7.0.2'},'tshy':{'compiler':selector,'exports':{'.':'./src/index.ts'}}}
            (project/'package.json').write_bytes((json.dumps(package,indent=2)+'\n').encode());(project/'src/index.ts').write_bytes(b'export const twice = (value: number): number => value * 2\n')
            client_metadata=run('owned-'+selector+'-normal-client-metadata',['npm','install','--package-lock-only','--ignore-scripts','--strict-peer-deps'],cwd=project)
            if client_metadata==0 and (project/'package-lock.json').is_file():
                client_lock=json.loads((project/'package-lock.json').read_bytes());client_rows=[]
                for name,r in client_lock['packages'].items():
                    if not name:continue
                    if name=='node_modules/@types/node' and r.get('link'):
                        assert r['resolved']=='vendor/owned-node-types';source_links.append({'profile':selector+'-client','path':name,**r});continue
                    if name=='vendor/owned-node-types':
                        assert r['version']==nt['candidate_manifest']['version'] and r['dependencies']==nt['candidate_manifest']['dependencies'];source_links.append({'profile':selector+'-client','path':name,**r});client_rows.append({'path':selector+'-client/'+name,'name':'@types/node','version':nt['public_version'],'explicit_private_parent_version':r['version']});continue
                    assert 'node_modules/' in name and not r.get('link') and r.get('version')
                    client_rows.append({'path':selector+'-client/'+name,'name':name.rsplit('node_modules/',1)[1],'version':r['version']})
                client_prereleases=[r for r in client_rows if re.search(r'^\d+\.\d+\.\d+-',r['version'])];save('owned-'+selector+'-client-prereleases.json',client_prereleases)
                rows.extend(client_rows);prereleases.extend(client_prereleases);(out/('owned-'+selector+'-client-package-lock.json')).write_bytes((project/'package-lock.json').read_bytes())
                if not client_prereleases:
                    run('owned-'+selector+'-normal-client-frozen-install',['npm','ci','--strict-peer-deps'],cwd=project)
                    run('owned-'+selector+'-complete-client-audit',['npm','audit','--json'],cwd=project)
                    run('owned-'+selector+'-complete-client-installed-graph',['npm','ls','--all','--json'],cwd=project)
            built=run('owned-real-'+selector+'-dual-module-build',['node',str(tool)],cwd=project)==0
            evaluated=False
            if built:
                esm=run('owned-real-'+selector+'-ESM-output',['node','--input-type=module','-e',"import {strict as assert} from 'node:assert';import {twice} from './dist/esm/index.js';assert.equal(twice(21),42);"],cwd=project)==0
                cjs=run('owned-real-'+selector+'-CJS-output',['node','-e',"const assert=require('node:assert/strict');const {twice}=require('./dist/commonjs/index.js');assert.equal(twice(21),42);"],cwd=project)==0
                evaluated=esm and cjs
            cases.append({'name':selector+'-real-native-dual-module-build-and-runtime','pass':built and evaluated})
            (project/'src/index.ts').write_bytes(b'export const broken: number = "not-a-number"\n')
            label='owned-real-'+selector+'-invalid-typed-source';code=run(label,['node',str(tool)],cwd=project);diagnostic=(out/(label+'.log')).read_text(errors='replace');cases.append({'name':selector+'-real-native-type-error-rejected','pass':code!=0 and 'TS2322' in diagnostic,'actual_exit':code})
        save('owned-real-native-build-case-results.json',{'cases':cases,'passed':sum(r['pass'] for r in cases),'failed':sum(not r['pass'] for r in cases),'skipped':0,'original_SDK_tests_replaced':False})
    if profile=='native-builder-runtime':
        baseline=temporary/'owned-public-parent-baseline';baseline.mkdir()
        baseline_manifest={'name':'owned-public-parent-baseline','version':'1.0.0','private':True,'dependencies':{n:p['highest_public_nonretired_stable'] for n,p in gparents.items()}}
        (baseline/'package.json').write_bytes((json.dumps(baseline_manifest,indent=2)+'\n').encode())
        baseline_metadata=run('owned-parent-baseline-normal-strict-metadata',['npm','install','--package-lock-only','--ignore-scripts','--strict-peer-deps'],cwd=baseline)
        if baseline_metadata==0:
            baseline_lock=json.loads((baseline/'package-lock.json').read_bytes());(out/'owned-parent-baseline-package-lock.json').write_bytes((baseline/'package-lock.json').read_bytes())
            baseline_prereleases=[{'path':n,'version':r.get('version')} for n,r in baseline_lock['packages'].items() if n and re.search(r'^\d+\.\d+\.\d+-',r.get('version',''))];save('owned-parent-baseline-prereleases.json',baseline_prereleases)
            if not baseline_prereleases:
                baseline_install=run('owned-parent-baseline-normal-frozen-install',['npm','ci','--strict-peer-deps'],cwd=baseline)
                run('owned-parent-baseline-complete-audit',['npm','audit','--json'],cwd=baseline)
                run('owned-parent-baseline-complete-installed-graph',['npm','ls','--all','--json'],cwd=baseline)
                if baseline_install==0:
                    baseline_sources={}
                    for name,p in gparents.items():
                        baseline_root=baseline/'node_modules'/name
                        baseline_sources[name]={r['path']:hashlib.sha256((baseline_root/r['path']).read_bytes()).hexdigest() for r in p['source_payloads']}
                        assert baseline_sources[name]=={r['path']:r['sha256'] for r in p['source_payloads']}
                    save('owned-parent-baseline-publisher-source-before.json',baseline_sources)
                    run('owned-parent-baseline-real-contract-cases',['node','--test',str(owner/'.github/scripts/tshy-glob-parent-contract.test.mjs')],cwd=baseline,extra_env={'OWNED_GLOB_PARENT_ROOT':str(baseline)})
                    after_baseline={name:{r['path']:hashlib.sha256((baseline/'node_modules'/name/r['path']).read_bytes()).hexdigest() for r in p['source_payloads']} for name,p in gparents.items()}
                    assert baseline_sources==after_baseline;save('owned-parent-baseline-publisher-source-after.json',after_baseline)
        run('owned-parent-candidate-real-contract-cases',['node','--test',str(owner/'.github/scripts/tshy-glob-parent-contract.test.mjs')],extra_env={'OWNED_GLOB_PARENT_ROOT':str(sdk)})
    run('complete-unfiltered-audit',['npm','audit','--json']);run('complete-installed-graph',['npm','ls','--all','--json'])
selected=collections.defaultdict(set)
for r in rows:selected[r['name']].add(r['version'])
def public(name):
    try:
        with urllib.request.urlopen('https://registry.npmjs.org/'+urllib.parse.quote(name,safe=''),timeout=45) as stream:raw=stream.read(48*1024*1024+1)
        assert len(raw)<=48*1024*1024;m=json.loads(raw);versions=[v for v,r in m['versions'].items() if re.fullmatch(r'\d+\.\d+\.\d+',v) and not r.get('deprecated')];highest=max(versions,key=lambda x:tuple(map(int,x.split('.')))) if versions else None
        return {'name':name,'selected':sorted(selected[name]),'highest_public_nonretired_stable':highest,'highest_stable_met':selected[name]=={highest},'query':'ok','metadata_sha256':hashlib.sha256(raw).hexdigest()}
    except Exception as error:return {'name':name,'selected':sorted(selected[name]),'query':'error','error':str(error),'highest_stable_met':False}
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:comparison=list(pool.map(public,sorted(selected)))
save('complete-public-graph-comparison.json',comparison)
final={n:hashlib.sha256((sdk/n).read_bytes()).hexdigest() for n in expected if n!='package-lock.json'};save('SDK-source-after.json',final);changes=[{'path':n,'before':initial[n],'after':final[n]} for n in initial if initial[n]!=final[n]];save('source-side-effects.json',changes)
protected={n:r for n,r in expected.items() if n.startswith(('test/','tests/','tap-snapshots/'))};assert len(protected)==114;unchanged=all(final[n]==r['sha256'] for n,r in protected.items());save('original-case-helper-fixture-snapshot-source-after.json',protected)
owned_after={r['path']:hashlib.sha256((cli/r['path']).read_bytes()).hexdigest() for r in d['owned_cli_source_files']};save('owned-CLI-source-after.json',owned_after);assert owned_before==owned_after
gafter={name:{r['path']:hashlib.sha256((groot[name]/r['path']).read_bytes()).hexdigest() for r in p['source_payloads']} for name,p in gparents.items()};assert gbefore==gafter
assert all(gafter[name][r['path']]==r['sha256'] for name,p in gparents.items() for r in p['source_payloads'] if r['path']!='package.json')
save('Glob-parent-publisher-source-after.json',gafter)
ntafter={n:hashlib.sha256((ntroot/n).read_bytes()).hexdigest() for n in ntexpected};assert ntbefore==ntafter
save('Node-types-explicit-parent-source-after.json',{'actual':ntafter,'all_original_nonmanifest_payloads_unchanged':all(ntafter[n]==r['sha256'] for n,r in ntexpected.items() if n!='package.json'),'original_declared_dependency':nt['original_manifest']['dependencies'],'explicit_private_parent_dependency':nt['candidate_manifest']['dependencies']})
for selector in ['tsc','tsgo']:
    client=temporary/('owned-'+selector)/'vendor/owned-node-types'
    if client.is_dir():assert {n:hashlib.sha256((client/n).read_bytes()).hexdigest() for n in ntexpected}==ntbefore
after=snapshot();save('owner-source-after.json',after);assert before==after
summary={'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'profile':profile,'publisher_payloads_verified':174,'original_case_helper_fixture_snapshot_payloads':114,'original_case_helper_fixture_snapshot_payloads_unchanged':unchanged,'original_scripts_unchanged':json.loads((sdk/'package.json').read_bytes())['scripts']==d['original_scripts'],'metadata_exit':metadata,'installed':installed,'prereleases':prereleases,'explicit_source_links':source_links,'actual_native_compiler':native_proof,'original_npm_test_exit':original_test_exit,'owned_runtime_cases':cases,'source_side_effects':changes,'public_namespaces':len(comparison),'public_current_version_gaps':sum(not r['highest_stable_met'] for r in comparison),'public_metadata_query_errors':sum(r['query']!='ok' for r in comparison),'commands':commands,'cross_platform_original_SDK_matrix_not_established':True,'whole_original_SDK_qualified':False,'actual_parent_or_application_adoption':False}
save('qualification-summary.json',summary)
assert unchanged and not changes,'Original SDK source/helper mutation remains fatal.'
assert installed and not prereleases,'Strict current normal installation remains a required gate.'
negative_labels={'owned-real-tsc-invalid-typed-source','owned-real-tsgo-invalid-typed-source'}
assert all(r['exit_code']==0 for r in commands if r['label'] not in negative_labels),'An original SDK, native build, graph or audit command failed; no suppressions.'
if profile=='native-builder-runtime':assert len(cases)==4 and all(r['pass'] for r in cases)
assert all(r['highest_stable_met'] for r in comparison),'Complete selected public graph retains current-version gaps.'
