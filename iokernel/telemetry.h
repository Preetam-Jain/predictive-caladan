/* Only the scheduler/dataplane thread may submit telemetry. */
#pragma once
#include <stdint.h>

extern int telemetry_core;
/* Output prefix: each stream appends its own suffix. "-" reserves the core only. */
extern const char *telemetry_path;
extern unsigned int telemetry_seconds;

int telemetry_init(void);
void telemetry_tick(void);
void telemetry_bandwidth(uint64_t tsc, uint32_t cas);
void telemetry_llc(uint32_t core, uint32_t pid_at_request, uint64_t sched_gen_at_request,
                   uint64_t tsc, uint64_t misses, uint64_t app_id_at_request,
                   uint64_t app_owner_gen_at_request, uint64_t app_owner_gen_at_collection,
                   uint64_t sched_gen_at_collection);
void telemetry_pmc_failure(uint32_t core, uint64_t gen, uint64_t tsc);

/* Reasons for IAS assignment generation changes, stored in the scheduler stream. */
enum sched_event_reason {
    SCHED_WORKER_SCHEDULED = 1,
    SCHED_IDLE_PLACEHOLDER = 2,
    SCHED_CORE_IDLED = 3,
};
#define SCHED_PID_NONE 0U
void telemetry_sched_event(uint32_t core, uint32_t pid, uint64_t gen,
                           enum sched_event_reason reason);

/* Queue delays are cycles; flags: active=1, has_work=2, standing_queue=4. */
void telemetry_queue(uint64_t tsc, uint64_t runnable_cycles, uint64_t rx_cycles,
                     uint64_t timer_cycles, uint64_t directpath_cycles,
                     uint64_t storage_cycles, uint64_t total_cycles, uint32_t pid,
                     uint32_t tid, uint32_t core, uint32_t flags);
/* Observed elapsed time, not completed-task duration. Even RCU generations mean idle. */
void telemetry_task(uint64_t tsc, uint64_t task_elapsed_us, uint64_t kthread_elapsed_us,
                    uint32_t pid, uint32_t tid, uint32_t core, uint32_t rcu_gen);
/* Process flags: has_work=1, parked_busy=2, standing_queue=4. */
void telemetry_delay(uint64_t tsc, uint64_t rx_cycles, double max_us, double min_us,
                     double avg_us, uint32_t pid, uint32_t active_threads, uint32_t flags);
