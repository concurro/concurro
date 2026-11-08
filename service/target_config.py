"""Read either JSON or safe YAML target configuration with one unambiguous source."""

import json
from pathlib import Path

from github import NotFound
from policy import Blocked

CONFIG_PATHS = (".concurro/config.json", ".concurro/config.yaml")


def pairs(items):
    """Reject duplicate keys instead of silently taking a later assignment."""
    result = {}
    for key, value in items:
        if not isinstance(key, str) or key in result:
            raise ValueError("configuration keys must be unique strings")
        result[key] = value
    return result


def decode(text, path):
    """Load configuration data only; YAML objects and aliases cannot construct executable values."""
    try:
        if str(path).endswith(".json"):
            value = json.loads(text, object_pairs_hook=pairs)
        else:
            import yaml

            class Loader(yaml.SafeLoader):
                """A safe mapping loader that rejects ambiguous duplicate keys."""

                def construct_mapping(self, node, deep=False):
                    return pairs(
                        (self.construct_object(key, deep=deep), self.construct_object(value, deep=deep))
                        for key, value in node.value
                    )

            # Configs need only ordinary maps/lists/scalars; aliases allow recursive or amplified data.
            if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(text)):
                raise ValueError("YAML aliases are not supported in target configuration")
            value = yaml.load(text, Loader=Loader)
            json.dumps(value, allow_nan=False)
        if not isinstance(value, dict):
            raise ValueError("configuration must be an object")
        return value
    except (ValueError, TypeError, RecursionError) as error:
        raise Blocked("invalid target configuration: " + str(path)) from error
    except ImportError:
        raise Blocked("YAML configuration requires the service requirements to be installed") from None
    except Exception as error:
        # Parser diagnostics can contain source text; keep them out of public task messages.
        raise Blocked("invalid target configuration: " + str(path)) from error


def read_remote(read):
    """Read exactly one configuration at the caller's pinned commit."""
    found = []
    for path in CONFIG_PATHS:
        try:
            found.append((path, read(path)))
        except NotFound:
            pass
    if len(found) != 1:
        raise Blocked("provide exactly one of .concurro/config.json and .concurro/config.yaml")
    path, text = found[0]
    return decode(text, path)


def read_local(path):
    """Read a local configuration for migration or validation."""
    return decode(Path(path).read_text(), path)
