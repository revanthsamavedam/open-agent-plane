---
name: draft-report
version: 1.0.0
description: Draft a short report from a connector read (or from the prompt alone) for human approval.
write: true
---
# Draft Report

1. Optionally read from one allowed connector to gather facts.
2. Draft a report titled from the request, with a summary section and the
   facts found.
3. The draft goes through the reviewer and requires named human approval.
   On approval the draft is applied via the connector's write_draft (LIVE
   connectors write to their stand-in; SCAFFOLD connectors record the
   intended write in the run + audit, labelled as recorded intent).
