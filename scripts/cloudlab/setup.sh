#!/usr/bin/env bash
# Install dependencies and initialize patched submodules on a CloudLab node.
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: setup.sh [--packages-only | --skip-packages]

Run on each node from the deployed checkout. Default: install apt dependencies,
then run make submodules. Logs/state live in .git/cloudlab-setup/.

--packages-only  Install packages without initializing submodules.
--skip-packages  Initialize submodules without running apt.

A successful submodule setup is skipped on repeat invocations. An interrupted or
failed attempt is not retried automatically: the upstream script applies patches
and is not safe to rerun blindly. Inspect the saved log or deploy a fresh checkout.
This script does not build/load ksched, reserve hugepages, or install Rust yet.
USAGE
}
install_packages=true
initialize_submodules=true
case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --packages-only) initialize_submodules=false; shift ;;
    --skip-packages) install_packages=false; shift ;;
    '') ;;
    *) usage >&2; exit 2 ;;
esac
[[ $# == 0 ]] || { usage >&2; exit 2; }
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$repo"
state=$(git rev-parse --absolute-git-dir)/cloudlab-setup
mkdir -p "$state"
log="$state/setup-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
exec > >(tee -a "$log") 2>&1
trap 'printf "Setup failed. Log: %s\n" "$log" >&2' ERR
printf 'Repository: %s\nLog: %s\n' "$repo" "$log"

run_as_root() {
    if [[ $(id -u) == 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

if $install_packages; then
    run_as_root apt-get update
    run_as_root apt-get install -y \
        git rsync curl ca-certificates build-essential cmake pkg-config ninja-build \
        libnl-3-dev libnl-route-3-dev libnuma-dev uuid-dev libssl-dev \
        libaio-dev libcunit1-dev libclang-dev libncurses-dev meson \
        nettle-dev libacl1-dev liblzma-dev libzstd-dev liblz4-dev \
        libbz2-dev zlib1g-dev libxml2-dev \
        python3-pyelftools libarchive-dev "linux-headers-$(uname -r)" \
        pciutils ibverbs-utils iputils-arping mstflint
fi
if $initialize_submodules; then
    if [[ -f "$state/submodules.complete" ]]; then
        printf 'Submodule setup already completed; skipping.\n'
    elif [[ -e "$state/submodules.started" ]]; then
        printf 'Previous submodule setup did not complete. Inspect logs in %s.\n' "$state" >&2
        printf 'Use a fresh deployment before retrying patched submodule initialization.\n' >&2
        exit 1
    elif [[ -d dpdk/build || -d rdma-core/build || -d deps/pcm/build ]]; then
        printf 'Existing submodule builds found without a completion marker; inspect before running setup.\n' >&2
        exit 1
    else
        date -u > "$state/submodules.started"
        make submodules
        mv "$state/submodules.started" "$state/submodules.complete"
    fi
fi
printf 'Setup finished. Next: ./scripts/cloudlab/build.sh\n'
