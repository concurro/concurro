"""Check deployment ordering and failure stops using fake commands, without server access."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1]
SHA = "f190af41f16ed20848cf68c8be386c4d6dfc6d5c"
MOCK = """#!/bin/bash
name=${0##*/}
printf '%s %s\\n' "$name" "$*" >> "$TRACE"
case "$name $*" in
  'gh run list '*) echo 123 ;;
  'gh run watch '*) test "$FAIL" != ci ;;
  'gh run view '*) echo success ;;
  git*' merge-base '*) test "$FAIL" != unpublished ;;
esac
"""


class DeployTests(unittest.TestCase):
    def run_deploy(self, component="service", failure=""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for command in ("gh", "git", "ssh", "scp"):
                path = root / command
                path.write_text(MOCK)
                path.chmod(0o700)
            env = dict(
                os.environ,
                PATH=f"{root}:/usr/bin:/bin",
                TRACE=str(root / "trace"),
                SHA=SHA,
                FAIL=failure,
                CONCURRO_DEPLOY_HOST="example-host",
            )
            result = subprocess.run(
                ["bash", str(DEPLOY / "deploy.sh"), component, SHA], env=env, text=True, capture_output=True
            )
            return result, (root / "trace").read_text()

    def test_service_waits_for_ci_then_deploys_without_writing_to_github(self):
        result, trace = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(
            trace.index("--repo concurro/concurro --workflow ci.yml"), trace.index(f"host.sh service {SHA}")
        )
        self.assertNotIn(" push origin", trace)
        self.assertNotIn("deployed", trace)

    def test_ci_failure_never_touches_host(self):
        for component in ("service", "web"):
            with self.subTest(component=component):
                result, trace = self.run_deploy(component, "ci")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("ssh ", trace)

    def test_an_unpublished_release_never_touches_host(self):
        result, trace = self.run_deploy(failure="unpublished")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ssh ", trace)

    def test_web_waits_for_its_own_ci_and_leaves_the_release_ref(self):
        result, trace = self.run_deploy("web")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--repo concurro/concurro-backoffice --workflow ci.yml", trace)
        self.assertIn(f"host.sh web {SHA}", trace)
        self.assertNotIn("git ", trace)


HOST_MOCK = r"""#!/bin/bash
name=${0##*/}
printf '%s %s\n' "$name" "$*" >> "$TRACE"
if [[ "$name" == sudo ]]; then
  [[ "$1" == -n && "$2" == docker ]] || { echo 'Would request a password' >&2; exit 90; }
  [[ "$FAIL" != sudo ]] || { echo 'sudo: a password is required' >&2; exit 1; }
  # Compose refuses the release's files until its image tag is recorded.
  if [[ "$3" == compose ]] && ! grep -q '^CONCURRO_RELEASE=' "$5"; then
    echo 'required variable CONCURRO_RELEASE is missing a value' >&2
    exit 1
  fi
  case " $* " in
    *' ps -q service '*) echo container-id ;;
    *' inspect '*) echo '2026-11-05T00:00:00Z' ;;
    *' logs '*) echo "serving engine $SHA" ;;
  esac
elif [[ "$name" == git && "$1" == merge-base ]]; then
  # Only the fetched main, or the release itself, descends from a recorded release.
  [[ "$FAIL" != backward || "$3" == "$4" || "$4" == origin/main ]]
fi
"""
WEB = "CONCURRO_WEB_RELEASE=" + "1" * 40 + "\n"


class HostTests(unittest.TestCase):
    """Run the server helper against passwordless-Docker-only sudo, without a server."""

    def run_host(self, phase, failure="", releases=WEB):
        """Return helper output, calls and the release file from an isolated fake host."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "concurro").mkdir()
            (root / ".local/lib/concurro-deploy").mkdir(parents=True)
            (root / "concurro-release.env").write_text(releases)
            commands = root / "bin"
            commands.mkdir()
            for command in ("sudo", "git", "flock"):
                path = commands / command
                path.write_text(HOST_MOCK)
                path.chmod(0o700)
            env = dict(
                os.environ,
                HOME=str(root),
                PATH=f"{commands}:/usr/bin:/bin",
                TRACE=str(root / "trace"),
                SHA=SHA,
                FAIL=failure,
            )
            result = subprocess.run(
                ["bash", str(DEPLOY / "host.sh"), phase, SHA], env=env, text=True, capture_output=True
            )
            trace = root / "trace"
            return result, trace.read_text() if trace.exists() else "", (root / "concurro-release.env").read_text()

    def test_every_phase_only_uses_noninteractive_docker_sudo(self):
        deployed = WEB + f"CONCURRO_RELEASE={SHA}\n"
        for phase, releases in (("service", WEB), ("service", deployed), ("web", deployed)):
            with self.subTest(phase=phase, releases=releases):
                result, trace, _ = self.run_host(phase, releases=releases)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [line for line in trace.splitlines() if line.startswith("sudo ")]
                self.assertTrue(calls)
                self.assertTrue(all(line.startswith("sudo -n docker ") for line in calls))
                self.assertNotIn(" build", trace)
                self.assertNotIn("deployed", trace)

    def test_denied_sudo_stops_before_service_stop(self):
        result, trace, _ = self.run_host("service", "sudo")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("password is required", result.stderr)
        self.assertNotIn(" stop service", trace)

    def test_service_records_the_release_pulls_while_stopped_and_starts(self):
        result, trace, releases = self.run_host("service")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(releases, WEB + f"CONCURRO_RELEASE={SHA}\n")
        self.assertLess(trace.index(" stop service"), trace.index(" pull"))
        self.assertLess(trace.index(" pull"), trace.index(" up -d"))

    def test_service_needs_a_web_release_before_stopping(self):
        result, trace, _ = self.run_host("service", releases="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deploy.sh web", result.stderr)
        self.assertNotIn(" stop service", trace)

    def test_service_never_moves_backward(self):
        result, trace, releases = self.run_host("service", "backward", releases=WEB + f"CONCURRO_RELEASE={'2' * 40}\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backward or sideways", result.stderr)
        self.assertIn(f"CONCURRO_RELEASE={'2' * 40}\n", releases)
        self.assertNotIn("sudo ", trace)

    def test_web_replaces_its_release_and_restarts_only_the_web_app(self):
        result, trace, releases = self.run_host("web", releases=WEB + f"CONCURRO_RELEASE={'2' * 40}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(releases, f"CONCURRO_RELEASE={'2' * 40}\nCONCURRO_WEB_RELEASE={SHA}\n")
        self.assertIn(" pull web", trace)
        self.assertIn(" --wait-timeout 120 web", trace)
        self.assertNotIn(" stop ", trace)

    def test_web_only_records_before_the_first_service_deployment(self):
        result, trace, releases = self.run_host("web", releases="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(releases, f"CONCURRO_WEB_RELEASE={SHA}\n")
        self.assertNotIn("sudo ", trace)

    def test_web_fails_when_its_deployment_does(self):
        result, trace, _ = self.run_host("web", "sudo", releases=WEB + f"CONCURRO_RELEASE={SHA}\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(" up ", trace)

    def test_fresh_host_bootstraps_web_then_service(self):
        result, _, releases = self.run_host("web", releases="")
        self.assertEqual(result.returncode, 0, result.stderr)
        result, trace, releases = self.run_host("service", releases=releases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"CONCURRO_RELEASE={SHA}\n", releases)
        self.assertLess(trace.index(f"ghcr.io/concurro/service:{SHA}"), trace.index(" stop service"))


if __name__ == "__main__":
    unittest.main()
