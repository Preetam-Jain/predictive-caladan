#!/usr/bin/env bash
# Install rustup if absent, then build loadgen using its repository toolchain.
set -euo pipefail
usage() {
    cat <<'USAGE'
Usage: build-loadgen.sh

Run as the normal login user on each node, after build.sh succeeds. Installs
rustup from https://sh.rustup.rs if needed, then builds apps/loadgen in release
mode using its rust-toolchain.toml. No sudo. Logs: .git/cloudlab-setup/loadgen-*.log
USAGE
}
case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    '') ;;
    *) usage >&2; exit 2 ;;
esac
[[ $# == 0 ]] || { usage >&2; exit 2; }
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
state=$(git -C "$repo" rev-parse --absolute-git-dir)/cloudlab-setup
mkdir -p "$state"
log="$state/loadgen-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
exec > >(tee -a "$log") 2>&1
trap 'printf "Loadgen build failed. Log: %s\n" "$log" >&2' ERR
for library in libbase.a libnet.a libruntime.a; do
    if [[ ! -f "$repo/$library" ]]; then
        printf 'Missing %s; run ./scripts/cloudlab/build.sh first.\n' "$library" >&2
        exit 1
    fi
done
export PATH="${CARGO_HOME:-$HOME/.cargo}/bin:$PATH"
if ! command -v rustup >/dev/null; then
    command -v curl >/dev/null || { printf 'curl is required; rerun setup.sh --packages-only.\n' >&2; exit 1; }
    installer=$(mktemp)
    trap 'rm -f -- "$installer"' EXIT
    curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o "$installer"
    sh "$installer" -y --profile minimal --default-toolchain none --no-modify-path
fi
cd "$repo/apps/loadgen"
# These select/install the toolchain declared by rust-toolchain.toml and log it.
rustc --version
cargo --version
if [[ -f Cargo.lock ]]; then
    cargo build --release --locked
else
    cargo build --release
fi
printf 'Loadgen built: %s/apps/loadgen/target/release/loadgen\nLog: %s\n' "$repo" "$log"
printf 'Keep Cargo.lock and this toolchain version with the experiment metadata.\n'
