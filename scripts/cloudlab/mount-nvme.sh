#!/usr/bin/env bash
# Mount the node's NVMe SSD at /mnt/nvme for experiment results (run with sudo).
#   sudo scripts/cloudlab/mount-nvme.sh [--device /dev/nvme0n1] [--owner USER]
# Formats the device ONLY if it is a whole NVMe disk with no partitions, no
# filesystem signature and nothing mounted from it. Otherwise it mounts an
# existing ext4 filesystem, or refuses. Idempotent: does nothing if
# /mnt/nvme is already a mount point.
set -euo pipefail
mountpoint_dir=/mnt/nvme
device=
owner=${SUDO_USER:-}
while (($#)); do
    case $1 in
        --device) device=$2; shift 2 ;;
        --owner) owner=$2; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
die() { echo "mount-nvme: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root"

if mountpoint -q "$mountpoint_dir"; then
    echo "$mountpoint_dir already mounted:"; df -h "$mountpoint_dir"; exit 0
fi

if [[ -z $device ]]; then
    mapfile -t disks < <(lsblk -dnpo NAME,TYPE | awk '$2=="disk" && $1 ~ /nvme/ {print $1}')
    ((${#disks[@]} == 1)) || die "expected exactly one NVMe disk, found: ${disks[*]:-none}; pass --device"
    device=${disks[0]}
fi
[[ -b $device ]] || die "$device is not a block device"
if lsblk -nro MOUNTPOINT "$device" | grep -q .; then
    die "$device (or a partition on it) is mounted; refusing"
fi
parts=$(lsblk -nro TYPE "$device" | grep -c part || true)
fstype=$(blkid -o value -s TYPE "$device" 2>/dev/null || true)

if [[ -z $fstype && $parts -eq 0 ]]; then
    if wipefs -n "$device" | grep -q .; then
        die "$device has signatures (wipefs -n shows them); refusing to format"
    fi
    echo "Formatting empty $device as ext4"
    mkfs.ext4 -q -L caladan-results -E lazy_itable_init=1,lazy_journal_init=1 "$device"
    fstype=ext4
elif [[ $fstype != ext4 ]]; then
    die "$device has filesystem '${fstype:-?}' or $parts partition(s); not touching it"
fi

mkdir -p "$mountpoint_dir"
mount -o noatime "$device" "$mountpoint_dir"
mkdir -p "$mountpoint_dir/caladan-experiments"
if [[ -n $owner ]]; then
    chown "$owner" "$mountpoint_dir/caladan-experiments"
fi
df -h "$mountpoint_dir"
echo "NUMA node of $device: $(cat /sys/block/$(basename "$device")/device/numa_node 2>/dev/null || echo unknown)"
