/* Exercise the shared reader, including deterministic overlap and wraparound. */
#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <base/atomic.h>

static void read_overlap(void);
#undef rmb
#define rmb() do { barrier(); read_overlap(); barrier(); } while (0)
#include <iokernel/control.h>

static struct q_ptrs queue;
static bool inject_overlap;
static pthread_mutex_t writer_lock = PTHREAD_MUTEX_INITIALIZER;

static void read_overlap(void)
{
    if (inject_overlap) {
        inject_overlap = false;
        rq_write_begin(&queue);
        queue.rq_head++;
        queue.oldest_tsc = 1000000ULL + queue.rq_tail;
        rq_write_end(&queue);
    }
}

static void *writer(void *arg)
{
    (void)arg;
    for (unsigned int i = 0; i < 200000; i++) {
        pthread_mutex_lock(&writer_lock);
        rq_write_begin(&queue);
        queue.rq_tail++;
        queue.rq_head = queue.rq_tail + 1;
        queue.oldest_tsc = 1000000ULL + queue.rq_tail;
        rq_write_end(&queue);
        pthread_mutex_unlock(&writer_lock);
    }
    return NULL;
}

int main(void)
{
    struct rq_snapshot snapshot = {.head = 123};
    queue.rq_head = 1;
    queue.oldest_tsc = 1000000;
    assert(rq_read_snapshot(&queue, &snapshot) == RQ_SNAPSHOT_VALID);
    assert(snapshot.head == 1 && snapshot.tail == 0);
    rq_write_begin(&queue);
    snapshot.head = 123;
    assert(rq_read_snapshot(&queue, &snapshot) == RQ_SNAPSHOT_WRITER_BUSY);
    rq_write_end(&queue);
    inject_overlap = true;
    assert(rq_read_snapshot(&queue, &snapshot) == RQ_SNAPSHOT_CHANGED);
    queue.rq_seq = UINT32_MAX - 1;
    rq_write_begin(&queue);
    rq_write_end(&queue);
    assert(queue.rq_seq == 0);
    queue.rq_head = queue.rq_tail + 1;

    pthread_t a, b;
    assert(!pthread_create(&a, NULL, writer, NULL));
    assert(!pthread_create(&b, NULL, writer, NULL));
    unsigned int valid = 0;
    for (unsigned int i = 0; i < 500000; i++) {
        if (rq_read_snapshot(&queue, &snapshot) != RQ_SNAPSHOT_VALID)
            continue;
        assert(snapshot.head == snapshot.tail + 1);
        assert(snapshot.oldest_tsc == 1000000ULL + snapshot.tail);
        valid++;
    }
    assert(!pthread_join(a, NULL));
    assert(!pthread_join(b, NULL));
    assert(valid);
    return 0;
}
