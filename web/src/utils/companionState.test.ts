import { describe, expect, it } from 'vitest';
import { isCompanionView, parseCompanionState } from './companionState';

describe('companion tab state', () => {
  it('does not restore a browser tab without its live desktop session', () => {
    expect(parseCompanionState(JSON.stringify({tabs:['collaboration','browser:manual-browser-test'],active:'browser:manual-browser-test',closed:[]})))
      .toEqual({tabs:['collaboration'],active:'collaboration',closed:[]});
  });
  it('restores valid tabs and discards stale values', () => {
    expect(parseCompanionState(JSON.stringify({
      tabs: ['collaboration', 'preview:outputs%2Fone.pdb', 'page:review', 'page:missing'],
      active: 'preview:outputs%2Fone.pdb', closed: ['assets', 'page:missing'],
    }))).toEqual({
      tabs: ['collaboration', 'preview:outputs%2Fone.pdb', 'page:review'],
      active: 'preview:outputs%2Fone.pdb', closed: ['assets'],
    });
  });

  it('falls back safely for corrupt state', () => {
    expect(parseCompanionState('{')).toEqual({ tabs: ['collaboration'], active: 'collaboration', closed: [] });
    expect(isCompanionView('changes')).toBe(true);
    expect(isCompanionView('preview:%E0%A4%A')).toBe(false);
  });

  it('drops tabs that are unavailable on the current surface', () => {
    const allowed = (page: string) => !['mcp', 'settings'].includes(page);
    expect(parseCompanionState(JSON.stringify({
      tabs: ['collaboration', 'page:mcp', 'page:settings', 'page:plugins'],
      active: 'page:settings', closed: ['page:mcp', 'assets'],
    }), allowed)).toEqual({
      tabs: ['collaboration', 'page:plugins'], active: 'page:plugins', closed: ['assets'],
    });
  });

  it('restores independent paper preview tabs', () => {
    const paper = `paper:${encodeURIComponent(JSON.stringify({id:'paper-123',title:'Example paper'}))}`;
    expect(isCompanionView(paper)).toBe(true);
    expect(parseCompanionState(JSON.stringify({tabs:['page:library',paper],active:paper,closed:[]}))).toEqual({
      tabs:['page:library',paper],active:paper,closed:[],
    });
  });
});
