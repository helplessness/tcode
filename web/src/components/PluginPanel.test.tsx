import { renderToString } from 'solid-js/web';
import { describe, expect, it } from 'vitest';
import PluginPanel from './PluginPanel';

describe('plugin management default view', () => {
  it('prioritizes the plugin list and opens installation only on demand', () => {
    const html = renderToString(() => <PluginPanel embedded width={420} workdir="/workspace/demo" />);
    expect(html).toContain('安装插件');
    expect(html).toContain('高级信息');
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain('aria-label="插件列表"');
    expect(html).toContain('min-h-0 flex-1 overflow-y-auto');
    expect(html).not.toContain('type="file"');
    expect(html).not.toContain('插件高级信息');
    expect(html).not.toContain('>钩子</button>');
    expect(html).not.toContain('>工具</button>');
    expect(html).not.toContain('>命令</button>');
    expect(html).not.toContain('>诊断</button>');
    expect(html).not.toContain('max-h-[40vh]');
  });
});
