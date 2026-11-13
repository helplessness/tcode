import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { describe, expect, it } from 'vitest';
import { encodeModelGif, gifFrameSvg, gifPlan, GIF_WIDTH, GIF_HEIGHT } from './modelGif';
import { diagramPalette } from './modelDiagram';
import { vectorModel, vectorTree } from './__fixtures__/modelDiagram';

const pillowAvailable = spawnSync('python3', ['-c', 'import PIL'], { encoding: 'utf8' }).status === 0;

describe('deterministic GIF export', () => {
  it('samples both endpoints and preserves declared duration while bounding long animations', () => {
    for (const duration of [1, 6, 60]) {
      const plan = gifPlan(duration);
      expect(plan.length).toBeLessThanOrEqual(181);
      expect(plan[0].progress).toBe(0); expect(plan[plan.length - 1].progress).toBe(1);
      expect(plan.reduce((sum, frame) => sum + frame.delay, 0)).toBe(duration * 1000 + 10);
    }
    expect(() => gifPlan(0)).toThrow(); expect(() => gifPlan(Infinity)).toThrow();
  });

  it('renders the exact same art/threshold state and current theme into the export frame', () => {
    const frame = gifFrameSvg(vectorModel, .5, { load: 0 }, [vectorTree], diagramPalette('manual'));
    expect(frame).toContain('rotate(90 100 80)'); expect(frame).toContain('Q 240 65');
    expect(frame).toContain('较高 请检查'); expect(frame).toContain('负载值：5');
    expect(frame).toContain('#c24545'); expect(frame).toContain('50.0%');
    expect(frame).not.toContain('script');
  });

  it.skipIf(!pillowAvailable)('writes a real multi-frame GIF independently decoded with Pillow, including endpoint pixels and timing', async () => {
    const controller = new AbortController(), seen: string[] = [], progress: number[] = [];
    const blob = await encodeModelGif(vectorModel, { load: 0 }, [vectorTree], diagramPalette('clay'), controller.signal, fraction => progress.push(fraction), async svg => {
      seen.push(svg);
      const rgba = new Uint8ClampedArray(GIF_WIDTH * GIF_HEIGHT * 4);
      const color = svg.includes('100.0%') ? [0, 0, 255, 255] : [255, 0, 0, 255];
      for (let i = 0; i < rgba.length; i += 4) rgba.set(color, i);
      return rgba;
    });
    expect(seen).toHaveLength(11); expect(progress[progress.length - 1]).toBe(1);
    const directory = mkdtempSync(join(tmpdir(), 'tcode-gif-'));
    try {
      const path = join(directory, 'animation.gif'); writeFileSync(path, Buffer.from(await blob.arrayBuffer()));
      const decoded = spawnSync('python3', ['-c', `import json,sys
from PIL import Image,ImageSequence
im=Image.open(sys.argv[1]); frames=[(f.convert('RGB').getpixel((0,0)),f.info.get('duration',0)) for f in ImageSequence.Iterator(im)]
print(json.dumps(dict(size=im.size,count=len(frames),first=frames[0][0],last=frames[-1][0],duration=sum(f[1] for f in frames),loop=im.info.get('loop'))))`, path], { encoding: 'utf8' });
      expect(decoded.status, decoded.stderr).toBe(0);
      expect(JSON.parse(decoded.stdout)).toEqual({ size: [400, 600], count: 11, first: [255, 0, 0], last: [0, 0, 255], duration: 1010, loop: null });
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });

  it('cancels without returning a partial download and rejects unexpected raster sizes', async () => {
    const controller = new AbortController(); controller.abort();
    await expect(encodeModelGif(vectorModel, {}, [vectorTree], diagramPalette('clay'), controller.signal, () => {})).rejects.toThrow();
    await expect(encodeModelGif(vectorModel, {}, [vectorTree], diagramPalette('clay'), new AbortController().signal, () => {}, async () => new Uint8ClampedArray(4))).rejects.toThrow('尺寸');
    const mid = new AbortController(); let renders = 0;
    await expect(encodeModelGif(vectorModel, {}, [vectorTree], diagramPalette('clay'), mid.signal, () => mid.abort(), async () => { renders++; return new Uint8ClampedArray(GIF_WIDTH * GIF_HEIGHT * 4); })).rejects.toThrow();
    expect(renders).toBe(1);
  });
});
