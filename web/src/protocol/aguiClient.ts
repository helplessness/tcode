import { HttpAgent, type HttpAgentFetchFn } from '@ag-ui/client';

export type TCodeAGUIAgentOptions = {
  baseUrl?: string;
  agentId?: string;
  threadId?: string;
  apiKey?: string;
  fetch?: HttpAgentFetchFn;
  workspaceId?: string;
  runtimeId?: string;
  modelId?: string;
};

/** 创建可直接交给 AG-UI/CopilotKit 消费的 TCode HttpAgent。 */
export function createTCodeAGUIAgent(options: TCodeAGUIAgentOptions = {}): HttpAgent {
  const baseUrl = String(options.baseUrl || '').replace(/\/$/, '');
  const agentId = options.agentId || 'default';
  const agent = new HttpAgent({
    url: `${baseUrl}/api/ag-ui/v1/agents/${encodeURIComponent(agentId)}/run`,
    agentId,
    threadId: options.threadId,
    headers: options.apiKey ? { Authorization: `Bearer ${options.apiKey}` } : {},
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
  const run = agent.run.bind(agent);
  agent.run = (input) => {
    const forwardedProps = input.forwardedProps && typeof input.forwardedProps === 'object'
      ? input.forwardedProps as Record<string, any>
      : {};
    const existing = forwardedProps.tcode && typeof forwardedProps.tcode === 'object'
      ? forwardedProps.tcode as Record<string, any>
      : {};
    return run({
      ...input,
      forwardedProps: {
        ...forwardedProps,
        tcode: {
          workspaceId: options.workspaceId || existing.workspaceId || 'workspace-1',
          runtimeId: options.runtimeId || existing.runtimeId || 'tcode-native',
          modelId: options.modelId || existing.modelId || '',
          ...existing,
        },
      },
    });
  };
  return agent;
}
