#!/usr/bin/env bash
# Deploy committed baseline + working diff + untracked source, without committing.
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: deploy.sh [--repo DIRECTORY] [--identity KEY] USER@HOST [USER@HOST ...]

Run locally from any directory. Deploys this checkout to a NEW directory under
each remote user's home (default: predictive-caladan). Existing destinations
are refused, preserving remote work. Use --repo to choose a fresh directory.

Requires git, ssh, scp, and tar locally; bash, git, and tar remotely.
SSH config is respected. No commits or pushes are made. Ignored files, submodule
working trees, and untracked PDF/build artifacts are not transferred.
USAGE
}

source_root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
remote_repo=predictive-caladan
ssh_command=(ssh)
scp_command=(scp)
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --repo)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            remote_repo=$2
            shift 2 ;;
        --identity)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            ssh_command+=(-i "$2")
            scp_command+=(-i "$2")
            shift 2 ;;
        --) shift; break ;;
        -*) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
        *) break ;;
    esac
done
[[ $# -gt 0 ]] || { usage >&2; exit 2; }
# A single directory name also keeps SSH's remote argument handling unambiguous.
[[ $remote_repo =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || {
    printf '%s\n' '--repo must be a single directory name under the remote home.' >&2
    exit 2
}
for host in "$@"; do
    [[ $host =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]] || {
        printf 'Invalid SSH destination: %s\n' "$host" >&2
        exit 2
    }
done
for command in git ssh scp tar; do
    command -v "$command" >/dev/null || { printf 'Missing command: %s\n' "$command" >&2; exit 1; }
done

staging=$(mktemp -d)
trap 'rm -rf -- "$staging"' EXIT
baseline=$(git -C "$source_root" rev-parse HEAD)
git -C "$source_root" bundle create "$staging/base.bundle" HEAD
git -C "$source_root" diff --binary HEAD > "$staging/working.patch"

# git ls-files includes new source files but respects .gitignore. Submodule changes
# need separate deployment; do not accidentally ship machine-specific builds.
while IFS= read -r -d '' path; do
    case "$path" in
        *.pdf|*.o|*.d|*.a|*.ko|*.log|*.bin|iokerneld|*/target/*) continue ;;
    esac
    printf '%s\0' "$path"
done < <(git -C "$source_root" ls-files --others --exclude-standard -z) > "$staging/untracked.list"
tar -C "$source_root" --null -T "$staging/untracked.list" -cf "$staging/untracked.tar"

for host in "$@"; do
    printf 'Deploying baseline %s and working files to %s:~/%s\n' "$baseline" "$host" "$remote_repo"
    "${ssh_command[@]}" "$host" bash -s -- "$remote_repo" <<'REMOTE_CHECK'
set -euo pipefail
command -v git >/dev/null
command -v tar >/dev/null
[[ ! -e "$HOME/$1" && ! -L "$HOME/$1" ]] || {
    printf 'Destination already exists: %s/%s; use --repo with a fresh name.\n' "$HOME" "$1" >&2
    exit 1
}
REMOTE_CHECK
    # Stage transport files in a private temporary directory on the target.
    remote_staging=$("${ssh_command[@]}" "$host" mktemp -d /tmp/caladan-deploy.XXXXXXXX)
    [[ $remote_staging =~ ^/tmp/caladan-deploy\.[a-zA-Z0-9]+$ ]] || {
        printf 'Unexpected remote temporary path: %s\n' "$remote_staging" >&2
        exit 1
    }
    "${scp_command[@]}" "$staging/base.bundle" "$staging/working.patch" \
        "$staging/untracked.tar" "$host:$remote_staging/"
    "${ssh_command[@]}" "$host" bash -s -- "$remote_repo" "$remote_staging" "$baseline" <<'REMOTE_DEPLOY'
set -euo pipefail
repo="$HOME/$1"
staging=$2
baseline=$3
trap 'rm -rf -- "$staging"' EXIT
# mkdir without -p refuses an existing destination even if another deploy raced us.
mkdir "$repo"
git -C "$repo" init -q
git -C "$repo" fetch -q "$staging/base.bundle" HEAD
git -C "$repo" checkout -q --detach "$baseline"
if [[ -s "$staging/working.patch" ]]; then
    git -C "$repo" apply --check "$staging/working.patch"
    git -C "$repo" apply "$staging/working.patch"
fi
tar -C "$repo" -xf "$staging/untracked.tar"
mkdir -p "$repo/.git/cloudlab-deploy"
printf '%s\n' "$baseline" > "$repo/.git/cloudlab-deploy/baseline"
cp "$staging/working.patch" "$repo/.git/cloudlab-deploy/source.patch"
git -C "$repo" status --short
REMOTE_DEPLOY
done
