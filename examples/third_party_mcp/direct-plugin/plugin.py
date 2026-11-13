from tcode_agent.plugin_sdk import PluginMetadata


class Plugin:
    info = PluginMetadata(name='community-lookup', version='1.0.0', description='Direct MCP example')

    async def start(self, ctx, config):
        pass
