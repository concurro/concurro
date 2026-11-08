"""Git environment isolation shared by native turns, mirrors and repository tools."""

import os
import tempfile

GIT_ISOLATION = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def isolated_git_env():
    """A fresh Git-only environment without inherited credentials or configuration."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tempfile.gettempdir(),
        "LC_ALL": "C",
        **GIT_ISOLATION,
    }


def safe_directory(value):
    """Trust the caller's directory value through Git's environment configuration."""
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": value,
    }
