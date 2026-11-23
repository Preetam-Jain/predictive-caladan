"""Exercise the scheduler's actual queue measurement function without NIC dependencies."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class QueueTest(unittest.TestCase):
    def test_contributions_and_shared_directpath(self):
        source = (ROOT / 'iokernel/sched.c').read_text()
        start = source.index('static bool\nsched_measure_kthread_delay')
        end = source.index('#define EWMA_WEIGHT', start)
        harness = r'''
#include <assert.h>
#include <base/time.h>
#include "iokernel/defs.h"
#include "iokernel/telemetry.h"
static uint64_t cur_tsc = 1000;
struct iokernel_cfg cfg;
int cycles_per_us = 1;
static uint64_t values[5];
static uint32_t sample_flags;
static uint32_t failure_reason, failure_count;
void telemetry_queue_snapshot_failure(uint64_t tsc, uint32_t pid, uint32_t tid,
                                      uint32_t core, uint32_t reason)
{
    assert(tsc == cur_tsc && pid == 42 && tid == 43 && core == 7);
    failure_reason = reason;
    failure_count++;
}
void telemetry_queue(uint64_t tsc, uint64_t runnable, uint64_t rx,
                     uint64_t timer, uint64_t directpath, uint64_t storage,
                     uint32_t pid, uint32_t tid,
                     uint32_t core, uint32_t flags)
{
    assert(tsc == cur_tsc && pid == 42 && tid == 43 && core == 7);
    values[0] = runnable; values[1] = rx; values[2] = timer;
    values[3] = directpath; values[4] = storage;
    sample_flags = flags;
}
static uint64_t calc_delay_tsc(uint64_t tsc)
{
    return cur_tsc - MIN(tsc, cur_tsc);
}
/* Mock only the hardware observation; zero delay leaves the output untouched,
 * matching the real helper's empty/disabled-queue behavior. */
static void sched_measure_hardware_delay(struct thread *th, struct hwq *h,
    bool update, bool *busy, bool *standing, uint64_t *delay)
{
    (void)th; (void)update;
    if (h->enabled && h->busy_since) {
        *delay = h->busy_since;
        *busy = true;
        *standing = true;
    }
}
'''
        harness += source[start:end]
        harness += r'''
int main(void)
{
    struct proc p = {.pid = 42, .has_directpath = true, .has_storage = true};
    struct q_ptrs q = {.rq_head = 1, .oldest_tsc = 900,
                      .rxq_wb = 0, .next_timer_tsc = 970};
    struct thread th = {.active = true, .tid = 43, .core = 7, .q_ptrs = &q,
                        .rxq_busy_since = 800};
    th.rxq.send_head = 1;
    th.directpath_hwq.enabled = th.storage_hwq.enabled = true;
    th.directpath_hwq.busy_since = 40;
    th.storage_hwq.busy_since = 50;
    uint64_t total = 0, shared = 123, next;
    bool busy = false, standing = false;
    sched_measure_kthread_delay(&p, &th, &total, &shared, &busy, &standing, &next);
    assert(values[0] == 100 && values[1] == 200 && values[2] == 30);
    assert(values[3] == 40 && values[4] == 50);
    assert(total == 420 && shared == 123 && next == 970 && sample_flags == 7);

    /* QSTEER contributes to the shared process delay, not worker total. */
    th.directpath_hwq.hwq_type = HWQ_MLX5_QSTEER;
    total = 0;
    sched_measure_kthread_delay(&p, &th, &total, &shared, &busy, &standing, &next);
    assert(values[3] == 40 && total == 380 && shared == 40);
    th.directpath_hwq.busy_since = 0;
    total = 0;
    sched_measure_kthread_delay(&p, &th, &total, &shared, &busy, &standing, &next);
    assert(values[3] == 0 && shared == 40 && total == 380);

    /* Empty software queues, a future timer and no hardware work. */
    q.rq_head = 0; th.rxq.send_head = 0; q.next_timer_tsc = 2000;
    p.has_directpath = p.has_storage = false;
    th.active = false; total = 0; busy = standing = false;
    sched_measure_kthread_delay(&p, &th, &total, &shared, &busy, &standing, &next);
    assert(total == 0 && sample_flags == 0 && next == 2000);
    for (unsigned int i = 0; i < 5; i++) assert(values[i] == 0);

    /* A writer in progress is rejected once, keeps prior queue history,
     * and is explicitly unavailable while other queue signals survive. */
    cfg.queue_snapshot_failure_logging = true;
    q.rq_seq = 1; q.rq_head = 9; q.oldest_tsc = 1;
    uint32_t old_head = th.last_rq_head;
    total = 0; busy = standing = false;
    assert(!sched_measure_kthread_delay(&p, &th, &total, &shared, &busy, &standing, &next));
    assert(q.rq_seq == 1 && th.last_rq_head == old_head);
    assert(sample_flags == 8 && failure_count == 1);
    assert(failure_reason == RQ_SNAPSHOT_WRITER_BUSY);
    assert(total == 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            test = Path(directory) / 'queue_test.c'
            binary = Path(directory) / 'queue_test'
            test.write_text(harness)
            subprocess.run(['cc', '-D_GNU_SOURCE', '-std=gnu11', '-O2', '-Wall',
                            '-Werror', '-Iinc', '-I.', str(test), '-o', str(binary)],
                           cwd=ROOT, check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
