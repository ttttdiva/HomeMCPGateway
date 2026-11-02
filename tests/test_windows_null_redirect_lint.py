import unittest
from pathlib import Path

from scripts.check_windows_null_redirects import analyze_text, scan_root


class WindowsNullRedirectLintTests(unittest.TestCase):
    def test_reports_powerShell_redirection(self):
        findings = analyze_text("run.ps1", "Write-Output value > /dev/null\n")
        self.assertEqual(len(findings), 1)
        self.assertIn("PowerShell/cmd", findings[0].reason)

    def test_reports_cmd_redirection(self):
        findings = analyze_text("run.cmd", "echo value 2> /dev/null\n")
        self.assertEqual(len(findings), 1)

    def test_ignores_powerShell_comment(self):
        findings = analyze_text("run.ps1", "# /dev/null is POSIX-only\n")
        self.assertEqual(findings, [])

    def test_reports_python_shell_true_with_windows_command(self):
        source = 'subprocess.run("powershell -Command echo x > /dev/null", shell=True)\n'
        findings = analyze_text("run.py", source)
        self.assertEqual(len(findings), 1)
        self.assertIn("Python shell command", findings[0].reason)

    def test_reports_os_system_windows_command(self):
        findings = analyze_text("run.py", 'os.system("cmd /c echo x > /dev/null")\n')
        self.assertEqual(len(findings), 1)

    def test_reports_node_child_process_exec_windows_command(self):
        source = "const { exec } = require('child_process');\nexec('pwsh -Command echo x > /dev/null');\n"
        findings = analyze_text("run.js", source)
        self.assertEqual(len(findings), 1)
        self.assertIn("Node child_process.exec", findings[0].reason)

    def test_allows_explicit_posix_shell_command(self):
        source = 'subprocess.run("bash -lc \'echo x > /dev/null\'", shell=True)\n'
        self.assertEqual(analyze_text("run.py", source), [])

    def test_reports_python_direct_null_write_without_posix_guard(self):
        findings = analyze_text("run.py", 'subprocess.run("tool", stdout=open("/dev/null", "w"))\n')
        self.assertEqual(len(findings), 1)
        self.assertIn("filesystem sink", findings[0].reason)

    def test_allows_python_direct_null_path_with_posix_guard(self):
        source = 'if os.name == "posix":\n    subprocess.run("tool", stdout=open("/dev/null", "w"))\n'
        self.assertEqual(analyze_text("run.py", source), [])

    def test_allows_read_only_posix_null_path(self):
        self.assertEqual(analyze_text("run.py", 'value = os.open("/dev/null", os.O_RDONLY)\n'), [])

    def test_reports_yaml_windows_step(self):
        source = "runs-on: windows-latest\nsteps:\n  - name: test\n    shell: pwsh\n    run: Write-Output x > /dev/null\n"
        self.assertEqual(len(analyze_text("workflow.yml", source)), 1)

    def test_allows_yaml_bash_step_on_windows(self):
        source = "runs-on: windows-latest\nsteps:\n  - name: test\n    shell: bash\n    run: echo x > /dev/null\n"
        self.assertEqual(analyze_text("workflow.yml", source), [])

    def test_gateway_source_tree_is_clean(self):
        root = Path(__file__).resolve().parents[1]
        scanned, findings = scan_root(root)
        self.assertGreater(scanned, 0)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
