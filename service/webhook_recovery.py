"""Authenticated private App delivery listing and redelivery for Rust ingress."""

import hmac
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from .identity import IdentityError

PAGE_SIZE = 100


class DeliveryBridge:
    """Serve the App delivery listing and redelivery calls to private ingress."""

    def __init__(self, identity, bearer):
        """Bind App authentication and the ingress-only bearer."""
        self.identity, self.bearer = identity, bearer

    def handle(self, method, path, authorization):
        """Return an authenticated delivery API response."""
        if not hmac.compare_digest(authorization, "Bearer " + self.bearer):
            return 401, {"error": "unauthorized"}
        parsed = urlsplit(path)
        if method == "GET" and parsed.path == "/v1/deliveries":
            cursors = parse_qs(parsed.query).get("cursor", [])
            if len(cursors) > 1:
                return 400, {"error": "invalid cursor"}
            query = {"per_page": PAGE_SIZE}
            if cursors:
                query["cursor"] = cursors[0]
            values, headers = self.identity.api(
                "GET", "app/hook/deliveries?" + urlencode(query), self.identity.app_jwt(), with_headers=True
            )
            if not isinstance(values, list):
                raise IdentityError("invalid webhook delivery listing")
            following = next_page(headers)
            next_cursor = parse_qs(urlsplit(following).query)["cursor"][0] if following else None
            return 200, {"deliveries": values, "next_cursor": next_cursor}
        match = re.fullmatch(r"/v1/deliveries/([0-9]+)/redeliver", parsed.path)
        if method == "POST" and match and not parsed.query:
            self.identity.api("POST", f"app/hook/deliveries/{match[1]}/attempts", self.identity.app_jwt())
            return 202, {}
        return 404, {"error": "not found"}


def next_page(headers):
    """Use GitHub's next cursor without forwarding credentials to a supplied URL."""
    for link in headers.get("link", "").split(","):
        target, *parameters = link.strip().split(";")
        if not any(parameter.strip() == 'rel="next"' for parameter in parameters):
            continue
        parsed = urlsplit(target.removeprefix("<").removesuffix(">"))
        cursor = parse_qs(parsed.query).get("cursor")
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.github.com"
            or parsed.path != "/app/hook/deliveries"
            or not cursor
        ):
            raise IdentityError("invalid webhook delivery pagination")
        return "app/hook/deliveries?" + urlencode({"per_page": PAGE_SIZE, "cursor": cursor[0]})
    return None
