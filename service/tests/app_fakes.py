"""In-memory GitHub App transport shared by identity and target tests."""

from datetime import datetime, timezone
import json
import threading
import time
from fixtures import Reply


OTHER = "owner/second"


class App:
    """An in-memory GitHub App API with switchable misbehaviour."""

    def __init__(self, clock):
        self.clock = clock
        self.selection, self.owner = "selected", None
        self.extra_permission = self.extra_repository = False
        self.minted, self.revoked, self.requests = [], [], []
        self.guard = threading.Lock()
        self.delay = 0

    def connection(self, host, timeout):
        return Connection(self)

    def handle(self, method, target, headers, body):
        token = headers["Authorization"].removeprefix("Bearer ")
        data = json.loads(body) if body else None
        self.requests.append((method, target, data))
        if (method, target) == ("GET", "/app"):
            return 200, {"slug": "concurro-controller"}
        if method == "GET" and target.endswith("/installation"):
            repository = target.removeprefix("/repos/").removesuffix("/installation")
            return 200, {"id": 5, "repository_selection": self.selection,
                         "account": {"login": self.owner or repository.split("/")[0]}}
        if (method, target) == ("POST", "/app/installations/5/access_tokens"):
            time.sleep(self.delay)
            with self.guard:
                minted = f"token-{len(self.minted)}"
                self.minted.append((minted, data["repositories"], data["permissions"]))
            permissions = {**data["permissions"], "metadata": "read"}
            if self.extra_permission:
                permissions["actions"] = "write"
            expires = datetime.fromtimestamp(self.clock() + 3600, timezone.utc).isoformat().replace("+00:00", "Z")
            return 201, {"token": minted, "permissions": permissions, "expires_at": expires}
        if (method, target) == ("GET", "/installation/repositories?per_page=100"):
            [(_, names, _)] = [m for m in self.minted if m[0] == token]
            listed = [{"full_name": "owner/" + names[0]}] + ([{"full_name": OTHER}] if self.extra_repository else [])
            return 200, {"total_count": len(listed), "repositories": listed}
        if (method, target) == ("DELETE", "/installation/token"):
            self.revoked.append(token)
            return 204, None
        return 404, {"message": "Not Found"}


class Connection:
    """The parts of `http.client.HTTPSConnection` the transport uses."""

    def __init__(self, app):
        self.app = app

    def connect(self):
        pass

    def request(self, method, target, body=None, headers=None):
        self.reply = self.app.handle(method, target, headers, body)

    def getresponse(self):
        return Reply(*self.reply)

    def close(self):
        pass
