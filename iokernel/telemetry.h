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

/* Queue contributions are cycles; identity is the runtime worker TID, even parked. */
struct telemetry_queue_record {
    uint64_t tsc, runnable_cycles, rx_cycles, timer_cycles;
    uint64_t directpath_cycles, storage_cycles, total_cycles;
    uint32_t pid, tid, core, flags; /* flags: active=1, has_work=2, standing=4 */
};
/* Observations, not completed-task durations. Even RCU generations mean idle. */
struct telemetry_task_record {
    uint64_t tsc, task_elapsed_us, kthread_elapsed_us;
    uint32_t pid, tid, core, rcu_gen;
};
struct telemetry_delay_record {
    uint64_t tsc, rx_cycles;
    double max_us, min_us, avg_us;
    uint32_t pid, active_threads, flags, reserved;
    /* flags: has_work=1, parked_busy=2, standing_queue=4 */
};
void telemetry_queue(const struct telemetry_queue_record *record);
void telemetry_task(const struct telemetry_task_record *record);
void telemetry_delay(const struct telemetry_delay_record *record);
