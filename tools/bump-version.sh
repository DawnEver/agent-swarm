#!/usr/bin/env bash
# Mint and publish the next version tag on every push of the default branch.
#
# The version is derived from git (pyproject: [tool.hatch.version] source = "vcs"), so "bumping the
# version" IS "creating the next tag". There is nowhere else a version could be written.
#
# THE FAILURE MODE THIS REFUSES, and it is the reason this script is not a copy of motronics'
# `bump-version.sh`. That one is best-effort by construction: every path exits 0 so tagging can never
# block a code push, and a failed `git push refs/tags/...` prints "will retry next push" and lets the
# push through. The shape is the one this project names as forbidden -- a guard reporting into a
# stdout nobody reads is indistinguishable from no guard, and pre-commit DISCARDS a passing hook's
# output. So on that design the observable outcome of "the tag could not be created" and "the tag was
# created and published" is the same: a successful push. The version silently does not move, which
# is precisely the defect the SCM versioning was adopted to fix.
#
# Here, a bump that does not happen FAILS THE PUSH, on stderr, naming why. That is a real tradeoff
# and it is taken deliberately: an unpublishable tag means the next reinstall of this package cannot
# tell the new commit from the old one, and shipping that is worse than a blocked push whose remedy
# is printed.
#
# ATOMIC WITH THE BRANCH, not before it. Publishing the tag on its own would leave a tag pointing at
# a commit no branch reaches if the branch push were then rejected -- motronics accepts that orphan
# explicitly. `--push --atomic <branch> <tag>` lands both refs or neither, so there is no state where
# the tag exists remotely and the code does not. The outer push that triggered this hook then finds
# its branch already up to date and succeeds as a no-op.
#
# `--no-verify` on the inner push: without it this hook re-enters itself.
set -uo pipefail

# pre-commit exports the push's real remote; the positional is for tests, which drive this script
# against a local bare repo. Both name the SAME thing, so neither can be silently wrong about it.
REMOTE="${1:-${PRE_COMMIT_REMOTE_NAME:-origin}}"

die() {
  echo "bump-version: REFUSED -- $1" >&2
  shift
  for line in "$@"; do echo "  ${line}" >&2; done
  exit 1
}

# Only the default branch carries releases: a topic-branch push must not mint a release tag. This is
# a legitimate no-op rather than a failure, and it still says so -- on stderr, which pre-commit shows
# even for a passing hook, so "skipped" and "ran" are never confused.
DEFAULT_BRANCH=$(git symbolic-ref --quiet --short "refs/remotes/${REMOTE}/HEAD" 2>/dev/null | sed "s@^${REMOTE}/@@")
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "${CURRENT_BRANCH}" != "${DEFAULT_BRANCH}" ]; then
  echo "bump-version: on '${CURRENT_BRANCH}', not the release branch '${DEFAULT_BRANCH}' -- no tag minted." >&2
  echo "  Builds off this branch spell themselves <next>.dev<N>+g<sha>, which is already unique." >&2
  exit 0
fi

# Refresh remote tags so the highest-tag computation and the collision scan both see a tag another
# machine already pushed. A failure here is NOT fatal by itself -- the atomic publish below is what
# actually decides, and it will refuse a stale or colliding tag on its own.
git fetch --quiet --tags "${REMOTE}" >/dev/null 2>&1 || true

publish() {
  # AN OPERATION'S EXIT IS NOT ITS EFFECT: the push is checked, and then the REMOTE is read back.
  local tag="$1"
  if ! git push --no-verify --atomic "${REMOTE}" "refs/heads/${CURRENT_BRANCH}" "refs/tags/${tag}" >/dev/null 2>&1; then
    return 1
  fi
  git ls-remote --tags "${REMOTE}" "refs/tags/${tag}" 2>/dev/null | grep -q "refs/tags/${tag}"
}

# HEAD already tagged: do not mint a second version for one commit, but do make sure the tag reached
# the remote -- an unpublished tag is a version that exists only on this machine.
EXISTING=$(git tag --points-at HEAD --list 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null | head -1)
if [ -n "${EXISTING}" ]; then
  if publish "${EXISTING}"; then
    echo "bump-version: HEAD already tagged ${EXISTING}; published." >&2
    exit 0
  fi
  die "HEAD is tagged ${EXISTING} but that tag is not on '${REMOTE}' and could not be pushed." \
    "The installed version of this commit would be indistinguishable from another commit's." \
    "Fix the remote, then: git push --atomic ${REMOTE} ${CURRENT_BRANCH} refs/tags/${EXISTING}"
fi

# Highest plain-semver tag by NUMERIC field order. `git tag --sort=-version:refname` depends on a
# recent git plus versionsort config and mis-orders multi-digit patches on older git-bash installs,
# so the three fields are sorted here instead -- portable across every git that has `git tag`.
LATEST=$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null \
  | sed 's/^v//' \
  | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' \
  | sort -t. -k1,1n -k2,2n -k3,3n \
  | tail -1)

if [ -z "${LATEST}" ]; then
  # THE FIRST TAG THIS REPO EVER GETS. 0.1.0, not 0.0.1: the hardcoded string this mechanism
  # replaced was 0.1.0, and starting below it would make the first derived version rank lower than
  # the last hand-written one. Documented in pyproject.toml beside [tool.hatch.version].
  NEW_TAG='v0.1.0'
else
  MAJOR="${LATEST%%.*}"
  REST="${LATEST#*.}"
  MINOR="${REST%%.*}"
  PATCH="${REST#*.}"
  # Increment ONCE and recompute the candidate each iteration, so a collision scan walks consecutive
  # versions and cannot skip one.
  PATCH=$((PATCH + 1))
  NEW_TAG="v${MAJOR}.${MINOR}.${PATCH}"
  while git rev-parse -q --verify "refs/tags/${NEW_TAG}" >/dev/null 2>&1; do
    PATCH=$((PATCH + 1))
    NEW_TAG="v${MAJOR}.${MINOR}.${PATCH}"
  done
fi

if ! git tag -a "${NEW_TAG}" -m "Release ${NEW_TAG}" >/dev/null 2>&1; then
  die "could not create the tag ${NEW_TAG} locally." \
    "Without it this push ships a commit whose version string equals the previous commit's."
fi

if publish "${NEW_TAG}"; then
  echo "bump-version: tagged and published ${NEW_TAG}" >&2
  exit 0
fi

# NO RESIDUE ON FAILURE. A local-only tag would make the NEXT push think this version was already
# released and skip straight past it, so the version that could not be published is removed.
git tag -d "${NEW_TAG}" >/dev/null 2>&1
die "minted ${NEW_TAG} but could not publish it to '${REMOTE}'; the local tag was removed." \
  "The push is blocked because it would otherwise ship a commit that reinstalls as the previous" \
  "version -- byte-identical, against different code." \
  "Check connectivity and permissions, then push again."
