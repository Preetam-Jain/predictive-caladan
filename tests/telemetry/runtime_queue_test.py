"""Exercise actual runtime queue functions, without a NIC or ksched module."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class RuntimeQueueTest(unittest.TestCase):
    def test_overflow_steal_and_empty_timestamp(self):
        source = (ROOT / 'runtime/sched.c').read_text()
        functions = source[source.index('static void drain_overflow'):source.index('static bool work_available')]
        functions += source[source.index('static void update_oldest_tsc'):source.index('static bool steal_work')]
        functions += source[source.index('static void thread_ready_prepare'):source.index('/**\n * thread_ready -')]
        harness = r'''
#include <assert.h>
#include <stdlib.h>
#include "runtime/defs.h"
void logk_bug(bool fatal, const char *expr, const char *file, int line, const char *func)
{ (void)fatal; (void)expr; (void)file; (void)line; (void)func; abort(); }
static struct kthread *current;
#define myk() current
#undef STAT
static uint64_t counters[STAT_NR];
#define STAT(name) counters[STAT_ ## name]
static bool cores_have_affinity(unsigned int a, unsigned int b) { return a == b; }
'''+functions+r'''
static void init(struct kthread *k, struct q_ptrs *q) {
    k->q_ptrs = q;
    spin_lock_init(&k->lock);
    list_head_init(&k->rq_overflow);
    q->oldest_tsc = UINT64_MAX;
}
int main(void) {
    struct kthread local = {0}, remote = {0};
    struct q_ptrs lq = {0}, rq = {0};
    thread_t threads[RUNTIME_RQ_SIZE + 8] = {0};
    struct rq_snapshot snapshot;
    LIST_HEAD(taken);
    init(&local, &lq); init(&remote, &rq);
    current = &local;
    spin_lock(&local.lock);
    for (unsigned int i = 0; i < RUNTIME_RQ_SIZE + 3; i++)
        thread_ready_locked(&threads[i]);
    assert(rq_read_snapshot(&lq, &snapshot) == RQ_SNAPSHOT_VALID);
    assert(snapshot.head - snapshot.tail == RUNTIME_RQ_SIZE + 3);
    assert(snapshot.oldest_tsc == threads[0].ready_tsc);
    assert(drain_threads(&local, &taken, RUNTIME_RQ_SIZE, true) == RUNTIME_RQ_SIZE);
    assert(rq_read_snapshot(&lq, &snapshot) == RQ_SNAPSHOT_VALID);
    assert(snapshot.head - snapshot.tail == 3);
    assert(snapshot.oldest_tsc == threads[RUNTIME_RQ_SIZE].ready_tsc);
    assert(drain_threads(&local, &taken, 3, true) == 3);
    assert(rq_read_snapshot(&lq, &snapshot) == RQ_SNAPSHOT_VALID);
    assert(snapshot.head == snapshot.tail && snapshot.oldest_tsc == UINT64_MAX);
    thread_ready_head_locked(&threads[RUNTIME_RQ_SIZE+3]);
    spin_unlock(&local.lock);
    current = &remote;
    spin_lock(&remote.lock);
    thread_ready_locked(&threads[RUNTIME_RQ_SIZE+4]);
    thread_ready_locked(&threads[RUNTIME_RQ_SIZE+5]);
    spin_unlock(&remote.lock);
    current = &local;
    spin_lock(&local.lock); spin_lock(&remote.lock);
    merge_runqueues(&local, 1, &remote, 2); /* releases remote.lock */
    assert(rq_read_snapshot(&lq, &snapshot) == RQ_SNAPSHOT_VALID);
    assert(snapshot.head - snapshot.tail == 3);
    assert(rq_read_snapshot(&rq, &snapshot) == RQ_SNAPSHOT_VALID);
    assert(snapshot.head == snapshot.tail && snapshot.oldest_tsc == UINT64_MAX);
    spin_unlock(&local.lock);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as d:
            c = Path(d) / 'runtime_queue.c'
            exe = Path(d) / 'runtime_queue'
            c.write_text(harness)
            subprocess.run(['cc', '-std=gnu11', '-D_GNU_SOURCE', '-O2', '-Wall', '-Werror',
                            '-Iinc', '-I.', str(c), '-o', str(exe)], cwd=ROOT, check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    unittest.main()
