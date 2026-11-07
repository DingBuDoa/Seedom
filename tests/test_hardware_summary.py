"""Compact screenshot diagnostics and failed runtime detection."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from hardware import get_hardware, hardware_summary


class HardwareSummaryTests(unittest.TestCase):
    def test_summary_uses_total_vram_and_system_version(self):
        summary = hardware_summary(dict(
            cpu='Intel(R) Core(TM) i7-14700K', system_label='Windows 11 25H2 (26200.1000)',
            devices=[dict(name='NVIDIA GeForce RTX 5070 Ti', total_bytes=16 * 2**30)],
            free_bytes=2**30, cuda_usable=True))
        self.assertEqual(summary, 'CPU: Intel Core i7-14700K | GPU: RTX 5070 Ti · 显存 16.0 GiB | Windows 11 25H2 (26200.1000)')

    def test_failed_probe_retains_cpu_and_system(self):
        identity = dict(cpu='AMD Ryzen 9 9950X', system_label='Windows 11 24H2 (26100.1000)')
        with patch('hardware.subprocess.run', side_effect=subprocess.TimeoutExpired('probe', 45)), \
             patch('hardware.system_information', return_value=identity):
            profile = get_hardware(sys.executable, force=True)
        summary = hardware_summary(profile)
        self.assertIn('CPU: AMD Ryzen 9 9950X', summary)
        self.assertIn('Windows 11 24H2', summary)
        self.assertIn('GPU: 检测失败', summary)
        self.assertFalse(profile['cuda_usable'])


if __name__ == '__main__':
    unittest.main()
