# Developer Sync

Start a live bidirectional file sync session. Syncs `tools/`, `workflows/`,
`renderers/`, and `public/` between the server and a developer's local machine.

1. Click **Start** to activate the sync endpoint, it will pause the workflow and you can stop the server from the queue popup
2. Download `imp-sync.py` from the queue popup
3. Run it locally: `python imp-sync.py`
4. Edit files on either side — changes sync automatically
5. Click **Stop sync** in the queue to end the session, finish the workflow
