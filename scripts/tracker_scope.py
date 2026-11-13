"""Compatibility import for source and frozen Symphony sidecars."""
import sys
from tcode_agent.runtime import tracker_scope

sys.modules[__name__] = tracker_scope
