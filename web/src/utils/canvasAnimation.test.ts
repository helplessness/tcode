import { expect, it } from 'vitest';
import { explosionPortion, timelineShare } from './canvasAnimation';

it('shares a reversible staggered timeline between preview and exported frames',()=>{
  expect([0,.25,.5,.75,1].map(p=>timelineShare(p,'roundtrip'))).toEqual([0,.5,1,.5,0]);
  expect([0,.5,1].map(p=>timelineShare(p,'assemble'))).toEqual([1,.5,0]);
  expect([0,.5,1].map(p=>timelineShare(p,'explode'))).toEqual([0,.5,1]);
  expect(explosionPortion(.1,0,.25)).toBeGreaterThan(0);
  expect(explosionPortion(.1,.25,.25)).toBe(0);
  expect(explosionPortion(.9,0,.25)).toBe(1);
  expect(explosionPortion(.9,.25,.25)).toBeLessThan(1);
});
