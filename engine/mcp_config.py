"""Target-declared MCP servers: one validated definition that each CLI adapter renders."""

import re

from model_config import NAME
from policy import Blocked

WORKSPACE = "{workspace}"
FIELDS = {"transport", "command", "args", "env"}
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def servers(config):
    """Validate the optional `mcp.servers` map and return each server's normalized launch."""
    mcp = config.get("mcp")
    if mcp is None:
        return {}
    if not isinstance(mcp, dict) or set(mcp) != {"servers"} or not isinstance(mcp["servers"], dict):
        raise Blocked("mcp must hold only a servers map")
    normalized = {}
    for name, server in mcp["servers"].items():
        if (
            not isinstance(name, str)
            or not NAME.fullmatch(name)
            or not isinstance(server, dict)
            or not {"transport", "command"} <= set(server) <= FIELDS
        ):
            raise Blocked("invalid MCP server name or fields")
        if server["transport"] != "stdio":
            raise Blocked("MCP servers support the stdio transport only")
        command, args, env = server["command"], server.get("args", []), server.get("env", {})
        if (
            not isinstance(command, str)
            or not command.strip()
            or not isinstance(args, list)
            or not all(isinstance(a, str) for a in args)
            or not isinstance(env, dict)
            or not all(isinstance(k, str) and VARIABLE.fullmatch(k) and isinstance(v, str) for k, v in env.items())
        ):
            raise Blocked("invalid MCP server command, args or env")
        normalized[name] = {"command": command, "args": list(args), "env": dict(env)}
    return normalized


def resolved(servers, workspace):
    """Bind `{workspace}` to the turn's checkout; a substituted path stays within its argument."""
    return {
        name: {**server, "args": [a.replace(WORKSPACE, workspace) for a in server["args"]]}
        for name, server in servers.items()
    }
