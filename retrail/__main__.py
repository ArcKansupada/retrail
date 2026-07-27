"""`python -m retrail`, identical to the `retrail` console script.

RetrailGroup promises errors render the same however the CLI is entered. This
module is what makes `python -m` one of those ways; without it the command
fails before any of that error handling can apply.
"""

from .cli import main

if __name__ == "__main__":
    main()
