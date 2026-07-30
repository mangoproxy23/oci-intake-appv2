# OCI BOM Creator — Setup & Run Guide

A step-by-step guide to run this app locally on a Mac, from a fresh machine.

The app is a self-contained Python web server (no database, no Node build). You
clone the repo, install a few Python packages, and run one command.

---

## 1. Prerequisites

**Xcode Command Line Tools** (gives you `git` and a C compiler). If you don't
have them yet:

```bash
xcode-select --install
```

That's the only system prerequisite — macOS already includes Python 3.

> Tip: confirm the basics are available:
> ```bash
> git --version
> python3 --version
> ```

---

## 2. Clone the repository

```bash
git clone https://github.com/mangoproxy23/oci-intake-appv2.git
cd oci-intake-appv2
git checkout main
```

---

## 3. Create a virtual environment

This keeps the app's packages isolated and avoids macOS's
`error: externally-managed-environment` from pip.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt will now start with `(.venv)`.

---

## 4. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This installs `pandas`, `openpyxl`, `pypdf`, `boto3`, and `legacy-cgi`.

> **Why `legacy-cgi`?** The app uses Python's `cgi` module for file uploads.
> `cgi` was removed in Python 3.13, so `requirements.txt` pulls in the
> `legacy-cgi` backport automatically on 3.13+. No manual step needed.

---

## 5. Run the app

```bash
python3 app.py
```

Then open **http://localhost:8787** in your browser.

- Stop the server with **Ctrl+C**.
- Use a different port: `PORT=9000 python3 app.py` → http://localhost:9000

---

## Running it again later

```bash
cd oci-intake-app
source .venv/bin/activate
python3 app.py
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'cgi'` | You skipped the install or aren't in the venv. Run `pip install -r requirements.txt` (it includes `legacy-cgi`). |
| `error: externally-managed-environment` from pip | You're installing outside a venv. Either use the venv (Step 3) or add `--break-system-packages` to the pip command. |
| `ModuleNotFoundError: No module named 'pandas'` (or pypdf/openpyxl/boto3) | Dependencies not installed — re-run Step 4 with the venv active. |
| `command not found: python` | On macOS it's `python3`, not `python`. |
| `Address already in use` (port 8787) | A server is already running. Use `PORT=9000 python3 app.py`, or stop the other one. |
| `git` / compiler errors | Install the Command Line Tools: `xcode-select --install`. |

---

## What's in the repo

- `app.py` — the web server and pricing/mapping engine
- `bom_export.py` — Excel export (BOM, Product Breakdown, Service Mapping, Overview)
- `aws_pricing.py` — live AWS Price List lookups (optional; needs boto3 + AWS creds)
- `data/` — OCI price list, service prices, and AWS→OCI service mappings
- `static/` — the web UI (HTML/CSS/JS)
- `requirements.txt` — Python dependencies
