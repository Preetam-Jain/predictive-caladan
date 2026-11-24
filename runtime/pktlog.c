/*
 * pktlog.c - attach to the per-packet log segment and hand out chunks
 *
 * Enabled by the runtime config line "pktlog_path <file>". The file lives on
 * hugetlbfs, already exists (pktlogctl create) and is empty (pktlogctl reset).
 * The runtime holds an exclusive flock on it for its lifetime, so pktlogctl
 * cannot dump or reset it while records may still be written; the kernel
 * releases the lock if the process dies. If the line is present and the
 * segment cannot be used, runtime startup fails: an experiment must never run
 * silently without its packet log.
 */

#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/stat.h>

#include <base/log.h>
#include <base/time.h>

#include "defs.h"
#include "pktlog.h"

bool pktlog_enabled;
struct pktlog_kstate pktlog_ks[NCPU];

static struct pktlog_segment_header *seg;
static char *pktlog_path;
static int pktlog_fd = -1;

#define PKTLOG_DROP_FLUSH 1024U

static int parse_pktlog_path(const char *name, const char *val)
{
	if (val[0] != '/') {
		log_err("pktlog: pktlog_path must be absolute, got '%s'", val);
		return -EINVAL;
	}
	pktlog_path = strdup(val);
	return pktlog_path ? 0 : -ENOMEM;
}

static struct cfg_handler pktlog_path_handler = {
	.name = "pktlog_path",
	.fn = parse_pktlog_path,
	.required = false,
};

REGISTER_CFG(pktlog_path_handler);

bool pktlog_claim_chunk(struct pktlog_kstate *s, unsigned int kidx)
{
	struct pktlog_chunk_header *h;
	uint64_t chunk;

	if (s->exhausted)
		return false;

	chunk = __atomic_fetch_add(&seg->next_chunk, 1, __ATOMIC_RELAXED);
	if (chunk >= seg->nr_chunks) {
		s->exhausted = true;
		__atomic_fetch_or(&seg->flags, PKTLOG_SEG_EXHAUSTED, __ATOMIC_RELAXED);
		log_warn_ratelimited("pktlog: segment full; records are being dropped");
		return false;
	}

	h = (struct pktlog_chunk_header *)((unsigned char *)seg + seg->header_bytes +
					   chunk * seg->chunk_bytes);
	memcpy(h->magic, PKTLOG_CHUNK_MAGIC, sizeof(h->magic));
	h->generation = seg->generation;
	h->claim_tsc = rdtsc();
	h->kthread = kidx;
	h->pid = seg->writer_pid;
	memset(h->reserved, 0, sizeof(h->reserved));

	s->pos = (unsigned char *)h + PKTLOG_CHUNK_HEADER_BYTES;
	s->end = (unsigned char *)h + seg->chunk_bytes;
	return true;
}

/* Dropped records are counted in batches to keep remote atomics rare. */
void pktlog_count_drop(struct pktlog_kstate *s)
{
	if (++s->pending_drops < PKTLOG_DROP_FLUSH)
		return;
	__atomic_fetch_add(&seg->dropped, s->pending_drops, __ATOMIC_RELAXED);
	s->pending_drops = 0;
}

static void pktlog_fail_cleanup(void *addr, size_t len)
{
	if (addr)
		munmap(addr, len);
	if (pktlog_fd >= 0)
		close(pktlog_fd);	/* releases the lock */
	pktlog_fd = -1;
	seg = NULL;
}

int pktlog_init(void)
{
	struct stat st;
	void *addr;
	int ret;

	if (!pktlog_path)
		return 0;

	if (!cfg_directpath_enabled()) {
		log_err("pktlog: requires directpath (NIC completion timestamps)");
		return -EINVAL;
	}
	if (maxks > UINT8_MAX + 1) {
		log_err("pktlog: at most 256 kthreads are supported");
		return -EINVAL;
	}

	pktlog_fd = open(pktlog_path, O_RDWR | O_CLOEXEC);
	if (pktlog_fd < 0) {
		ret = -errno;
		log_err("pktlog: cannot open %s (run pktlogctl create): %s",
			pktlog_path, strerror(errno));
		return ret;
	}
	if (flock(pktlog_fd, LOCK_EX | LOCK_NB)) {
		ret = -errno;
		log_err("pktlog: %s is in use by another process: %s", pktlog_path,
			strerror(errno));
		pktlog_fail_cleanup(NULL, 0);
		return ret;
	}
	if (fstat(pktlog_fd, &st) || st.st_size < (off_t)PKTLOG_HEADER_BYTES) {
		log_err("pktlog: %s is not a packet log segment", pktlog_path);
		pktlog_fail_cleanup(NULL, 0);
		return -EINVAL;
	}

	/* MAP_POPULATE maps every (already allocated) page now, not on the
	 * packet path */
	addr = mmap(NULL, st.st_size, PROT_READ | PROT_WRITE,
		    MAP_SHARED | MAP_POPULATE, pktlog_fd, 0);
	if (addr == MAP_FAILED) {
		ret = -errno;
		log_err("pktlog: cannot map %s: %s", pktlog_path, strerror(errno));
		pktlog_fail_cleanup(NULL, 0);
		return ret;
	}
	seg = addr;

	if (memcmp(seg->magic, PKTLOG_MAGIC, sizeof(seg->magic)) ||
	    seg->version != PKTLOG_VERSION ||
	    seg->header_bytes != PKTLOG_HEADER_BYTES ||
	    seg->record_bytes != PKTLOG_RECORD_BYTES ||
	    seg->chunk_bytes != PKTLOG_CHUNK_BYTES ||
	    seg->segment_bytes != (uint64_t)st.st_size ||
	    seg->header_bytes + (uint64_t)seg->nr_chunks * seg->chunk_bytes >
	    seg->segment_bytes) {
		log_err("pktlog: segment layout does not match this runtime");
		pktlog_fail_cleanup(addr, st.st_size);
		return -EINVAL;
	}
	if (seg->next_chunk != 0 || seg->dropped != 0 || seg->flags != 0 ||
	    seg->writer_pid != 0 || seg->dumped != 0) {
		log_err("pktlog: segment holds an earlier run's records (run pktlogctl dump, then reset)");
		pktlog_fail_cleanup(addr, st.st_size);
		return -EEXIST;
	}

	seg->writer_pid = getpid();
	seg->attach_count++;
	seg->attach_tsc = rdtsc();
	seg->cycles_per_us = cycles_per_us;

	/* every kthread claims its first chunk on its first record; the fd
	 * (and its lock) stays open until the process exits */
	memset(pktlog_ks, 0, sizeof(pktlog_ks));
	pktlog_enabled = true;
	log_info("pktlog: attached %s, %u chunks of %lu MiB on node %d",
		 pktlog_path, seg->nr_chunks, (unsigned long)(seg->chunk_bytes >> 20),
		 seg->numa_node);
	return 0;
}
