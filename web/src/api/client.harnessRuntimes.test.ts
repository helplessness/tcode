import { afterEach, describe, expect, it, vi } from 'vitest';

import { getChatRuntimes } from './client';

describe('Harness runtime discovery API', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('keeps source, version, capability and conformance metadata', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        default_runtime_id: 'tcode-native',
        preferred_model_id: 'deepseek/deepseek-v4-flash',
        runtimes: [{
          runtime_id: 'example-harness',
          display_name: 'Example Harness',
          available: true,
          enabled: true,
          release: 'experimental',
          models: ['example/model'],
          capabilities: {
            text: true,
            tools: false,
            verifiedCapabilities: ['cancel', 'text'],
            conformanceState: 'passed',
          },
          source_type: 'entrypoint',
          source_name: 'example-dist:example-harness',
          distribution_name: 'example-dist',
          distribution_version: '1.2.3',
          definition_digest: 'sha256:abc',
          trust_state: 'trusted',
          compatibility_state: 'compatible',
          conformance_state: 'passed',
          capabilities_verified: ['cancel', 'text'],
        }],
      }),
    }));

    const result = await getChatRuntimes('session-1');

    expect(result.runtimes[0]).toMatchObject({
      runtime_id: 'example-harness',
      source_type: 'entrypoint',
      distribution_name: 'example-dist',
      distribution_version: '1.2.3',
      definition_digest: 'sha256:abc',
      trust_state: 'trusted',
      compatibility_state: 'compatible',
      conformance_state: 'passed',
      capabilities_verified: ['cancel', 'text'],
      capabilities: {
        text: true,
        verifiedCapabilities: ['cancel', 'text'],
        conformanceState: 'passed',
      },
    });
  });
});
