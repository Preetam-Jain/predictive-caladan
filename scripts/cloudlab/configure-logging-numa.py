#!/usr/bin/env python3
"""Place logging work on a NUMA node; save the original settings for restoration."""
import argparse
import json
import subprocess
from pathlib import Path


def expand(value):
    result = set()
    for part in value.strip().split(','):
        ends = list(map(int, part.split('-')))
        result.update(range(ends[0], ends[-1] + 1))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', type=int, default=0)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    if args.restore:
        state = json.loads(args.state.read_text())
        for path, value in state['settings'].items():
            if Path(path).read_text() != value:
                Path(path).write_text(value)
        if state['irqbalance_active']:
            subprocess.run(['systemctl', 'start', 'irqbalance'], check=True)
        print('Restored original logging affinity settings')
        return
    if args.state.exists():
        raise SystemExit('State file already exists; restore first or use a new state path')
    cpus = sorted(expand(Path(f'/sys/devices/system/node/node{args.node}/cpulist').read_text()) &
                  expand(Path('/sys/devices/system/cpu/online').read_text()))
    if not cpus:
        raise SystemExit('Requested NUMA node has no online CPUs')
    mask = sum(1 << cpu for cpu in cpus)
    groups = [f'{(mask >> shift) & 0xffffffff:08x}'
              for shift in range(((max(cpus) // 32) * 32), -1, -32)]
    cpu_mask = ','.join(groups)
    cpu_list = ','.join(map(str, cpus))
    paths = [Path('/sys/devices/virtual/workqueue/cpumask'),
             Path('/sys/bus/workqueue/devices/writeback/cpumask')]
    # Storage controllers only; leave network IRQs untouched.
    irq_paths = []
    for device in Path('/sys/bus/pci/devices').iterdir():
        if int((device / 'class').read_text().strip(), 16) >> 16 != 0x01:
            continue
        irq_paths.extend(Path('/proc/irq') / irq.name / 'smp_affinity_list'
                         for irq in (device / 'msi_irqs').glob('*'))
    state = dict(node=args.node, cpus=cpus, settings={},
                 irqbalance_active=subprocess.run(['systemctl', 'is-active', '--quiet', 'irqbalance']).returncode == 0)
    for path in paths + irq_paths:
        state['settings'][str(path)] = path.read_text()
    args.state.write_text(json.dumps(state, indent=2))
    if state['irqbalance_active']:
        subprocess.run(['systemctl', 'stop', 'irqbalance'], check=True)
    for path in paths:
        path.write_text(cpu_mask)
        if int(path.read_text().replace(',', '').strip(), 16) != mask:
            raise RuntimeError('Workqueue affinity verification failed: ' + str(path))
    pinned, managed = [], []
    for path in irq_paths:
        try:
            path.write_text(cpu_list)
        except OSError:
            # Managed IRQs cannot always be moved by userspace; report actual placement.
            managed.append(dict(path=str(path), effective=(path.parent / 'effective_affinity_list').read_text().strip()))
        else:
            pinned.append(str(path))
    state['storage_irqs_not_movable'] = managed
    args.state.write_text(json.dumps(state, indent=2))
    print(json.dumps(dict(node=args.node, cpus=cpu_list, workqueue_mask=cpu_mask,
                          storage_irqs_pinned=len(pinned), storage_irqs_not_movable=len(managed),
                          restore_state=str(args.state)), indent=2))


if __name__ == '__main__':
    main()
