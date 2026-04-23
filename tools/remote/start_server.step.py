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
            "Or copy this command:<br>"
            '<code id="sync-cmd" style="background:#161b22;padding:4px 8px;border-radius:4px;font-size:12px;">'
            "curl -o imp-sync.py http://HOST:8421/imp-sync.py && python imp-sync.py</code></p>"
            '<p style="font-size:12px;color:#8b949e;margin-top:12px;">'
            "Syncing: tools/, workflows/, renderers/, public/</p>"
        ),
        "actions": [
            {"label": "Stop sync", "action": "continue"},
        ],
        "ok": True,
        "output": "Sync active — download link in queue popup",
    }
