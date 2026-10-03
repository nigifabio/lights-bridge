import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from lights_bridge.drivers import Govee
from lights_bridge.light import Light

# no real waiting in tests
Light.retry_delay = 0
Govee.query_wait = 0
