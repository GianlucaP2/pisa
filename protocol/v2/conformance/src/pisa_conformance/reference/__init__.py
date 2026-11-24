"""Reference emitter: how a backend turn becomes contract 2 frames (AG-UI 1.0 + A2UI 1.0 + pisa.*).

It produces the recorded fixture streams and the approval vectors, and maps every v1 message to contract 2.
A sketch for backends to port, not a server. Synthetic data only.
"""
from .agui import Clock, Ids, LiveSession, RunWriter
from .a2ui import CATALOG_ID, SurfaceBuilder

__all__ = ["Clock", "Ids", "LiveSession", "RunWriter", "CATALOG_ID", "SurfaceBuilder"]
