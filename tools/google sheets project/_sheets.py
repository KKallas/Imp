"""Google Sheets utility that manages a project-tracking spreadsheet with issues and pull_requests tabs.
Inputs: authenticates via service_account.json (headless), credentials.json (OAuth2), or cached token.json; configuration stored in .sheet_config.json.
Process: discovers or creates a spreadsheet named "Imp Project Tracker" on Google Drive, then exposes helpers for reading rows, appending rows, updating cells, and finding rows by integer ID.
Output: returns spreadsheet data as lists of strings, API response dicts from write operations, or integer IDs for new entries."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_DIR / ".sheet_config.json"

SHEET_TITLE = "Imp Project Tracker"

ISSUES_TAB = "issues"
PRS_TAB = "pull_requests"

ISSUES_HEADERS = [
    "ID", "Title", "Body", "State", "Labels", "Created", "Updated",
]
PRS_HEADERS = [
    "ID", "Title", "Body", "State", "Base", "Head", "Created", "Updated",
]

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _get_credentials() -> Credentials:
    """Return valid Google API credentials.

    Tries service account first, then falls back to OAuth2 installed-app flow.
    """
    sa_path = TOOL_DIR / "service_account.json"
    creds_path = TOOL_DIR / "credentials.json"
    token_path = TOOL_DIR / "token.json"

    # --- service account ---------------------------------------------------
    if sa_path.exists():
        return service_account.Credentials.from_service_account_file(
            str(sa_path), scopes=SCOPES,
        )

    # --- OAuth2 ------------------------------------------------------------
    creds: Optional[Credentials] = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    "No credentials found. Place one of the following in "
                    f"{TOOL_DIR}/:\n"
                    "  - service_account.json  (service account key)\n"
                    "  - credentials.json      (OAuth2 client secret)\n"
                )
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), SCOPES,
            )
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())

    return creds


def sheets_service():
    """Build the Sheets v4 API service."""
    return build("sheets", "v4", credentials=_get_credentials())


def drive_service():
    """Build the Drive v3 API service (used for finding spreadsheets)."""
    return build("drive", "v3", credentials=_get_credentials())


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def get_spreadsheet_id() -> Optional[str]:
    return load_config().get("spreadsheet_id")


def set_spreadsheet_id(sid: str) -> None:
    cfg = load_config()
    cfg["spreadsheet_id"] = sid
    save_config(cfg)


# ---------------------------------------------------------------------------
# Spreadsheet creation / discovery
# ---------------------------------------------------------------------------


def find_spreadsheet() -> Optional[str]:
    """Search Google Drive for a spreadsheet named *SHEET_TITLE*.

    Returns the spreadsheet ID or ``None``.
    """
    svc = drive_service()
    query = (
        f"name = '{SHEET_TITLE}' and mimeType = "
        "'application/vnd.google-apps.spreadsheet' and trashed = false"
    )
    resp = svc.files().list(q=query, fields="files(id, name)", pageSize=5).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def create_spreadsheet() -> str:
    """Create a new spreadsheet with *issues* and *pull_requests* tabs.

    Returns the new spreadsheet ID.
    """
    svc = sheets_service()
    body = {
        "properties": {"title": SHEET_TITLE},
        "sheets": [
            {
                "properties": {"title": ISSUES_TAB, "index": 0},
                "data": [{
                    "startRow": 0,
                    "startColumn": 0,
                    "rowData": [{
                        "values": [{"userEnteredValue": {"stringValue": h}}
                                   for h in ISSUES_HEADERS]
                    }],
                }],
            },
            {
                "properties": {"title": PRS_TAB, "index": 1},
                "data": [{
                    "startRow": 0,
                    "startColumn": 0,
                    "rowData": [{
                        "values": [{"userEnteredValue": {"stringValue": h}}
                                   for h in PRS_HEADERS]
                    }],
                }],
            },
        ],
    }
    sheet = svc.spreadsheets().create(body=body, fields="spreadsheetId").execute()
    return sheet["spreadsheetId"]


def ensure_spreadsheet() -> str:
    """Return the spreadsheet ID, creating one if needed.

    1. Check local config for a stored ID.
    2. Search Drive by name.
    3. Create a new spreadsheet.

    Stores the result in config for next time.
    """
    sid = get_spreadsheet_id()
    if sid:
        # Quick validation: try to read the sheet title
        try:
            svc = sheets_service()
            svc.spreadsheets().get(
                spreadsheetId=sid, fields="properties.title",
            ).execute()
            return sid
        except Exception:
            pass  # stale ID, keep looking

    sid = find_spreadsheet()
    if not sid:
        sid = create_spreadsheet()
    set_spreadsheet_id(sid)
    return sid


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_all_rows(tab: str) -> List[List[str]]:
    """Return every row (including the header) from *tab*."""
    sid = ensure_spreadsheet()
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{tab}!A:Z",
    ).execute()
    return resp.get("values", [])


def append_row(tab: str, row: List[str]) -> dict:
    """Append a single row to the bottom of *tab*."""
    sid = ensure_spreadsheet()
    svc = sheets_service()
    return svc.spreadsheets().values().append(
        spreadsheetId=sid,
        range=f"{tab}!A:A",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def next_id(tab: str) -> int:
    """Return the next available integer ID for *tab*."""
    rows = read_all_rows(tab)
    if len(rows) <= 1:          # header only or empty
        return 1
    max_id = 0
    for row in rows[1:]:
        try:
            max_id = max(max_id, int(row[0]))
        except (ValueError, IndexError):
            pass
    return max_id + 1


def update_cell(tab: str, row_index: int, col_index: int, value: str) -> dict:
    """Update a single cell (0-indexed row & col) in *tab*."""
    sid = ensure_spreadsheet()
    svc = sheets_service()
    # Convert to A1 notation
    col_letter = chr(ord("A") + col_index)
    cell = f"{tab}!{col_letter}{row_index + 1}"
    return svc.spreadsheets().values().update(
        spreadsheetId=sid,
        range=cell,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()


def find_row_by_id(tab: str, target_id: int) -> Optional[int]:
    """Return the 0-based row index whose ID column matches *target_id*,
    or ``None`` if not found."""
    rows = read_all_rows(tab)
    for i, row in enumerate(rows):
        if i == 0:
            continue  # skip header
        try:
            if int(row[0]) == target_id:
                return i
        except (ValueError, IndexError):
            pass
    return None
