/* Test actual IAS ownership and asynchronous PMC collection without a kernel module. */
#include "../../iokernel/ias.c"
#include "../../iokernel/ias_bw.c"
#include <assert.h>
#include <stdio.h>

struct iokernel_cfg cfg;
DEFINE_BITMAP(ias_ht_punished_cores, NCPU);
unsigned int sched_siblings[NCPU];
unsigned int sched_cores_tbl[NCPU] = {1};
int sched_cores_nr = 1;
int cycles_per_us = 2400;
int ksched_count, ksched_pmc_count;
struct ksched_shm_cpu *ksched_shm;
cpu_set_t ksched_set;
static unsigned int readings, failures;
static uint64_t saved_app_id, saved_request_owner_gen, saved_collection_owner_gen;
static uint32_t saved_pid;
static uint64_t saved_sched_request, saved_sched_collection;

void logk(int level, const char *format, ...) { (void)level; (void)format; }
void telemetry_pmc_failure(uint32_t core, uint64_t gen, uint64_t tsc)
{ (void)core; (void)gen; (void)tsc; failures++; }
void telemetry_llc(uint32_t core, uint32_t pid_at_request, uint64_t gen,
                   uint64_t tsc, uint64_t misses, uint64_t app_id_at_request,
                   uint64_t app_owner_gen_at_request, uint64_t app_owner_gen_at_collection,
                   uint64_t sched_gen_at_collection)
{
    assert(core == 1 && tsc && misses);
    saved_sched_request = gen;
    saved_sched_collection = sched_gen_at_collection;
    readings++;
    saved_pid = pid_at_request;
    saved_app_id = app_id_at_request;
    saved_request_owner_gen = app_owner_gen_at_request;
    saved_collection_owner_gen = app_owner_gen_at_collection;
}

static void finish_request(uint64_t value)
{
    ksched_shm[1].pmcval = value;
    ksched_shm[1].pmctsc = value * 10;
    store_release(&ksched_shm[1].pmc, 0);
}

int main(void)
{
    struct proc application_a = {.pid = 42};
    struct proc application_b = {.pid = 43};
    struct sched_spec specification = {.max_cores = 1};
    struct pmc_sample samples[NCPU] = {0};
    ksched_shm = calloc(NCPU, sizeof(*ksched_shm));
    assert(ksched_shm);
    assert(!ias_attach(&application_a, &specification));
    assert(!ias_attach(&application_b, &specification));
    struct ias_data *a = (void *)application_a.policy_data;
    struct ias_data *b = (void *)application_b.policy_data;
    assert(a->app_id && b->app_id && a->app_id != b->app_id);
    uint64_t original_app_id = a->app_id;
    ias_assign_app_owner(a, 1);
    uint64_t original_owner_gen = app_owner_gen[1];

    /* Parking and reacquiring the core for A preserves application ownership. */
    cores[1] = a;
    a->threads_active = 1;
    ias_cleanup_core(1);
    assert(!cores[1] && last_app_id[1] == original_app_id);
    ias_assign_app_owner(a, 1);
    assert(app_owner_gen[1] == original_owner_gen);
    cores[1] = a;
    a->threads_active = 1;
    ias_bw_request_pmc(PMC_LLC_MISSES, samples);
    assert(samples[1].request_pending);
    assert(samples[1].app_owner_gen_at_request == original_owner_gen);
    int submitted = ksched_pmc_count;

    /* A late read must survive parking and scheduling-generation changes. */
    ias_cleanup_core(1);
    ias_gen[1]++;
    ias_bw_gather_pmc(samples);
    assert(failures == 1 && samples[1].request_pending);
    ias_bw_request_pmc(PMC_LLC_MISSES, samples);
    assert(ksched_pmc_count == submitted);
    finish_request(100);
    ias_bw_gather_pmc(samples);
    assert(readings == 1 && !samples[1].request_pending);
    assert(saved_request_owner_gen == saved_collection_owner_gen);
    assert(saved_sched_collection == saved_sched_request + 1);
    assert(saved_app_id == original_app_id && saved_pid == 42);

    /* Do not wake idle cores just to issue new reads. */
    ias_bw_request_pmc(PMC_LLC_MISSES, samples);
    assert(ksched_pmc_count == submitted);
    ias_assign_app_owner(a, 1);
    cores[1] = a;
    a->threads_active = 1;
    /* Retain raw data across A -> B -> A. */
    ias_bw_request_pmc(PMC_LLC_MISSES, samples);
    ias_assign_app_owner(b, 1);
    ias_assign_app_owner(a, 1);
    assert(app_owner_gen[1] == original_owner_gen + 2);
    finish_request(200);
    ias_bw_gather_pmc(samples);
    assert(readings == 2 && saved_pid == 42);
    assert(saved_request_owner_gen != saved_collection_owner_gen);

    /* Detach invalidates idle ownership and cannot turn a reused PID into old A. */
    ias_detach(&application_a);
    assert(last_app_id[1] == IAS_APP_NONE);
    submitted = ksched_pmc_count;
    ias_bw_request_pmc(PMC_LLC_MISSES, samples);
    assert(ksched_pmc_count == submitted);
    assert(!ias_attach(&application_a, &specification));
    a = (void *)application_a.policy_data;
    assert(a->app_id != original_app_id);
    ias_assign_app_owner(a, 1);
    cores[1] = a;
    ias_bw_request_pmc(PMC_LLC_MISSES, samples);
    uint64_t detached_request_owner_gen = samples[1].app_owner_gen_at_request;
    ias_detach(&application_a);
    finish_request(300);
    ias_bw_gather_pmc(samples);
    assert(readings == 3 && saved_request_owner_gen == detached_request_owner_gen);
    assert(saved_request_owner_gen != saved_collection_owner_gen);
    ias_detach(&application_b);
    free(ksched_shm);
    puts("PASS: park/resume ownership, A-B-A, PID reuse, pending reads, changed-owner raw reads");
    return 0;
}
