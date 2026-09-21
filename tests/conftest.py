"""Test configuration.

Puts the project root on sys.path so `import autofix` and `import bot` work
without installing the project as a package.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
