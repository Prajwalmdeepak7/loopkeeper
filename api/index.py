import os
import sys
from pathlib import Path

# Add project root to sys.path so server can be imported
sys.path.insert(0, str(Path(__file__).parent.parent))

from server import app
