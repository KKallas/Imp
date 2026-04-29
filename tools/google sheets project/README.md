# google sheets project

Project issue and PR tracking backed by a Google Sheet with two tabs: **issues** and **pull_requests**.

## Setup

### 1. Google API Credentials

You need a Google Cloud project with the **Sheets API** and **Drive API** enabled, plus a credentials file. Follow **one** of the two paths below.

---

#### Option A — OAuth2 Desktop App (recommended for personal use)

This opens a browser window on first run so you can log in with your Google account.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. **Create a project** (or select an existing one).
   - Click the project dropdown at the top → **New Project** → give it a name → **Create**.
3. **Enable the APIs.**
   - In the left sidebar go to **APIs & Services → Library**.
   - Search for **Google Sheets API** → click it → **Enable**.
   - Go back to Library, search for **Google Drive API** → click it → **Enable**.
4. **Configure the OAuth consent screen.**
   - Go to **APIs & Services → OAuth consent screen**.
   - Choose **External** (or Internal if you have a Workspace org) → **Create**.
   - Fill in the required fields (App name, User support email, Developer email). Everything else can be left blank.
   - Click **Save and Continue** through Scopes, Test Users, and Summary.
   - On the **Test Users** step, add your own Google email address so you can log in during testing.
5. **Create OAuth2 credentials.**
   - Go to **APIs & Services → Credentials**.
   - Click **+ Create Credentials → OAuth client ID**.
   - Application type: **Desktop app**.
   - Give it a name (e.g. "Imp Sheets") → **Create**.
   - A dialog shows your Client ID and Client Secret. Click **Download JSON**.
6. **Save the file.**
   - Rename the downloaded file to `credentials.json`.
   - Move it into this directory (`tools/google sheets project/`).
7. **First run.** When you run `python setup_sheet.py`, a browser tab opens. Log in with the Google account you added as a test user, grant access, and a `token.json` is saved automatically — future runs won't ask again.

> **Note:** While the app is in "Testing" mode the consent screen shows a warning. This is normal. You can publish the app later or just keep it in testing.

---

#### Option B — Service Account (for CI / headless / shared use)

A service account authenticates without a browser, ideal for automation.

1. Follow steps 1–3 from Option A (create project, enable Sheets + Drive APIs).
2. **Create a service account.**
   - Go to **APIs & Services → Credentials**.
   - Click **+ Create Credentials → Service account**.
   - Give it a name (e.g. "imp-sheets-bot") → **Create and Continue**.
   - Skip the optional role/permissions steps → **Done**.
3. **Generate a key.**
   - In the Service Accounts list, click the account you just created.
   - Go to the **Keys** tab → **Add Key → Create new key**.
   - Choose **JSON** → **Create**. A file downloads automatically.
4. **Save the file.**
   - Rename it to `service_account.json`.
   - Move it into this directory (`tools/google sheets project/`).
5. **Share the spreadsheet with the service account.**
   - Open the key file and find the `client_email` field (looks like `imp-sheets-bot@your-project.iam.gserviceaccount.com`).
   - If connecting to an existing Google Sheet, open that sheet in your browser and click **Share** → paste the `client_email` → give it **Editor** access.
   - If creating a new sheet via `setup_sheet.py`, the service account owns it. To see it in your own Google Drive, share it with your personal email from the script output link.

---

#### Credential file summary

| File | Use case |
|---|---|
| `credentials.json` | Interactive / desktop use (OAuth2 — opens browser on first run) |
| `service_account.json` | Headless / CI environments (no browser needed) |
| `token.json` | Auto-generated after first OAuth2 login — do not edit |

> **Security:** Never commit `credentials.json`, `service_account.json`, or `token.json` to version control. They are already in `.gitignore`.

### 2. Connect to a Sheet

```bash
# Auto-create or find an existing "Imp Project Tracker" sheet
python setup_sheet.py

# Connect to a specific existing spreadsheet by ID
python setup_sheet.py --id 1AbC_xYz...

# Force create a new sheet even if one exists
python setup_sheet.py --force-new
```

The spreadsheet ID is saved in `.sheet_config.json` so subsequent commands find it automatically.

## Tools

| Script | Purpose | Key Arguments |
|---|---|---|
| `setup_sheet.py` | Create or connect to the project spreadsheet | `--id`, `--force-new` |
| `list_issues.py` | List issues with optional filters | `--state`, `--limit`, `--label` |
| `open_issue.py` | Create a new issue | `--title` (required), `--body`, `--label` |
| `close_issue.py` | Close an issue, optionally with a comment | `issue` (required), `--reason`, `--comment` |
| `list_prs.py` | List pull requests | `--state`, `--limit` |
| `open_pr.py` | Create a pull request entry | `--title` (required), `--body`, `--base`, `--head` |

## Sheet Structure

### issues tab

| ID | Title | Body | State | Labels | Created | Updated |
|---|---|---|---|---|---|---|

### pull_requests tab

| ID | Title | Body | State | Base | Head | Created | Updated |
|---|---|---|---|---|---|---|---|

## Usage Examples

```bash
# Set up / connect to sheet (run once)
python setup_sheet.py

# List open issues
python list_issues.py

# List issues with a specific label
python list_issues.py --label bug --limit 10

# Create a new issue
python open_issue.py --title "Fix login bug" --body "Steps to reproduce..." --label bug

# Close an issue with a comment
python close_issue.py 42 --comment "Fixed in PR #3"

# List open PRs
python list_prs.py

# Create a pull request entry
python open_pr.py --title "Add search feature" --body "Implements full-text search" --base main --head feat/search
```

## Dependencies

```
google-api-python-client
google-auth-httplib2
google-auth-oauthlib
```

> Install with: `pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib`
