/*
 * pktlog.h - runtime producer side of the per-packet log (see inc/pktlog.h)
 *
 * Each kthread appends records to its own chunk of a shared segment that is
 * bound to the other NUMA node. Records are written with non-temporal stores,
 * so the logged lines are not read for ownership and are not allocated in
 * this socket's caches. movnti uses general registers only. Nothing reads the
 * segment while the application runs, so there is no ring index and no fence
 * per record.
 */

#pragma once

#include <immintrin.h>

#include <base/stddef.h>
#include <pktlog.h>

#include "defs.h"

extern bool pktlog_enabled;

struct pktlog_kstate {
	unsigned char	*pos;
	unsigned char	*end;
	bool		exhausted;
	uint32_t	pending_drops;
} __aligned(CACHE_LINE_SIZE);

extern struct pktlog_kstate pktlog_ks[NCPU];
extern bool pktlog_claim_chunk(struct pktlog_kstate *s, unsigned int kidx);
extern void pktlog_count_drop(struct pktlog_kstate *s);

/* Must be called with preemption disabled; @kidx is the current kthread. */
static inline void pktlog_append_locked(unsigned int kidx, uint8_t type,
					uint64_t ts, uint32_t seq_start,
					uint32_t seq_end, uint32_t remote_ip,
					uint16_t remote_port, uint16_t local_port)
{
	struct pktlog_kstate *s = &pktlog_ks[kidx];
	union {
		struct pktlog_record r;
		long long w[4];
	} u;
	long long *dst;

	if (unlikely(s->pos == s->end) && !pktlog_claim_chunk(s, kidx)) {
		pktlog_count_drop(s);
		return;
	}

	u.r.ts = ts;
	u.r.seq_start = seq_start;
	u.r.seq_end = seq_end;
	u.r.remote_ip = remote_ip;
	u.r.remote_port = remote_port;
	u.r.local_port = local_port;
	u.r.type = type;
	u.r.kthread = (uint8_t)kidx;
	u.r.flags = 0;
	u.r.tsc_low = (uint32_t)rdtsc();

	/* movnti: four 8-byte non-temporal stores using general registers */
	dst = (long long *)s->pos;
	_mm_stream_si64(dst, u.w[0]);
	_mm_stream_si64(dst + 1, u.w[1]);
	_mm_stream_si64(dst + 2, u.w[2]);
	_mm_stream_si64(dst + 3, u.w[3]);
	s->pos += sizeof(u.r);
}

/* Callable from any context; disables preemption around the append. */
static inline void pktlog_append(uint8_t type, uint64_t ts, uint32_t seq_start,
				 uint32_t seq_end, uint32_t remote_ip,
				 uint16_t remote_port, uint16_t local_port)
{
	struct kthread *k;

	if (likely(!pktlog_enabled))
		return;
	k = getk();
	pktlog_append_locked(kthread_idx(k), type, ts, seq_start, seq_end,
			     remote_ip, remote_port, local_port);
	putk();
}
