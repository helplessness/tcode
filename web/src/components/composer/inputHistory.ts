export type InputHistoryState = { index: number; draft: string };

export function navigateInputHistory(
  entries: string[],
  current: string,
  state: InputHistoryState,
  direction: 'older' | 'newer',
): { value: string; state: InputHistoryState } | null {
  if (!entries.length || (direction === 'newer' && state.index < 0)) return null;
  if (direction === 'older') {
    const index = state.index < 0 ? entries.length - 1 : Math.max(0, state.index - 1);
    return { value: entries[index], state: { index, draft: state.index < 0 ? current : state.draft } };
  }
  if (state.index < entries.length - 1) {
    const index = state.index + 1;
    return { value: entries[index], state: { ...state, index } };
  }
  return { value: state.draft, state: { index: -1, draft: '' } };
}
