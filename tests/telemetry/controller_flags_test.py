"""Compile the actual startup flag parser without DPDK dependencies."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ControllerFlagsTest(unittest.TestCase):
    def test_parser(self):
        source = (ROOT / 'iokernel/main.c').read_text()
        start = source.index('} else if (!strcmp(argv[i], "bw_intervention")')
        end = source.index('} else if (!strcmp(argv[i], "telemetry"))', start)
        branch = source[start:end]
        config_start = source.index('struct iokernel_cfg cfg = {')
        config_end = source.index('};', config_start) + 2
        config = source[config_start:config_end]
        program = r'''
#include <stdio.h>
#include <string.h>
#include "iokernel/defs.h"
'''+config+r'''
int main(int argc, char **argv) {
    bool controller_flags_supplied = false;
    for (int i = 1; i < argc; i++) {
        if (false) {
'''+branch+r'''
        } else { return 2; }
    }
    printf("%d %d %d %d %d\n", cfg.bw_intervention, cfg.ht_intervention,
           cfg.timesharing, cfg.core_allocation, controller_flags_supplied);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            test = Path(directory) / 'flags.c'
            binary = Path(directory) / 'flags'
            test.write_text(program)
            subprocess.run(['cc', '-D_GNU_SOURCE', '-std=gnu11', '-O2', '-Iinc', '-I.',
                            str(test), '-o', str(binary)], cwd=ROOT, check=True)
            def run(*args):
                return subprocess.run([str(binary), *args], capture_output=True, text=True)
            self.assertEqual(run().stdout.strip(), '0 0 0 0 0')
            options = ['bw_intervention', 'ht_intervention', 'timesharing', 'core_allocation']
            for index, option in enumerate(options):
                for value in ('on', 'off'):
                    expected = [0, 0, 0, 0, 1]
                    expected[index] = int(value == 'on')
                    result = run(option, value)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), ' '.join(map(str, expected)))
                self.assertNotEqual(run(option).returncode, 0)
                self.assertNotEqual(run(option, 'invalid').returncode, 0)
            result = run('bw_intervention', 'on', 'ht_intervention', 'off',
                         'timesharing', 'off', 'core_allocation', 'off')
            self.assertEqual(result.stdout.strip(), '1 0 0 0 1')


if __name__ == '__main__':
    unittest.main()
