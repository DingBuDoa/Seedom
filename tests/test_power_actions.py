"""Power-action commands are opt-in and remain easy to verify without shutting down CI."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import system_actions

NO_WINDOW = getattr(system_actions.subprocess, 'CREATE_NO_WINDOW', 0)


class PowerActionTests(unittest.TestCase):
    def test_shutdown_and_sleep_commands(self):
        with patch.object(system_actions.os, 'name', 'nt'), patch.object(system_actions.subprocess, 'Popen') as popen:
            system_actions.execute_power_action('shutdown')
            popen.assert_called_once_with(['shutdown.exe', '/s', '/t', '0'], creationflags=NO_WINDOW, close_fds=True)
            popen.reset_mock()
            system_actions.execute_power_action('sleep')
            popen.assert_called_once_with(
                ['rundll32.exe', 'powrprof.dll,SetSuspendState', '0', '1', '0'],
                creationflags=NO_WINDOW, close_fds=True)

    def test_unknown_action_defaults_to_shutdown(self):
        self.assertEqual(system_actions.power_action_label('other'), '关机')


if __name__ == '__main__':
    unittest.main()
