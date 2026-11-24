/*
 * Test producer for the packet log segment. Mirrors runtime/pktlog.[ch]:
 * per-thread chunk claim with one atomic, chunk header with ordinary stores,
 * records with four movnti stores. Usage:
 *   producer PATH THREADS RECORDS_PER_THREAD [hold-seconds]
 * Record i of thread t has seq_start = i, seq_end = i + 1, remote_port = t.
 */
#define _GNU_SOURCE
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <x86intrin.h>

#include <pktlog.h>

static struct pktlog_segment_header *seg;
static unsigned long per_thread;

struct state {
	unsigned char *pos, *end;
	int exhausted;
	unsigned long drops;
};

static int claim(struct state *s, unsigned int t)
{
	unsigned long c;
	struct pktlog_chunk_header *h;

	if (s->exhausted)
		return 0;
	c = __atomic_fetch_add(&seg->next_chunk, 1, __ATOMIC_RELAXED);
	if (c >= seg->nr_chunks) {
		s->exhausted = 1;
		__atomic_fetch_or(&seg->flags, PKTLOG_SEG_EXHAUSTED, __ATOMIC_RELAXED);
		return 0;
	}
	h = (void *)((unsigned char *)seg + seg->header_bytes + c * seg->chunk_bytes);
	memcpy(h->magic, PKTLOG_CHUNK_MAGIC, 8);
	h->generation = seg->generation;
	h->claim_tsc = __rdtsc();
	h->kthread = t;
	h->pid = seg->writer_pid;
	s->pos = (unsigned char *)h + PKTLOG_CHUNK_HEADER_BYTES;
	s->end = (unsigned char *)h + seg->chunk_bytes;
	return 1;
}

static void *worker(void *arg)
{
	unsigned int t = (unsigned int)(uintptr_t)arg;
	struct state s = {0};

	for (unsigned long i = 0; i < per_thread; i++) {
		union { struct pktlog_record r; long long w[4]; } u;
		long long *dst;

		if (s.pos == s.end && !claim(&s, t)) {
			s.drops++;
			continue;
		}
		memset(&u, 0, sizeof(u));
		u.r.ts = 1000 + i;
		u.r.seq_start = (uint32_t)i;
		u.r.seq_end = (uint32_t)i + 1;
		u.r.remote_port = (uint16_t)t;
		u.r.local_port = 11211;
		u.r.type = (i % 3) + 1;
		u.r.kthread = (uint8_t)t;
		u.r.tsc_low = (uint32_t)__rdtsc();
		dst = (long long *)s.pos;
		_mm_stream_si64(dst, u.w[0]);
		_mm_stream_si64(dst + 1, u.w[1]);
		_mm_stream_si64(dst + 2, u.w[2]);
		_mm_stream_si64(dst + 3, u.w[3]);
		s.pos += sizeof(u.r);
	}
	if (s.drops)
		__atomic_fetch_add(&seg->dropped, s.drops, __ATOMIC_RELAXED);
	return NULL;
}

int main(int argc, char **argv)
{
	pthread_t tid[64];
	unsigned int threads;
	struct stat st;
	int fd;

	assert(argc >= 4);
	fd = open(argv[1], O_RDWR);
	assert(fd >= 0);
	assert(!flock(fd, LOCK_EX | LOCK_NB));	/* as the runtime does */
	assert(!fstat(fd, &st));
	seg = mmap(NULL, st.st_size, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE, fd, 0);
	assert(seg != MAP_FAILED);
	assert(!memcmp(seg->magic, PKTLOG_MAGIC, 8));
	assert(seg->next_chunk == 0 && seg->writer_pid == 0 && !seg->dumped);
	seg->writer_pid = getpid();
	seg->cycles_per_us = 2400.0;
	threads = (unsigned int)atoi(argv[2]);
	per_thread = strtoul(argv[3], NULL, 0);
	for (unsigned int t = 0; t < threads; t++)
		pthread_create(&tid[t], NULL, worker, (void *)(uintptr_t)t);
	for (unsigned int t = 0; t < threads; t++)
		pthread_join(tid[t], NULL);
	_mm_sfence();
	if (argc > 4)
		sleep((unsigned int)atoi(argv[4]));
	munmap(seg, st.st_size);
	close(fd);
	return 0;
}
