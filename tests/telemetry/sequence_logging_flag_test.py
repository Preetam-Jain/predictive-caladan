"""Check the actual IOKernel sequence diagnostic toggle without DPDK headers."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SequenceLoggingFlagTest(unittest.TestCase):
    def test_toggle_defaults_off_and_rejects_bad_arguments(self):
        source = (ROOT/'iokernel/main.c').read_text()
        start = source.index('} else if (!strcmp(argv[i], "queue_snapshot_failure_logging"))')
        end = source.index('} else if (!strcmp(argv[i], "bw_intervention")', start)
        branch = source[start:end]
        program = '''
#include <stdio.h>
#include <string.h>
#include "iokernel/defs.h"
struct iokernel_cfg cfg;
int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        if (false) {
'''+branch+'''
        } else { return 2; }
    }
    printf("%d\\n", cfg.queue_snapshot_failure_logging);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'flag.c';exe = Path(d)/'flag';p.write_text(program)
            subprocess.run(['cc','-D_GNU_SOURCE','-std=gnu11','-O2','-Iinc','-I.',str(p),'-o',str(exe)],cwd=ROOT,check=True)
            def run(*args):return subprocess.run([str(exe),*args],capture_output=True,text=True)
            self.assertEqual(run().stdout.strip(),'0')
            self.assertEqual(run('queue_snapshot_failure_logging','on').stdout.strip(),'1')
            self.assertEqual(run('queue_snapshot_failure_logging','off').stdout.strip(),'0')
            self.assertNotEqual(run('queue_snapshot_failure_logging').returncode,0)
            self.assertNotEqual(run('queue_snapshot_failure_logging','yes').returncode,0)


if __name__=='__main__':unittest.main()
