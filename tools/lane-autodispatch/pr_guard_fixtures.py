"""Offline gh fixture for tests unrelated to the open-PR guard."""
import json
from functools import wraps


def no_open_prs(gh):
    """Answer GraphQL with an empty page and delegate all other gh calls."""
    @wraps(gh)
    def call(*args, **kwargs):
        argv = next((arg for arg in args if isinstance(arg, (list, tuple))), ())
        if argv[:2] == ["api", "graphql"] or argv[:3] == ["gh", "api", "graphql"]:
            return 0, json.dumps({"data": {"repository": {"pullRequests": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}), ""
        return gh(*args, **kwargs)
    return call
