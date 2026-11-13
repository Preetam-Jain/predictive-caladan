/* Exercise IAS policy decisions without NIC or ksched dependencies. */
#define main ownership_test_main
#include "ownership_test.c"
#undef main

struct thread *sched_get_thread_on_core(unsigned int core)
{ (void)core; return NULL; }

struct ias_ht_data ias_ht_percore[NCPU];
int sched_run_on_core(struct proc *p, unsigned int core)
{ (void)p; (void)core; return 0; }
int sched_idle_on_core(uint32_t hint, unsigned int core)
{ (void)hint; (void)core; return 0; }
unsigned int ias_ht_relinquish_core(struct ias_data *sd)
{ (void)sd; return NCPU; }
void telemetry_sched_event(uint32_t core, uint32_t pid, uint64_t gen,
                           enum sched_event_reason reason)
{ (void)core; (void)pid; (void)gen; (void)reason; }
void init_shutdown(int status) { exit(status); }
static unsigned int bandwidth_readings;
uint32_t pcm_caladan_get_cas_count(uint32_t channel)
{ (void)channel; return 1000; }
void telemetry_bandwidth(uint64_t tsc, uint32_t cas)
{ (void)tsc; (void)cas; bandwidth_readings++; }

int main(void)
{
    struct proc process = {.pid = 42};
    struct proc other = {.pid = 43};
    struct sched_spec spec = {.guaranteed_cores = 1, .max_cores = 4};
    cfg.noht = true;
    cfg.core_allocation = false;
    assert(!ias_attach(&process, &spec));
    struct ias_data *sd = (void *)process.policy_data;
    assert(sd->threads_limit == 1 && sd->threads_guaranteed == 1);
    assert(bitmap_test(sd->reserved_cores, 0));
    assert(ias_can_preempt_core(sd, 0));
    assert(!ias_can_preempt_core(sd, 1));
    for (unsigned int i = 0; i < ARRAY_SIZE(congested_procs); i++)
        list_head_init(&congested_procs[i]);
    ias_mark_congested(sd);
    assert(ias_choose_kthread(0) == sd);
    assert(ias_choose_kthread(1) == NULL);
    ias_unmark_congested(sd);
    assert(ias_choose_kthread(0) == NULL);
    spec.guaranteed_cores = 0;
    assert(ias_attach(&other, &spec) == -EINVAL);
    cfg.core_allocation = true;
    assert(!ias_attach(&other, &spec));
    struct ias_data *other_sd = (void *)other.policy_data;
    assert(other_sd->threads_limit == 4);
    assert(ias_can_preempt_core(other_sd, 1));

    /* Disabled bandwidth policy still gathers telemetry, then enabled policy throttles. */
    cores[1] = sd;
    ias_gen[1] = 9;
    ias_assign_app_owner(sd, 1);
    ksched_shm = calloc(NCPU, sizeof(*ksched_shm));
    assert(ksched_shm);
    sd->is_lc = false;
    sd->threads_limit = 4;
    sd->threads_active = 2;
    process.thread_count = 4;
    process.active_thread_count = 2;
    pending_samples[1] = (struct pmc_sample){.gen = 9,
        .app_id_at_request = sd->app_id,
        .app_owner_gen_at_request = app_owner_gen[1]};
    pending_samples[1].pid_at_request = 42;
    pending_samples[1].request_pending = true;
    finish_request(200);
    ias_bw_thresh = 0;
    cfg.bw_intervention = false;
    ias_bw_poll();
    assert(bandwidth_readings == 1 && readings == 1);
    assert(ias_bw_punish_count == 0 && sd->threads_limit == 4);
    /* Simulate a fresh startup with the original policy enabled. */
    pending_samples[1].request_pending = false;
    cfg.bw_intervention = true;
    ias_bw_poll(); /* RELAX -> SAMPLE: request start, no punishment. */
    assert(bandwidth_readings == 2 && readings == 1 && ias_bw_punish_count == 0);
    finish_request(300);
    ias_bw_poll(); /* SAMPLE -> PUNISH: gather start and request end. */
    assert(bandwidth_readings == 3 && readings == 2 && ias_bw_punish_count == 0);
    finish_request(400);
    ias_bw_poll(); /* PUNISH: gather end, punish, swap buffers, request next end. */
    assert(bandwidth_readings == 4 && readings == 3);
    assert(ias_bw_punish_count == 1 && sd->threads_limit == 1);
    free(ksched_shm);
    puts("PASS: fixed reservations, original bandwidth state machine and policy toggle");
    return 0;
}
