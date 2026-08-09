#!/usr/bin/env bash
#
# CheapSecurity release helper.
#
# Run from the repo root on the `develop` branch. It will:
#   1. Bump the version in pyproject.toml and src/cheapsecurity/__init__.py
#   2. Commit and push the version bump to `develop`
#   3. Open a pull/merge request from `develop` to `main`
#   4. Create and push an annotated tag `vX.Y.Z`
#   5. Create a GitHub release for the tag
#
# Usage:
#   ./release.sh [NEW_VERSION]
#
# If NEW_VERSION is omitted the current minor version is bumped
# (e.g. 1.0.0 -> 1.1.0).
#
# Requires either the GitHub CLI (`gh`) authenticated, or a
# GITHUB_TOKEN environment variable with `repo` scope.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
error() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; }

fail() {
    error "$1"
    exit 1
}

parse_remote() {
    local url
    url="$(git remote get-url origin 2>/dev/null || true)"
    [[ -n "$url" ]] || fail "could not determine git remote 'origin'"

    if [[ "$url" =~ ^git@github\.com:(.+)/(.+)\.git$ ]]; then
        OWNER="${BASH_REMATCH[1]}"
        REPO="${BASH_REMATCH[2]}"
    elif [[ "$url" =~ ^https?://github\.com/(.+)/(.+?)(\.git)?$ ]]; then
        OWNER="${BASH_REMATCH[1]}"
        REPO="${BASH_REMATCH[2]}"
    else
        fail "unsupported remote URL format: $url"
    fi
}

current_version() {
    grep -E '^version\s*=\s*"' pyproject.toml | head -n1 | sed -E 's/^version\s*=\s*"([^"]+)".*/\1/'
}

bump_minor() {
    python3 - "$1" <<'PY'
import sys, re
v = sys.argv[1]
m = re.match(r'(\d+)\.(\d+)\.(\d+)', v)
if not m:
    sys.exit(1)
print(f"{m.group(1)}.{int(m.group(2)) + 1}.0")
PY
}

update_version_files() {
    local new="$1"
    sed -i -E "s/^version\s*=\s*\"[^\"]+\"/version = \"$new\"/" pyproject.toml
    sed -i -E "s/__version__\s*=\s*\"[^\"]+\"/__version__ = \"$new\"/" src/cheapsecurity/__init__.py

    # Sanity check
    local check
    check="$(current_version)"
    [[ "$check" == "$new" ]] || fail "version bump failed (got $check)"
}

generate_notes() {
    local since_tag
    since_tag="$(git describe --tags --abbrev=0 2>/dev/null || true)"
    if [[ -n "$since_tag" ]]; then
        echo "Changes since $since_tag:"
        echo
        git log "$since_tag"..HEAD --pretty=format:'- %s (%h)'
    else
        echo "Changes in this release:"
        echo
        git log --pretty=format:'- %s (%h)'
    fi
    echo
}

# ------------------------------------------------------------------
# GitHub API helpers (used when `gh` is unavailable)
# ------------------------------------------------------------------

have_gh() {
    command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1
}

have_api() {
    [[ -n "${GITHUB_TOKEN:-}" ]] && command -v jq >/dev/null 2>&1
}

api_post() {
    local endpoint="$1" payload="$2"
    curl -fsS -X POST \
        -H "Authorization: Bearer $GITHUB_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        "https://api.github.com/repos/$OWNER/$REPO/$endpoint" \
        -d "$payload"
}

create_pr_api() {
    local title="$1" body="$2"
    local json_body
    json_body="$(printf '%s' "$body" | jq -Rs .)"
    api_post pulls "{\"title\":\"$title\",\"body\":$json_body,\"head\":\"develop\",\"base\":\"main\"}"
}

create_release_api() {
    local tag="$1" title="$2" body="$3"
    local json_body
    json_body="$(printf '%s' "$body" | jq -Rs .)"
    api_post releases "{\"tag_name\":\"$tag\",\"name\":\"$title\",\"body\":$json_body}"
}

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

BRANCH="$(git branch --show-current)"
[[ "$BRANCH" == "develop" ]] || fail "must be on the 'develop' branch (currently on '$BRANCH')"

if ! git diff --quiet || ! git diff --cached --quiet; then
    fail "working tree is not clean; commit or stash changes first"
fi

info "Fetching latest develop ..."
git pull --ff-only origin develop

CURRENT="$(current_version)"
[[ -n "$CURRENT" ]] || fail "could not read current version from pyproject.toml"

if [[ $# -ge 1 ]]; then
    NEW_VERSION="$1"
else
    NEW_VERSION="$(bump_minor "$CURRENT")"
    info "No version supplied; bumping minor: $CURRENT -> $NEW_VERSION"
fi

if ! [[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    fail "version must be semver X.Y.Z (got '$NEW_VERSION')"
fi

[[ "$NEW_VERSION" != "$CURRENT" ]] || fail "new version ($NEW_VERSION) must differ from current version"

parse_remote

info "Updating version: $CURRENT -> $NEW_VERSION"
update_version_files "$NEW_VERSION"

info "Committing version bump"
git add pyproject.toml src/cheapsecurity/__init__.py
git commit -m "chore(release): bump version to $NEW_VERSION"

info "Pushing develop"
git push origin develop

info "Creating pull request from develop to main"
PR_TITLE="Release v$NEW_VERSION"
PR_BODY="$(generate_notes)"

if have_gh; then
    if gh pr list --head develop --base main --json number | jq -e '.[0].number' >/dev/null 2>&1; then
        warn "a pull request from develop to main already exists"
    else
        gh pr create --base main --head develop --title "$PR_TITLE" --body "$PR_BODY"
    fi
elif have_api; then
    if curl -fsS -H "Authorization: Bearer $GITHUB_TOKEN" \
        "https://api.github.com/repos/$OWNER/$REPO/pulls?head=$OWNER:develop&base=main" \
        | jq -e '.[0].number' >/dev/null 2>&1; then
        warn "a pull request from develop to main already exists"
    else
        create_pr_api "$PR_TITLE" "$PR_BODY"
        echo
        info "Pull request created via GitHub API"
    fi
else
    warn "GitHub CLI not authenticated and GITHUB_TOKEN not set; create the PR manually:"
    echo "  https://github.com/$OWNER/$REPO/compare/main...develop?expand=1"
    echo
fi

info "Tagging v$NEW_VERSION"
git tag -a "v$NEW_VERSION" -m "Release v$NEW_VERSION"
git push origin "v$NEW_VERSION"

info "Creating GitHub release for v$NEW_VERSION"
RELEASE_TITLE="CheapSecurity v$NEW_VERSION"
RELEASE_BODY="$PR_BODY"

if have_gh; then
    if gh release view "v$NEW_VERSION" >/dev/null 2>&1; then
        warn "release v$NEW_VERSION already exists"
    else
        gh release create "v$NEW_VERSION" --title "$RELEASE_TITLE" --notes "$RELEASE_BODY"
    fi
elif have_api; then
    if curl -fsS -H "Authorization: Bearer $GITHUB_TOKEN" \
        "https://api.github.com/repos/$OWNER/$REPO/releases/tags/v$NEW_VERSION" \
        >/dev/null 2>&1; then
        warn "release v$NEW_VERSION already exists"
    else
        create_release_api "v$NEW_VERSION" "$RELEASE_TITLE" "$RELEASE_BODY"
        echo
        info "Release created via GitHub API"
    fi
else
    warn "GitHub CLI not authenticated and GITHUB_TOKEN not set; create the release manually:"
    echo "  https://github.com/$OWNER/$REPO/releases/new?tag=v$NEW_VERSION"
fi

info "Done. New version: $NEW_VERSION"
