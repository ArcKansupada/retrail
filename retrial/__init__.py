"""retrial - git for agent trajectories.

Branch, diff, and bisect LLM agent runs, backed by real re-execution instead of
static logs.
"""

from .bisect import bisect
from .diff import diff
from .errors import (
    AmbiguousSha,
    ExportFormatError,
    IntegrationError,
    NotFound,
    ReplayIntegrityError,
    RetrialError,
    SchemaVersionError,
)
from .explore import ablate, sweep
from .fork import fork
from .patch import apply_patch
from .pricing import cost_of, trajectory_cost
from .record import record
from .regress import rerun
from .storage import Store
from .trajectory import trajectory
from .transfer import export, import_

# The shapes every function above returns. Re-exported so annotating your own
# code never means importing from a private-looking submodule. See
# retrial/types.py, and CHANGELOG.md for what "stable" means before 1.0.
from .types import (
    JSON,
    AblateProbe,
    AblateResult,
    Agent,
    BisectProbe,
    BisectResult,
    Check,
    CheckFunction,
    DiffBlock,
    DiffResult,
    Divergence,
    Edit,
    EditProvenance,
    ExportHeader,
    ExportRow,
    ExportSession,
    ExportStep,
    Origin,
    Patch,
    PatchOp,
    RerunOutcome,
    RerunResult,
    RerunVerdict,
    Session,
    SessionStatus,
    Step,
    StepType,
    SweepBoundary,
    SweepProbe,
    SweepResult,
    TrajectoryEntry,
)

__version__ = "0.1.1"

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
    "export",
    "import_",
    "apply_patch",
    "Store",
    "RetrialError",
    "NotFound",
    "AmbiguousSha",
    "ReplayIntegrityError",
    "IntegrationError",
    "SchemaVersionError",
    "ExportFormatError",
    # types
    "JSON",
    "AblateProbe",
    "AblateResult",
    "Agent",
    "BisectProbe",
    "BisectResult",
    "Check",
    "CheckFunction",
    "DiffBlock",
    "DiffResult",
    "Divergence",
    "Edit",
    "EditProvenance",
    "ExportHeader",
    "ExportRow",
    "ExportSession",
    "ExportStep",
    "Origin",
    "Patch",
    "PatchOp",
    "RerunOutcome",
    "RerunResult",
    "RerunVerdict",
    "Session",
    "SessionStatus",
    "Step",
    "StepType",
    "SweepBoundary",
    "SweepProbe",
    "SweepResult",
    "TrajectoryEntry",
    "__version__",
]
