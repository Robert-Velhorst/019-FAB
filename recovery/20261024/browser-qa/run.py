import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(r'C:\Users\NO\Documents\Codex\2026-06-03\019-FAB-pr125-next')
sys.path.insert(0, str(ROOT))
from scripts import run_stack_rehearsal as rehearsal
from src.operations.local_ledger import LocalOperationsLedger
import requests

real_popen = subprocess.Popen
real_request = requests.Session.request
state = {}


def capture_child(*args, **kwargs):
    environment = kwargs.get('env') or {}
    if environment.get('FAB_INSTANCE_ROOT'):
        state['root'] = Path(environment['FAB_INSTANCE_ROOT'])
    return real_popen(*args, **kwargs)


def browser_before_handoff(session, method, url, **kwargs):
    if '/api/fab/operator-session?next=/api/live' in url and not state.get('checked'):
        state['checked'] = True
        root = state['root']
        assert root.name.startswith('fab-stack-rehearsal-')
        ledger = LocalOperationsLedger(str(root / 'ledger.sqlite3'))
        with ledger.write_transaction():
            for index in range(350):
                document = ledger.register_document({
                    'source': 'synthetic_browser_qa', 'sourceDocumentId': str(index),
                    'originalFilename': f'synthetic-browser-{index:03}.pdf',
                    'vendorName': f'Synthetic QA Vendor {index:03}', 'totalAmount': 4.28,
                    'transactionDate': '2026-10-20', 'category': 'Manual Review',
                    'documentType': 'receipt', 'processingStatus': 'needs_review',
                })
                ledger.create_review_item({'documentId': document, 'reason': 'manual_review_category', 'details': 'Synthetic browser QA only'})
        parsed = urlsplit(url)
        base = f'{parsed.scheme}://{parsed.netloc}'
        headers = kwargs['headers']
        response = real_request(session, 'POST', base + '/api/trpc/fab.refreshControlCenter', headers=headers, json={'json': None}, timeout=30)
        assert response.status_code == 200
        environment = rehearsal.isolated_environment()
        environment['FAB_REHEARSAL_BROWSER'] = json.dumps({'webBase': base, 'cookie': headers['Cookie']})
        result = subprocess.run(['node', str(Path(__file__).with_name('browser.cjs'))], env=environment,
                                capture_output=True, text=True, timeout=150)
        if result.returncode:
            raise RuntimeError('Synthetic browser check failed: ' + result.stderr + result.stdout)
        print(result.stdout.strip(), flush=True)
    return real_request(session, method, url, **kwargs)


subprocess.Popen = capture_child
requests.Session.request = browser_before_handoff
try:
    print(json.dumps(rehearsal.rehearse(None)), flush=True)
finally:
    subprocess.Popen = real_popen
    requests.Session.request = real_request
