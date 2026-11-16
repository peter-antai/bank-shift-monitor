"""
Test setup.

The monitor reads its settings from the environment at import
time, so they are set here before any test imports it. Nothing
in these tests opens a browser or touches the network.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ["ALLOCATE_HOME"] = tempfile.mkdtemp(prefix="monitor-tests-")
os.environ["ALLOCATE_TENANT"] = "TESTTENANT"
os.environ["TARGET_UNIT"] = "Test Unit"
os.environ["TARGET_GRADE"] = "Test Grade"
os.environ["SITE_NAME"] = "Test Site"
os.environ["STAFF_BANK_NAME"] = "Test Staff Bank"
os.environ["WEEKLY_LIMIT"] = "4"

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "monitor"),
)
