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
        self.assertEqual(sum(line.startswith("ssh ") for line in trace.splitlines()), 1)
        self.assertNotIn("scp ", trace)
        self.assertNotIn("mkdir ", trace)
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
if [[ "$name" == python3 ]]; then
  [[ "$FAIL" != ci ]] || exit 1
  echo "$SHA $WEB_SHA"
elif [[ "$name" == sudo ]]; then
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

    def run_host(
        self, phase, failure="", releases=WEB, service_override=False, private_settings=False, installed=False
    ):
        """Return helper output, calls and the release file from an isolated fake host."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "concurro").mkdir()
            (root / ".local/lib/concurro-deploy").mkdir(parents=True)
            (root / "concurro-release.env").write_text(releases)
            commands = root / "bin"
            commands.mkdir()
            for command in ("sudo", "git", "flock", "python3"):
                path = commands / command
                path.write_text(HOST_MOCK)
                path.chmod(0o700)
            env = dict(
                os.environ,
                HOME=str(root),
                PATH=f"{commands}:/usr/bin:/bin",
                TRACE=str(root / "trace"),
                SHA=SHA,
                WEB_SHA="3" * 40,
                FAIL=failure,
            )
            private = root / "private-service"
            private.mkdir(mode=0o700)
            private_override = private / "compose.host.yaml"
            override = root / "compose.service.host.yaml"
            if service_override:
                override.write_text(
                    "services:\n  backoffice:\n    volumes:\n      - /var/lib/concurro/backoffice:/var/lib/concurro/backoffice-auth\n"
                )
                private_override.write_text(override.read_text())
            if private_settings:
                private.chmod(0)
                if os.geteuid() != 0:
                    self.assertFalse(os.access(private, os.X_OK))
            helper = root / "host.sh"
            # Substitute only the external fixture path; execute the production argument construction.
            helper.write_text(
                (DEPLOY / "host.sh")
                .read_text()
                .replace("/etc/concurro/service/compose.host.yaml", str(private_override))
                .replace("/etc/concurro/compose.service.host.yaml", str(override))
            )
            command = ["bash", str(helper), phase, SHA]
            if phase == "all":
                command.append(env["WEB_SHA"])
            if installed:
                setup = subprocess.run(["bash", str(DEPLOY / "install.sh")], env=env, text=True, capture_output=True)
                self.assertEqual(setup.returncode, 0, setup.stderr)
                self.assertNotIn("sudo", (root / "trace").read_text())
                installed_host = root / ".local/lib/concurro-deploy/host.sh"
                installed_host.write_text(
                    installed_host.read_text().replace("/etc/concurro/compose.service.host.yaml", str(override))
                )
                (root / "trace").write_text("")
                command = [str(root / ".local/bin/update-concurro.sh")]
            result = subprocess.run(command, env=env, text=True, capture_output=True)
            private.chmod(0o700)
            trace = root / "trace"
            return result, trace.read_text() if trace.exists() else "", (root / "concurro-release.env").read_text()

    def test_all_updates_both_releases_under_one_lock_and_restart(self):
        """A combined update includes web in the service rollout instead of restarting it separately."""
        result, trace, releases = self.run_host("all", releases="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(releases, f"CONCURRO_WEB_RELEASE={'3' * 40}\nCONCURRO_RELEASE={SHA}\n")
        self.assertEqual(trace.count("flock -n 9"), 1)
        self.assertEqual(trace.count(" stop service"), 1)
        self.assertNotIn(" pull web", trace)
        self.assertIn(" --wait-timeout 180", trace)

    def test_combined_preflight_failure_does_not_change_either_release(self):
        """Service authorization failure leaves both recorded tags and the running service untouched."""
        before = WEB + f"CONCURRO_RELEASE={'2' * 40}\n"
        result, trace, releases = self.run_host("all", "backward", releases=before)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(releases, before)
        self.assertNotIn("sudo ", trace)

    def test_installed_command_runs_locally_and_stops_before_deployment_on_ci_failure(self):
        """Installation creates a standalone host command; failed release resolution never invokes Docker."""
        for failure in ("", "ci"):
            with self.subTest(failure=failure):
                result, trace, releases = self.run_host(
                    "all", failure, releases="", installed=True, service_override=True
                )
                self.assertEqual(result.returncode == 0, not failure, result.stderr)
                self.assertNotIn("ssh ", trace)
                self.assertNotIn("scp ", trace)
                self.assertIn("python3 ", trace)
                if failure:
                    self.assertNotIn("sudo ", trace)
                    self.assertNotIn("flock ", trace)
                    self.assertEqual(releases, "")
                else:
                    self.assertIn("Deployment verified:", result.stdout)
                    self.assertEqual(trace.count(" stop service"), 1)
                    calls = [
                        line
                        for line in trace.splitlines()
                        if "docker compose" in line and "/concurro/service/compose.yaml" in line
                    ]
                    self.assertTrue(calls)
                    self.assertTrue(all("compose.service.host.yaml" in line for line in calls))

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

    def test_service_override_persists_for_service_and_web_releases(self):
        """Every service-stack Compose action retains an explicitly installed host volume overlay."""
        for phase in ("service", "web", "all"):
            with self.subTest(phase=phase):
                result, trace, _ = self.run_host(
                    phase, releases=WEB + f"CONCURRO_RELEASE={SHA}\n", service_override=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [
                    line
                    for line in trace.splitlines()
                    if "docker compose" in line and "/concurro/service/compose.yaml" in line
                ]
                self.assertTrue(calls)
                self.assertTrue(
                    all("compose.service.host.yaml" in line and line.count(" -f ") == 2 for line in calls), calls
                )

    def test_service_override_is_discovered_outside_untraversable_private_settings(self):
        """The deploy user can find the public overlay without traversing uid65533 settings."""
        result, trace, _ = self.run_host("service", service_override=True, private_settings=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [
            line for line in trace.splitlines() if "docker compose" in line and "/concurro/service/compose.yaml" in line
        ]
        self.assertTrue(calls)
        self.assertTrue(all("compose.service.host.yaml" in line and "/private-service/" not in line for line in calls))

    def test_absent_service_override_keeps_existing_compose_arguments(self):
        """Unconfigured deployments keep the original base-only service command."""
        result, trace, _ = self.run_host("service")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [
            line for line in trace.splitlines() if "docker compose" in line and "/concurro/service/compose.yaml" in line
        ]
        self.assertTrue(calls)
        self.assertTrue(
            all("compose.service.host.yaml" not in line and line.count(" -f ") == 1 for line in calls), calls
        )

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
