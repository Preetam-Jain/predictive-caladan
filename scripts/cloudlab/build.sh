#!/usr/bin/env bash
# Build Caladan and ksched; machine configuration is a separate step.
set -euo pipefail
usage() {
    printf 'Usage: build.sh [--jobs N]\nBuild Caladan/runtime, then ksched for the running kernel.\nLogs: .git/cloudlab-setup/build-*.log\n'
}
jobs=$(nproc)
case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --jobs) [[ $# == 2 ]] || { usage >&2; exit 2; }; jobs=$2; shift 2 ;;
    '') ;;
    *) usage >&2; exit 2 ;;
esac
[[ $# == 0 && $jobs =~ ^[1-9][0-9]*$ ]] || { usage >&2; exit 2; }
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$repo"
state=$(git rev-parse --absolute-git-dir)/cloudlab-setup
mkdir -p "$state"
log="$state/build-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
exec > >(tee -a "$log") 2>&1
trap 'printf "Build failed. Log: %s\n" "$log" >&2' ERR
printf 'Building for kernel %s with %s jobs. Log: %s\n' "$(uname -r)" "$jobs" "$log"
make -j"$jobs"
# ksched/Makefile uses the environment's PWD for the kernel module directory.
# Enter it in the shell so PWD matches; make -C alone leaves PWD at the repo root.
(
    cd ksched
    make -j"$jobs"
)
printf 'Build completed. Next: ./scripts/cloudlab/machine-setup.sh\n'
