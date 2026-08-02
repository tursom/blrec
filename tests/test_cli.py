import unittest
from unittest.mock import patch

import blrec.cli.main as cli_main


class CliMainTestCase(unittest.TestCase):
    def test_main_preserves_successful_system_exit(self) -> None:
        with patch.object(cli_main, 'cli', side_effect=SystemExit(0)):
            self.assertEqual(cli_main.main(), 0)

    def test_main_preserves_failed_system_exit(self) -> None:
        with patch.object(cli_main, 'cli', side_effect=SystemExit(2)):
            self.assertEqual(cli_main.main(), 2)


if __name__ == '__main__':
    unittest.main()
