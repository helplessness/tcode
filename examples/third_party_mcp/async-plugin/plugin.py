from tcode_agent.plugin_sdk import PluginMetadata


class Plugin:
    info = PluginMetadata(name='community-predict', version='1.0.0', description='Remote compute example')

    async def start(self, ctx, config):
        # No scheduler, credentials, host imports, or direct MCP calls here.
        pass
