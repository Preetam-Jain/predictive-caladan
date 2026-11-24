/*
 * pktlog.h - layout of the per-packet log kept in a file on hugetlbfs
 *
 * The segment file is created once per machine session by
 * scripts/pktlog/pktlogctl (its pages bound to the NUMA node that does not run
 * Caladan), mapped by the runtime of the logged application, and dumped to
 * ordinary files by pktlogctl after the application exits. A file is used
 * rather than SysV shared memory because Caladan machine setup sets
 * kernel.shm_rmid_forced=1, which destroys a SysV segment when its last
 * user detaches. Nothing reads the segment while an experiment runs.
 *
 * Little-endian, x86-64 only. Changing any layout here requires bumping
 * PKTLOG_VERSION (segment) or PKTLOG_FILE_VERSION (dump files) and updating
 * docs/packet-logging.md, scripts/pktlog/pktlogctl.c and
 * scripts/join_requests.py.
 */

#pragma once

#include <stdint.h>

#define PKTLOG_MAGIC		"CALPKTL"
#define PKTLOG_CHUNK_MAGIC	"CALPKTC"
#define PKTLOG_FILE_MAGIC	"CALPKTD"
#define PKTLOG_VERSION		1U
#define PKTLOG_FILE_VERSION	1U

#define PKTLOG_HEADER_BYTES	4096UL
#define PKTLOG_CHUNK_BYTES	(64UL << 20)	/* 64 MiB, a multiple of 2 MiB */
#define PKTLOG_RECORD_BYTES	32U
#define PKTLOG_CHUNK_HEADER_BYTES 64U
#define PKTLOG_RECORDS_PER_CHUNK \
	((PKTLOG_CHUNK_BYTES - PKTLOG_CHUNK_HEADER_BYTES) / PKTLOG_RECORD_BYTES)

/* Record types. Zero marks unused space at the end of a chunk. */
enum pktlog_type {
	PKTLOG_EMPTY = 0,
	PKTLOG_RX = 1,		/* TCP segment with payload received */
	PKTLOG_TX = 2,		/* TCP segment with payload transmitted */
	PKTLOG_CONN = 3,	/* TCP connection reached ESTABLISHED */
};

/* Segment flags. */
#define PKTLOG_SEG_EXHAUSTED	0x1U	/* a producer found no free chunk */

/*
 * One 32-byte record. All multi-byte fields are host (little-endian) order.
 *
 * RX:   ts = NIC completion timestamp (device clock ticks) of the packet
 *       seq_start/seq_end = TCP sequence range of the payload [start, end)
 * TX:   ts = NIC completion timestamp of the transmitted packet
 *       seq_start/seq_end = TCP sequence range of the payload [start, end)
 * CONN: ts = rdtsc at the state change
 *       seq_start = initial receive sequence number (irs)
 *       seq_end   = initial send sequence number (iss)
 *
 * tsc_low is the low 32 bits of rdtsc when the runtime wrote the record.
 */
struct pktlog_record {
	uint64_t ts;
	uint32_t seq_start;
	uint32_t seq_end;
	uint32_t remote_ip;
	uint16_t remote_port;
	uint16_t local_port;
	uint8_t  type;
	uint8_t  kthread;
	uint16_t flags;
	uint32_t tsc_low;
};

/* First 64 bytes of every claimed chunk. Written with ordinary stores. */
struct pktlog_chunk_header {
	char     magic[8];
	uint64_t generation;	/* must equal the segment generation */
	uint64_t claim_tsc;
	uint32_t kthread;
	uint32_t pid;
	uint8_t  reserved[32];
};

/*
 * Segment header, at offset 0 of the segment, PKTLOG_HEADER_BYTES long.
 * Chunk i starts at PKTLOG_HEADER_BYTES + i * chunk_bytes.
 */
struct pktlog_segment_header {
	char     magic[8];
	uint32_t version;
	uint32_t header_bytes;
	uint64_t segment_bytes;
	uint64_t chunk_bytes;
	uint32_t nr_chunks;
	uint32_t record_bytes;
	int32_t  numa_node;
	uint32_t dumped;		/* set by pktlogctl dump; reset requires it */
	uint64_t generation;	/* incremented by every reset */
	/* Written by the runtime when it attaches. */
	uint32_t writer_pid;
	uint32_t attach_count;
	uint64_t attach_tsc;
	double   cycles_per_us;
	/* Producer-shared counters, each on its own cache line. */
	_Alignas(64) uint64_t next_chunk;	/* atomic; may exceed nr_chunks */
	_Alignas(64) uint64_t dropped;		/* atomic; records lost after exhaustion */
	_Alignas(64) uint32_t flags;		/* atomic; PKTLOG_SEG_* */
};

/* Header of each dump file written by pktlogctl dump: one file per kthread. */
struct pktlog_file_header {
	char     magic[8];
	uint32_t version;
	uint32_t header_bytes;
	uint32_t record_bytes;
	uint32_t kthread;
	uint64_t records;
	uint64_t segment_generation;
	uint64_t segment_dropped;	/* segment-wide, repeated in every file */
	double   cycles_per_us;
	uint32_t flags;			/* bit 0: complete; bit 1: segment exhausted */
	uint32_t chunks;
	uint32_t writer_pid;
	uint32_t reserved;
	uint8_t  experiment_id[16];	/* copied from the IOKernel capture, or zero */
	uint64_t reserved2;
};

#define PKTLOG_FILE_COMPLETE	0x1U
#define PKTLOG_FILE_EXHAUSTED	0x2U

_Static_assert(sizeof(struct pktlog_record) == PKTLOG_RECORD_BYTES,
	       "pktlog record size");
_Static_assert(sizeof(struct pktlog_chunk_header) == PKTLOG_CHUNK_HEADER_BYTES,
	       "pktlog chunk header size");
_Static_assert(sizeof(struct pktlog_segment_header) <= PKTLOG_HEADER_BYTES,
	       "pktlog segment header size");
_Static_assert(sizeof(struct pktlog_file_header) == 96, "pktlog file header size");
_Static_assert(PKTLOG_CHUNK_BYTES % (2UL << 20) == 0, "chunk must be hugepage aligned");
