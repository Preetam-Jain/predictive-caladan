#!/usr/bin/env bash
# Prepare the packet log segment on the server (run as root, once per boot).
#   pktlog-setup.sh --path /dev/hugepages/caladan-pktlog --gib 64 --node 0 --ctl PKTLOGCTL
# Reserve 2 MiB hugepages for the segment and leave headroom for DPDK.
# An existing segment is left intact.
# Prints one JSON object (the pktlogctl create/stat result) on success.
set -euo pipefail

path= gib= node= ctl=
while (($#)); do
    case $1 in
        --path) path=$2; shift 2 ;;
        --gib) gib=$2; shift 2 ;;
        --node) node=$2; shift 2 ;;
        --ctl) ctl=$2; shift 2 ;;
        *) echo "{\"ok\": false, \"error\": \"unknown argument $1\"}"; exit 2 ;;
    esac
done
fail() { printf '{"ok": false, "error": "%s"}\n' "$*"; exit 1; }
[[ -n $path && -n $gib && -n $node && -x $ctl ]] || fail "need --path --gib --node --ctl"
[[ $path == /* ]] || fail "--path must be absolute"

counter=/sys/devices/system/node/node$node/hugepages/hugepages-2048kB
[[ -d $counter ]] || fail "no 2 MiB hugepage counters for node $node"
# DPDK requests 128 MiB on socket 0. Leave 256 MiB free after prefaulting.
headroom_pages=128
needed=$headroom_pages
if [[ ! -e $path ]]; then
    mount_dir=$(dirname "$path")
    if ! findmnt -n -t hugetlbfs "$mount_dir" >/dev/null; then
        mkdir -p "$mount_dir"
        mount -t hugetlbfs -o pagesize=2M none "$mount_dir" || fail "cannot mount hugetlbfs at $mount_dir"
    fi
    needed=$(( needed + (gib << 30) / (2 << 20) + 1 ))
fi
free=$(cat "$counter/free_hugepages")
if (( free < needed )); then
    total=$(cat "$counter/nr_hugepages")
    want=$(( total + needed - free ))
    echo "$want" > "$counter/nr_hugepages"
    got=$(cat "$counter/nr_hugepages")
    if (( got < want )); then
        echo "$total" > "$counter/nr_hugepages"   # give back a partial reservation
        fail "node $node reserved only $got of $want hugepages (memory fragmented; reboot and run this first)"
    fi
fi

free=$(cat "$counter/free_hugepages")
(( free >= needed )) || fail "node $node has only $free free hugepages; need $needed"
if [[ -e $path ]]; then
    exec "$ctl" stat --path "$path"
fi

result=$("$ctl" create --path "$path" --gib "$gib" --node "$node" --threads 32)
free=$(cat "$counter/free_hugepages")
(( free >= headroom_pages )) || fail "node $node has only $free free hugepages after creating the packet log; need $headroom_pages"
printf '%s\n' "$result"
