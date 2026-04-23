"""Activate developer sync — pause with download link."""


def run(context):
    # URLs use relative paths — the browser resolves them to whatever host it's connected to
    return {
        "pause": True,
        "title": "Developer sync active",
        "detail_html": (
            "<h3>Developer Sync Active</h3>"
            "<p>The sync endpoint is ready. Download the sync script and run it locally:</p>"
            '<p style="margin:16px 0;">'
            '<a href="/imp-sync.py" download="imp-sync.py"'
            ' style="display:inline-block;padding:8px 20px;background:#58a6ff;color:#fff;'
            ' border-radius:6px;text-decoration:none;font-weight:600;font-size:14px;">'
            "Download imp-sync.py</a></p>"
            '<p style="font-size:13px;color:#8b949e;">'
            "Or copy the command below (replace HOST with your server address):<br>"
            '<code id="sync-cmd" style="background:#161b22;padding:4px 8px;border-radius:4px;font-size:12px;">'
            "loading...</code></p>"
            '<script>document.getElementById("sync-cmd").textContent='
            '"curl -o imp-sync.py " + location.origin + "/imp-sync.py && python imp-sync.py";</script>'
            '<p style="font-size:12px;color:#8b949e;margin-top:12px;">'
            "Syncing: tools/, workflows/, renderers/, public/</p>"
        ),
        "actions": [
            {"label": "Stop sync", "action": "continue"},
        ],
        "ok": True,
        "output": "Sync active — download link in queue popup",
    }
