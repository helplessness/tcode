import { renderToString } from 'solid-js/web';
import { describe, expect, it, vi } from 'vitest';
import type { ModelSection } from '../api/intelligentPage';
import PageModel, { waitForGifResource } from './PageModel';

const section: ModelSection = {
  type: 'model', id: 'demo', title: '形状试算', hidden: false, description: '',
  parameters: [{ id: 'amount', label: '负载', unit: '', min: 0, max: 10, step: 1, value: 5 }],
  outputs: [{ label: '负载值', unit: '', operation: 'product', args: ['amount', 1], min: 0, max: 10, description: '',
    threshold: { value: 5, below: { label: '较低', description: '' }, at_or_above: { label: '较高', description: '' } } }],
  graphics: [{ label: '负载块', shape: 'rect', x: { output_index: 0, min: 10, max: 110 }, y: 20,
    width: 100, height: 40, color: { output_index: 0, below: 'positive', at_or_above: 'danger' } },
    { label: '标记圆', shape: 'ellipse', x: 300, y: 100, width: 30, height: 30, color: 'accent' }],
};

describe('linked diagram rendering', () => {
  it('renders explicit playback controls and a labeled progress slider without autoplay', () => {
    const animated = { ...section, animation: { duration_seconds: 6, tracks: [{ parameter_id: 'amount', start: 5, end: 10 }] } };
    const html = renderToString(() => <PageModel section={animated} onValues={() => {}} />);
    expect(html).toContain('>播放</button>');
    expect(html).toContain('重置动画');
    expect(html).toContain('动画进度');
    expect(html).toContain('导出 GIF');
    expect(html).toContain('最多181帧、24MB');
    expect(html).toContain('已暂停');
    expect(html).toContain('0.0%');
    expect(html).not.toContain('正在播放');
  });
  it('renders computed geometry with theme roles and readable non-color explanations', () => {
    const html = renderToString(() => <PageModel section={section} onValues={() => {}} />);
    expect(html).toContain('viewBox="0 0 400 240"');
    expect(html).toContain('x="60" y="20" width="100" height="40"');
    expect(html).toContain('#b95164');
    expect(html).toContain('cx="315" cy="115" rx="15" ry="15"');
    expect(html).toContain('红色 · 较高');
    expect(html).toContain('负载块');
  });

  it('does not render unavailable geometry or describe stale dimensions as current', () => {
    const invalid = { ...section, outputs: [{ ...section.outputs[0], operation: 'ratio' as const, args: [1, 0] }] };
    const html = renderToString(() => <PageModel section={invalid} onValues={() => {}} />);
    expect(html).not.toContain('x="60" y="20" width="100" height="40"');
    expect(html).toContain('关联结果暂不可计算，图形暂不显示');
    expect(html).toContain('标记圆');
    expect(html).toContain('cx="315"');
  });
});

describe('GIF resource loading', () => {
  it('cancels a stalled import before any encoder can run', async () => {
    const controller = new AbortController();
    let finish!: (value: string) => void;
    const resource = new Promise<string>(resolve => { finish = resolve; });
    const waiting = waitForGifResource(resource, controller.signal);
    controller.abort();
    await expect(waiting).rejects.toMatchObject({ name: 'AbortError' });
    finish('late encoder');
    await expect(waiting).rejects.toMatchObject({ name: 'AbortError' });
  });
  it('bounds stalled loading and still accepts an available encoder', async () => {
    vi.useFakeTimers();
    try {
      const waiting = waitForGifResource(new Promise(() => {}), new AbortController().signal);
      const rejected = expect(waiting).rejects.toThrow('资源加载超时');
      await vi.advanceTimersByTimeAsync(15000);
      await rejected;
      await expect(waitForGifResource(Promise.resolve('encoder'), new AbortController().signal)).resolves.toBe('encoder');
    } finally { vi.useRealTimers(); }
  });
});
