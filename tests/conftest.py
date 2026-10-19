import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))           # tests import the fake Resolve as `fake`
sys.path.insert(0, str(Path(__file__).parents[1]))       # and the extension's package from the checkout
