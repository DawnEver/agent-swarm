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
# THE TAG ONLY, NOT THE BRANCH -- AND WHY THAT REVERSES WHAT THIS HEADER USED TO SAY. It previously
# pushed `--atomic <branch> <tag>` so both refs landed or neither, and claimed "the outer push then
# finds its branch already up to date and succeeds as a no-op". MEASURED, that claim is false on
# every path that mints: git computed the outer update as a compare-and-swap (old -> new) before the
# hook ran, the atomic push moved the ref underneath it, and the outer push is then rejected with
#
#     cannot lock ref 'refs/heads/main': is at <new> but expected <old>
#     ! [remote rejected] <topic> -> main (failed to update ref)
#
# -- refs correctly published, push reported as failed, every single release. It went unnoticed only
# because the hook had never once published: it was deciding on the local branch, so it declined on
# every real push. Fixing what it reads exposes this, so the two must land together.
#
# The orphan the atomic push existed to prevent -- a tag reaching the remote when the branch does not
# -- is instead prevented by a PRECONDITION: the remote's current trunk must be an ancestor of the
# commit being released, checked against the remote itself just before minting. If it is not, the
# outer push was going to be rejected anyway, so this refuses without minting rather than tagging a
# commit that will not become the trunk. What remains is a genuine race -- another integrator landing
# in the moment between the tag push and the outer branch push -- which costs one burned version
# number and is named here rather than hidden. This is a smaller window than it replaces, since the
# old shape failed the push EVERY time rather than rarely.
#
# `--no-verify` on the inner push: without it this hook re-enters itself.
#
# WHAT DECIDES: THE REMOTE REF, NOT THE LOCAL BRANCH. `refs/heads/<default>` being updated IS the
# release, whatever the pusher happens to have checked out. Deciding on
# `git rev-parse --abbrev-ref HEAD` instead asks a DIFFERENT question that agrees only in the common
# case: right for someone sitting on the default branch pushing it, silently inert for an integrator
# running `git push origin <topic>:main` -- exactly the workflow this project prescribes, where every
# producer emits a Submission and one integrator advances the trunk. Three such pushes reported
# "Passed" while the remote gained zero tags.
#
# WHERE THAT FACT COMES FROM, AND WHY NOT STDIN. A raw `.git/hooks/pre-push` is fed one line per ref
# on stdin -- `<local ref> <local sha> <remote ref> <remote sha>`. This script never sees them:
# pre-commit's pre-push hook-impl CONSUMES stdin itself to compute the diff range for the hooks it
# runs, and hands `entry` scripts nothing. Reading stdin here is not merely redundant, it is a branch
# that cannot execute, and a first attempt at this fix shipped exactly that -- correct logic against
# an input that is always empty, which reported "no tag minted" on a real `<topic>:main` push.
#
# MEASURED against pre-commit 4.6.2 with a real `pre-commit install --hook-type pre-push` and real
# pushes into a local bare remote (topic->main, two refs at once in both orders, topic->topic,
# tag-only, delete-main, first-push-of-an-empty-remote):
#
#   stdin                      ALWAYS EMPTY.       Never a usable source.
#   positional args            ALWAYS EMPTY.       pre-commit passes none; the config declares none.
#   PRE_COMMIT_REMOTE_NAME     always set.         The remote.
#   PRE_COMMIT_REMOTE_BRANCH   always set.         The remote ref -- the fact that decides.
#   PRE_COMMIT_LOCAL_BRANCH    always set.         The local ref feeding it.
#   PRE_COMMIT_TO_REF          set EXCEPT on the first push into a remote with no refs at all.
#
# `_TO_REF` being conditional is why the sha falls back to resolving `_LOCAL_BRANCH`: the one case it
# is missing -- an empty remote -- is precisely the v0.1.0 path, so a script trusting it would fail
# on the only push that mints the FIRST tag and nowhere else.
#
# A DELETION and a TAG-ONLY push never reach here at all: pre-commit does not run pre-push hooks for
# either (measured). That is the outcome this script wants in both cases, so it is not re-implemented
# here -- a branch for it would be untestable through the real entry point and unreachable in
# production, which is the defect above wearing different clothes.
#
# `PRE_COMMIT_REMOTE_BRANCH` reports ONE ref pair even when several are pushed. Measured, it picked
# the release ref with the refs given in either order -- but what it actually prefers is a ref that
# already exists on the remote over one being created, which merely COINCIDED with the release ref in
# both trials. So it is "the ref pre-commit chose", not "the ref that matters", and when it names
# something other than the release ref this script declines rather than claiming to know better.
#
# It also fixes WHICH COMMIT is tagged: the version belongs to the commit the trunk is being moved
# to, and HEAD need not be it.
set -uo pipefail

REMOTE="${PRE_COMMIT_REMOTE_NAME:-}"
REMOTE_BRANCH="${PRE_COMMIT_REMOTE_BRANCH:-}"
LOCAL_BRANCH="${PRE_COMMIT_LOCAL_BRANCH:-}"
TO_REF="${PRE_COMMIT_TO_REF:-}"

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

# "I CANNOT TELL" IS NOT "NO RELEASE", so it BLOCKS rather than declining. Every invocation this
# script is reachable from sets these; an invocation that does not is a shape nobody has measured,
# and the failure it would otherwise produce is the silent one this whole script exists to refuse --
# indistinguishable, from the operator's side, from a release that was correctly skipped. Blocking is
# recoverable and prints its remedy; silence is what let three pushes ship untagged.
[ -n "${REMOTE}" ] || die "PRE_COMMIT_REMOTE_NAME is unset, so the push's remote is unknown." \
  "This script reads pre-commit's environment and nothing else; run it as the pre-push hook." \
  "Refusing rather than guessing 'origin': guessing wrong publishes a tag to the wrong forge."
[ -n "${REMOTE_BRANCH}" ] || die "PRE_COMMIT_REMOTE_BRANCH is unset, so WHICH REF this push updates is unknown." \
  "Whether this is a release cannot be decided, and 'cannot decide' must not read as 'not a release'" \
  "-- that is the exact failure this hook exists to prevent." \
  "If pre-commit changed what it exports, re-measure it and fix this script; do not silence it."

# Only the default branch carries releases.
DEFAULT_BRANCH=$(git symbolic-ref --quiet --short "refs/remotes/${REMOTE}/HEAD" 2>/dev/null | sed "s@^${REMOTE}/@@")
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
RELEASE_REF="refs/heads/${DEFAULT_BRANCH}"

if [ "${REMOTE_BRANCH}" != "${RELEASE_REF}" ]; then
  decline "this push updates '${REMOTE_BRANCH}' on '${REMOTE}', not '${RELEASE_REF}'." \
    "Builds off any other ref spell themselves <next>.dev<N>+g<sha>, which is already unique."
fi

# THE COMMIT BEING RELEASED. `_TO_REF` is already a sha; the fallback resolves the local ref instead,
# for the empty-remote case where pre-commit exports no `_TO_REF` -- see the header's measurement.
TARGET_SHA="${TO_REF}"
if [ -z "${TARGET_SHA}" ]; then
  TARGET_SHA=$(git rev-parse --verify --quiet "${LOCAL_BRANCH}" 2>/dev/null)
fi
if [ -z "${TARGET_SHA}" ]; then
  die "'${RELEASE_REF}' is being updated but the commit it moves to could not be resolved." \
    "PRE_COMMIT_TO_REF was empty and PRE_COMMIT_LOCAL_BRANCH ('${LOCAL_BRANCH}') did not resolve." \
    "This IS a release push, so it is blocked rather than skipped."
fi
TARGET_LOCAL_REF="${LOCAL_BRANCH:-${TARGET_SHA}}"

# Refresh remote tags so the highest-tag computation and the collision scan both see a tag another
# machine already pushed. A failure here is NOT fatal by itself -- the publish below is what actually
# decides, and it will refuse a stale or colliding tag on its own.
git fetch --quiet --tags "${REMOTE}" >/dev/null 2>&1 || true

# THE PRECONDITION THAT REPLACES ATOMICITY (see the header). Read the remote's trunk and require it
# to be an ancestor of the commit being released. An absent trunk is fine -- the outer push creates
# it. A trunk that is NOT an ancestor means the outer push is about to be rejected as a
# non-fast-forward, so minting now would publish a tag for a commit that never becomes the trunk.
REMOTE_TRUNK=$(git ls-remote "${REMOTE}" "${RELEASE_REF}" 2>/dev/null | awk '{print $1; exit}')
if [ -n "${REMOTE_TRUNK}" ] && ! git merge-base --is-ancestor "${REMOTE_TRUNK}" "${TARGET_SHA}" 2>/dev/null; then
  die "'${RELEASE_REF}' on '${REMOTE}' is at ${REMOTE_TRUNK}, which ${TARGET_SHA} does not descend from." \
    "The push that triggered this hook is going to be rejected as a non-fast-forward, so no version" \
    "is minted for a commit that will not become the trunk." \
    "Integrate the remote trunk first, then push again."
fi

publish() {
  # AN OPERATION'S EXIT IS NOT ITS EFFECT: the push is checked, and then the REMOTE is read back.
  # THE TAG ALONE. Pushing the branch here too would move the ref out from under the outer push's
  # compare-and-swap and get that push rejected -- measured, on every mint. See the header.
  local tag="$1"
  if ! git push --no-verify "${REMOTE}" "refs/tags/${tag}" >/dev/null 2>&1; then
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
    "Fix the remote, then: git push ${REMOTE} refs/tags/${EXISTING}"
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
