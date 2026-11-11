"""Runner deployment addresses are explicit masked secrets, never public workflow inputs."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class WorkflowSecretsTests(unittest.TestCase):
    """Exercise the caller contract and the missing-secret startup boundary."""

    def test_display_inputs_reach_names_and_final_summary_without_shell_interpolation(self):
        """Display metadata is optional and reaches both workflow layers as data."""
        caller = yaml.load(
            (ROOT / "examples/stacks-core/.github/workflows/concurro-executor.yml").read_text(), Loader=yaml.BaseLoader
        )
        workflow = yaml.load((ROOT / ".github/workflows/agent.yml").read_text(), Loader=yaml.BaseLoader)
        for name in ("task-label", "operation-label", "agent-label"):
            self.assertEqual(caller["on"]["workflow_dispatch"]["inputs"][name]["default"], "")
            self.assertEqual(workflow["on"]["workflow_call"]["inputs"][name]["default"], "")
            self.assertEqual(caller["jobs"]["role"]["with"][name], "${{ inputs." + name + " }}")
            self.assertIn("inputs." + name, caller["run-name"])
        self.assertIn("inputs.operation-label", caller["jobs"]["role"]["name"])
        self.assertIn("inputs.agent-label", workflow["jobs"]["role"]["name"])
        steps = workflow["jobs"]["role"]["steps"]
        self.assertTrue(any("inputs.task-label" in step["name"] for step in steps))
        self.assertEqual(steps[-1]["if"], "always()")
        self.assertEqual(steps[-1]["env"]["JOB_STATUS"], "${{ job.status }}")
        for step in steps:
            self.assertNotIn("${{ inputs.", step.get("run", ""))

    def test_gateway_is_required_secret_not_public_input(self):
        """No caller can accidentally expose the address through a normal input or default."""
        workflow = (ROOT / ".github/workflows/agent.yml").read_text()
        declaration = workflow.split("    secrets:", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("CONCURRO_GATEWAY_URL:", declaration)
        self.assertIn("required: true", declaration)
        self.assertNotIn("      gateway:", workflow.split("    secrets:", 1)[0])
        self.assertNotIn("inputs.gateway", workflow)
        self.assertIn("GATEWAY: ${{ secrets.CONCURRO_GATEWAY_URL }}", workflow)
        caller = (ROOT / "examples/stacks-core/.github/workflows/concurro-executor.yml").read_text()
        self.assertIn("    secrets:\n      CONCURRO_GATEWAY_URL: ${{ secrets.CONCURRO_GATEWAY_URL }}", caller)
        self.assertNotIn("secrets: inherit", caller)

    def test_missing_secret_stops_before_oidc_and_claim(self):
        """The real first-step guard refuses an unset secret; a configured value is never echoed."""
        workflow = (ROOT / ".github/workflows/agent.yml").read_text()
        script = workflow.split("        run: |\n", 1)[1].split("          token=", 1)[0]
        script = "\n".join(line[10:] for line in script.splitlines())
        for gateway, succeeds in (("", False), ("https://deployment.example.test/concurro", True)):
            with self.subTest(configured=succeeds), tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ, GATEWAY=gateway, SESSION="11111111-1111-1111-1111-111111111111")
                result = subprocess.run(
                    ["bash", "-c", script], env=env, cwd=directory, capture_output=True, text=True, timeout=5
                )
                self.assertEqual(result.returncode == 0, succeeds)
                if succeeds:
                    self.assertNotIn(gateway, result.stdout + result.stderr)
                else:
                    self.assertIn("CONCURRO_GATEWAY_URL must be configured", result.stdout)


if __name__ == "__main__":
    unittest.main()
