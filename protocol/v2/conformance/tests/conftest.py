import sys
from pathlib import Path

HERE = Path(__file__).resolve()
# Test the source tree, installed or not.
sys.path.insert(0, str(HERE.parents[1] / "src"))

V2 = HERE.parents[2]            # protocol/v2
PROTOCOL = V2.parent            # protocol/ (v1 lives here: schema/, fixtures/)
