# Historical FAB Local Source Recovery

This branch preserves local source before owner-requested cleanup on 2026-10-24.
It is not a merge-ready production update. Current verified routing changes are
on draft PR #146. In particular, historical ngrok code here must not override the
owner's later decision to retire ngrok.

The first recovery commit snapshots 131 modified/untracked source, test,
configuration and documentation files from the older working checkout, without
changing its original HEAD, index, or working files. Runtime data, credentials,
financial documents, databases and backups are not included in that snapshot.
A pattern scan of the snapshot files found no matching credential signatures;
this is not a full security assessment or production verification.

The files in this recovery directory are exact copies of source saved during an
earlier interrupted test run. `local_routing.py` and
`test_routing_transactions.latest.py` match the corresponding committed blobs
at `798515e`. The older `test_routing_transactions.py` is retained for exact
recoverability, not proposed as a replacement for the newer regression tests.
