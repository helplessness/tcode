"""Plugin runtime for Tcode."""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "LoadedPlugin",
    "HookRegistry",
    "HookCategory",
    "HookDispatchMode",
    "HookEvent",
    "HookEventSpec",
    "HookRegistrationDisposer",
    "HookRegistrationToken",
    "PluginCommand",
    "PluginCommandRegistry",
    "PluginBase",
    "PluginContext",
    "LegacyPluginContext",
    "PluginContextV0",
    "PUBLIC_PLUGIN_SERVICE_KEYS",
    "PluginContextClosedError",
    "PluginContextError",
    "PluginInstance",
    "PluginLogger",
    "MemoryService",
    "PluginScope",
    "PluginServiceUnavailableError",
    "ServiceView",
    "ToolDescriptor",
    "UndeclaredServiceAccessError",
    "PluginHealth",
    "PluginHealthStatus",
    "PluginInstanceState",
    "PluginInstanceStatus",
    "PluginInfo",
    "PLUGIN_API_VERSION",
    "PluginManifest",
    "PluginManifestApiVersionError",
    "PluginManifestConfigError",
    "PluginManifestDependencyError",
    "PluginManifestEntryError",
    "PluginManifestError",
    "PluginManifestNotFoundError",
    "PluginManifestPermissionError",
    "PluginManifestSchemaError",
    "PluginManager",
    "PluginPermission",
    "PluginPermissionError",
    "PluginPermissionScope",
    "PluginRegistry",
    "PluginRuntime",
    "PluginType",
    "create_plugin_manager",
    "load_manifest",
    "plan_manifest_order",
]


_PUBLIC_EXPORTS: dict[str, tuple[str, str]] = {
    "LegacyPluginContext": (".base", "LegacyPluginContext"),
    "PluginBase": (".base", "PluginBase"),
    "PluginContext": (".base", "PluginContext"),
    "PluginInfo": (".base", "PluginInfo"),
    "PluginType": (".base", "PluginType"),
    "PUBLIC_PLUGIN_SERVICE_KEYS": (".context", "PUBLIC_PLUGIN_SERVICE_KEYS"),
    "PluginContextV0": (".context", "PluginContext"),
    "PluginContextClosedError": (".context", "PluginContextClosedError"),
    "PluginContextError": (".context", "PluginContextError"),
    "PluginInstance": (".context", "PluginInstance"),
    "PluginLogger": (".context", "PluginLogger"),
    "MemoryService": (".context", "MemoryService"),
    "PluginScope": (".context", "PluginScope"),
    "PluginServiceUnavailableError": (".context", "PluginServiceUnavailableError"),
    "ServiceView": (".context", "ServiceView"),
    "ToolDescriptor": (".context", "ToolDescriptor"),
    "UndeclaredServiceAccessError": (".context", "UndeclaredServiceAccessError"),
    "PluginCommand": (".command_registry", "PluginCommand"),
    "PluginCommandRegistry": (".command_registry", "PluginCommandRegistry"),
    "HookCategory": (".hooks", "HookCategory"),
    "HookDispatchMode": (".hooks", "HookDispatchMode"),
    "HookEvent": (".hooks", "HookEvent"),
    "HookEventSpec": (".hooks", "HookEventSpec"),
    "HookRegistrationDisposer": (".hooks", "HookRegistrationDisposer"),
    "HookRegistrationToken": (".hooks", "HookRegistrationToken"),
    "HookRegistry": (".hooks", "HookRegistry"),
    "PluginManager": (".manager", "PluginManager"),
    "create_plugin_manager": (".manager", "create_plugin_manager"),
    "PLUGIN_API_VERSION": (".manifest", "PLUGIN_API_VERSION"),
    "PluginManifest": (".manifest", "PluginManifest"),
    "PluginManifestApiVersionError": (".manifest", "PluginManifestApiVersionError"),
    "PluginManifestConfigError": (".manifest", "PluginManifestConfigError"),
    "PluginManifestDependencyError": (".manifest", "PluginManifestDependencyError"),
    "PluginManifestEntryError": (".manifest", "PluginManifestEntryError"),
    "PluginManifestError": (".manifest", "PluginManifestError"),
    "PluginManifestNotFoundError": (".manifest", "PluginManifestNotFoundError"),
    "PluginManifestPermissionError": (".manifest", "PluginManifestPermissionError"),
    "PluginManifestSchemaError": (".manifest", "PluginManifestSchemaError"),
    "load_manifest": (".manifest", "load_manifest"),
    "plan_manifest_order": (".manifest", "plan_manifest_order"),
    "PluginPermission": (".permissions", "PluginPermission"),
    "PluginPermissionError": (".permissions", "PluginPermissionError"),
    "PluginPermissionScope": (".permissions", "PluginPermissionScope"),
    "PluginRuntime": (".permissions", "PluginRuntime"),
    "LoadedPlugin": (".registry", "LoadedPlugin"),
    "PluginRegistry": (".registry", "PluginRegistry"),
    "PluginHealth": (".lifecycle", "PluginHealth"),
    "PluginHealthStatus": (".lifecycle", "PluginHealthStatus"),
    "PluginInstanceState": (".lifecycle", "PluginInstanceState"),
    "PluginInstanceStatus": (".lifecycle", "PluginInstanceStatus"),
}


def __getattr__(name: str):
    """Resolve one documented plugin export without eager runtime bootstrap.

    Args:
        name: Attribute requested through the ``tcode_agent.plugins``
            package.

    Returns:
        The public object imported from its owning plugin module.  The resolved
        value is cached in package globals so later access keeps normal module
        identity and cost.

    Raises:
        AttributeError: If the requested name is not a documented export.
    """

    target = _PUBLIC_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
