# OCI Intake Application — Local Setup

Getting this app running on a Mac that has never seen it before. The app is a
self-contained Python web server: no database, no Node build, no Docker. Clone,
install some Python packages, run one command.

Budget about 15 minutes, most of it waiting on downloads.

---

## 0. What you're installing, and why

Only two things are genuinely required: **git** (to get the code) and **Python
3.10 or newer** (to run it; 3.12 is the safe pick). macOS ships neither in a form you should rely on —
the built-in `python3` is whatever Apple last shipped, and on older machines
that can be too old.

Homebrew is the easiest way to get current versions of both, which is why it's
Step 1. If you already have Homebrew, or you already have git and Python 3.10+
from somewhere else (pyenv, python.org, Xcode Command Line Tools), skip to
Step 2 — nothing here depends on Homebrew specifically.

Everything the app needs beyond that is a normal pip package. **There are no
native libraries to install** — no Cairo, no Postgres, no image toolchains.

---

## 1. Install Homebrew

Paste this into Terminal:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

It will ask for your Mac password (it's installing to a system location) and may
ask you to press Return once to confirm.

**On Apple Silicon (M1/M2/M3/M4), the installer finishes by telling you to run
two extra commands to put `brew` on your PATH.** Do not skip that — it looks
like decoration and it isn't. It's usually:

```bash
echo >> ~/.zprofile
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

Confirm it worked:

```bash
brew --version
```

If that says `command not found`, the PATH step above didn't happen. Run it,
or open a new Terminal window and try again.

---

## 2. Install git and Python

```bash
brew install git python@3.12
```

Confirm both:

```bash
git --version      # any recent version is fine
python3 --version  # must be 3.10 or newer
```

> **Why 3.10+?** That's the range the app is actually exercised on — every
> module imports and the test suite passes on 3.10 and 3.12. Older versions are
> untested rather than known-broken. On 3.13+ Python removed the `cgi` module
> the upload handler uses, which is already handled: `requirements.txt` pulls in
> the `legacy-cgi` backport automatically on those versions. Nothing manual
> either way, at either end.

---

## 3. Get access to the repository

The repo lives at **https://github.com/Chris-Wegenek/oci-intake-app**.

If it's private, the person cloning needs to be added as a collaborator on
GitHub first, and needs to authenticate. The least painful way:

```bash
brew install gh
gh auth login
```

Choose *GitHub.com* → *HTTPS* → *Login with a web browser*, and follow the
prompts. This stores credentials so `git clone` and `git pull` just work.

If the repo is public, skip this step entirely.

---

## 4. Clone the repo

```bash
cd ~/Documents
git clone https://github.com/Chris-Wegenek/oci-intake-app.git
cd oci-intake-app
```

`git clone` checks out `main` for you, which is the branch to use — there is no
second checkout step.

Confirm you got current code:

```bash
git log --oneline -3
```

You should see recent dates. If the newest commit is from July 2026 or earlier
and someone has told you there are newer fixes, `main` hasn't been updated yet —
go back to whoever sent you here rather than working around it.

---

## 5. Create a virtual environment

This isolates the app's packages, and avoids macOS's
`error: externally-managed-environment` from pip.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt now starts with `(.venv)`. That prefix is how you know the next
step will install into the right place.

---

## 6. Install the Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

That's `pandas`, `openpyxl`, `xlrd`, `lxml`, `pypdf`, `pillow`, `boto3`, and on
Python 3.13+ `legacy-cgi`. Give it a minute — pandas is a large wheel.

> The app also self-heals: `bootstrap.py` checks its own imports at startup and
> pip-installs anything missing before the app loads. So a forgotten package
> usually fixes itself on first run. Set `OCI_APP_NO_BOOTSTRAP=1` to turn that
> off on a locked-down or offline machine.

---

## 7. (Optional) Add an OpenAI key

The app runs fully without one — pricing, SKU mapping, BOM math and exports are
all deterministic and never touch OpenAI. A key only enables two assists:
messy-spreadsheet column mapping on upload, and the architecture-plan draft.
Both have deterministic fallbacks.

To enable them:

```bash
cp .env.example .env.local
```

Then open `.env.local` and replace the placeholder with a real key. The app
reads this file itself on startup — you do not need to export anything, and
`.env.local` is git-ignored so the key can't be committed by accident.

---

## 8. Run it

```bash
python3 app.py
```

You'll see:

```
OCI Intake app running at http://127.0.0.1:8787
```

Open **http://127.0.0.1:8787** in a browser.

- Stop the server with **Ctrl+C**.
- Different port: `PORT=9000 python3 app.py`
- The server binds to `127.0.0.1` only, so it is reachable from that Mac and
  nowhere else. That's deliberate — it isn't hardened for exposure to a network.

---

## 9. Confirm it actually works

Upload something. Any of these paths is a good smoke test:

- **Other OCI Bill** → upload an Oracle Cost Estimator `.xlsx` export. The
  imported total should match the sheet's own "Monthly Total" to the cent.
- **On-prem inventory** → upload a server inventory workbook.
- **Cloud bill** → upload an AWS/Azure/GCP cost export.

To run the test suite instead:

```bash
python3 scripts/test_bom_db_mapping.py
python3 scripts/test_cloud_shape_catalog.py
python3 scripts/test_object_storage_tiers.py
python3 scripts/test_export_parity.py
```

> Two scripts fail for environmental reasons rather than broken code:
> `test_ai_assists.py` needs an OpenAI key, and `test_cross_cloud.py` wants a
> large AWS bill fixture that isn't committed to the repo.

---

## Running it again tomorrow

```bash
cd ~/Documents/oci-intake-app
source .venv/bin/activate
git pull origin main
python3 app.py
```

The venv step is the one people forget. If you get
`ModuleNotFoundError: No module named 'pandas'`, that's what happened.

## Getting later changes

```bash
git pull                          # you are on main; nothing else to switch to
pip install -r requirements.txt   # only needed if requirements.txt changed
```

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `brew: command not found` | The PATH line from the Homebrew installer wasn't run. See the end of Step 1, or open a new Terminal. |
| `Repository not found` on clone | Private repo and you're not authenticated or not a collaborator. Do Step 3, and check the invite. |
| `error: externally-managed-environment` | pip outside a venv. Activate it (Step 5), or add `--break-system-packages`. |
| `ModuleNotFoundError: No module named 'pandas'` | The venv isn't active. `source .venv/bin/activate`. |
| `ModuleNotFoundError: No module named 'cgi'` | Python 3.13+ without `legacy-cgi`. Re-run `pip install -r requirements.txt`. |
| `Address already in use` | Port 8787 is taken, often by an older copy of this app. `PORT=9000 python3 app.py`, or quit the other one. |
| `command not found: python` | On macOS it's `python3`. |
| Edits to `.py` files do nothing | Restart the server — there's no auto-reload. |
| Edits to the UI do nothing | Hard-refresh the browser: **Cmd+Shift+R**. `app.js` caches aggressively. |

---

## What's in the repo

- `app.py` — the web server, pricing engine, and SKU mapping
- `bom_convert.py` — imports an existing OCI BOM or Cost Estimator export
- `bom_export.py` / `bom_template.py` — the Excel deliverables
- `bom_diagram.py` — the architecture diagram
- `oci_catalog.py` — the OCI service catalog and per-service cost cards
- `data/` — OCI price list, estimator snapshot, AWS/Azure/GCP mappings
- `static/` — the web UI (`index.html`, `app.js`, `styles.css`)
- `scripts/` — the test suite
- `requirements.txt` — Python dependencies
