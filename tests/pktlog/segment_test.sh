#!/usr/bin/env bash
# End-to-end test of pktlogctl with a real segment file (--no-hugetlb):
# create, produce from several threads, refusals, dump contents, reset,
# exhaustion, destroy. Usage: tests/pktlog/segment_test.sh [scratch-dir]
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
work=${1:-$(mktemp -d)}
mkdir -p "$work"
seg="$work/segment"
rm -f "$seg"
ctl="$work/pktlogctl"
producer="$work/producer"
cc -std=gnu11 -O2 -Wall -Wextra -I"$repo/inc" -pthread "$repo/scripts/pktlog/pktlogctl.c" -o "$ctl"
cc -std=gnu11 -O2 -Wall -I"$repo/inc" -pthread "$repo/tests/pktlog/producer.c" -o "$producer"

cleanup() { "$ctl" destroy --path "$seg" --discard-undumped >/dev/null 2>&1 || true; }
trap cleanup EXIT

json() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d$2)" "$1"; }
expect_fail() { if out=$("$@"); then echo "expected failure: $*"; echo "$out"; exit 1; fi; }

# 1 GiB -> 15 chunks of 64 MiB (2,097,150 records each)
out=$("$ctl" create --path "$seg" --gib 1 --node 0 --no-hugetlb --threads 4)
[ "$(json "$out" "['chunks']")" = 15 ]
[ "$(json "$out" "['placement_verified']")" = True ]
out=$("$ctl" stat --path "$seg"); [ "$(json "$out" "['empty']")" = True ]

# 3 threads x 5,000,000 records -> 3 chunks each, 9 chunks total
"$producer" "$seg" 3 5000000 3 &
pid=$!
sleep 1
out=$("$ctl" stat --path "$seg"); [ "$(json "$out" "['in_use']")" = True ]
expect_fail "$ctl" dump --path "$seg" --out "$work/early"       # in use
expect_fail "$ctl" reset --path "$seg"                          # in use
wait $pid
expect_fail "$ctl" reset --path "$seg"                          # not dumped

# Capture header with a known experiment id for the dump to copy
python3 - "$work/capture.bw.bin" <<'EOF'
import struct, sys
open(sys.argv[1], 'wb').write(struct.pack('<8sIIIIdQIIIIdQQ16s', b'CALATLM\0', 10, 96, 16, 1,
                                          2400.0, 0, 0, 0, 0, 1, 0.0, 0, 0, bytes(range(16))))
EOF
out=$("$ctl" dump --path "$seg" --out "$work/run" --experiment-id-from "$work/capture.bw.bin")
[ "$(json "$out" "['records']")" = 15000000 ]
[ "$(json "$out" "['file_count']")" = 3 ]
[ "$(json "$out" "['exhausted']")" = False ]
expect_fail "$ctl" dump --path "$seg" --out "$work/again"       # already dumped
python3 - "$work" <<'EOF'
import struct, sys, numpy as np
from pathlib import Path
d = Path(sys.argv[1])
dt = np.dtype([('ts','<u8'),('seq_start','<u4'),('seq_end','<u4'),('remote_ip','<u4'),
               ('remote_port','<u2'),('local_port','<u2'),('type','u1'),('kthread','u1'),
               ('flags','<u2'),('tsc_low','<u4')])
for t in range(3):
    p = d / f'run.pkt.k{t:03d}.bin'
    h = struct.unpack('<8sIIIIQQQdIIII16sQ', p.read_bytes()[:96])
    assert h[0] == b'CALPKTD\0' and h[4] == t and h[5] == 5000000 and h[9] & 1, h
    assert h[13] == bytes(range(16)) and h[10] == 3, h
    r = np.fromfile(p, dtype=dt, offset=96)
    assert len(r) == 5000000
    assert (r['seq_start'] == np.arange(5000000)).all()
    assert (r['remote_port'] == t).all() and (r['kthread'] == t).all()
    assert (r['ts'] == 1000 + np.arange(5000000)).all()
    assert (r['type'] == (np.arange(5000000) % 3) + 1).all()
print('dump contents verified')
EOF

out=$("$ctl" reset --path "$seg" --threads 4)
[ "$(json "$out" "['empty']")" = True ]
[ "$(json "$out" "['generation']")" = 2 ]

# Exhaustion: 2 threads x 20,000,000 records > 15 chunks
"$producer" "$seg" 2 20000000
out=$("$ctl" stat --path "$seg")
[ "$(json "$out" "['exhausted']")" = True ]
dropped=$(json "$out" "['dropped']")
out=$("$ctl" dump --path "$seg" --out "$work/full")
[ "$(json "$out" "['exhausted']")" = True ]
total=$(( $(json "$out" "['records']") + dropped ))
[ "$total" = 40000000 ] || { echo "records+dropped=$total"; exit 1; }
"$ctl" reset --path "$seg" >/dev/null
"$ctl" destroy --path "$seg" >/dev/null
[ ! -e "$seg" ]
trap - EXIT
echo "PASS: pktlogctl create/produce/refusals/dump/reset/exhaustion/destroy"
