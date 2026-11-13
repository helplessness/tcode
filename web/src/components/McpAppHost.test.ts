import { describe, expect, it } from 'vitest';

import { HOST_CSP, buildHostCsp, isolatedDocument, redactToolArguments } from './McpAppHost';

describe('MCP App iframe isolation', () => {
  it('injects the host CSP before untrusted document content', () => {
    const source = '<html><head><base href="https://evil.example/"><meta http-equiv="Content-Security-Policy" content="default-src *"></head><body><form action="https://evil.example/"></form></body></html>';
    const document = isolatedDocument(source);
    expect(document).toContain(HOST_CSP);
    expect(document.indexOf(HOST_CSP)).toBeLessThan(document.indexOf('<base'));
    expect(HOST_CSP).toContain("connect-src 'none'");
    expect(HOST_CSP).toContain("form-action 'none'");
    expect(HOST_CSP).toContain("object-src 'none'");
  });

  it('uses only sanitized host-granted resource metadata', () => {
    const policy = buildHostCsp({
      csp: {
        connectDomains: [
          'https://api.example.com',
          'wss://events.example.com',
          'javascript:alert(1)',
        ],
        resourceDomains: ['https://cdn.example.com', 'http://unsafe.example.com'],
        frameDomains: ['https://viewer.example.com'],
      },
    });
    expect(policy).toContain('connect-src https://api.example.com wss://events.example.com');
    expect(policy).toContain("script-src 'unsafe-inline' https://cdn.example.com");
    expect(policy).toContain('frame-src https://viewer.example.com');
    expect(policy).not.toContain('javascript:');
    expect(policy).not.toContain('http://unsafe.example.com');
    expect(policy).toContain("object-src 'none'");
    expect(policy).toContain("form-action 'none'");
  });

  it('redacts capability tokens from the confirmation preview', () => {
    expect(redactToolArguments({
      job_id: 'job-1',
      viewer_token: 'view-secret',
      nested: { apiKey: 'secret', rank: 1 },
    })).toEqual({
      job_id: 'job-1',
      viewer_token: '[已隐藏]',
      nested: { apiKey: '[已隐藏]', rank: 1 },
    });
  });
});
