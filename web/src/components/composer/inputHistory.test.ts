import { describe, expect, it } from 'vitest';
import { navigateInputHistory } from './inputHistory';

describe('navigateInputHistory', () => {
  it('walks older and newer inputs and restores the draft', () => {
    let state = { index: -1, draft: '' };
    const first = navigateInputHistory(['first', 'second'], 'unfinished', state, 'older')!;
    expect(first.value).toBe('second'); state = first.state;
    const second = navigateInputHistory(['first', 'second'], first.value, state, 'older')!;
    expect(second.value).toBe('first'); state = second.state;
    const third = navigateInputHistory(['first', 'second'], second.value, state, 'newer')!;
    expect(third.value).toBe('second'); state = third.state;
    expect(navigateInputHistory(['first', 'second'], third.value, state, 'newer')!.value).toBe('unfinished');
  });
});
