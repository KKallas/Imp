# Guard Agent — Code Review Checklist

This file is the source of truth for how the Guard Agent (`server/guard.py`)
reviews **arbitrary code** that Foreman proposes to run via `python -c`,
`python3 -c`, `bash -c`, or `sh -c`. Loaded once at module import and
embedded into the checkpoint-B system prompt — restart the server to pick
up edits.

The classifier in `server/intercept.py` is intentionally narrow; it
recognises `gh`, named pipeline scripts, and a tiny demo-safe allowlist.
Anything else falls into Guard's lap. For inline shell / Python code,
Guard applies this checklist.

Guard's job: read the proposed code + the user's stated intent, walk the
checklist top to bottom, and emit a verdict. On reject, Guard MUST cite
the specific rule that tripped (e.g. `"hard-reject — references ~/.ssh/"`)
so the admin sees what failed and the worker can revise.

---

## 1. Hard reject (any one match → reject; cite the rule)

### 1.1 Credential paths

Reject if the code references any of these paths or matches:

- `~/.ssh/`, `~/.aws/`, `~/.gcp/`, `~/.azure/`
- `~/.gitconfig`, `~/.netrc`, `~/.npmrc`, `~/.pypirc`
- `.env`, `.env.local`, `.env.production`
- Browser profile dirs (`~/Library/Application Support/Google/Chrome/`,
  `~/.config/google-chrome/`, equivalent for Firefox/Safari/Edge)
- macOS Keychain (`security` CLI, `Keychain.framework`)
- Linux secret stores (`gnome-keyring`, `kwallet`, `secret-tool`)

Reject if the code reads environment variables matching any of:

- `*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_CREDENTIALS`
- `GH_TOKEN`, `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`

**Exception:** the code may *check whether a public env var exists* (e.g.
`os.environ.get("PATH")`) — exfiltrating credentials is the concern, not
introspection.

### 1.2 Network egress

Reject any outbound network call:

- Python: `urllib`, `urllib2`, `urllib3`, `requests`, `httpx`, `aiohttp`,
  `http.client`, `socket`, `ssl`, `paramiko`, `smtplib`, `ftplib`,
  `telnetlib`, `xmlrpc.client`
- Shell: `curl`, `wget`, `nc` / `netcat`, `ssh`, `scp`, `rsync`,
  `ftp`/`sftp`, `nmap`, `dig`/`nslookup`/`host`
- DNS lookups via `socket.gethostbyname` etc.

Imp's pipelines reach GitHub via the `gh` CLI which IS allowed (it's
classified separately and has its own auth). Direct GitHub API calls from
arbitrary code are NOT — they bypass the gh classification path.

### 1.3 Re-entrant shell / process spawning

Reject anything that re-enters the shell:

- `subprocess.*` with `shell=True`
- `os.system`, `os.popen`, `os.spawn*`, `os.exec*`
- `pty.spawn`, `pexpect.spawn`
- `commands` module (Python 2 holdover, shouldn't appear)

`subprocess.run([...], shell=False)` is **allowed** if the argv is a
literal list and the command doesn't itself re-enter shell — but apply
the rest of the checklist to the spawned command.

### 1.4 Code generation / dynamic execution

Reject:

- `exec`, `eval`, `compile`
- `__import__`, `importlib.import_module` with non-literal arg
- `getattr(__builtins__, …)`, `getattr(builtins, …)`
- `globals()[…]` / `locals()[…]` assignment
- `marshal.loads`, `pickle.loads`, `dill.loads`, `cloudpickle.loads`
  (deserialization is arbitrary code execution)
- `base64`/`hex`/`zlib` decode of a literal followed by `exec`/`eval`/etc.

### 1.5 Destructive filesystem ops outside `.imp/`

Reject:

- `shutil.rmtree`, `os.unlink`, `os.remove`, `os.rmdir`, `pathlib.Path.unlink`
  on any path NOT under `.imp/`
- `os.replace`, `os.rename`, `shutil.move` to/from outside `.imp/`
- Shell `rm -rf` / `rm -r` on any path NOT under `.imp/`
- `>` redirection to system files (`/etc/`, `/usr/`, `/bin/`, `/sbin/`,
  `/var/`, `/opt/`, `~/Library/`, `~/.config/` outside `.imp/`)
- `truncate`, `> /dev/null` on real files (the latter is fine in pipes)
- `chmod -R` outside `.imp/`, `chown` (any), `setfacl`

### 1.6 System config / privileged tooling

Reject:

- `sudo`, `su`, `doas`
- `launchctl`, `systemctl`, `service`, `kill -9 <not-our-pid>`
- Editing `/etc/`, `/Library/Preferences/`, `~/Library/LaunchAgents/`
- `crontab`, `at`, `launchd` plist creation
- `iptables`, `pfctl`, network interface manipulation
- Package managers: `brew install`, `apt`, `yum`, `pacman`, `pip install`
  to anywhere outside the project venv

---

## 2. Scope check (judgment call — apply when the hard-rejects all pass)

For each proposed code action, Guard asks:

1. **Does the code's apparent purpose match the user's intent for this
   turn?** If the admin asked "moderate issue 42" and the code is
   reading `.imp/issues.json`, that's plausibly on-task. If the code
   touches issue 99 or PR 17, it's drifted.

2. **Does it touch issues / PRs / files the admin didn't mention?**
   Bias toward reject when in doubt — Foreman can revise with a
   narrower scope.

3. **Is the side effect proportional to the request?** Reading data is
   cheap; writing files in `.imp/output/` is fine; writing files at the
   repo root or under `99-tools/` is suspicious unless the admin
   explicitly asked.

A scope-check rejection isn't "this is malicious" — it's "this is
broader than what was asked for." Cite as
`"scope: code touches X but admin asked about Y"`.

---

## 3. Obfuscation flags (raise the rejection bar)

Treat these as evidence the code is hiding something — apply the
hard-reject and scope-check sections more strictly when present:

- Long base64 string literals (>60 chars) decoded into something
  executable
- Long hex string literals (`bytes.fromhex(...)`)
- `chr()` / `ord()` chains that build identifiers (`chr(115)+chr(104)` →
  `"sh"`)
- `getattr(obj, "x" + "yz")` — name built from string concatenation
- `__getattribute__` / `__setattr__` indirection on builtins
- Reflective module loading via string keys
- Embedded zlib / gzip / lzma blobs decoded to source

If the obfuscation is justified by the user's intent (e.g. admin asked
to "decode this base64 and show me what it is"), it's fine — the test
is whether the code's intent is HIDDEN from a human reader.

---

## 4. Commonly fine (for Guard's calibration)

These patterns appear all the time and are typically benign — Guard
shouldn't flag them unless something else trips:

- Reading `.imp/*.json` (sync / heuristics output)
- Writing under `.imp/output/*` or `.imp/sandbox/*`
- Standard library reads: `json`, `re`, `datetime`, `pathlib`,
  `collections`, `dataclasses`, `itertools`, `functools`
- Computation: `statistics`, `math`, `decimal`, `fractions`
- Text wrangling: `csv`, `string`, `textwrap`, `unicodedata`
- Iteration helpers: `enumerate`, `zip`, `sorted`, `filter`, `map`
- `print()` / writing to stdout/stderr (subprocess output is captured
  by `intercept.py` and shown to the admin anyway)
- `pathlib.Path.read_text()` / `write_text()` confined to `.imp/`

If 100% of the code is in this list and the scope-check passes, approve
without further analysis.

---

## 5. Verdict format

Guard returns a single JSON object, no prose around it:

```json
{"verdict": "approve", "reason": "<short explanation>"}
```

or

```json
{"verdict": "reject", "reason": "hard-reject — <rule>: <evidence>"}
```

Reasons are capped at ~200 characters. On reject, the rule citation is
mandatory so the admin sees which checklist item failed and the worker
agent can revise its proposal.
