/* Script partial syscall results against the actual implementation. */
#include <sys/uio.h>
static ssize_t scripted_writev(int fd, const struct iovec *parts, int count);
#define writev scripted_writev
#define main existing_writer_test_main
#include "writer_test.c"
#undef main
#undef writev

static const int *results;
static size_t result_count;
static size_t calls;
static char output[32];
static size_t output_bytes;

static ssize_t scripted_writev(int fd, const struct iovec *parts, int count)
{
    (void)fd;
    assert(calls < result_count);
    int result = results[calls++];
    if (result < 0) {
        errno = -result;
        return -1;
    }
    size_t remaining = result;
    for (int i = 0; i < count && remaining; i++) {
        size_t bytes = remaining < parts[i].iov_len ? remaining : parts[i].iov_len;
        assert(output_bytes + bytes <= sizeof(output));
        memcpy(output + output_bytes, parts[i].iov_base, bytes);
        output_bytes += bytes;
        remaining -= bytes;
    }
    assert(!remaining);
    return result;
}

static void script(const int *values, size_t count)
{
    results = values;
    result_count = count;
    calls = 0;
    output_bytes = 0;
}

int main(void)
{
    char first[] = "ABCDE";
    char second[] = "FGHI";
    struct iovec parts[] = {{first, 5}, {second, 4}};
    /* Retry EINTR, stop within region one, cross the boundary, finish region two. */
    const int partial[] = {-EINTR, 2, 4, 3};
    script(partial, ARRAY_SIZE(partial));
    assert(!writev_all(-1, parts, ARRAY_SIZE(parts)));
    assert(calls == ARRAY_SIZE(partial));
    assert(output_bytes == 9 && !memcmp(output, "ABCDEFGHI", 9));

    unsigned char records[] = "CD....AB";
    struct stream stream = {
        .records = records,
        .capacity = 8,
        .record_bytes = 1,
        .batch_size = 4,
        .head = ATOMIC_VAR_INIT(10),
        .tail = ATOMIC_VAR_INIT(6),
    };
    bool made_progress = false;
    const int full_batch[] = {4};
    script(full_batch, ARRAY_SIZE(full_batch));
    assert(!drain_batch(&stream, false, &made_progress));
    assert(calls == 1 && made_progress && atomic_load(&stream.tail) == 10);
    assert(output_bytes == 4 && !memcmp(output, "ABCD", 4));

    /* A partial write followed by failure must not release any ring slots. */
    atomic_store(&stream.tail, 6);
    made_progress = false;
    const int failed_batch[] = {1, -ENOSPC};
    script(failed_batch, ARRAY_SIZE(failed_batch));
    assert(drain_batch(&stream, false, &made_progress) == ENOSPC);
    assert(!made_progress && atomic_load(&stream.tail) == 6);

    const int zero_progress[] = {0};
    script(zero_progress, ARRAY_SIZE(zero_progress));
    assert(drain_batch(&stream, false, &made_progress) == EIO);
    assert(!made_progress && atomic_load(&stream.tail) == 6);
    puts("PASS: writev wrap ordering, partial writes, EINTR, errors, slot ownership");
    return 0;
}
