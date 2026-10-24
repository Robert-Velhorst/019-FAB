# Historical Container Source Recovery

These two files were recovered from the retained FAB API filesystem archive on
2026-10-24. They did not match blobs reachable from the three verified published
FAB history roots used during cleanup. The other 170 inspected application source
files were already represented in that history.

Preserve these copies for provenance only. They are older, superseded code with
weaker security behavior, not a production patch, and must not replace current
application modules. They have not been executed or accepted as production code.

Only source files are included. Runtime configuration, credentials, financial
data, filesystem exports and Docker volume contents remain local and are not
published. Pattern checks found no matching credential signatures in these two
source files; this is not a comprehensive security assessment.
