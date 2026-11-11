/* Standalone test of the actual implementation, without ksched/NIC dependencies. */
#include "../../iokernel/telemetry.c"
#include <assert.h>
#include <stdio.h>

int cycles_per_us = 2400;
unsigned int sched_dp_core;
int sched_cores_nr;
unsigned int sched_cores_tbl[NCPU];
float ias_bw_estimate_multiplier;
struct iokernel_cfg cfg;
void logk(int level, const char *fmt, ...) { (void)level; (void)fmt; }

static const enum sched_event_reason sched_reasons[] = {
    SCHED_WORKER_SCHEDULED, SCHED_IDLE_PLACEHOLDER, SCHED_CORE_IDLED,
};

static void emit_sample(struct stream *stream, uint64_t index)
{
    switch (stream->type) {
    case STREAM_BW:
        telemetry_bandwidth(index + 1, index);
        break;
    case STREAM_LLC:
        telemetry_llc(7, 42, 9, index + 1, index, 123, 8, 8, 10);
        break;
    case STREAM_PMC_FAILURE:
        telemetry_pmc_failure(7, index, index + 1);
        break;
    case STREAM_SCHED:
        telemetry_sched_event(7, 42, index, sched_reasons[index % ARRAY_SIZE(sched_reasons)]);
        break;
    }
}

static void verify_file(struct stream *stream)
{
    char *path;
    struct file_header header;
    unsigned char record[sizeof(struct llc_record)];
    assert(asprintf(&path, "%s%s", telemetry_path, stream->suffix) >= 0);
    FILE *file = fopen(path, "rb");
    assert(file);
    assert(fread(&header, sizeof(header), 1, file) == 1);
    assert(header.version == FILE_FORMAT_VERSION && header.flags == CAPTURE_COMPLETE);
    assert(!memcmp(header.experiment_id, experiment_id, sizeof(experiment_id)));
    assert(header.record_bytes == stream->record_bytes);
    assert(header.stream_type == stream->type);
    assert(header.produced == stream->produced && header.dropped == 17);
    assert(header.nr_cores == (uint32_t)sched_cores_nr);
    assert(header.header_bytes == sizeof(header) + sched_cores_nr * sizeof(uint32_t));
    for (int i = 0; i < sched_cores_nr; i++) {
        uint32_t core;
        assert(fread(&core, sizeof(core), 1, file) == 1);
        assert(core == sched_cores_tbl[i]);
    }
    for (uint64_t index = 0; index < header.produced; index++) {
        assert(fread(record, stream->record_bytes, 1, file) == 1);
        uint64_t tsc;
        memcpy(&tsc, record, sizeof(tsc));
        assert(stream->type == STREAM_SCHED ? tsc > 0 : tsc == index + 1);
        if (stream->type == STREAM_BW) {
            struct bandwidth_record sample;
            memcpy(&sample, record, sizeof(sample));
            assert(sample.cas == index);
        } else if (stream->type == STREAM_LLC) {
            struct llc_record sample;
            memcpy(&sample, record, sizeof(sample));
            assert(sample.misses == index && sample.sched_gen_at_request == 9);
            assert(sample.sched_gen_at_collection == 10);
            assert(sample.pid_at_request == 42 && sample.core == 7);
            assert(sample.app_id_at_request == 123);
            assert(sample.app_owner_gen_at_request == 8);
            assert(sample.app_owner_gen_at_collection == 8);
        } else if (stream->type == STREAM_PMC_FAILURE) {
            struct pmc_failure_record sample;
            memcpy(&sample, record, sizeof(sample));
            assert(sample.gen == index && sample.core == 7 && sample.reason == PMC_PENDING);
        } else {
            struct sched_event_record sample;
            memcpy(&sample, record, sizeof(sample));
            assert(sample.gen == index && sample.core == 7 && sample.pid == 42);
            assert(sample.reason == sched_reasons[index % ARRAY_SIZE(sched_reasons)]);
        }
    }
    assert(fgetc(file) == EOF);
    fclose(file);
    free(path);
}

int main(int argc, char **argv)
{
    pthread_t thread;
    assert(argc == 2);
    telemetry_path = argv[1];
    telemetry_core = 4;
    sched_cores_nr = 3;
    sched_cores_tbl[0] = 2;
    sched_cores_tbl[1] = 7;
    sched_cores_tbl[2] = 11;
    assert(!generate_experiment_id());
    /* Small rings exercise wraps and cap batches to a ring's capacity. */
    bandwidth_stream.capacity = 1024;
    llc_stream.capacity = 2048;
    failure_stream.capacity = 64;
    sched_stream.capacity = 128;
    for (size_t i = 0; i < ARRAY_SIZE(streams); i++)
        assert(!stream_open(streams[i]));
    active = true;
    for (size_t i = 0; i < ARRAY_SIZE(streams); i++) {
        struct stream *stream = streams[i];
        for (uint64_t index = 0; index < stream->capacity + 17; index++)
            emit_sample(stream, index);
        assert(stream->dropped == 17);
    }
    /* One full ring must not prevent submission to another. */
    assert(llc_stream.produced == llc_stream.capacity);
    assert(failure_stream.produced == failure_stream.capacity);
    assert(!pthread_create(&thread, NULL, writer, NULL));
    for (uint64_t index = 0; index < 10003; index++) {
        for (size_t i = 0; i < ARRAY_SIZE(streams); i++) {
            struct stream *stream = streams[i];
            while (atomic_load(&stream->head) - atomic_load(&stream->tail) == stream->capacity)
                __asm__ volatile("pause");
            emit_sample(stream, stream->capacity + index);
        }
    }
    deadline = 1;
    telemetry_tick();
    assert(!active);
    assert(!pthread_join(thread, NULL));
    assert(!atomic_load(&writer_error));
    for (size_t i = 0; i < ARRAY_SIZE(streams); i++)
        verify_file(streams[i]);

    uint8_t previous_id[sizeof(experiment_id)];
    memcpy(previous_id, experiment_id, sizeof(previous_id));
    assert(!generate_experiment_id());
    assert(memcmp(previous_id, experiment_id, sizeof(previous_id)));

    /* Until capture ends, do not drain a partial batch. */
    atomic_store(&bandwidth_stream.head, 1);
    atomic_store(&bandwidth_stream.tail, 0);
    bool made_progress = false;
    assert(!drain_batch(&bandwidth_stream, false, &made_progress));
    assert(!made_progress && !atomic_load(&bandwidth_stream.tail));

    /* An actual storage failure must stop the producer. */
    bandwidth_stream.fd = open("/dev/full", O_WRONLY);
    assert(bandwidth_stream.fd >= 0);
    active = true;
    writer(NULL);
    assert(atomic_load(&writer_error) == ENOSPC);
    telemetry_tick();
    assert(!active);
    for (size_t i = 0; i < ARRAY_SIZE(streams); i++)
        free(streams[i]->records);
    puts("PASS: independent rings/drops, concurrent ordering/wraps, partial drain, disk failure");
    return 0;
}
