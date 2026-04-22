# remote

Remote access tools — sync files between the Imp server and developer machines.

## Tools

| Script | Purpose |
|--------|---------|
| `start_server.py` | Start the Imp server (checks if already running) |
| `stop_server.py` | Stop the Imp server (SIGTERM, falls back to SIGKILL) |
| `sync.py` | Print the sync client download URL |

## Usage

### Quick start

1. Run the sync tool on the server (or start the `dev_sync` workflow)
2. On your local machine, download and run the sync script:

```bash
curl -o imp-sync.py http://<server-ip>:8421/imp-sync.py
python imp-sync.py
```

3. Edit files on either side — changes sync automatically every 2 seconds
4. Press Ctrl+C to stop

### What syncs

- `tools/` — all `.py`, `.step.py`, `.md` files
- `workflows/` — step scripts and READMEs (not `last_run.json`)
- `renderers/` — renderer plugins
- `public/` — static files

### Via workflow

Start the **Developer Sync** workflow from the Workflows tab. The queue popup
shows a download button with the server IP already configured.
