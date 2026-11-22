/*
 * ias_bw.c - the memory bandwidth subcontroller
 */

#include <base/stddef.h>
#include <base/log.h>

#include "defs.h"
#include "telemetry.h"
#include "sched.h"
#include "ksched.h"
#include "pmc.h"
#include "ias.h"
#include "pcm.h"

/* statistics */
uint64_t ias_bw_punish_count;
uint64_t ias_bw_relax_count;
uint64_t ias_bw_sample_failures;
uint64_t ias_bw_sample_aborts;
float	 ias_bw_estimate;
float	 ias_bw_estimate_multiplier;

/* bandwidth threshold in cache lines per cycle for a single channel */
static float ias_bw_thresh;


struct pmc_sample {
	uint64_t gen; /* Existing IAS scheduling generation at request time. */
	uint64_t val;
	uint64_t tsc;
	uint64_t app_id_at_request;
	uint64_t app_owner_gen_at_request;
	uint32_t pid_at_request;
	bool request_pending;
};

static void ias_bw_throttle_core(int core)
{
        struct ias_data *sd = cores[core];
        sd->threads_limit = MAX(0, MIN(sd->threads_limit - 1,
				       sd->threads_active - 1));
        if (ias_add_kthread_on_core(core))
                ias_idle_on_core(core);
}

/* Original controller helper; measurement-only collection when disabled. */
static void ias_bw_request_pmc(uint64_t sel, struct pmc_sample *samples)
{
	struct ias_data *sd;
	int core, tmp;

	if (!cfg.bw_intervention) {
		sched_for_each_allowed_core(core, tmp) {
			struct pmc_sample *sample = &samples[core];
			/* Never overwrite a request still being handled by the target CPU. */
			if (sample->request_pending || !cores[core] ||
			    bitmap_test(ias_ht_punished_cores, core))
				continue;

			/* Preserve normal eligibility; ownership can span intervening idle time. */
			sample->gen = ias_gen[core];
			sample->app_id_at_request = last_app_id[core];
			sample->app_owner_gen_at_request = app_owner_gen[core];
			sample->pid_at_request = last_app_pid[core];
			sample->request_pending = true;
			ksched_enqueue_pmc(core, sel);
		}
		return;
	}

	sched_for_each_allowed_core(core, tmp) {
		sd = cores[core];
		if (!sd || sd->is_lc ||
		    bitmap_test(ias_ht_punished_cores, core)) {
			samples[core].gen = ias_gen[core] - 1;
			continue;
		}

		samples[core].gen = ias_gen[core];
		samples[core].app_id_at_request = last_app_id[core];
		samples[core].app_owner_gen_at_request = app_owner_gen[core];
		samples[core].pid_at_request = last_app_pid[core];
		ksched_enqueue_pmc(core, sel);
	}
}

/* Original controller helper; measurement-only collection when disabled. */
static void ias_bw_gather_pmc(struct pmc_sample *samples)
{
	int core, tmp;
	struct pmc_sample *s;

	if (!cfg.bw_intervention) {
		sched_for_each_allowed_core(core, tmp) {
			struct pmc_sample *sample = &samples[core];
			if (!sample->request_pending)
				continue;
			if (!ksched_poll_pmc(core, &sample->val, &sample->tsc)) {
				ias_bw_sample_failures++;
				telemetry_pmc_failure(core, sample->gen, rdtsc());
				continue;
			}
			sample->request_pending = false;
			/* Preserve the physical-core reading even when ownership changed. */
			telemetry_llc(core, sample->pid_at_request, sample->gen,
				     sample->tsc, sample->val, sample->app_id_at_request,
				     sample->app_owner_gen_at_request, app_owner_gen[core],
				     ias_gen[core]);
		}
		return;
	}

	sched_for_each_allowed_core(core, tmp) {
		s = &samples[core];
		if (s->gen != ias_gen[core])
			continue;
		if (!ksched_poll_pmc(core, &s->val, &s->tsc)) {
			telemetry_pmc_failure(core, s->gen, rdtsc());
			s->gen = ias_gen[core] - 1;
			ias_bw_sample_failures++;
			continue;
		}
		telemetry_llc(core, s->pid_at_request, s->gen, s->tsc, s->val,
		              s->app_id_at_request, s->app_owner_gen_at_request,
		              app_owner_gen[core], ias_gen[core]);
	}
}

static struct ias_data *
ias_bw_choose_victim(struct pmc_sample *start, struct pmc_sample *end,
		     unsigned int *worst_core)
{
	struct ias_data *sd, *victim = NULL;
	float highest_l3miss_rate = 0.0, bw_estimate;
	int core, tmp;

	/* zero per-task llc miss counts */
	ias_for_each_proc(sd)
		sd->bw_llc_miss_rate = 0.0;

	/* convert per-core llc miss counts into per-task llc miss counts */
	sched_for_each_allowed_core(core, tmp) {
		if (cores[core] == NULL ||
		    start[core].gen != end[core].gen ||
		    start[core].gen != ias_gen[core]) {
			continue;
		}

		bw_estimate = (float)(end[core].val - start[core].val) /
			      (float)(end[core].tsc - start[core].tsc);
		cores[core]->bw_llc_miss_rate += bw_estimate;
	}

	/* find an eligible task with the highest overall llc miss count */
	ias_for_each_proc(sd) {
		if (sd->threads_limit == 0 ||
		    sd->threads_limit <= sd->threads_guaranteed)
			continue;
		if (sd->bw_llc_miss_rate * sched_cores_nr < ias_bw_thresh)
			continue;
		if (sd->bw_llc_miss_rate <= highest_l3miss_rate)
			continue;

		victim = sd;
		highest_l3miss_rate = sd->bw_llc_miss_rate;
	}
	if (!victim)
		return NULL;

	/* find that task's core with the highest llc miss count */
	highest_l3miss_rate = 0.0;
	*worst_core = NCPU;
	sched_for_each_allowed_core(core, tmp) {
		bw_estimate = (float)(end[core].val - start[core].val) /
			      (float)(end[core].tsc - start[core].tsc);
		if (bw_estimate <= highest_l3miss_rate)
			continue;
		if (cores[core] != victim)
			continue;

		*worst_core = core;
		highest_l3miss_rate = bw_estimate;
	}
	if (*worst_core == NCPU)
		return NULL;

	start[*worst_core].gen = ias_gen[core] - 1;
	return victim;
}

static int ias_bw_punish(struct pmc_sample *start, struct pmc_sample *end)
{
	struct ias_data *sd;
	unsigned int core;

	/* choose the victim task */
	sd = ias_bw_choose_victim(start, end, &core);
	if (!sd)
		return -EAGAIN;
	sd->is_bwlimited = true;
	ias_bw_punish_count++;

	/* throttle the core */
	ias_bw_throttle_core(core);
	return 0;
}

static void ias_bw_relax(void)
{
	struct ias_data *sd, *best_sd = NULL;

	ias_for_each_proc(sd) {
		if (!sd->is_bwlimited || !sd->is_congested)
			continue;
		if (best_sd && sd->threads_limit <= best_sd->threads_limit)
			continue;

		best_sd = sd;
	}

	if (best_sd) {
		best_sd->threads_limit++;
		if (best_sd->threads_limit >= best_sd->threads_max)
			best_sd->is_bwlimited = false;
		ias_bw_relax_count++;
	}
}


static float ias_measure_bw(void)
{
	static uint64_t last_tsc = 0;
	static uint32_t last_cas = 0;
	uint64_t tsc;
	uint32_t cur_cas;
	float bw_estimate;

	/* update the bandwidth estimate */
	barrier();
	tsc = rdtsc();
	barrier();
	cur_cas = pcm_caladan_get_cas_count(0);
	telemetry_bandwidth(tsc, cur_cas);
	bw_estimate = (float)(cur_cas - last_cas) / (float)(tsc - last_tsc);
	last_cas = cur_cas;
	last_tsc = tsc;
	ias_bw_estimate = bw_estimate;
	return bw_estimate;
}

enum {
	IAS_BW_STATE_RELAX = 0,
	IAS_BW_STATE_SAMPLE,
	IAS_BW_STATE_PUNISH,
};
static struct pmc_sample arr_1[NCPU], arr_2[NCPU];
static struct pmc_sample pending_samples[NCPU];

/**
 * ias_bw_poll - runs the bandwidth controller
 */
void ias_bw_poll(void)
{
	static struct pmc_sample *start = arr_1, *end = arr_2;
	static int state;
	bool throttle;

	assert(!cfg.nobw);

	if (!cfg.bw_intervention) {
		ias_measure_bw();
		ias_bw_gather_pmc(pending_samples);
		ias_bw_request_pmc(PMC_LLC_MISSES, pending_samples);
		return;
	}

	/* detect if we're over the bandwidth threshold */
	throttle = ias_measure_bw() >= ias_bw_thresh;

	/* run the state machine */
	switch (state) {
	case IAS_BW_STATE_RELAX:
		if (throttle) {
			ias_bw_request_pmc(PMC_LLC_MISSES, start);
			state = IAS_BW_STATE_SAMPLE;
			break;
		}
		ias_bw_relax();
		break;

	case IAS_BW_STATE_SAMPLE:
		state = IAS_BW_STATE_PUNISH;
		ias_bw_gather_pmc(start);
		ias_bw_request_pmc(PMC_LLC_MISSES, end);
		break;

	case IAS_BW_STATE_PUNISH:
		ias_bw_gather_pmc(end);
		if (!throttle) {
			ias_bw_sample_aborts++;
			state = IAS_BW_STATE_RELAX;
			break;
		}
		ias_bw_punish(start, end);
		swapvars(start, end);
		ias_bw_request_pmc(PMC_LLC_MISSES, end);
		break;

	default:
		panic("ias: invalid bw state");
	}
}

int ias_bw_init(void)
{
	int ret;
	unsigned int nr_channels;
	struct cpuid_info regs;
	const char *intel_cpu_str = "GenuineIntel";
	int namebytes[3];

	if (cfg.nobw)
		return 0;

	cpuid(0, 0, &regs);
	namebytes[0] = regs.ebx;
	namebytes[1] = regs.edx;
	namebytes[2] = regs.ecx;

	if (memcmp(namebytes, intel_cpu_str, strlen(intel_cpu_str))) {
		log_warn("Detected non-Intel CPU. Disabling memory bandwidth monitoring!");
		cfg.nobw = true;
		return 0;
	}

	cpuid(1, 0, &regs);
	if (regs.ecx & (1UL << 31UL)) {
		log_warn("Detected CPU virtualization. Disabling memory bandwidth monitoring!");
		cfg.nobw = true;
		return 0;
	}

	/* ensure threads created by pcm are pinned to control core */
	pin_thread(0, sched_ctrl_core);

	/* numanode selects the workload socket and its memory counters. */
	ret = pcm_caladan_init(managed_numa_node < 0 ? 0 : managed_numa_node);
	if (ret)
		return ret;

	log_info("ias: bandwidth counters on socket %d",
	         managed_numa_node < 0 ? 0 : managed_numa_node);

	/* We monitor 1 channel, so multiply measurements by nr_channels to estimate real bw */
	nr_channels = pcm_caladan_get_active_channel_count();
	if (nr_channels == 0)
		return -EINVAL;

	/* Use default limit if none supplied */
	if (!cfg.ias_bw_limit)
		cfg.ias_bw_limit = IAS_BW_LIMIT;

	/* Compute the multiplier to convert cache lines/cycle to bytes/us (= MB/s) */
	ias_bw_estimate_multiplier = cycles_per_us * nr_channels * CACHE_LINE_SIZE;

	/* convert from MB/s to per channel cache line/cycle */
	ias_bw_thresh = cfg.ias_bw_limit / ias_bw_estimate_multiplier;

	return 0;

}
