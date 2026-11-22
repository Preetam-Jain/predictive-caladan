#!/usr/bin/env bash
# Build the pinned artifact applications against THIS checkout, never its old runtime.
set -euo pipefail
usage() {
    cat <<'USAGE'
Usage: build-workloads.sh --artifact /absolute/path [--revision SHA] [--repository URL]
       [--jobs N] [--install-deps] [--memcached] [--stream-dram] [--swaptions-gc]

Clone the artifact only if absent; otherwise require the requested revision.
Never forces a checkout, cleans a tree, or substitutes the artifact's older runtime.
Logs and the last successful build's runtime identity: .git/cloudlab-setup/.
GC builds explicitly require inc/runtime/gc.h and the enable_gc runtime option.
USAGE
}
artifact= revision= repository=https://github.com/shenango/caladan-all.git
jobs=$(nproc)
install_deps=false
memcached=false
stream=false
gc=false
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --artifact|--revision|--repository|--jobs)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            case "$1" in
                --artifact) artifact=$2 ;;
                --revision) revision=$2 ;;
                --repository) repository=$2 ;;
                --jobs) jobs=$2 ;;
            esac
            shift 2 ;;
        --install-deps) install_deps=true; shift ;;
        --memcached) memcached=true; shift ;;
        --stream-dram) stream=true; shift ;;
        --swaptions-gc) gc=true; shift ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ $artifact == /* && $jobs =~ ^[1-9][0-9]*$ ]] || { usage >&2; exit 2; }
[[ -z $revision || $revision =~ ^[a-fA-F0-9]{40}$ ]] || { printf 'Revision must be a full SHA.\n' >&2; exit 2; }
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
state=$(git -C "$repo" rev-parse --absolute-git-dir)/cloudlab-setup
mkdir -p "$state"
log="$state/workloads-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
exec > >(tee -a "$log") 2>&1
trap 'printf "Workload build failed. Log: %s\n" "$log" >&2' ERR
cd "$repo"
if pgrep -x iokerneld >/dev/null; then
    printf 'Stop the running experiment before building workloads.\n' >&2
    exit 1
fi
# Fail with an actionable compatibility report rather than making unrelated runtime changes.
if $gc; then
    if [[ ! -f inc/runtime/gc.h ]] || ! grep -q '"enable_gc"' runtime/cfg.c 2>/dev/null; then
        printf '%s\n' \
            'BLOCKED: the artifact Boehm GC port requires inc/runtime/gc.h,' \
            'gc_stop_world(), gc_start_world(), gc_discover_all_stacks(), and enable_gc.' \
            'This runtime does not expose that GC integration.' \
            'Do not link against the artifact runtime as a workaround.' \
            'To rehearse the compatible workloads only, disable build_swaptions_gc' \
            'and the memcached_swaptions_gc condition in the launcher config.' >&2
        exit 1
    fi
fi
as_root() {
    if [[ $(id -u) == 0 ]]; then "$@"; else sudo -n "$@"; fi
}
if $install_deps; then
    as_root apt-get update
    as_root apt-get install -y autoconf automake libtool libevent-dev libatomic-ops-dev
fi
if $memcached || $gc; then
    if [[ ! -e $artifact ]]; then
        git clone --no-checkout "$repository" "$artifact"
        git -C "$artifact" checkout --detach "${revision:-HEAD}"
    fi
    actual=$(git -C "$artifact" rev-parse HEAD)
    if [[ -n $revision && $actual != "$revision" ]]; then
        printf 'Artifact revision mismatch: expected %s, found %s. No checkout was forced.\n' "$revision" "$actual" >&2
        exit 1
    fi
    modules=()
    if $memcached; then modules+=(memcached); fi
    if $gc; then modules+=(gc parsec); fi
    git -C "$artifact" submodule update --init --recursive -- "${modules[@]}"
fi
# Application builds can otherwise reuse objects linked against an older runtime.
# Refuse a different runtime identity in an existing build instead of cleaning it.
identity=$(mktemp)
trap 'rm -f "$identity"' EXIT
{
    printf '%s\n' "$repo"
    git rev-parse HEAD
    git diff --binary HEAD -- inc runtime shim bindings/cc build Makefile
    for library in libbase.a libnet.a libruntime.a; do
        [[ -f $library ]] || { printf 'Missing %s; build runtime first.\n' "$library" >&2; exit 1; }
        sha256sum "$library"
    done
} > "$identity"
identity_hash=$(sha256sum "$identity" | cut -d' ' -f1)
if [[ -e $artifact/.caladan-workload-runtime && $(cat "$artifact/.caladan-workload-runtime") != "$identity_hash" ]]; then
    printf 'Workloads were built against a different runtime. Use a fresh artifact directory to rebuild safely.\n' >&2
    exit 1
fi
make -C "$repo/bindings/cc" -j"$jobs"
make -C "$repo/shim" -j"$jobs"
export SHENANGODIR="$repo"
if $stream; then
    make -C "$repo/apps/netbench" -j"$jobs" stress_shm stress_shm_query
fi
if $memcached; then
    (
        cd "$artifact/memcached"
        for compatibility_patch in "$repo"/build/patches/memcached/*.patch; do
            if ! git apply --reverse --check "$compatibility_patch" 2>/dev/null; then
                git apply --check "$compatibility_patch"
                git apply "$compatibility_patch"
            fi
        done
        ./autogen.sh
        CFLAGS="${CFLAGS:--g -O2} -Wno-error=deprecated-declarations" \
            ./configure --with-shenango="$repo"
        make -j"$jobs"
    )
fi
if $gc; then
    (
        cd "$artifact/gc"
        ./autogen.sh
        ./configure --prefix="$artifact/gc/build" --enable-static --enable-large-config \
            --enable-handle-fork=no --enable-dlopen=no --disable-java-finalization \
            --enable-threads=shenango --enable-shared=no --with-shenango="$repo"
        make -j"$jobs" install
    )
    export GCDIR="$artifact/gc/build"
    "$artifact/parsec/bin/parsecmgmt" -a build -p swaptions -c gcc-shenango-gc
fi
if $memcached || $gc; then printf '%s\n' "$identity_hash" > "$artifact/.caladan-workload-runtime"; fi
printf 'Requested workloads built against %s. Log: %s\n' "$repo" "$log"
