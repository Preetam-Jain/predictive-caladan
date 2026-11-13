/* Test controller actions with actual polling code and simulated scheduler operations. */
#include "../../iokernel/ias_ht.c"
#include "../../iokernel/ias_ts.c"
#include <assert.h>
#include <stdio.h>

struct iokernel_cfg cfg;
struct ias_data *cores[NCPU];
unsigned int sched_cores_tbl[NCPU] = {0};
int sched_cores_nr = 1;
unsigned int sched_siblings[NCPU] = {1, 0};
struct list_head congested_procs[NCPU + 1];
uint64_t congested_lc_procs_nr;
static struct thread worker;
static unsigned int yields, idled;
struct thread *sched_get_thread_on_core(unsigned int core)
{ (void)core; return &worker; }
int sched_yield_on_core(unsigned int core)
{ (void)core; yields++; return 0; }
bool ias_can_add_kthread(struct ias_data *sd, bool ignore)
{ (void)sd; (void)ignore; return true; }
int ias_add_kthread(struct ias_data *sd)
{ (void)sd; return 0; }
int ias_idle_placeholder_on_core(struct ias_data *sd, unsigned int core)
{ (void)sd; (void)core; idled++; return 0; }
int ias_idle_on_core(unsigned int core)
{ (void)core; return 0; }
void logk(int level, const char *format, ...)
{ (void)level; (void)format; }

int main(void)
{
    struct ias_data app = {.quantum_us = 10, .ht_punish_us = 10};
    cores[0] = &app;
    bitmap_set(app.reserved_cores, 0);
    worker.metrics.uthread_elapsed_us = 20;
    worker.metrics.work_pending = true;
    cfg.timesharing = true;
    ias_ts_poll();
    assert(yields == 1);
    worker.metrics.work_pending = false;
    ias_ts_poll();
    assert(yields == 1);

    cfg.ht_intervention = true;
    cfg.core_allocation = false;
    ias_ht_poll();
    assert(idled == 1 && bitmap_test(ias_ht_punished_cores, 1));
    assert(ias_ht_percore[0].budget_used == 2);
    puts("PASS: timesharing respects pending work; HT updates budgets and idles siblings");
    return 0;
}
