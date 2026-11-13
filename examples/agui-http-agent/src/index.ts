import { HttpAgent } from '@ag-ui/client';

const baseUrl = String(process.env.TCODE_URL || 'http://127.0.0.1:5555').replace(/\/$/, '');
const apiKey = String(process.env.TCODE_API_KEY || '');
const prompt = process.argv.slice(2).join(' ').trim() || '请用一句话介绍你自己';
const threadId = crypto.randomUUID();

// This example intentionally uses the official AG-UI client directly. It does
// not import the TCode SDK and sends no forwardedProps.tcode extension. The
// server therefore selects its default workspace, runtime and model.
const agent = new HttpAgent({
  url: `${baseUrl}/api/ag-ui/v1/agents/default/run`,
  agentId: 'default',
  threadId,
  headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
});

agent.addMessage({
  id: crypto.randomUUID(),
  role: 'user',
  content: prompt,
});

const result = await agent.runAgent();
const answer = result.newMessages
  .filter((message) => message.role === 'assistant')
  .map((message) => typeof message.content === 'string' ? message.content : JSON.stringify(message.content))
  .join('\n')
  .trim();

if (!answer) {
  throw new Error('TCode returned no assistant message');
}

process.stdout.write(`${answer}\n`);
