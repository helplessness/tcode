import { describe, expect, it } from 'vitest';
import { animationValues, createPlayback } from './pageAnimation';
import { evaluateGraphics, evaluateModel } from './pageModel';
import type { ModelSection } from '../api/intelligentPage';

function clock() {
  let key = 0;
  const tasks = new Map<number, FrameRequestCallback>();
  const frames: Array<{ progress: number; playing: boolean }> = [];
  const player = createPlayback(1, (progress, playing) => frames.push({ progress, playing }), callback => { tasks.set(++key, callback); return key; }, id => { tasks.delete(id); });
  const tick = (time: number) => { const pending = [...tasks.values()]; tasks.clear(); pending.forEach(callback => callback(time)); };
  return { player, frames, tasks, tick };
}

describe('controlled animation clock', () => {
  it('starts only on demand, has one clock, pauses, resumes, stops at end and replays', () => {
    const c = clock();
    expect(c.tasks.size).toBe(0);
    c.player.play(); c.player.play(); expect(c.tasks.size).toBe(1);
    c.tick(0); c.tick(100); c.tick(200);
    expect(c.frames[c.frames.length-1]).toEqual({ progress: .2, playing: true });
    const stale = [...c.tasks.values()][0];
    c.player.pause(); expect(c.tasks.size).toBe(0); stale(300);
    expect(c.frames[c.frames.length-1]).toEqual({ progress: .2, playing: false });
    c.player.play(); c.tick(10000); c.tick(10100);
    expect(c.frames[c.frames.length-1]?.progress).toBeCloseTo(.3);
    for (let t = 10200; t <= 11000; t += 100) c.tick(t);
    expect(c.frames[c.frames.length-1]).toEqual({ progress: 1, playing: false });
    expect(c.tasks.size).toBe(0);
    c.player.play(); expect(c.frames[c.frames.length-1]).toEqual({ progress: 0, playing: true });
    c.player.dispose(); expect(c.tasks.size).toBe(0);
  });

  it('seeks exactly, ignores nonfinite input, clamps boundaries, resets and cancels old callbacks', () => {
    const c = clock(); c.player.play(); const stale = [...c.tasks.values()][0];
    c.player.seek(.5); stale(100);
    expect(c.frames[c.frames.length-1]).toEqual({ progress: .5, playing: false });
    c.player.seek(NaN); expect(c.frames[c.frames.length-1]?.progress).toBe(.5);
    c.player.seek(2); expect(c.frames[c.frames.length-1]?.progress).toBe(1);
    c.player.seek(-1); expect(c.frames[c.frames.length-1]?.progress).toBe(0);
    c.player.seek(.7); c.player.reset(); expect(c.frames[c.frames.length-1]).toEqual({ progress: 0, playing: false });
  });

  it('bounds a delayed frame and publishes at most 30 times per second', () => {
    const c = clock(); c.player.play(); c.tick(0);
    for (let t = 1; t <= 20; t++) c.tick(t);
    expect(c.frames).toHaveLength(2);
    c.tick(50000); expect(c.frames[c.frames.length-1]?.progress).toBeCloseTo(.12);
  });

  it('keeps all tracks, geometry, value and threshold description at the same progress', () => {
    const section: ModelSection = { type: 'model', id: 'motion', title: '动画', hidden: false, description: '',
      parameters: [{ id: 'load', label: '负载', min: 0, max: 10, step: 1, value: 0, unit: '' }],
      outputs: [{ label: '负载值', unit: '', operation: 'product', args: ['load', 1], min: 0, max: 10, description: '',
        threshold: { value: 5, below: { label: '较低', description: '继续' }, at_or_above: { label: '较高', description: '请检查' } } }],
      animation: { duration_seconds: 6, tracks: [{ parameter_id: 'load', start: 0, end: 10 }] },
      graphics: [{ label: '负载块', shape: 'rect', x: { output_index: 0, min: 20, max: 180 }, y: 30, width: { output_index: 0, min: 40, max: 140 }, height: 70, color: { output_index: 0, below: 'positive', at_or_above: 'danger' } }],
    };
    const values = animationValues(section.animation!, .5, { load: 0, untouched: 9 });
    const results = evaluateModel(section, values), graphics = evaluateGraphics(section, results);
    expect(values).toEqual({ load: 5, untouched: 9 });
    expect(results[0]).toMatchObject({ value: 5, state: { label: '较高', description: '请检查' } });
    expect(graphics[0]).toMatchObject({ x: 100, width: 90, color: 'danger', state: '较高' });
    expect(animationValues({ duration_seconds: 1, tracks: [{ parameter_id: 'load', start: 10, end: 0 }, { parameter_id: 'other', start: 0, end: 20 }] }, .25, {})).toEqual({ load: 7.5, other: 5 });
  });
});
