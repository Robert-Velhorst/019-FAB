"""Owned hosted metadata-only evidence: no dependency/application/SDK imports."""
import collections,concurrent.futures,datetime,json,pathlib,re,subprocess,urllib.parse,urllib.request
assert __import__('os').environ['GITHUB_REPOSITORY']=='Robert-Velhorst/019-FAB'
output=pathlib.Path('/tmp/fab-public-lock');lock=pathlib.Path('web/pnpm-lock.yaml').read_text()
documents=[d for d in re.split(r'(?m)^---\n',lock) if d.strip()]
versions=collections.defaultdict(set);private=[];prereleases=[]
for document in documents:
    packages=document.split('\npackages:\n',1)[1].split('\nsnapshots:\n',1)[0]
    for key in re.findall(r'^  (\S[^\n]*):$',packages,re.M):
        key=key.strip("'\"")
        if '@file:' in key:private.append(key);continue
        match=re.fullmatch(r'(.+)@(\d+\.\d+\.\d+(?:-[^()+]+)?(?:\+[^()]+)?)(?:\([^\n]*\))?',key)
        assert match,key
        versions[match[1]].add(match[2])
        if '-' in match[2]:prereleases.append({'name':match[1],'version':match[2]})
def config(key):
    raw=subprocess.check_output(['pnpm','--dir','web','config','get',key,'--json'],text=True).strip()
    return None if raw in {'','undefined'} else json.loads(raw)
policy={key:config(key) for key in ['engineStrict','minimumReleaseAge','minimumReleaseAgeStrict','minimumReleaseAgeExclude','minimumReleaseAgeIgnoreMissingTime','verifyStoreIntegrity','trustPolicy']}
assert policy['engineStrict'] is True and policy['minimumReleaseAge']==1440 and not policy['minimumReleaseAgeExclude']
def public(name):
    url='https://registry.npmjs.org/'+urllib.parse.quote(name,safe='')
    try:
        with urllib.request.urlopen(url,timeout=45) as stream:raw=stream.read(24*1024*1024+1)
        assert len(raw)<=24*1024*1024,'Registry record exceeds bounded metadata limit.'
        data=json.loads(raw);stable=[v for v,row in data['versions'].items() if re.fullmatch(r'\d+\.\d+\.\d+',v) and not row.get('deprecated')]
        highest=max(stable,key=lambda v:tuple(map(int,v.split('.')))) if stable else None
        selected=sorted(versions[name]);published=data.get('time',{}).get(highest) if highest else None
        eligible=(datetime.datetime.fromisoformat(published.replace('Z','+00:00'))+datetime.timedelta(minutes=policy['minimumReleaseAge'])).isoformat() if published else None
        return {'name':name,'source':url,'query':'ok','selected_versions':selected,'highest_non_deprecated_public_stable':highest,'latest_tag':data.get('dist-tags',{}).get('latest'),'published':published,'eligible_after_unchanged_age_policy':eligible,'all_selected_versions_equal_highest_public_stable':highest is not None and set(selected)=={highest}}
    except Exception as error:
        return {'name':name,'source':url,'query':'unavailable','selected_versions':sorted(versions[name]),'error':str(error),'all_selected_versions_equal_highest_public_stable':False}
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:rows=list(pool.map(public,sorted(versions)))
targets={name:next(r for r in rows if r['name']==name) for name in ['body-parser','qs']}
gaps=[r for r in rows if not r['all_selected_versions_equal_highest_public_stable']]
record={'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'All package-key namespaces in both generated lock documents; physical installation and SDK acceptance are separate.','effective_policy':policy,'public_namespaces':len(rows),'query_errors':sum(r['query']!='ok' for r in rows),'packages':rows,'release_gaps':gaps,'private_source_packages':private,'prerelease_rows':prereleases,'strict_security_refresh_targets':targets,'all_public_namespaces_at_highest_stable':not gaps,'full_SDK_native_application_or_data_acceptance':False}
(output/'complete-public-graph-version-metadata.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps({'namespaces':len(rows),'query_errors':record['query_errors'],'release_gaps':len(gaps),'prerelease_rows':len(prereleases),'targets':targets}))
assert not prereleases,'Prerelease graph entries remain; none is excluded.'
for name,row in targets.items():
    assert row['query']=='ok' and row['all_selected_versions_equal_highest_public_stable'],name+': target not reached; age-bound selection and next eligibility are retained in the complete metadata artifact.'
