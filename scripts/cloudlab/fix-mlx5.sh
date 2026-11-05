#!/usr/bin/env bash
# Backport the upstream Verbs/DevX queue-size fix, rebuild DPDK, relink IOKernel.
set -euo pipefail
case "${1:-}" in
    --help|-h)
        printf 'Usage: fix-mlx5.sh\nApply the bundled queue-limit compatibility patch, rebuild/install DPDK, relink iokerneld.\nNo firmware changes; run before starting workloads.\n'
        exit 0 ;;
    '') ;;
    *) exit 2 ;;
esac
[[ $# == 0 ]] || exit 2
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
patch="$repo/scripts/cloudlab/patches/mlx5-verbs-queue-limits.patch"
state=$(git -C "$repo" rev-parse --absolute-git-dir)/cloudlab-setup
mkdir -p "$state"
log="$state/mlx5-fix-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
exec > >(tee -a "$log") 2>&1
trap 'printf "mlx5 compatibility build failed. Log: %s\n" "$log" >&2' ERR
printf 'Log: %s\n' "$log"
if pgrep -x iokerneld >/dev/null; then
    printf 'Stop iokerneld and its applications before rebuilding DPDK.\n' >&2
    exit 1
fi
if [[ ! -f "$repo/dpdk/build/build.ninja" ]]; then
    printf 'DPDK is not built; complete setup.sh first.\n' >&2
    exit 1
fi
if git -C "$repo/dpdk" apply --reverse --check "$patch" 2>/dev/null; then
    printf 'Queue-size patch already applied; rebuilding/relinking safely.\n'
else
    git -C "$repo/dpdk" apply --check "$patch"
    git -C "$repo/dpdk" apply "$patch"
fi
ninja -C "$repo/dpdk/build"
ninja -C "$repo/dpdk/build" install
cd "$repo"
# DPDK static libraries are not explicit dependencies in the root Makefile.
# Mark one IOKernel source as changed for make, without touching its contents.
make -j"$(nproc)" -W iokernel/dpdk.c iokerneld
printf 'Compatibility patch built; no NIC firmware settings changed.\n'
