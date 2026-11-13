"""Task-local selection of workspace-owned plugin registries.

Registry APIs stay stable, but their mutable state no longer crosses workspace
boundaries. ContextVar propagation also covers child tasks and asyncio.to_thread.
Built-in tool definitions remain process-wide; only declared fields are scoped.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
from functools import wraps
import inspect
import threading
from pathlib import Path

plugin_workspace = ContextVar("plugin_workspace", default="")


def bind_plugin_workspace(workdir):
    return plugin_workspace.set(str(Path(workdir).resolve()) if workdir else "")


@contextmanager
def registry_workspace(workdir):
    token = bind_plugin_workspace(workdir)
    try:
        yield
    finally:
        plugin_workspace.reset(token)


class WorkspaceRegistryMeta(type):
    def __new__(meta, name, bases, namespace):
        namespace["_workspace_defaults"] = {
            field: namespace[field] for field in namespace.get("_workspace_fields", ())
        }
        namespace["_workspace_states"] = {}
        namespace["_workspace_state_lock"] = threading.RLock()
        return super().__new__(meta, name, bases, namespace)

    def __getattribute__(cls, name):
        defaults = type.__getattribute__(cls, "_workspace_defaults")
        if name in defaults:
            states = type.__getattribute__(cls, "_workspace_states")
            with type.__getattribute__(cls, "_workspace_state_lock"):
                state = states.setdefault(plugin_workspace.get(), {})
                if name not in state:
                    state[name] = copy(defaults[name])
                return state[name]
        return type.__getattribute__(cls, name)

    def __setattr__(cls, name, value):
        defaults = type.__getattribute__(cls, "_workspace_defaults")
        if name in defaults:
            states = type.__getattribute__(cls, "_workspace_states")
            with type.__getattribute__(cls, "_workspace_state_lock"):
                states.setdefault(plugin_workspace.get(), {})[name] = value
        else:
            type.__setattr__(cls, name, value)


def in_manager_workspace(method):
    """Select the manager's workspace for direct API/CLI lifecycle calls too."""
    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def async_call(self, *args, **kwargs):
            bind_plugin_workspace(self.workdir)
            return await method(self, *args, **kwargs)
        return async_call
    @wraps(method)
    def call(self, *args, **kwargs):
        bind_plugin_workspace(self.workdir)
        return method(self, *args, **kwargs)
    return call
