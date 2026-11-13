import {describe,it,expect} from 'vitest';
import {resizedTextareaHeight,composerTextareaHeight} from './createComposerLayout';

describe('top-edge composer resize',()=>{
  it('grows with lines, caps long input and shrinks again; restored manual height is only a floor',()=>{
    expect(composerTextareaHeight(28,44,280)).toBe(44);
    expect(composerTextareaHeight(140,44,280)).toBe(140);
    expect(composerTextareaHeight(900,44,280)).toBe(280);
    expect(composerTextareaHeight(140,44,280,48)).toBe(140);
    expect(composerTextareaHeight(28,44,280,96)).toBe(96);
    expect(composerTextareaHeight(28,44,280,480)).toBe(280);
    expect(composerTextareaHeight(28,44,280)).toBe(44);
  });
  it('grows upward, shrinks downward and keeps the input inside its pane',()=>{
    expect(resizedTextareaHeight(80,600,400,48,400)).toBe(280);
    expect(resizedTextareaHeight(280,400,550,48,400)).toBe(130);
    expect(resizedTextareaHeight(80,600,0,48,400)).toBe(400);
    expect(resizedTextareaHeight(80,600,900,48,400)).toBe(48);
  });
});
