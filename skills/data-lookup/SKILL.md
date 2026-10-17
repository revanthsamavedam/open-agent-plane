---
name: data-lookup
version: 1.0.0
description: Answer a question by running a read-only lookup against one allowed connector.
write: false
---
# Data Lookup

1. Run exactly one read against the chosen connector (for databases: a single
   read-only SELECT).
2. Return the rows as the answer. No draft, no approval needed — reads finish
   the run, and every read is written to the audit trail.
