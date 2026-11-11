#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/random.h>
#include <sys/uio.h>
#include <base/time.h>
#include <base/log.h>
#include "defs.h"
#include "sched.h"
#include "pmc.h"
#include "ias.h"
#include "telemetry.h"

/* Versioned little-endian disk layouts, documented in docs/telemetry.md. */
struct bandwidth_record {
    uint64_t tsc;
    uint64_t cas;
};

struct llc_record {
    uint64_t tsc;
    uint64_t sched_gen_at_request;
    uint64_t misses;
    uint64_t app_id_at_request;
    uint64_t app_owner_gen_at_request;
    uint64_t app_owner_gen_at_collection;
    uint64_t sched_gen_at_collection;
    uint32_t core;
    uint32_t pid_at_request;
};

/* A failed gather has no counter value or reliable process attribution. */
struct pmc_failure_record {
    uint64_t tsc;
    uint64_t gen;
    uint32_t core;
    uint32_t reason;
};

/* CPU IDs fit in 16 bits; keeping reason/core compact avoids padding. */
struct sched_event_record {
    uint64_t tsc;
    uint64_t gen;
    uint32_t pid;
    uint16_t core;
    uint16_t reason;
};

/* Queue contributions are cycles; identity is the runtime worker TID, even parked. */
struct queue_record {
    uint64_t tsc, runnable_cycles, rx_cycles, timer_cycles;
    uint64_t directpath_cycles, storage_cycles, total_cycles;
    uint32_t pid, tid, core, flags; /* flags: active=1, has_work=2, standing=4 */
};
/* Observations, not completed-task durations. Even RCU generations mean idle. */
struct task_record {
    uint64_t tsc, task_elapsed_us, kthread_elapsed_us;
    uint32_t pid, tid, core, rcu_gen;
};
struct delay_record {
    uint64_t tsc, rx_cycles;
    double max_us, min_us, avg_us;
    uint32_t pid, active_threads, flags, reserved;
    /* flags: has_work=1, parked_busy=2, standing_queue=4 */
};

enum stream_type {
    STREAM_BW = 1,
    STREAM_LLC = 2,
    STREAM_PMC_FAILURE = 3,
    STREAM_SCHED = 4,
    STREAM_QUEUE = 5,
    STREAM_TASK = 6,
    STREAM_DELAY = 7,
};

enum failure_reason {
    PMC_PENDING = 1,
};

enum capture_flags {
    CAPTURE_COMPLETE = 1,
};

struct file_header {
    char magic[8];
    uint32_t version;
    uint32_t header_bytes;
    uint32_t record_bytes;
    uint32_t stream_type;
    double cycles_per_us;
    uint64_t selector;
    uint32_t writer_core;
    uint32_t scheduler_core;
    uint32_t nr_cores;
    uint32_t flags;
    double bandwidth_multiplier;
    uint64_t produced;
    uint64_t dropped;
    uint8_t experiment_id[16];
    uint32_t cores[];
};

_Static_assert(sizeof(struct bandwidth_record) == 16, "bandwidth record size assert failed");
_Static_assert(sizeof(struct llc_record) == 64, "llc record size assert failed");
_Static_assert(sizeof(struct pmc_failure_record) == 24, "pmc failure record size assert failed");
_Static_assert(sizeof(struct sched_event_record) == 24, "scheduler event record size assert failed");
_Static_assert(sizeof(struct queue_record) == 72, "queue record size assert failed");
_Static_assert(sizeof(struct task_record) == 40, "task record size assert failed");
_Static_assert(sizeof(struct delay_record) == 56, "delay record size assert failed");
_Static_assert(NCPU <= (uint32_t)UINT16_MAX + 1, "scheduler event CPU ID width assert failed");
_Static_assert(sizeof(struct file_header) == 96, "file header size assert failed");
_Static_assert(offsetof(struct file_header, cores) == sizeof(struct file_header),
               "file header CPU list offset assert failed");

#define BATCH_BYTES (256U * 1024) // 256 KiB
#define BANDWIDTH_CAPACITY (1U << 16) // 65,536
#define LLC_CAPACITY (1U << 20) // 1,048,576
#define SCHED_EVENT_CAPACITY (1U << 18) // 262,144
#define FAILURE_CAPACITY (1U << 12) // 4,096

/* Version 8 adds queue, task elapsed-time, and process delay streams. */
#define FILE_FORMAT_VERSION 8U
#define MICROSECONDS_PER_SECOND 1000000ULL
/* Bandwidth comes from the memory controller, not a selected core PMC event. */
#define PMC_SELECTOR_NOT_APPLICABLE 0ULL

/* Each stream has one producer and one consumer, with separately owned cursors. */
struct stream {
    const char *suffix;
    enum stream_type type;
    size_t record_bytes;
    size_t capacity;
    size_t batch_size;
    unsigned char *records;
    int fd;
    struct file_header *header;
    _Alignas(64) atomic_uint_fast64_t head;
    uint64_t produced;
    uint64_t dropped;
    _Alignas(64) atomic_uint_fast64_t tail;
};

static struct stream bandwidth_stream = {
    .suffix = ".bw.bin",
    .type = STREAM_BW,
    .record_bytes = sizeof(struct bandwidth_record),
    .capacity = BANDWIDTH_CAPACITY,
    .fd = -1,
};
static struct stream llc_stream = {
    .suffix = ".llc.bin",
    .type = STREAM_LLC,
    .record_bytes = sizeof(struct llc_record),
    .capacity = LLC_CAPACITY,
    .fd = -1,
};
static struct stream failure_stream = {
    .suffix = ".pmc-failure.bin",
    .type = STREAM_PMC_FAILURE,
    .record_bytes = sizeof(struct pmc_failure_record),
    .capacity = FAILURE_CAPACITY,
    .fd = -1,
};
static struct stream sched_stream = {
    .suffix = ".sched.bin",
    .type = STREAM_SCHED,
    .record_bytes = sizeof(struct sched_event_record),
    .capacity = SCHED_EVENT_CAPACITY,
    .fd = -1,
};
static struct stream queue_stream = {
    .suffix = ".queue.bin",
    .type = STREAM_QUEUE,
    .record_bytes = sizeof(struct queue_record),
    .capacity = SCHED_EVENT_CAPACITY,
    .fd = -1,
};
static struct stream task_stream = {
    .suffix = ".task.bin",
    .type = STREAM_TASK,
    .record_bytes = sizeof(struct task_record),
    .capacity = SCHED_EVENT_CAPACITY,
    .fd = -1,
};
static struct stream delay_stream = {
    .suffix = ".delay.bin",
    .type = STREAM_DELAY,
    .record_bytes = sizeof(struct delay_record),
    .capacity = SCHED_EVENT_CAPACITY,
    .fd = -1,
};
static struct stream *const streams[] = {
    &bandwidth_stream,
    &llc_stream,
    &failure_stream,
    &sched_stream,
    &queue_stream,
    &task_stream,
    &delay_stream,
};

int telemetry_core = -1;
const char *telemetry_path;
unsigned int telemetry_seconds = 120;
static atomic_bool finished;
static atomic_int writer_error;
static uint64_t deadline;
static bool active;
static uint8_t experiment_id[16];

/* RFC UUID version 4: random bytes, with version and variant bits set. */
static int generate_experiment_id(void)
{
    size_t bytes_remaining = sizeof(experiment_id);
    uint8_t *cursor = experiment_id;

    while (bytes_remaining) {
        ssize_t bytes_read = getrandom(cursor, bytes_remaining, 0);
        if (bytes_read < 0 && errno == EINTR)
            continue;
        if (bytes_read <= 0)
            return bytes_read < 0 ? errno : EIO;
        cursor += bytes_read;
        bytes_remaining -= bytes_read;
    }
    /* Byte 6's upper nibble identifies version 4; byte 8 starts with 10. */
    const unsigned int version_byte = 6;
    const unsigned int variant_byte = 8;
    const uint8_t version_mask = 0x0f;
    const uint8_t version_bits = 0x40;
    const uint8_t variant_mask = 0x3f;
    const uint8_t variant_bits = 0x80;
    experiment_id[version_byte] = (experiment_id[version_byte] & version_mask) | version_bits;
    experiment_id[variant_byte] = (experiment_id[variant_byte] & variant_mask) | variant_bits;
    return 0;
}

static int write_all(int fd, const void *buffer, size_t bytes_remaining)
{
    const unsigned char *cursor = buffer;
    while (bytes_remaining) {
        ssize_t bytes_written = write(fd, cursor, bytes_remaining);
        if (bytes_written < 0 && errno == EINTR)
            continue;
        if (bytes_written <= 0)
            return bytes_written < 0 ? errno : EIO;
        cursor += bytes_written;
        bytes_remaining -= bytes_written;
    }
    return 0;
}

/* Updates these local descriptors as partial writes consume their contents. */
static int writev_all(int fd, struct iovec *parts, int part_count)
{
    while (part_count) {
        ssize_t bytes_written = writev(fd, parts, part_count);
        if (bytes_written < 0 && errno == EINTR)
            continue;
        if (bytes_written <= 0)
            return bytes_written < 0 ? errno : EIO;

        size_t bytes_consumed = bytes_written;
        while (part_count && bytes_consumed >= parts->iov_len) {
            bytes_consumed -= parts->iov_len;
            parts++;
            part_count--;
        }
        if (part_count) {
            parts->iov_base = (unsigned char *)parts->iov_base + bytes_consumed;
            parts->iov_len -= bytes_consumed;
        }
    }
    return 0;
}

static int stream_open(struct stream *stream)
{
    char *path;
    int error;

    stream->batch_size = BATCH_BYTES / stream->record_bytes;
    if (stream->batch_size > stream->capacity)
        stream->batch_size = stream->capacity;

    error = posix_memalign((void **)&stream->records, 64,
                          stream->capacity * stream->record_bytes);
    if (error)
        return error;
    /* First-touch after dataplane initialization follows the socket's policy. */
    memset(stream->records, 0, stream->capacity * stream->record_bytes);
    if (asprintf(&path, "%s%s", telemetry_path, stream->suffix) < 0)
        return ENOMEM;
    stream->fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
    if (stream->fd < 0) {
        error = errno;
        log_err("telemetry: cannot create %s: %s", path, strerror(error));
        free(path);
        return error;
    }
    free(path);
    size_t header_bytes = sizeof(struct file_header) + sched_cores_nr * sizeof(uint32_t);
    stream->header = calloc(1, header_bytes);
    if (!stream->header)
        return ENOMEM;
    *stream->header = (struct file_header){
        .magic = "CALATLM",
        .version = FILE_FORMAT_VERSION,
        .header_bytes = header_bytes,
        .record_bytes = stream->record_bytes,
        .stream_type = stream->type,
        .cycles_per_us = cycles_per_us,
        .selector = (stream->type == STREAM_LLC || stream->type == STREAM_PMC_FAILURE)
                    ? PMC_LLC_MISSES : PMC_SELECTOR_NOT_APPLICABLE,
        .writer_core = telemetry_core,
        .scheduler_core = sched_dp_core,
        .nr_cores = sched_cores_nr,
        .bandwidth_multiplier = stream->type == STREAM_BW ? ias_bw_estimate_multiplier : 0,
    };
    memcpy(stream->header->experiment_id, experiment_id, sizeof(experiment_id));
    for (int i = 0; i < sched_cores_nr; i++)
        stream->header->cores[i] = sched_cores_tbl[i];
    return write_all(stream->fd, stream->header, stream->header->header_bytes);
}

/* Advance at most one batch per stream per pass, including across a ring wrap. */
static int drain_batch(struct stream *stream, bool done, bool *made_progress)
{
    uint64_t consumed = atomic_load_explicit(&stream->tail, memory_order_relaxed);
    uint64_t available = atomic_load_explicit(&stream->head, memory_order_acquire) - consumed;
    size_t records_to_write = available;
    int error;

    if (!available || (!done && available < stream->batch_size))
        return 0;
    if (records_to_write > stream->batch_size)
        records_to_write = stream->batch_size;
    size_t offset = consumed % stream->capacity;
    size_t first_part = records_to_write;
    if (first_part > stream->capacity - offset)
        first_part = stream->capacity - offset;
    struct iovec parts[] = {
        {
            .iov_base = stream->records + offset * stream->record_bytes,
            .iov_len = first_part * stream->record_bytes,
        },
        {
            .iov_base = stream->records,
            .iov_len = (records_to_write - first_part) * stream->record_bytes,
        },
    };
    int part_count = first_part < records_to_write ? ARRAY_SIZE(parts) : 1;
    error = writev_all(stream->fd, parts, part_count);
    if (error)
        return error;
    atomic_store_explicit(&stream->tail, consumed + records_to_write, memory_order_release);
    *made_progress = true;
    return 0;
}

/* Called only after the producer publishes finished and all rings are drained. */
static int complete_stream(struct stream *stream)
{
    int error;
    if (fsync(stream->fd))
        return errno;
    stream->header->produced = stream->produced;
    stream->header->dropped = stream->dropped;
    stream->header->flags = CAPTURE_COMPLETE;
    if (lseek(stream->fd, 0, SEEK_SET) < 0)
        return errno;
    error = write_all(stream->fd, stream->header, stream->header->header_bytes);
    if (!error && fsync(stream->fd))
        error = errno;
    return error;
}

static void *writer(void *unused)
{
    int error = 0;
    (void)unused;
    for (;;) {
        bool done = atomic_load_explicit(&finished, memory_order_acquire);
        bool made_progress = false;
        for (size_t i = 0; i < ARRAY_SIZE(streams); i++) {
            error = drain_batch(streams[i], done, &made_progress);
            if (error) {
                log_err("telemetry%s: write failed: %s", streams[i]->suffix, strerror(error));
                goto close_streams;
            }
        }
        if (done && !made_progress)
            break;
        if (!made_progress)
            __asm__ volatile("pause");
    }
    for (size_t i = 0; i < ARRAY_SIZE(streams); i++) {
        error = complete_stream(streams[i]);
        if (error) {
            log_err("telemetry%s: finalization failed: %s", streams[i]->suffix, strerror(error));
            goto close_streams;
        }
    }
close_streams:
    /* Report errors before further cleanup so the scheduler stops producing. */
    if (error)
        atomic_store_explicit(&writer_error, error, memory_order_release);
    for (size_t i = 0; i < ARRAY_SIZE(streams); i++) {
        if (close(streams[i]->fd) && !error)
            error = errno;
        streams[i]->fd = -1;
        free(streams[i]->header);
        streams[i]->header = NULL;
    }
    if (error) {
        atomic_store_explicit(&writer_error, error, memory_order_release);
        log_err("telemetry: capture failed: %s", strerror(error));
    } else {
        for (size_t i = 0; i < ARRAY_SIZE(streams); i++)
            log_info("telemetry%s: records=%lu dropped=%lu", streams[i]->suffix,
                     streams[i]->produced, streams[i]->dropped);
        log_info("telemetry: all streams drained and closed");
    }
    return NULL;
}

int telemetry_init(void)
{
    pthread_attr_t attributes;
    pthread_t thread;
    cpu_set_t cpu_set;
    int error;

    if (!telemetry_path || !strcmp(telemetry_path, "-"))
        return 0;
    if (cfg.nobw) {
        log_err("telemetry: bandwidth monitoring is unavailable");
        return -EINVAL;
    }
    error = generate_experiment_id();
    if (error) {
        log_err("telemetry: cannot generate experiment UUID: %s", strerror(error));
        return -error;
    }
    for (size_t i = 0; i < ARRAY_SIZE(streams); i++) {
        error = stream_open(streams[i]);
        if (error)
            goto fail;
    }
    error = pthread_attr_init(&attributes);
    if (error)
        goto fail;
    CPU_ZERO(&cpu_set);
    CPU_SET(telemetry_core, &cpu_set);
    error = pthread_attr_setaffinity_np(&attributes, sizeof(cpu_set), &cpu_set);
    if (!error)
        error = pthread_create(&thread, &attributes, writer, NULL);
    pthread_attr_destroy(&attributes);
    if (error)
        goto fail;
    pthread_detach(thread);
    active = true;
    return 0;
fail:
    log_err("telemetry: initialization failed: %s", strerror(error));
    for (size_t i = 0; i < ARRAY_SIZE(streams); i++) {
        if (streams[i]->fd >= 0)
            close(streams[i]->fd);
        streams[i]->fd = -1;
        free(streams[i]->records);
        streams[i]->records = NULL;
        free(streams[i]->header);
        streams[i]->header = NULL;
    }
    /* Partially created files remain incomplete, never overwriting prior runs. */
    return -error;
}

void telemetry_tick(void)
{
    if (active && (atomic_load_explicit(&writer_error, memory_order_acquire) ||
                   (deadline && rdtsc() >= deadline))) {
        active = false;
        atomic_store_explicit(&finished, true, memory_order_release);
    }
}

static void submit(struct stream *stream, const void *record)
{
    uint64_t next;
    if (!active)
        return;
    if (!deadline)
        deadline = rdtsc() + (uint64_t)telemetry_seconds * MICROSECONDS_PER_SECOND * cycles_per_us;
    next = atomic_load_explicit(&stream->head, memory_order_relaxed);
    if (next - atomic_load_explicit(&stream->tail, memory_order_acquire) == stream->capacity) {
        stream->dropped++;
        return;
    }
    memcpy(stream->records + (next % stream->capacity) * stream->record_bytes,
           record, stream->record_bytes);
    stream->produced++;
    atomic_store_explicit(&stream->head, next + 1, memory_order_release);
}

void telemetry_bandwidth(uint64_t tsc, uint32_t cas)
{
    struct bandwidth_record record = {
        .tsc = tsc,
        .cas = cas,
    };
    submit(&bandwidth_stream, &record);
}

void telemetry_llc(uint32_t core, uint32_t pid_at_request, uint64_t sched_gen_at_request,
                   uint64_t tsc, uint64_t misses, uint64_t app_id_at_request,
                   uint64_t app_owner_gen_at_request, uint64_t app_owner_gen_at_collection,
                   uint64_t sched_gen_at_collection)
{
    struct llc_record record = {
        .tsc = tsc,
        .sched_gen_at_request = sched_gen_at_request,
        .misses = misses,
        .app_id_at_request = app_id_at_request,
        .app_owner_gen_at_request = app_owner_gen_at_request,
        .app_owner_gen_at_collection = app_owner_gen_at_collection,
        .sched_gen_at_collection = sched_gen_at_collection,
        .core = core,
        .pid_at_request = pid_at_request,
    };
    submit(&llc_stream, &record);
}

void telemetry_pmc_failure(uint32_t core, uint64_t gen, uint64_t tsc)
{
    struct pmc_failure_record record = {
        .tsc = tsc,
        .gen = gen,
        .core = core,
        .reason = PMC_PENDING,
    };
    submit(&failure_stream, &record);
}

void telemetry_sched_event(uint32_t core, uint32_t pid, uint64_t gen,
                           enum sched_event_reason reason)
{
    if (!active)
        return;
    struct sched_event_record record = {
        .tsc = rdtsc(),
        .gen = gen,
        .pid = pid,
        .core = core,
        .reason = reason,
    };
    submit(&sched_stream, &record);
}

void telemetry_queue(uint64_t tsc, uint64_t runnable_cycles, uint64_t rx_cycles,
                     uint64_t timer_cycles, uint64_t directpath_cycles,
                     uint64_t storage_cycles, uint64_t total_cycles, uint32_t pid,
                     uint32_t tid, uint32_t core, uint32_t flags)
{
    struct queue_record record = {
        .tsc = tsc,
        .runnable_cycles = runnable_cycles,
        .rx_cycles = rx_cycles,
        .timer_cycles = timer_cycles,
        .directpath_cycles = directpath_cycles,
        .storage_cycles = storage_cycles,
        .total_cycles = total_cycles,
        .pid = pid,
        .tid = tid,
        .core = core,
        .flags = flags,
    };
    submit(&queue_stream, &record);
}

void telemetry_task(uint64_t tsc, uint64_t task_elapsed_us, uint64_t kthread_elapsed_us,
                    uint32_t pid, uint32_t tid, uint32_t core, uint32_t rcu_gen)
{
    struct task_record record = {
        .tsc = tsc,
        .task_elapsed_us = task_elapsed_us,
        .kthread_elapsed_us = kthread_elapsed_us,
        .pid = pid,
        .tid = tid,
        .core = core,
        .rcu_gen = rcu_gen,
    };
    submit(&task_stream, &record);
}

void telemetry_delay(uint64_t tsc, uint64_t rx_cycles, double max_us, double min_us,
                     double avg_us, uint32_t pid, uint32_t active_threads, uint32_t flags)
{
    struct delay_record record = {
        .tsc = tsc,
        .rx_cycles = rx_cycles,
        .max_us = max_us,
        .min_us = min_us,
        .avg_us = avg_us,
        .pid = pid,
        .active_threads = active_threads,
        .flags = flags,
    };
    submit(&delay_stream, &record);
}
