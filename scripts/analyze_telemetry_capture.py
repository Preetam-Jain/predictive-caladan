#!/usr/bin/env python3
"""Analyze a finalized attempt using bounded C++ streaming and compact output."""
import argparse
import csv
import json
from pathlib import Path
import re
import subprocess
import uuid
from decode_telemetry import read_header, capture_streams


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('attempt',type=Path)
    p.add_argument('--plot-only',action='store_true')
    args=p.parse_args()
    attempt=args.attempt.resolve();out=attempt/'analysis';out.mkdir(exist_ok=True)
    record=json.loads((attempt/'run.json').read_text());config=json.loads((attempt/'config.json').read_text())
    prefix=str(attempt/'server/capture');headers={};identity=None
    for suffix,kind in capture_streams(prefix):
        path=Path(prefix+'.'+suffix+'.bin')
        with path.open('rb') as f:h,cores=read_header(f)
        current=(h.experiment_id,h.cycles_per_us,h.writer_core,h.scheduler_core,cores)
        if identity is not None and current!=identity:raise ValueError('Mismatched capture identity')
        identity=current
        if path.stat().st_size!=h.header_bytes+h.record_bytes*h.produced:raise ValueError('Invalid file size '+suffix)
        headers[suffix]=dict(version=h.version,record_bytes=h.record_bytes,produced=h.produced,dropped=h.dropped,bytes=path.stat().st_size)
    cpu=identity[1];start=record['summary']['first_server_tsc'];duration=config['suite']['measurement_seconds']
    pid=int(re.search(r'process pid:\s*(\d+)',(attempt/'server/memcached.log').read_text()).group(1))
    metadata=dict(attempt=str(attempt),client=record['summary'],measurement_start_tsc=start,measurement_seconds=duration,
                  alignment='First measured response server TSC; endpoint approximates the scheduled window within response-latency scale.',
                  pid=pid,cycles_per_us=cpu,writer_cpu=identity[2],scheduler_cpu=identity[3],cores=identity[4],
                  experiment_id=str(uuid.UUID(bytes=identity[0])),streams=headers,
                  percentile_method='All observations histogrammed; exact integer bins through 65535 raw units, <=0.4% relative bin width above. Process delay rounded to nanoseconds.',
                  bandwidth_method='Caladan channel-0 CAS deltas modulo 2^32, scaled by header bandwidth_multiplier/cycles_per_us; socket estimate, not sum of all channel counters.',
                  limitations=['Observation-weighted distributions, not per-request or completed-task distributions.','No request-level latency traces were enabled; cannot correlate individual requests with telemetry.','LLC deltas exclude ownership changes, counter decreases and nonpositive time; see coverage.','LLC delta bins use interval end timestamps; long gaps limit temporal resolution.'])
    (out/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    source=Path(__file__).with_name('telemetry_capture_stats.cc');binary=out/'stream-analyzer'
    subprocess.run(['g++','-std=c++17','-O3',str(source),'-o',str(binary)],check=True)
    if not args.plot_only:
        subprocess.run([str(binary),prefix,str(start),str(duration),str(pid),str(out),str(cpu)],check=True)
    def rows(name):
        with (out/name).open() as f:return list(csv.DictReader(f))
    report=dict(metadata=metadata,counters=json.loads((out/'counters.json').read_text()),distributions=rows('distributions.csv'))
    (out/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    ts=rows('timeseries.csv');x=[float(r['second']) for r in ts]
    fig,axs=plt.subplots(4,1,figsize=(11,10),sharex=True,constrained_layout=True)
    def plot(ax,key,label):ax.plot(x,[float(r[key]) for r in ts],label=label,lw=1.5)
    plot(axs[0],'estimated_bandwidth_GB_s','Estimated socket bandwidth');axs[0].set_ylabel('GB/s')
    plot(axs[1],'llc_misses_per_second','Attributed LLC misses');axs[1].set_ylabel('Misses/s')
    plot(axs[2],'process_delay_mean_us','Mean process max-delay observation');plot(axs[2],'process_delay_max_us','Largest process delay');axs[2].set_ylabel('µs (symlog)');axs[2].set_yscale('symlog',linthresh=1);axs[2].legend(fontsize=8)
    plot(axs[3],'active_threads_mean','Active runtime workers');axs[3].set_ylabel('Active workers');axs[3].set_ylim(0,25);right=axs[3].twinx();right.plot(x,[float(r['user_context_percent']) for r in ts],color='tab:orange',label='User-context observations (%)');right.set_ylabel('User-context observations (%)');right.set_ylim(0,100);axs[3].set_xlabel('Seconds from first measured response');axs[3].legend(loc='upper left',fontsize=8);right.legend(loc='upper right',fontsize=8)
    for ax in axs:ax.grid(alpha=.25)
    fig.suptitle('Memcached alone · 25 cores · other-socket logging · attempt-002')
    fig.savefig(out/'measurement-timeseries.png',dpi=170);fig.savefig(out/'measurement-timeseries.svg');plt.close(fig)
    print(json.dumps(dict(output=str(out),estimated_bandwidth_GB_s=report['counters']['estimated_mean_bandwidth_GB_s'],records_in_window=report['counters']['counts']),indent=2))


if __name__=='__main__':main()
