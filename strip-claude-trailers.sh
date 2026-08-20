#!/usr/bin/env bash
# Remove the "Co-Authored-By: Claude" / "Claude-Session:" trailers from every commit on
# this branch that carries them, so GitHub stops crediting a `claude` contributor.
#
# Run this from Terminal, in the repo, on your own machine:
#     cd "$HOME/Documents/HPC Lab/oci-intake-app"
#     bash strip-claude-trailers.sh
#
# It rewrites commit messages only. File contents, authors, and author dates are all
# untouched. Every commit from the first rewritten one forward gets a NEW hash, which is
# why the push at the end has to be a force-push.
#
# Nothing is pushed by this script. It stops and prints the push command for you to run.

set -euo pipefail

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# Rewrite only the commits on this branch that aren't already on BASE.
#
# The default (origin/main) is right when cleaning a feature branch, and WRONG when the
# branch you need to clean IS main - "commits on main not already on main" is nothing, so
# the script would report success having rewritten zero commits. That is exactly how main
# kept 24 tainted commits after this script cleaned Chris'-Branch.
#
# Override it for that case. `auto` picks the parent of the earliest tainted commit on the
# current branch, which is the correct starting point whatever branch you are on:
#
#     BASE=auto bash strip-claude-trailers.sh
#
BASE="${BASE:-origin/main}"
if [ "$BASE" = "auto" ]; then
  EARLIEST="$(git log "$BRANCH" -i --grep='Co-Authored-By: Claude' --format='%H' | tail -1)"
  if [ -z "$EARLIEST" ]; then
    echo "Nothing to do - no Claude trailers on $BRANCH."
    exit 0
  fi
  BASE="$EARLIEST^"
  echo "BASE=auto resolved to $(git rev-parse --short "$BASE") (parent of the earliest tainted commit)"
fi

echo "Repo:   $(git rev-parse --show-toplevel)"
echo "Branch: $BRANCH"

# --- Safety checks -----------------------------------------------------------------
if [ -n "$(git status --porcelain)" ]; then
  echo
  echo "You have uncommitted changes. Stash or commit them first:"
  echo "    git stash push -u -m 'before trailer rewrite'"
  echo "    bash strip-claude-trailers.sh"
  echo "    git stash pop"
  exit 1
fi

BEFORE_COMMITS="$(git rev-list --count "$BRANCH")"
BEFORE_TRAILERS="$(git log "$BRANCH" --format='%b' | grep -ci 'co-authored-by: claude' || true)"
echo "Commits on branch: $BEFORE_COMMITS"
echo "Commits carrying a Claude trailer: $BEFORE_TRAILERS"
if [ "$BEFORE_TRAILERS" -eq 0 ]; then
  echo "Nothing to do."
  exit 0
fi

# A tag to roll back to, in case anything looks wrong afterwards.
BACKUP="backup/pre-trailer-rewrite-$(date +%Y%m%d-%H%M%S)"
git tag "$BACKUP" "$BRANCH"
echo "Rollback point tagged: $BACKUP"

# --- The filter --------------------------------------------------------------------
# Drops only the two Claude lines and any blank lines they left trailing. Every other
# byte of every message is preserved.
FILTER="$(mktemp "${TMPDIR:-/tmp}/strip_claude.XXXXXX")"
cat > "$FILTER" <<'PY'
import sys
msg = sys.stdin.read()
kept = [
    line for line in msg.split("\n")
    if not line.startswith("Co-Authored-By: Claude")
    and not line.startswith("Claude-Session:")
]
sys.stdout.write("\n".join(kept).rstrip("\n") + "\n")
PY

echo
echo "Rewriting..."
FILTER_BRANCH_SQUELCH_WARNING=1 \
  git filter-branch -f --msg-filter "python3 $FILTER" -- "$BASE..$BRANCH"

rm -f "$FILTER"

# --- Verify ------------------------------------------------------------------------
AFTER_COMMITS="$(git rev-list --count "$BRANCH")"
AFTER_TRAILERS="$(git log "$BRANCH" --format='%b' | grep -ci 'co-authored-by: claude' || true)"
echo
echo "Commits on branch: $BEFORE_COMMITS -> $AFTER_COMMITS   (must be equal)"
echo "Claude trailers:   $BEFORE_TRAILERS -> $AFTER_TRAILERS   (must be 0)"

# The working tree must be byte-identical to before: messages changed, content did not.
if [ "$(git rev-parse "$BRANCH^{tree}")" = "$(git rev-parse "$BACKUP^{tree}")" ]; then
  echo "Tree hash unchanged: file contents are identical to before."
else
  echo "WARNING: tree hash changed - do NOT push. Roll back with:"
  echo "    git reset --hard $BACKUP"
  exit 1
fi

if [ "$AFTER_COMMITS" -ne "$BEFORE_COMMITS" ] || [ "$AFTER_TRAILERS" -ne 0 ]; then
  echo "WARNING: counts are wrong - do NOT push. Roll back with:"
  echo "    git reset --hard $BACKUP"
  exit 1
fi

cat <<EOF

Local rewrite is clean. Nothing has been pushed.

Review a rewritten message:
    git log -1 --format=%B \$(git log --format=%H -n1 --grep='Stop database lines')

When you're happy, force-push (this rewrites the branch on GitHub):
    git push --force-with-lease origin "$BRANCH"

To undo everything instead:
    git reset --hard $BACKUP

Once pushed, GitHub's contributor graph takes up to ~24 hours to drop the claude card.
EOF
