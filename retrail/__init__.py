"""retrail - git for agent trajectories.

Branch, diff, and bisect LLM agent runs, backed by real re-execution instead of
static logs.
"""

from .bisect import bisect
from .diff import diff
from .errors import (
    AmbiguousSha,
    IntegrationError,
    NotFound,
    ReplayIntegrityError,
    RetrailError,
)
from .explore import ablate, sweep
from .fork import fork
from .patch import apply_patch
from .pricing import cost_of, trajectory_cost
from .record import record
from .regress import rerun
from .storage import Store
from .trajectory import trajectory

__version__ = "0.1.0"

__all__ = [
    "record",
    "fork",
    "diff",
    "bisect",
    "ablate",
    "sweep",
    "rerun",
    "cost_of",
    "trajectory_cost",
    "trajectory",
    "apply_patch",
    "Store",
    "RetrailError",
    "NotFound",
    "AmbiguousSha",
    "ReplayIntegrityError",
    "IntegrationError",
    "__version__",
]
