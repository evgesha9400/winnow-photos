"""
tests/test_cli.py - Tests for the winnow-photos CLI entrypoint and argument parsing.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import main


class TestCLIEntrypoint(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("main.run_server")
    def test_default_folder_resolves_to_cwd(self, mock_run_server):
        import os
        orig_cwd = os.getcwd()
        try:
            os.chdir(self.dir_path)
            with patch.object(sys, "argv", ["winnow-photos"]):
                main.main()
                mock_run_server.assert_called_once_with(
                    port=8766,
                    initial_folder=str(self.dir_path),
                    force_rescan=False,
                    open_browser=False
                )
        finally:
            os.chdir(orig_cwd)

    @patch("main.run_server")
    def test_explicit_folder_argument(self, mock_run_server):
        sub = self.dir_path / "subfolder"
        sub.mkdir()
        with patch.object(sys, "argv", ["winnow-photos", str(sub), "--port", "9123", "--scan", "--open"]):
            main.main()
            mock_run_server.assert_called_once_with(
                port=9123,
                initial_folder=str(sub),
                force_rescan=True,
                open_browser=True
            )

    def test_nonexistent_folder_exits(self):
        non_existent = str(self.dir_path / "does_not_exist")
        with patch.object(sys, "argv", ["winnow-photos", non_existent]):
            with self.assertRaises(SystemExit) as ctx:
                main.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_file_instead_of_dir_exits(self):
        file_path = self.dir_path / "some_file.txt"
        file_path.touch()
        with patch.object(sys, "argv", ["photo-studio", str(file_path)]):
            with self.assertRaises(SystemExit) as ctx:
                main.main()
            self.assertEqual(ctx.exception.code, 1)
