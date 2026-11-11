# Telemetry file format versions

The version identifies the on-disk layout, not a Caladan release or experiment.
The current writer produces **version 6**. The decoder supports **versions 4, 5,
and 6**, so existing captures remain readable. Versions 1–3 document development
formats and are rejected by the current decoder.

All versions use little-endian integers and IEEE-754 64-bit doubles. A field's
size and position matter. Changing a field's order, width, interpretation, or the
record layout requires a new version and an update to this document and decoder.
Changing ring capacities, batch sizes, filenames, or implementation details that
do not change the file layout does not require a new version.

Every file starts with the 8-byte signature `CALATLM\0`, followed by a u32 version.
Sizes below include compiler padding from the original x86 C layout where noted.
`NCPU` is Caladan's compile-time CPU limit; later formats avoid that dependency.

## Version 1 — one mixed stream, fixed CPU array

Header: 1080 bytes with NCPU=256. Fields, in order:

| Field | Type |
| --- | --- |
| magic | 8 bytes |
| version, record_bytes | 2 × u32 |
| cycles_per_us | f64 |
| selector | u64 |
| writer_core, scheduler_core, nr_cores, reserved | 4 × u32 |
| cores | 256 × u32 |
| bandwidth_multiplier | f64 |

The header size was not stored. Its fixed CPU array coupled the decoder to NCPU.
All events shared one ring/file with 40-byte records:

`u32 type, core, pid, reserved; u64 generation, tsc, value`

Record types:

- 1: bandwidth; value holds the raw 32-bit channel-0 CAS counter.
- 2: LLC; value holds the hardware counter snapshot.
- 3: completion footer; generation holds enqueued record count, value holds drops.
- 4: failed PMC gather; value 1 means the request was still pending.

Bandwidth records have unused CPU/PID/generation fields set to zero. Failure
records have observation timestamps rather than hardware measurement timestamps.
A missing completion footer means the file is incomplete.

## Version 2 — explicit sizes and variable-length CPU list

Replaced the fixed CPU array with an actual-count list. The fixed header prefix
is 64 bytes, followed by nr_cores × u32 CPU IDs:

| Field | Type |
| --- | --- |
| magic | 8 bytes |
| version, header_bytes, record_bytes, prefix_reserved | 4 × u32 |
| cycles_per_us | f64 |
| selector | u64 |
| writer_core, scheduler_core, nr_cores, reserved | 4 × u32 |
| bandwidth_multiplier | f64 |
| cores | nr_cores × u32 |

`header_bytes = 64 + 4 × nr_cores`. Records and completion footer are unchanged
from version 1. The record size remains 40 bytes.

## Version 3 — separate streams with compact records

Each event stream gets its own ring and file. The path argument becomes an output
prefix. The files append `.bw.bin`, `.llc.bin`, and `.pmc-failure.bin`.

The fixed header prefix is 80 bytes, followed by nr_cores × u32 CPU IDs:

| Field | Type |
| --- | --- |
| magic | 8 bytes |
| version, header_bytes, record_bytes, stream_type | 4 × u32 |
| cycles_per_us | f64 |
| selector | u64 |
| writer_core, scheduler_core, nr_cores, flags | 4 × u32 |
| bandwidth_multiplier | f64 |
| produced, dropped | 2 × u64 |
| cores | nr_cores × u32 |

`header_bytes = 80 + 4 × nr_cores`.

| Stream type | Record layout, in order | Size |
| --- | --- | ---: |
| 1: bandwidth | u64 tsc, cas | 16 bytes |
| 2: LLC | u64 tsc, generation, misses; u32 core, pid | 32 bytes |
| 3: PMC failure | u64 tsc, generation; u32 core, reason | 24 bytes |

CAS is the raw 32-bit reading widened to u64; counter differences still wrap at
32 bits. Failure reason 1 means the request was pending at gather time.

Per-record type tags and completion footers were removed. Initially flags and
counts are zero. After draining, the writer updates the header: flag bit 0 means
complete; produced is the number of enqueued records; dropped counts overflow
losses for that stream. Other flag bits are reserved. Each file must contain
exactly produced records after its header. A missing completion flag means the
file is incomplete.

The selector is unused/zero for bandwidth and describes the sampled hardware
event for LLC and PMC failures. bandwidth_multiplier is used only for bandwidth.

## Version 4 — shared experiment UUID

Adds a 16-byte experiment_id after dropped, before the variable CPU list. All
other version-3 fields, stream IDs, record layouts, and completion semantics stay
the same.

- Fixed header prefix: **96 bytes**.
- `header_bytes = 96 + 4 × nr_cores`.
- experiment_id: random version-4 UUID in standard UUID byte order, not an integer
  subject to little-endian conversion.
- One UUID is generated per capture and copied into all three files.
- The decoder prints the UUID as `experiment_id=...` in standard hyphenated form.

Matching UUIDs associate files even if they are renamed or moved. Different
captures receive different IDs. Pairing baseline and enabled runs belongs in
external experiment metadata.

## Version 5 — scheduler generation-change events

Adds a fourth file, `.sched.bin`, identified by stream type 4. The fixed header
remains 96 bytes plus the variable CPU list. The original three record layouts
are unchanged; all four files carry the same experiment UUID and version 5.
The scheduler stream has no PMC selector (zero/not applicable).

Its 24-byte record is, in order:

| Field | Type | Meaning |
| --- | --- | --- |
| tsc | u64 | Scheduler-side timestamp immediately after generation increment. |
| generation | u64 | New IAS generation for the core. |
| pid | u32 | Assigned process, or previous process for explicit idling. |
| core | u16 | Logical CPU ID. |
| reason | u16 | One of the reasons below. |

Reasons:

1. Worker scheduled: ias_run_kthread_on_core successfully requested execution.
2. Idle placeholder: ias_idle_placeholder_on_core kept the core accounted to a
   process while making/keeping it idle (used by the hyperthread controller).
3. Core explicitly idled: ias_idle_on_core removed the assignment; PID is the
   former process or zero if none existed.

These are IAS decisions, not timestamps proving that the target CPU completed a
context switch. Natural worker parking does not itself increment IAS generation;
a subsequent successful scheduling request does. Only successful increment sites
emit an event, and only while capture is active. Events can be lost if their ring
fills; that stream's dropped count reports this independently.

The ring holds 262,144 records (6 MiB). The existing dedicated writer drains it
with the other streams. No new worker thread or scheduler policy is introduced.
Version-5 validation requires all four files. Version-4 captures still require
only their original three files; no events can be reconstructed retroactively.

## Version 6 — application ownership metadata (current)

The file header and four stream IDs remain unchanged. Only the LLC record layout
changes, from 32 to **56 bytes**, in this order:

| Field | Type | Meaning |
| --- | --- | --- |
| tsc | u64 | Counter-read timestamp from the target CPU. |
| sched_gen_at_request | u64 | Original IAS scheduling generation when requested. |
| misses | u64 | Raw cumulative LLC counter reading. |
| app_id_at_request | u64 | Application lifetime ID at request time. |
| app_owner_gen_at_request | u64 | Application ownership generation when requested. |
| app_owner_gen_at_collection | u64 | Ownership generation when collecting the result. |
| core | u32 | Logical CPU ID. |
| pid_at_request | u32 | PID captured when requesting, never looked up at collection. |

Application IDs are monotonic and unique within an IOKernel lifetime, including
reattachment with a reused PID. Combine with experiment UUID across captures.
The ownership generation increments when a different application is assigned to
a core or its owner detaches. Parking, explicit idling, and rescheduling the same
application preserve ownership. The existing IAS scheduling generation is unchanged.

Completed readings are retained across either kind of generation change. An
interval may be attributed to an application only when its ID is nonzero and
identical at both endpoints, and all four ownership-generation values match
(request and collection for each endpoint). A -> B -> A is detected even when
endpoint application IDs match. This is IAS assignment attribution, including
intervening idle/kernel activity; it is not a count of user-mode instructions alone.

The collector keeps one outstanding request per core. A pending request is never
overwritten; a pending failure record counts an observation, not necessarily a
unique request. Normal core eligibility is retained: new requests require an
assigned application and an unpunished core. Collection still accepts a completed
result after the worker parks or its application detaches. Thus a valid interval
can span parking without additional interrupts solely to sample an idle core.

The LLC ring still holds 1,048,576 records, now using 56 MiB. More readings can be
retained than before; disk volume and collection overhead need fresh measurements.
The decoder and summary continue supporting the old layouts in versions 4 and 5.

## Version 7 — scheduling generation at collection

The header and other streams retain their version-6 layouts. LLC records grow
from 56 to 64 bytes. Fields appear in this order:

```
u64 tsc, sched_gen_at_request, misses, app_id_at_request;
u64 app_owner_gen_at_request, app_owner_gen_at_collection, sched_gen_at_collection;
u32 core, pid_at_request;
```

`sched_gen_at_collection` is the current IAS scheduling generation when the
scheduler collects the completed counter request. It is not the generation at
which the target CPU actually read the counter. Comparing it with the request
generation reveals scheduling changes during that period. Attribution continues
to use application ownership generations, so scheduling changes alone do not
invalidate an application interval. The LLC ring now occupies 64 MiB.
The decoder supports versions 4, 5, 6, and 7.

## Future changes

Append a new version section with its exact layout and migration rationale when
introducing an incompatible format. Keep these historical descriptions. Update
the writer, decoder, and format tests together. The decoder must explicitly reject
unsupported versions rather than silently interpreting them as the current layout.

Current usage and measurement semantics are documented in [telemetry.md](telemetry.md).
