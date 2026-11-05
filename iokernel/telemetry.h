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
void telemetry_llc(uint32_t core, uint32_t pid, uint64_t gen,
                   uint64_t tsc, uint64_t misses);
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
