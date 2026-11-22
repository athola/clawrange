"""The app most tests share, built around a throwaway brain.

`app.py` builds nothing on import (uvicorn serves `create_app` as a
factory), so tests that want a ready app import it from here. conftest.py
empties this brain after every test. A test that needs an app of its own
calls `create_app(brain_db=...)` directly, as test_app_factory.py does.
"""

import os
import tempfile

from app import create_app, open_brain
from brain_db import BrainDB

_tmpdir = tempfile.mkdtemp(prefix="clawrange-test-brain-")

brain_db: BrainDB = open_brain(os.path.join(_tmpdir, "test_brain.db"))
app = create_app(brain_db=brain_db)
