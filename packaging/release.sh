#!/usr/bin/env bash
# Cut a yapcode release and stamp the Homebrew formula.
#
#   packaging/release.sh v0.1.0
#
# What it does:
#   1. Verifies a clean working tree.
#   2. Creates + pushes an annotated git tag (skips if it already exists).
#   3. Downloads the exact tarball GitHub will serve to `brew` and computes its
#      sha256.
#   4. Stamps `url` and `sha256` into packaging/yapcode.rb.
#
# After it runs, copy packaging/yapcode.rb into the tap repo
# (github.com/nithiink/homebrew-yapcode -> Formula/yapcode.rb) and push.
#
# Note: the repo must be PUBLIC for the tarball fetch (and ultimately `brew
# install`) to work — a private repo returns 404 to anonymous HTTPS.
set -euo pipefail

VERSION="${1:-}"
[ -n "$VERSION" ] || { echo "usage: $0 vX.Y.Z" >&2; exit 2; }
case "$VERSION" in
  v[0-9]*) ;;
  *) echo "version must look like v0.1.0 (got '$VERSION')" >&2; exit 2 ;;
esac

REPO_SLUG="${YAPCODE_REPO:-nithiink/yapcode}"
OWNER="${REPO_SLUG%%/*}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORMULA="$ROOT/packaging/yapcode.rb"
[ -f "$FORMULA" ] || { echo "formula not found at $FORMULA" >&2; exit 1; }

# 1. clean tree
if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  echo "working tree is not clean — commit or stash first" >&2; exit 1
fi
branch="$(git -C "$ROOT" branch --show-current)"
[ "$branch" = "main" ] || echo "warning: releasing from '$branch', not main" >&2

# 2. tag + push (idempotent)
if git -C "$ROOT" rev-parse -q --verify "refs/tags/$VERSION" >/dev/null; then
  echo "tag $VERSION already exists — reusing it" >&2
else
  git -C "$ROOT" tag -a "$VERSION" -m "yapcode $VERSION"
  git -C "$ROOT" push origin "$VERSION"
fi

# 3. fetch the release tarball and hash it
URL="https://github.com/$REPO_SLUG/archive/refs/tags/$VERSION.tar.gz"
echo "fetching $URL …" >&2
tarball="$(mktemp)"
trap 'rm -f "$tarball"' EXIT
if ! curl -fsSL "$URL" -o "$tarball"; then
  echo "could not download $URL — is the repo public and the tag pushed?" >&2
  exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
  SHA="$(sha256sum "$tarball" | awk '{print $1}')"
else
  SHA="$(shasum -a 256 "$tarball" | awk '{print $1}')"
fi
echo "sha256 = $SHA" >&2

# 4. stamp url + sha256 into the formula (| delimiter avoids escaping the URL).
#    Both patterns replace to end-of-line so any trailing placeholder comment is
#    dropped on stamp.
tmp="$(mktemp)"
sed -E \
  -e "s|^([[:space:]]*url ).*|\1\"$URL\"|" \
  -e "s|^([[:space:]]*sha256 ).*|\1\"$SHA\"|" \
  "$FORMULA" > "$tmp"
mv "$tmp" "$FORMULA"
echo "stamped $FORMULA" >&2

cat >&2 <<EOF

Done.
  1. Review:  git -C "$ROOT" diff -- packaging/yapcode.rb
  2. Copy packaging/yapcode.rb to the tap repo
     (github.com/$OWNER/homebrew-yapcode) at Formula/yapcode.rb,
     commit, and push.
  3. Users then install with:
       brew tap $OWNER/yapcode
       brew install yapcode
EOF
