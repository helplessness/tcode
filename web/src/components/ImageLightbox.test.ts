import { describe, expect, it } from 'vitest';
import { clampImageScale, lightboxDownloadName } from './ImageLightbox';

describe('图片灯箱辅助逻辑', () => {
  it('把缩放限制在安全范围内', () => {
    expect(clampImageScale(0.01)).toBe(0.25);
    expect(clampImageScale(2)).toBe(2);
    expect(clampImageScale(20)).toBe(8);
    expect(clampImageScale(Number.NaN)).toBe(1);
  });

  it('优先使用显式文件名并安全回退 URL 文件名', () => {
    expect(lightboxDownloadName({ src: '/api/media/asset-1', downloadName: 'figures/a107n.png' })).toBe('a107n.png');
    expect(lightboxDownloadName({ src: '/api/evidence/1/images/0?workspace_id=ws', alt: '证据图' })).toBe('0');
    expect(lightboxDownloadName({ src: '' })).toBe('image');
  });
});

