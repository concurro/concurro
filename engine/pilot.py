"""Prepare the gateway credential and advance one supervised local task, with merging disabled."""

import argparse
import base64
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from bundle import Bundle
from controller import validate_config
from policy import Blocked, IMAGE, design_roster
import private

# Run from the target repository checkout; the engine is this file's checkout.
ENGINE = Path(__file__).resolve().parents[1]
TARGET = Path.cwd()
WORKFLOW = ".github/workflows/concurro.yml"
ENGINE_PIN = re.compile(r"uses:\s*[\w.-]+/concurro@([0-9a-f]{40})\b")
DIRECTORY = Path.home() / ".config/concurro-pilot"
GATEWAY_TOKEN = "CONCURRO_GATEWAY_TOKEN"
PERMISSIONS = {"contents": "write", "issues": "write", "pull_requests": "write",
               "checks": "read", "administration": "read"}


def private_file(path):
    """Read a bounded owner-only regular file outside both checkouts."""
    return private.read(path, (TARGET, ENGINE))


def write_private(path, data):
    """Create a private file outside both checkouts; never replace an existing credential."""
    private.write(path, data, (TARGET, ENGINE))


def read_secrets(path, config):
    """Accept only the gateway's operator token; provider and GitHub credentials never live here."""
    data = json.loads(private_file(path))
    if (not isinstance(data, dict) or set(data) != {GATEWAY_TOKEN}
            or not all(isinstance(v, str) and v.strip() and "\x00" not in v for v in data.values())):
        raise Blocked("credential file must hold only " + GATEWAY_TOKEN)
    return data


def capture_secrets(path):
    """Prompt on a real terminal without echoing or accepting secrets as arguments."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise Blocked("credential capture requires an interactive terminal")
    if Path(path).expanduser().exists():
        raise Blocked("credential file already exists; use a new private filename for rotation")
    data = {GATEWAY_TOKEN: getpass.getpass(GATEWAY_TOKEN + " (hidden): ").strip()}
    if not all(data.values()):
        raise Blocked("empty credentials were not saved")
    write_private(path, data)
    print("Dedicated credentials saved with owner-only permissions; no credentials printed.")


def owner_api(path, settings):
    """Use the operator's gh login, optionally a named account, for read-only preflight."""
    environment = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    user = settings.get("gh_user")
    if user:
        token = subprocess.run(["gh", "auth", "token", "--hostname", "github.com", "--user", user],
                               env=environment, capture_output=True, text=True, timeout=30)
        if token.returncode:
            raise Blocked("gh has no login for the configured gh_user")
        environment["GH_TOKEN"] = token.stdout.strip()
    result = subprocess.run(["gh", "api", path], cwd=TARGET, env=environment,
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise Blocked("owner preflight request failed: " + path)
    return json.loads(result.stdout)


def api(path, token, method="GET", data=None):
    """Use GitHub HTTPS with in-memory credentials and sanitized errors."""
    request = urllib.request.Request("https://api.github.com/" + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "concurro-local-pilot"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise Blocked("GitHub response exceeds pilot limit")
        return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        raise Blocked("GitHub App request failed (HTTP " + str(error.code) + ")") from None
    except urllib.error.URLError:
        raise Blocked("GitHub App connection failed") from None


def installation_token(settings, config):
    """Mint a short-lived App token narrowed to the sole pilot repository."""
    key = Path(settings["private_key_path"]).expanduser()
    private_file(key)
    def encode(value):
        """Encode a JWT segment without padding."""
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    now = int(time.time())
    message = encode(b'{"alg":"RS256","typ":"JWT"}') + b"." + encode(json.dumps(
        {"iat": now - 60, "exp": now + 540, "iss": str(settings["app_id"])}).encode())
    signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)],
                            input=message, capture_output=True, timeout=15)
    if signed.returncode:
        raise Blocked("GitHub App key could not sign authentication request")
    jwt = (message + b"." + encode(signed.stdout)).decode()
    app = api("app", jwt)
    if app.get("slug", "") + "[bot]" != config["bot_login"]:
        raise Blocked("App identity differs from configured bot_login")
    installation = api("repos/" + config["repository"] + "/installation", jwt)
    if installation.get("repository_selection") != "selected":
        raise Blocked("install the App on selected repositories, not all repositories")
    if installation.get("account", {}).get("login") != config["repository"].split("/")[0]:
        raise Blocked("App installation owner differs from pilot repository owner")
    granted = api(f"app/installations/{installation['id']}/access_tokens", jwt, "POST",
                  {"repositories": [config["repository"].split("/")[1]], "permissions": PERMISSIONS})
    token = granted["token"]
    try:
        permissions = {k: v for k, v in granted["permissions"].items() if k != "metadata"}
        if permissions != PERMISSIONS:
            raise Blocked("App token permissions differ from the least-privilege pilot scope")
        repositories = api("installation/repositories?per_page=100", token)
        if (repositories.get("total_count") != 1 or
                [r["full_name"] for r in repositories["repositories"]] != [config["repository"]]):
            raise Blocked("App token repository scope differs from the pilot repository")
    except BaseException:
        revoke(token)
        raise
    return token


def revoke(token):
    """Revoke only the temporary token minted for this invocation."""
    try:
        api("installation/token", token, "DELETE")
    except Exception:
        print("Temporary token revocation failed; it retains GitHub's normal expiry.", file=sys.stderr)


def preflight(settings, config):
    """Collect blockers without invoking models or writing GitHub state."""
    validate_config(config)
    problems = []
    if config["merge_enabled"] is not False:
        problems.append("merge_enabled must be false")
    if any(not i["model"].strip() for i in design_roster(config)):
        problems.append("all participating roles require explicit model IDs")
    if not isinstance(settings.get("app_id"), int) or settings["app_id"] <= 0:
        problems.append("GitHub App ID is not configured")
    try:
        private_file(settings.get("private_key_path") or "/nonexistent-concurro-key")
    except (OSError, Blocked):
        problems.append("GitHub App private key is missing or not an owner-only file")
    try:
        read_secrets(settings["credentials_file"], config)
    except (OSError, ValueError):
        problems.append("the gateway operator token is missing or invalid")
    if not str(settings.get("gateway_url", "")).startswith("https://"):
        problems.append("gateway_url is not configured")
    for kind in ("agent", "execution"):
        image = settings.get(kind + "_image", "")
        if not IMAGE.fullmatch(image):
            problems.append(kind + " image must be pinned by digest or local image ID")
            continue
        result = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30)
        if result.returncode:
            problems.append(kind + " image is unavailable locally")
    if IMAGE.fullmatch(settings.get("agent_image", "")):
        bundle = Bundle(ENGINE, target=TARGET, profiles=config["execution_profiles"])
        governance = json.dumps({"charter": bundle.charter_text, "profiles": config["execution_profiles"],
                                 "profile_texts": bundle.target_profile_texts})
        result = subprocess.run(["docker", "run", "--rm", "-i", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--entrypoint", "python3", settings["agent_image"],
            "-I", "-c", "import json, sys; sys.path.insert(0,'/opt/concurro/engine'); from bundle import Bundle; "
            "g = json.load(sys.stdin); print(Bundle('/opt/concurro', charter_text=g['charter'], "
            "profiles=g['profiles'], profile_texts=g['profile_texts']).identity)"],
            input=governance, capture_output=True, text=True, timeout=30)
        if result.returncode or result.stdout.strip() != bundle.identity:
            problems.append("inference image governance bundle differs from this engine checkout")
    repository = "repos/" + config["repository"]
    try:
        remote = owner_api(repository + "/branches/" + config["base"], settings)
        local = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=TARGET, text=True).strip()
        if local != remote["commit"]["sha"]:
            problems.append("local HEAD must equal the published main commit")
        for root in (TARGET, ENGINE):
            dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=root)
            untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=root)
            if dirty.returncode or untracked.strip():
                problems.append("review, sign, and publish all pipeline changes before a live pilot")
                break
        engine = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ENGINE, text=True).strip()
        if ENGINE_PIN.findall((TARGET / WORKFLOW).read_text()) != [engine]:
            problems.append("the target workflow must pin exactly this engine checkout's commit")
        for suffix in ("actions/variables", "environments/concurro/variables"):
            response = owner_api(repository + "/" + suffix + "?per_page=100", settings)
            variables = response["variables"]
            if response.get("total_count", len(variables)) > len(variables):
                raise Blocked("too many variables to establish disabled execution")
            enabled = next((v["value"] for v in variables if v["name"] == "CONCURRO_ENABLED"), "false")
            if enabled.lower() != "false":
                problems.append("scheduled Concurro execution must remain explicitly disabled")
        info = owner_api(repository, settings)
        if info.get("allow_auto_merge"):
            problems.append("GitHub auto-merge must remain disabled")
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            response = owner_api(repository + "/actions/runs?status=" + status + "&per_page=100", settings)
            runs = response["workflow_runs"]
            if response.get("total_count", len(runs)) > len(runs):
                raise Blocked("too many active runs to establish exclusive local execution")
            if any(r.get("path") == WORKFLOW for r in runs):
                problems.append("an Actions controller run is already pending or active")
    except (OSError, ValueError, subprocess.SubprocessError):
        problems.append("could not establish remote pilot safety settings")
    return problems


def advance(settings, config, issue):
    """Advance one explicit issue under an exclusive local lock, then revoke its token."""
    if issue <= 0:
        raise Blocked("issue must be a positive number")
    lock_path = DIRECTORY / "pilot.lock"
    DIRECTORY.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked("another local pilot invocation is active") from None
        problems = preflight(settings, config)
        if problems:
            raise Blocked("Pilot blocked: " + "; ".join(problems))
        credentials = read_secrets(settings["credentials_file"], config)
        reports = Path(tempfile.mkdtemp(prefix=f"issue-{issue}-", dir=DIRECTORY))
        token = installation_token(settings, config)
        try:
            environment = {k: os.environ[k] for k in ("PATH", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}
            home = reports / "controller-home"
            home.mkdir(mode=0o700)
            environment.update(credentials, HOME=str(home), XDG_STATE_HOME=str(home / ".local/state"),
                               XDG_CONFIG_HOME=str(home / ".config"), XDG_CACHE_HOME=str(home / ".cache"),
                               GH_TOKEN=token, CONCURRO_ISSUE=str(issue), CONCURRO_GATEWAY_URL=settings["gateway_url"],
                               CONCURRO_GATEWAY_HEADERS=json.dumps(settings.get("gateway_headers", {})),
                               CONCURRO_LOCAL_REPORT_DIR=str(reports),
                               CONCURRO_AGENT_IMAGE=settings["agent_image"], CONCURRO_EXECUTION_IMAGE=settings["execution_image"])
            result = subprocess.run([sys.executable, "-B", str(ENGINE / "engine/controller.py")], cwd=TARGET,
                                    env=environment, capture_output=True)
            def redact(output):
                """Keep bounded private diagnostics without the credentials passed to the child."""
                text = output.decode("utf-8", errors="replace")
                for secret in [token, *credentials.values()]:
                    text = text.replace(secret, "[REDACTED]")
                return text[-65536:]

            write_private(reports / "controller.json", {"exit_code": result.returncode,
                "stdout": redact(result.stdout), "stderr": redact(result.stderr)})
            print(f"Controller {'failed' if result.returncode else 'finished'} (exit {result.returncode}). "
                  f"Private diagnostics: {reports}")
            return result.returncode
        finally:
            revoke(token)


def main():
    """Expose read-only preflight, hidden credential capture and explicit one-stage execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "capture-secrets", "advance"))
    parser.add_argument("--settings", type=Path, default=DIRECTORY / "pilot.json")
    parser.add_argument("--credentials", type=Path, default=DIRECTORY / "credentials.json")
    parser.add_argument("--issue", type=int)
    args = parser.parse_args()
    try:
        if args.command == "capture-secrets":
            capture_secrets(args.credentials)
            return 0
        settings = json.loads(private_file(args.settings))
        config = json.loads((TARGET / ".concurro/config.json").read_text())
        if args.command == "preflight":
            problems = preflight(settings, config)
            print(json.dumps({"ready": not problems, "blockers": problems}, indent=2))
            return int(bool(problems))
        if args.issue is None:
            raise Blocked("advance requires --issue; queue scanning is disabled for the pilot")
        return advance(settings, config, args.issue)
    except (Blocked, OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, Blocked) else "Pilot setup is incomplete or unavailable; no raw credential errors displayed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
