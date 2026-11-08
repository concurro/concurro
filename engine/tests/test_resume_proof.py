"""The Codex resume proof's own plumbing, with Docker and Codex faked."""

import io
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

import resume_proof


class FakeCodex:
    """Plays `docker run ... codex exec`: a first turn writes a transcript and caches; a resume reads it."""

    def __init__(self, forget=False):
        self.forget, self.calls = forget, []

    def __call__(self, command, input, **_):
        codex = Path(_["env"]["CODEX_HOME"])
        self.calls.append(command)
        if "resume" in command:
            transcript = codex / "sessions/t.jsonl"
            answer = "" if self.forget or not transcript.exists() else transcript.read_text()
            assert not (codex / ".tmp").exists(), "caches are not restored"
        else:
            (codex / "sessions").mkdir(parents=True)
            (codex / "sessions/t.jsonl").write_text(re.search(r"concurro-[0-9a-f]+", input).group())
            (codex / ".tmp/plugins").mkdir(parents=True)
            (codex / ".tmp/plugins/big").write_bytes(b"x" * 8192)
            answer = "noted"
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps({"answer": answer}))
        stdout = json.dumps({"type": "thread.started", "thread_id": "0190a1a7-6de4-7733-87b3-42daa52eb665"}) + "\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")


class ResumeProofTests(unittest.TestCase):
    def run_proof(self, codex):
        root = Path(tempfile.mkdtemp())
        (root / "key").write_text("session-key\n")
        output = io.StringIO()
        with patch.object(resume_proof.subprocess, "run", codex), redirect_stdout(output):
            resume_proof.main(
                [
                    "--gateway",
                    "https://gateway.invalid",
                    "--key",
                    str(root / "key"),
                    "--model",
                    "gpt-test",
                    "--root",
                    str(root / "proof"),
                ]
            )
        return output.getvalue()

    def test_a_restored_archive_resumes_with_its_context_and_without_caches(self):
        codex = FakeCodex()
        output = self.run_proof(codex)
        self.assertIn('dropped        8 KiB  "codex/.tmp"', output)
        self.assertIn("proof: the restored archive resumed the same thread", output)
        first, resumed = codex.calls
        self.assertNotIn("session-key", " ".join(first), "the key travels in the environment, not the arguments")
        self.assertEqual(resumed[resumed.index("resume") :][-2:], ["0190a1a7-6de4-7733-87b3-42daa52eb665", "-"])

    def test_a_resume_without_the_context_fails(self):
        with self.assertRaisesRegex(resume_proof.ProofFailed, "did not recall"):
            self.run_proof(FakeCodex(forget=True))


if __name__ == "__main__":
    unittest.main()
