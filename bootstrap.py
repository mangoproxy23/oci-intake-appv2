"""Make the repo self-sufficient: install its own dependencies on startup.

Why this exists
---------------
The Full BOM export died in the wild with "You must install Pillow to fetch image
objects". Pillow wasn't in requirements.txt, so nobody's virtualenv had it, and openpyxl
raises that ImportError from deep inside save() - the export failed on the decorative
Oracle logo before it ever reached the numbers. The dependency was invisible until it
broke, and the fix ("go run pip install") was something a user of this app should never
have to know about.

So: check every module the app actually imports, and install anything missing into the
running interpreter before the app imports it. Uses only the standard library, because it
must run before any third-party import succeeds.

Set OCI_APP_NO_BOOTSTRAP=1 to skip (e.g. in a locked-down or offline deployment).
"""

import importlib
import importlib.util
import os
import subprocess
import sys

# (importable module name, pip requirement) - the module name is what we actually check,
# because "pillow" installs as "PIL" and a missing one is only discovered at import time.
REQUIREMENTS = [
    ("pandas", "pandas"),
    ("openpyxl", "openpyxl"),
    ("PIL", "pillow"),          # nice-to-have; embedding now works without it too
    # NOTE: pycairo is intentionally NOT auto-installed. It compiles against system cairo
    # (brew install cairo / apt install libcairo2-dev) and a bare `pip install pycairo`
    # hangs or fails on machines without those libs - which would block app startup. It's
    # optional: without it the .drawio diagram still builds, only the embedded PNG is
    # skipped. Install it yourself to get the rendered diagram (see requirements.txt).
    ("xlrd", "xlrd"),
    ("pypdf", "pypdf"),
    ("boto3", "boto3"),
]
if sys.version_info >= (3, 13):
    REQUIREMENTS.append(("cgi", "legacy-cgi"))   # cgi was removed from the stdlib in 3.13


def _missing():
    out = []
    for module, package in REQUIREMENTS:
        try:
            if importlib.util.find_spec(module) is None:
                out.append((module, package))
        except (ImportError, ValueError):
            out.append((module, package))
    return out


def ensure(quiet=False):
    """Install any dependency the app needs but the environment doesn't have.

    Returns the list of packages installed (empty when nothing was needed).
    """
    if os.environ.get("OCI_APP_NO_BOOTSTRAP"):
        return []

    missing = _missing()
    if not missing:
        return []

    packages = [pkg for _, pkg in missing]
    if not quiet:
        print(f"[setup] missing dependencies: {', '.join(packages)}", flush=True)
        print(f"[setup] installing into {sys.executable} ...", flush=True)

    # Fail fast. If there's no network, pip's default retry/backoff hangs startup for a
    # minute per package - the server must always come up, installed or not.
    base = [sys.executable, "-m", "pip", "install", "--retries", "1", "--timeout", "10",
            "--disable-pip-version-check", *packages]
    # Debian/Ubuntu system Pythons refuse to install without this; harmless elsewhere.
    attempts = [base + ["--break-system-packages"], base] if sys.version_info >= (3, 11) else [base]

    proc = None
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except Exception as exc:                  # no pip, sandboxed, killed, ...
            _warn(packages, str(exc))
            return []
        if proc.returncode == 0:
            break

    if proc is None or proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() if proc else "pip did not run"
        # pip's output is a wall of retry noise; the last real ERROR line is the useful bit.
        errs = [ln for ln in detail.splitlines() if ln.startswith("ERROR")]
        _warn(packages, errs[-1] if errs else detail[-300:])
        return []

    # The new packages landed on disk after this process started - let import see them.
    importlib.invalidate_caches()
    still = [p for _, p in _missing()]
    if still:
        _warn(still, "pip reported success but the module is still not importable")
        return []

    if not quiet:
        print(f"[setup] installed: {', '.join(packages)}", flush=True)
    return packages


def _warn(packages, detail):
    print("=" * 72, file=sys.stderr)
    print(f"[setup] COULD NOT INSTALL: {', '.join(packages)}", file=sys.stderr)
    print(f"[setup] {detail}", file=sys.stderr)
    print(f"[setup] Install them yourself, then restart:", file=sys.stderr)
    print(f"[setup]     {sys.executable} -m pip install {' '.join(packages)}", file=sys.stderr)
    print("[setup] The app will still start; features needing these may fail.", file=sys.stderr)
    print("=" * 72, file=sys.stderr)


CATALOG_MAX_AGE_DAYS = 7


def catalog_status():
    """How old the Oracle SKU catalog is. Returns {version, refreshedUtc, ageDays, stale}."""
    import datetime
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent / "data" / "oci_price_list.json"
    out = {"version": "", "refreshedUtc": "", "ageDays": None, "stale": True,
           "skus": 0, "maxAgeDays": CATALOG_MAX_AGE_DAYS}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return out
    out["skus"] = len(data.get("items") or [])
    out["version"] = str(data.get("oracleCatalogVersion") or "")
    stamp = str(data.get("oracleCatalogRefreshedUtc") or "")
    out["refreshedUtc"] = stamp
    if not stamp:
        return out                      # never refreshed - treat as stale, but don't crash
    try:
        when = datetime.datetime.fromisoformat(stamp)
        age = (datetime.datetime.utcnow() - when).total_seconds() / 86400.0
        out["ageDays"] = round(age, 2)
        out["stale"] = age > CATALOG_MAX_AGE_DAYS
    except Exception:
        pass
    return out


def refresh_catalog_if_stale(quiet=False):
    """Pull a fresh Oracle SKU catalog when the local one is over a week old.

    Runs on a BACKGROUND thread so startup is never blocked by a network call, and a failure is
    logged and swallowed - a stale price list still prices correctly, it just misses SKUs Oracle
    published this week, whereas a refresh that hangs the app on a bad connection is a worse
    outcome than slightly old data.

    This is what stops the catalog silently rotting. A cron entry works too, but it has to be
    installed on every machine that runs the app and nothing reminds you when it isn't; checking
    at startup means the app maintains itself wherever it happens to be running.

    Set OCI_APP_NO_CATALOG_REFRESH=1 to disable (offline or locked-down deployments).
    """
    import threading
    if os.environ.get("OCI_APP_NO_CATALOG_REFRESH") == "1":
        return None
    status = catalog_status()
    if not status["stale"]:
        return status

    def _run():
        import subprocess
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "scripts", "refresh_oracle_catalog.py")
        try:
            subprocess.run([sys.executable, script, "--quiet"], timeout=180,
                           capture_output=True, check=False)
        except Exception:
            pass                        # offline is fine; the existing catalog still works

    age = "never refreshed" if status["ageDays"] is None else f"{status['ageDays']:.0f} days old"
    if not quiet:
        print(f"[catalog] Oracle SKU catalog is {age} - refreshing in the background")
    threading.Thread(target=_run, daemon=True, name="oracle-catalog-refresh").start()
    return status


if __name__ == "__main__":
    installed = ensure()
    still_missing = [pkg for _, pkg in _missing()]
    if still_missing:
        sys.exit(1)                                   # _warn() already explained why
    if installed:
        print(f"[setup] installed {len(installed)} package(s)")
    else:
        print("[setup] all dependencies present")
