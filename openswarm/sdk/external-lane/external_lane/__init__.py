"""openswarm external-lane SDK: plug a GitHub-only agent (no SSH) into an openswarm/phanes lane queue."""
from . import contract
from .agent import ExternalLane, LaneError
from .github import GitHub, GitHubError

__all__ = ["contract", "ExternalLane", "LaneError", "GitHub", "GitHubError"]
__version__ = "0.1.0"
