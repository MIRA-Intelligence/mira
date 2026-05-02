"""Runtime helpers (Python interpreter / venv lifecycle, etc.).

Modules in this package are imported lazily so that environments without
optional toolchains (e.g. ``uv`` not installed) still load the engine.
"""
