"""`python -m retrail`, identical to the `retrail` console script.

RetrailGroup's docstring promises errors render the same "however the CLI is
entered - console script, `python -m`, or a test harness". This module is what
makes the middle one true; without it `python -m retrail` fails before any of
that error handling can apply.
"""

from .cli import main

if __name__ == "__main__":
    main()
