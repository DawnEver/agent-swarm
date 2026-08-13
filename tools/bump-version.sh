#!/usr/bin/env bash
# Mint and publish the next version tag on every push that ADVANCES the default branch.
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
#
# WHAT DECIDES: THE REMOTE REF, NOT THE LOCAL BRANCH. git feeds a pre-push hook one line per ref on
# stdin -- `<local ref> <local sha> <remote ref> <remote sha>` -- and pre-commit passes that stdin
# through to `entry`. `refs/heads/<default>` being updated IS the release, whatever the pusher
# happens to have checked out. Deciding on `git rev-parse --abbrev-ref HEAD` instead asks a
# DIFFERENT question that agrees only in the common case: it is right for someone sitting on the
# default branch pushing it, and silently inert for an integrator running
# `git push origin <topic>:main`, which is exactly the workflow this project prescribes -- every
# producer emits a Submission and one integrator advances the trunk. Three such pushes reported
# "Passed" while the remote gained zero tags.
#
# It also fixes WHICH COMMIT is tagged. The version belongs to the commit the trunk is being moved
# to -- the local sha on that stdin line -- and HEAD need not be it.
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

# A no-op is a legitimate outcome, but it must be READABLE. It says what it declined and why, on
# stderr, and the hook is declared `verbose: true` in .pre-commit-config.yaml so pre-commit prints
# that output on the PASSING path too -- otherwise "correctly declined" and "did nothing" arrive at
# the operator as the same blank line, which is the shape this project refuses.
decline() {
  echo "bump-version: no tag minted -- $1" >&2
  shift
  for line in "$@"; do echo "  ${line}" >&2; done
  exit 0
}

# Only the default branch carries releases.
DEFAULT_BRANCH=$(git symbolic-ref --quiet --short "refs/remotes/${REMOTE}/HEAD" 2>/dev/null | sed "s@^${REMOTE}/@@")
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
RELEASE_REF="refs/heads/${DEFAULT_BRANCH}"

# THE REF LINES. A push may carry several at once; only the one updating the release ref decides, and
# the rest are irrelevant rather than disqualifying. A deletion arrives with an all-zero LOCAL sha
# (and `(delete)` as the local ref): the trunk is being removed, so there is no commit to version.
# `[ ! -t 0 ]` because git and pre-commit always hand this a pipe; a TTY means the script was invoked
# by hand with nothing being pushed, and reading would block forever rather than answer.
TARGET_SHA=''
TARGET_LOCAL_REF=''
DELETION_SEEN=''
SEEN_REFS=''
if [ ! -t 0 ]; then
  while read -r local_ref local_sha remote_ref _remote_sha; do
    [ -n "${remote_ref}" ] || continue
    SEEN_REFS="${SEEN_REFS}${SEEN_REFS:+, }${remote_ref}"
    [ "${remote_ref}" = "${RELEASE_REF}" ] || continue
    case "${local_sha}" in
      *[!0]*) TARGET_SHA="${local_sha}" ; TARGET_LOCAL_REF="${local_ref}" ;;
      *) DELETION_SEEN='yes' ;;
    esac
  done
fi

if [ -n "${DELETION_SEEN}" ] && [ -z "${TARGET_SHA}" ]; then
  decline "this push DELETES '${RELEASE_REF}' on '${REMOTE}'." \
    "There is no commit to version, so minting a tag here would point a release at nothing."
fi

if [ -z "${TARGET_SHA}" ]; then
  decline "this push updates no '${RELEASE_REF}' on '${REMOTE}'." \
    "It updates: ${SEEN_REFS:-<no refs on stdin>}" \
    "Builds off any other ref spell themselves <next>.dev<N>+g<sha>, which is already unique."
fi

# Refresh remote tags so the highest-tag computation and the collision scan both see a tag another
# machine already pushed. A failure here is NOT fatal by itself -- the atomic publish below is what
# actually decides, and it will refuse a stale or colliding tag on its own.
git fetch --quiet --tags "${REMOTE}" >/dev/null 2>&1 || true

publish() {
  # AN OPERATION'S EXIT IS NOT ITS EFFECT: the push is checked, and then the REMOTE is read back.
  # The branch half mirrors the OUTER push exactly -- the same sha onto the same remote ref -- so
  # the outer push then finds it already up to date and succeeds as a no-op.
  local tag="$1"
  if ! git push --no-verify --atomic "${REMOTE}" "${TARGET_SHA}:${RELEASE_REF}" "refs/tags/${tag}" >/dev/null 2>&1; then
    return 1
  fi
  git ls-remote --tags "${REMOTE}" "refs/tags/${tag}" 2>/dev/null | grep -q "refs/tags/${tag}"
}

# The pushed commit is already tagged: do not mint a second version for one commit, but do make sure
# the tag reached the remote -- an unpublished tag is a version that exists only on this machine.
EXISTING=$(git tag --points-at "${TARGET_SHA}" --list 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null | head -1)
if [ -n "${EXISTING}" ]; then
  if publish "${EXISTING}"; then
    echo "bump-version: ${TARGET_LOCAL_REF} -> ${RELEASE_REF} already tagged ${EXISTING}; published." >&2
    exit 0
  fi
  die "${TARGET_SHA} is tagged ${EXISTING} but that tag is not on '${REMOTE}' and could not be pushed." \
    "The installed version of this commit would be indistinguishable from another commit's." \
    "Fix the remote, then: git push --atomic ${REMOTE} ${TARGET_SHA}:${RELEASE_REF} refs/tags/${EXISTING}"
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

if ! git tag -a "${NEW_TAG}" -m "Release ${NEW_TAG}" "${TARGET_SHA}" >/dev/null 2>&1; then
  die "could not create the tag ${NEW_TAG} locally." \
    "Without it this push ships a commit whose version string equals the previous commit's."
fi

if publish "${NEW_TAG}"; then
  echo "bump-version: tagged ${TARGET_LOCAL_REF} (${TARGET_SHA}) as ${NEW_TAG} and published it with ${RELEASE_REF}" >&2
  exit 0
fi

# NO RESIDUE ON FAILURE. A local-only tag would make the NEXT push think this version was already
# released and skip straight past it, so the version that could not be published is removed.
git tag -d "${NEW_TAG}" >/dev/null 2>&1
die "minted ${NEW_TAG} but could not publish it to '${REMOTE}'; the local tag was removed." \
  "The push is blocked because it would otherwise ship a commit that reinstalls as the previous" \
  "version -- byte-identical, against different code." \
  "Check connectivity and permissions, then push again."
