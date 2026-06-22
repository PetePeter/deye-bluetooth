"""Make the repo root importable so tests can `import custom_components.deye_ble`."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
