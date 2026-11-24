/*
 * pktlogctl - manage the per-packet log segment (layout: inc/pktlog.h)
 *
 *   pktlogctl create  --path FILE --gib N --node M [--no-hugetlb] [--threads T]
 *   pktlogctl stat    --path FILE
 *   pktlogctl dump    --path FILE --out PREFIX [--experiment-id-from CAPTURE.bw.bin]
 *   pktlogctl reset   --path FILE [--threads T] [--discard-undumped]
 *   pktlogctl destroy --path FILE [--discard-undumped]
 *   pktlogctl now
 *
 * FILE is on hugetlbfs (e.g. /dev/hugepages/caladan-pktlog); --no-hugetlb
 * allows any filesystem (tests). Every command prints one JSON object on
 * stdout and exits nonzero on any failure. dump, reset and destroy take an
 * exclusive flock, so they refuse to run while a runtime has the segment
 * mapped. reset and destroy refuse to discard records that were not dumped
 * unless --discard-undumped is given.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <stdarg.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/vfs.h>
#include <sys/uio.h>
#include <linux/mempolicy.h>
#include <x86intrin.h>

#include <pktlog.h>

#define HUGETLBFS_MAGIC 0x958458f6

#define MAX_KTHREADS 256
#define HUGEPAGE_BYTES (2UL << 20)
#define DROP_BATCH (64UL << 20)	/* drop page cache every 64 MiB written */

static const char *cmd;

static void fail(const char *fmt, ...) __attribute__((noreturn, format(printf, 1, 2)));
static void fail(const char *fmt, ...)
{
	va_list ap;
	char message[512];

	va_start(ap, fmt);
	vsnprintf(message, sizeof(message), fmt, ap);
	va_end(ap);
	printf("{\"command\": \"%s\", \"ok\": false, \"error\": \"", cmd ? cmd : "?");
	for (const char *p = message; *p; p++) {
		if (*p == '"' || *p == '\\')
			putchar('\\');
		putchar(*p == '\n' ? ' ' : *p);
	}
	printf("\"}\n");
	exit(1);
}

struct options {
	const char *path;
	unsigned long gib;
	int node;
	bool no_hugetlb;
	bool discard_undumped;
	unsigned int threads;
	const char *out;
	const char *experiment_from;
};

struct segment {
	int fd;
	size_t bytes;
	struct pktlog_segment_header *h;
};

/* Opens and maps the segment; with @exclusive, fails if a runtime holds it. */
static void open_segment(const char *path, bool exclusive, struct segment *sg)
{
	struct stat st;
	void *addr;

	sg->fd = open(path, O_RDWR | O_CLOEXEC);
	if (sg->fd < 0)
		fail("cannot open %s: %s", path, strerror(errno));
	if (exclusive && flock(sg->fd, LOCK_EX | LOCK_NB))
		fail("%s is in use (a runtime still has it open); stop the application first",
		     path);
	if (fstat(sg->fd, &st))
		fail("cannot stat %s: %s", path, strerror(errno));
	if (st.st_size < (off_t)PKTLOG_HEADER_BYTES)
		fail("%s is too small to be a packet log segment", path);
	addr = mmap(NULL, st.st_size, PROT_READ | PROT_WRITE, MAP_SHARED, sg->fd, 0);
	if (addr == MAP_FAILED)
		fail("cannot map %s: %s", path, strerror(errno));
	sg->bytes = st.st_size;
	sg->h = addr;
}

static void close_segment(struct segment *sg)
{
	munmap(sg->h, sg->bytes);
	close(sg->fd);
}

static bool locked_by_other(const char *path)
{
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	bool busy;

	if (fd < 0)
		return false;
	busy = flock(fd, LOCK_EX | LOCK_NB) != 0;
	close(fd);
	return busy;
}

static void check_layout(const struct pktlog_segment_header *h, size_t shm_bytes)
{
	if (memcmp(h->magic, PKTLOG_MAGIC, sizeof(h->magic)) ||
	    h->version != PKTLOG_VERSION || h->header_bytes != PKTLOG_HEADER_BYTES ||
	    h->record_bytes != PKTLOG_RECORD_BYTES || h->chunk_bytes != PKTLOG_CHUNK_BYTES ||
	    h->segment_bytes != shm_bytes ||
	    h->header_bytes + (uint64_t)h->nr_chunks * h->chunk_bytes > h->segment_bytes)
		fail("segment layout does not match pktlogctl (version %u)", PKTLOG_VERSION);
}

static unsigned char *chunk_base(struct pktlog_segment_header *h, uint64_t i)
{
	return (unsigned char *)h + h->header_bytes + i * h->chunk_bytes;
}

static uint64_t used_chunks(const struct pktlog_segment_header *h)
{
	uint64_t next = __atomic_load_n(&h->next_chunk, __ATOMIC_ACQUIRE);

	return next < h->nr_chunks ? next : h->nr_chunks;
}

/* ---------- parallel touch / zero ---------- */

struct range_job {
	unsigned char *base;
	size_t bytes;
	bool zero;
};

static void *range_worker(void *arg)
{
	struct range_job *job = arg;

	if (job->zero) {
		memset(job->base, 0, job->bytes);
	} else {
		/* write one byte per page so each page is faulted under the policy */
		for (size_t off = 0; off < job->bytes; off += 4096)
			((volatile unsigned char *)job->base)[off] = 0;
	}
	return NULL;
}

static void parallel_range(unsigned char *base, size_t bytes, unsigned int threads,
			   bool zero)
{
	pthread_t tid[256];
	struct range_job jobs[256];
	size_t per;

	if (threads == 0)
		threads = 1;
	if (threads > 256)
		threads = 256;
	per = (bytes / threads + HUGEPAGE_BYTES - 1) & ~(HUGEPAGE_BYTES - 1);
	for (unsigned int i = 0; i < threads; i++) {
		size_t start = (size_t)i * per;

		jobs[i].base = base + (start < bytes ? start : bytes);
		jobs[i].bytes = start >= bytes ? 0 : (bytes - start < per ? bytes - start : per);
		jobs[i].zero = zero;
		if (pthread_create(&tid[i], NULL, range_worker, &jobs[i]))
			fail("cannot create worker thread");
	}
	for (unsigned int i = 0; i < threads; i++)
		pthread_join(tid[i], NULL);
}

/* Returns the number of sampled pages not on @node, or -1 if unknown. */
static long verify_node(void *base, size_t bytes, int node)
{
	enum { SAMPLES = 64 };
	void *pages[SAMPLES];
	int status[SAMPLES];
	long wrong = 0;

	for (int i = 0; i < SAMPLES; i++)
		pages[i] = (unsigned char *)base +
			   ((bytes / SAMPLES) * i & ~(HUGEPAGE_BYTES - 1));
	if (syscall(SYS_move_pages, 0, SAMPLES, pages, NULL, status, 0))
		return -1;
	for (int i = 0; i < SAMPLES; i++)
		if (status[i] != node)
			wrong++;
	return wrong;
}

/* ---------- commands ---------- */

static long free_hugepages(int node)
{
	char path[128];
	FILE *f;
	long n = -1;

	snprintf(path, sizeof(path),
		 "/sys/devices/system/node/node%d/hugepages/hugepages-2048kB/free_hugepages",
		 node);
	f = fopen(path, "r");
	if (!f)
		return -1;
	if (fscanf(f, "%ld", &n) != 1)
		n = -1;
	fclose(f);
	return n;
}

static void cmd_create(const struct options *o)
{
	uint64_t bytes, nr_chunks;
	struct pktlog_segment_header *h;
	unsigned long mask;
	long wrong, free_pages;
	struct timespec t0, t1;
	struct statfs fs;
	int fd;

	if (o->gib == 0 || o->node < 0 || o->node >= 64)
		fail("create needs --gib N and --node M");
	bytes = (uint64_t)o->gib << 30;
	nr_chunks = (bytes - PKTLOG_HEADER_BYTES) / PKTLOG_CHUNK_BYTES;
	/* round the segment so header + chunks fit in whole hugepages */
	bytes = PKTLOG_HEADER_BYTES + nr_chunks * PKTLOG_CHUNK_BYTES;
	bytes = (bytes + HUGEPAGE_BYTES - 1) & ~(HUGEPAGE_BYTES - 1);

	fd = open(o->path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
	if (fd < 0)
		fail("cannot create %s: %s", o->path, strerror(errno));
	if (fstatfs(fd, &fs) || (!o->no_hugetlb && (unsigned long)fs.f_type != HUGETLBFS_MAGIC)) {
		close(fd);
		unlink(o->path);
		fail("%s is not on hugetlbfs (mount one, e.g. /dev/hugepages)", o->path);
	}
	if (!o->no_hugetlb) {
		free_pages = free_hugepages(o->node);
		if (free_pages < 0 || (uint64_t)free_pages < bytes / HUGEPAGE_BYTES) {
			close(fd);
			unlink(o->path);
			fail("node %d has %ld free 2 MiB hugepages, %" PRIu64 " needed; reserve them "
			     "(early after boot) in /sys/devices/system/node/node%d/hugepages",
			     o->node, free_pages, bytes / HUGEPAGE_BYTES, o->node);
		}
	}
	if (ftruncate(fd, bytes)) {
		int e = errno;

		close(fd);
		unlink(o->path);
		fail("cannot size %s to %" PRIu64 " bytes: %s", o->path, bytes, strerror(e));
	}
	h = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (h == MAP_FAILED) {
		int e = errno;

		close(fd);
		unlink(o->path);
		fail("cannot map %s: %s", o->path, strerror(e));
	}

	/* bind before the first touch, then fault every page in */
	mask = 1UL << o->node;
	if (syscall(SYS_mbind, h, bytes, MPOL_BIND, &mask, 64, 0)) {
		int e = errno;

		munmap(h, bytes);
		close(fd);
		unlink(o->path);
		fail("mbind to node %d failed: %s", o->node, strerror(e));
	}
	clock_gettime(CLOCK_MONOTONIC, &t0);
	parallel_range((unsigned char *)h, bytes, o->threads, false);
	clock_gettime(CLOCK_MONOTONIC, &t1);
	wrong = verify_node(h, bytes, o->node);
	if (wrong > 0) {
		munmap(h, bytes);
		close(fd);
		unlink(o->path);
		fail("%ld of 64 sampled pages are not on node %d; segment removed",
		     wrong, o->node);
	}

	memset(h, 0, PKTLOG_HEADER_BYTES);
	memcpy(h->magic, PKTLOG_MAGIC, sizeof(h->magic));
	h->version = PKTLOG_VERSION;
	h->header_bytes = PKTLOG_HEADER_BYTES;
	h->segment_bytes = bytes;
	h->chunk_bytes = PKTLOG_CHUNK_BYTES;
	h->nr_chunks = (uint32_t)nr_chunks;
	h->record_bytes = PKTLOG_RECORD_BYTES;
	h->numa_node = o->node;
	h->generation = 1;
	__atomic_thread_fence(__ATOMIC_SEQ_CST);
	munmap(h, bytes);
	close(fd);

	printf("{\"command\": \"create\", \"ok\": true, \"path\": \"%s\", "
	       "\"bytes\": %" PRIu64 ", \"chunks\": %" PRIu64 ", \"node\": %d, "
	       "\"hugetlb\": %s, \"placement_verified\": %s, \"prefault_seconds\": %.2f}\n",
	       o->path, bytes, nr_chunks, o->node, o->no_hugetlb ? "false" : "true",
	       wrong == 0 ? "true" : "false",
	       (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9);
}

static void print_state(const char *name, const char *path, bool in_use,
			const struct pktlog_segment_header *h)
{
	uint64_t used = used_chunks(h);

	printf("{\"command\": \"%s\", \"ok\": true, \"path\": \"%s\", \"in_use\": %s, "
	       "\"bytes\": %" PRIu64 ", \"chunks\": %u, \"used_chunks\": %" PRIu64 ", "
	       "\"used_bytes\": %" PRIu64 ", \"node\": %d, \"generation\": %" PRIu64 ", "
	       "\"writer_pid\": %u, \"dropped\": %" PRIu64 ", \"exhausted\": %s, "
	       "\"dumped\": %s, \"empty\": %s}\n",
	       name, path, in_use ? "true" : "false", h->segment_bytes, h->nr_chunks,
	       used, used * h->chunk_bytes, h->numa_node, h->generation, h->writer_pid,
	       h->dropped, (h->flags & PKTLOG_SEG_EXHAUSTED) ? "true" : "false",
	       h->dumped ? "true" : "false",
	       (h->next_chunk == 0 && h->writer_pid == 0 && h->dropped == 0 &&
		h->flags == 0 && h->dumped == 0) ? "true" : "false");
}

static void cmd_stat(const struct options *o)
{
	struct segment sg;
	bool in_use = locked_by_other(o->path);

	open_segment(o->path, false, &sg);
	check_layout(sg.h, sg.bytes);
	print_state("stat", o->path, in_use, sg.h);
	close_segment(&sg);
}

static void read_experiment_id(const char *path, uint8_t id[16])
{
	unsigned char header[96];
	int fd;

	memset(id, 0, 16);
	if (!path)
		return;
	fd = open(path, O_RDONLY);
	if (fd < 0)
		fail("cannot open %s: %s", path, strerror(errno));
	if (read(fd, header, sizeof(header)) != (ssize_t)sizeof(header) ||
	    memcmp(header, "CALATLM", 8))
		fail("%s is not a telemetry capture file", path);
	memcpy(id, header + 80, 16);
	close(fd);
}

static void write_full(int fd, const void *buf, size_t len, const char *path)
{
	const unsigned char *p = buf;

	while (len) {
		ssize_t n = write(fd, p, len);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			fail("write to %s failed: %s", path, n < 0 ? strerror(errno) : "short write");
		p += n;
		len -= n;
	}
}

static void pwrite_full(int fd, const void *buf, size_t len, off_t off, const char *path)
{
	const unsigned char *p = buf;

	while (len) {
		ssize_t n = pwrite(fd, p, len, off);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			fail("pwrite to %s failed: %s", path, n < 0 ? strerror(errno) : "short write");
		p += n;
		len -= n;
		off += n;
	}
}

/* Start writeback for what was written and drop it from the page cache. */
static void drop_written(int fd, off_t *dropped_to, off_t written)
{
	if (written - *dropped_to < (off_t)DROP_BATCH)
		return;
	sync_file_range(fd, *dropped_to, written - *dropped_to,
			SYNC_FILE_RANGE_WAIT_BEFORE | SYNC_FILE_RANGE_WRITE |
			SYNC_FILE_RANGE_WAIT_AFTER);
	posix_fadvise(fd, *dropped_to, written - *dropped_to, POSIX_FADV_DONTNEED);
	*dropped_to = written;
}

struct kthread_dump {
	int fd;
	char *path;
	uint64_t records;
	uint32_t chunks;
	off_t written;
	off_t dropped_to;
	uint64_t type_counts[4];
};

static void cmd_dump(const struct options *o)
{
	struct segment sg;
	struct pktlog_segment_header *h;
	struct kthread_dump kd[MAX_KTHREADS];
	struct pktlog_file_header fh;
	uint8_t experiment_id[16];
	uint64_t used, total_records = 0;
	unsigned int files = 0;
	uint32_t file_flags;

	if (!o->out)
		fail("dump needs --out PREFIX");
	read_experiment_id(o->experiment_from, experiment_id);
	open_segment(o->path, true, &sg);
	h = sg.h;
	check_layout(h, sg.bytes);
	if (h->dumped)
		fail("segment generation %" PRIu64 " was already dumped; reset it before reuse",
		     h->generation);

	memset(kd, 0, sizeof(kd));
	used = used_chunks(h);
	file_flags = PKTLOG_FILE_COMPLETE |
		     ((h->flags & PKTLOG_SEG_EXHAUSTED) ? PKTLOG_FILE_EXHAUSTED : 0);

	for (uint64_t c = 0; c < used; c++) {
		unsigned char *base = chunk_base(h, c);
		struct pktlog_chunk_header *ch = (struct pktlog_chunk_header *)base;
		const struct pktlog_record *r =
			(const struct pktlog_record *)(base + PKTLOG_CHUNK_HEADER_BYTES);
		uint64_t n = 0;
		struct kthread_dump *k;

		if (memcmp(ch->magic, PKTLOG_CHUNK_MAGIC, sizeof(ch->magic)) ||
		    ch->generation != h->generation || ch->kthread >= MAX_KTHREADS)
			fail("chunk %" PRIu64 " has an invalid header (claimed but never written?)", c);

		while (n < PKTLOG_RECORDS_PER_CHUNK && r[n].type != PKTLOG_EMPTY) {
			if (r[n].type > PKTLOG_CONN || r[n].kthread != ch->kthread)
				fail("chunk %" PRIu64 " record %" PRIu64 " is corrupt", c, n);
			n++;
		}
		/* nothing may follow the first empty slot in a chunk */
		for (uint64_t m = n; m < PKTLOG_RECORDS_PER_CHUNK && m < n + 64; m++)
			if (r[m].type != PKTLOG_EMPTY)
				fail("chunk %" PRIu64 " has records after an empty slot", c);

		k = &kd[ch->kthread];
		if (!k->path) {
			if (asprintf(&k->path, "%s.pkt.k%03u.bin", o->out, ch->kthread) < 0)
				fail("out of memory");
			k->fd = open(k->path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
			if (k->fd < 0)
				fail("cannot create %s: %s", k->path, strerror(errno));
			memset(&fh, 0, sizeof(fh));	/* incomplete until rewritten */
			write_full(k->fd, &fh, sizeof(fh), k->path);
			k->written = sizeof(fh);
			files++;
		}
		for (uint64_t i = 0; i < n; i++)
			k->type_counts[r[i].type]++;
		write_full(k->fd, r, n * sizeof(*r), k->path);
		k->written += n * sizeof(*r);
		k->records += n;
		k->chunks++;
		total_records += n;
		drop_written(k->fd, &k->dropped_to, k->written);
	}

	for (unsigned int t = 0; t < MAX_KTHREADS; t++) {
		struct kthread_dump *k = &kd[t];

		if (!k->path)
			continue;
		if (fsync(k->fd))
			fail("fsync %s: %s", k->path, strerror(errno));
		memset(&fh, 0, sizeof(fh));
		memcpy(fh.magic, PKTLOG_FILE_MAGIC, sizeof(fh.magic));
		fh.version = PKTLOG_FILE_VERSION;
		fh.header_bytes = sizeof(fh);
		fh.record_bytes = PKTLOG_RECORD_BYTES;
		fh.kthread = t;
		fh.records = k->records;
		fh.segment_generation = h->generation;
		fh.segment_dropped = h->dropped;
		fh.cycles_per_us = h->cycles_per_us;
		fh.flags = file_flags;
		fh.chunks = k->chunks;
		fh.writer_pid = h->writer_pid;
		memcpy(fh.experiment_id, experiment_id, sizeof(fh.experiment_id));
		pwrite_full(k->fd, &fh, sizeof(fh), 0, k->path);
		if (fsync(k->fd))
			fail("fsync %s: %s", k->path, strerror(errno));
		posix_fadvise(k->fd, 0, 0, POSIX_FADV_DONTNEED);
		close(k->fd);
	}

	h->dumped = 1;
	__atomic_thread_fence(__ATOMIC_SEQ_CST);

	printf("{\"command\": \"dump\", \"ok\": true, \"generation\": %" PRIu64 ", "
	       "\"writer_pid\": %u, \"used_chunks\": %" PRIu64 ", \"records\": %" PRIu64 ", "
	       "\"dropped\": %" PRIu64 ", \"exhausted\": %s, \"files\": [",
	       h->generation, h->writer_pid, used, total_records, h->dropped,
	       (h->flags & PKTLOG_SEG_EXHAUSTED) ? "true" : "false");
	for (unsigned int t = 0, first = 1; t < MAX_KTHREADS; t++) {
		struct kthread_dump *k = &kd[t];

		if (!k->path)
			continue;
		printf("%s{\"path\": \"%s\", \"kthread\": %u, \"records\": %" PRIu64
		       ", \"rx\": %" PRIu64 ", \"tx\": %" PRIu64 ", \"conn\": %" PRIu64 "}",
		       first ? "" : ", ", k->path, t, k->records, k->type_counts[PKTLOG_RX],
		       k->type_counts[PKTLOG_TX], k->type_counts[PKTLOG_CONN]);
		first = 0;
		free(k->path);
	}
	printf("], \"file_count\": %u}\n", files);
	close_segment(&sg);
}

static void cmd_reset(const struct options *o)
{
	struct segment sg;
	struct pktlog_segment_header *h;
	uint64_t used;
	bool empty;

	open_segment(o->path, true, &sg);
	h = sg.h;
	check_layout(h, sg.bytes);
	used = used_chunks(h);
	empty = h->next_chunk == 0 && h->writer_pid == 0;
	if (!empty && !h->dumped && !o->discard_undumped)
		fail("segment holds records that were not dumped; dump first or pass --discard-undumped");

	parallel_range(chunk_base(h, 0), used * h->chunk_bytes, o->threads, true);
	h->next_chunk = 0;
	h->dropped = 0;
	h->flags = 0;
	h->writer_pid = 0;
	h->attach_tsc = 0;
	h->cycles_per_us = 0;
	h->dumped = 0;
	h->generation++;
	__atomic_thread_fence(__ATOMIC_SEQ_CST);
	print_state("reset", o->path, false, h);
	close_segment(&sg);
}

static void cmd_destroy(const struct options *o)
{
	struct segment sg;
	struct pktlog_segment_header *h;

	open_segment(o->path, true, &sg);
	h = sg.h;
	if (!memcmp(h->magic, PKTLOG_MAGIC, sizeof(h->magic)) &&
	    h->next_chunk != 0 && !h->dumped && !o->discard_undumped)
		fail("segment holds records that were not dumped; dump first or pass --discard-undumped");
	if (unlink(o->path))
		fail("cannot remove %s: %s", o->path, strerror(errno));
	close_segment(&sg);
	printf("{\"command\": \"destroy\", \"ok\": true, \"path\": \"%s\"}\n", o->path);
}

static void cmd_now(void)
{
	struct timespec real, mono;
	unsigned int aux;
	uint64_t before, after;

	before = __rdtscp(&aux);
	clock_gettime(CLOCK_REALTIME, &real);
	clock_gettime(CLOCK_MONOTONIC, &mono);
	after = __rdtscp(&aux);
	printf("{\"command\": \"now\", \"ok\": true, \"tsc_before\": %" PRIu64
	       ", \"tsc_after\": %" PRIu64 ", \"realtime_ns\": %lld, \"monotonic_ns\": %lld}\n",
	       before, after, (long long)real.tv_sec * 1000000000LL + real.tv_nsec,
	       (long long)mono.tv_sec * 1000000000LL + mono.tv_nsec);
}

int main(int argc, char **argv)
{
	static const struct option long_options[] = {
		{"path", required_argument, NULL, 'p'},
		{"gib", required_argument, NULL, 'g'},
		{"node", required_argument, NULL, 'n'},
		{"out", required_argument, NULL, 'o'},
		{"experiment-id-from", required_argument, NULL, 'e'},
		{"threads", required_argument, NULL, 't'},
		{"no-hugetlb", no_argument, NULL, 'H'},
		{"discard-undumped", no_argument, NULL, 'D'},
		{NULL, 0, NULL, 0},
	};
	struct options o = { .node = -1, .threads = 16 };
	int c;

	if (argc < 2)
		fail("usage: pktlogctl create|stat|dump|reset|destroy|now [options]");
	cmd = argv[1];
	optind = 2;
	while ((c = getopt_long(argc, argv, "", long_options, NULL)) != -1) {
		switch (c) {
		case 'p': o.path = optarg; break;
		case 'g': o.gib = strtoul(optarg, NULL, 0); break;
		case 'n': o.node = atoi(optarg); break;
		case 'o': o.out = optarg; break;
		case 'e': o.experiment_from = optarg; break;
		case 't': o.threads = (unsigned int)atoi(optarg); break;
		case 'H': o.no_hugetlb = true; break;
		case 'D': o.discard_undumped = true; break;
		default: fail("unknown option");
		}
	}
	if (strcmp(cmd, "now") && (!o.path || o.path[0] != '/'))
		fail("--path must be an absolute file path");

	if (!strcmp(cmd, "create"))
		cmd_create(&o);
	else if (!strcmp(cmd, "stat"))
		cmd_stat(&o);
	else if (!strcmp(cmd, "dump"))
		cmd_dump(&o);
	else if (!strcmp(cmd, "reset"))
		cmd_reset(&o);
	else if (!strcmp(cmd, "destroy"))
		cmd_destroy(&o);
	else if (!strcmp(cmd, "now"))
		cmd_now();
	else
		fail("unknown command");
	return 0;
}
