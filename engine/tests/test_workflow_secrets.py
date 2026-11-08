"""Runner deployment addresses are explicit masked secrets, never public workflow inputs."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class WorkflowSecretsTests(unittest.TestCase):
    """Exercise the caller contract and the missing-secret startup boundary."""

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
