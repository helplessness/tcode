import { describe, expect, it } from 'vitest';
import { applySideChatText } from './sideChat';
import { parseCompanionState, restoreSideChatTabs } from './companionState';

describe('independent side chat', () => {
  it('restores its first-class panel tab', () => {
    expect(parseCompanionState(JSON.stringify({tabs:['sidechat:side-one','sidechat:side-two'],active:'sidechat:side-two',closed:[]})))
      .toEqual({tabs:['sidechat:side-one','sidechat:side-two'],active:'sidechat:side-two',closed:[]});
  });
  it('replaces all old-session side tabs and never restores a closed side chat', () => {
    const state = {tabs:['browser:live','sidechat:old-a','sidechat:old-b'] as const,active:'sidechat:old-b' as const,closed:['sidechat:deleted','assets'] as const};
    const restored=restoreSideChatTabs({tabs:[...state.tabs],active:state.active,closed:[...state.closed]}, ['new-a','new-b','new-a'],'new-a');
    expect(restored).toEqual({tabs:['browser:live','sidechat:new-a','sidechat:new-b'],active:'sidechat:new-a',closed:['assets']});
    expect(restoreSideChatTabs(restored,['new-b']).tabs).toEqual(['browser:live','sidechat:new-b']);
  });
  it('appends deltas and replaces with the completed text without duplicate replies', () => {
    let messages = applySideChatText([], {kind:'model_output',message_id:'reply',delta:'你好'});
    messages = applySideChatText(messages, {kind:'model_output',message_id:'reply',delta:'，用户'});
    expect(messages[0].content).toBe('你好，用户');
    messages = applySideChatText(messages, {kind:'model_output',message_id:'reply',content:'你好，用户。'});
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe('你好，用户。');
    expect(applySideChatText(messages, {kind:'model_output.superseded',message_id:'reply'})).toEqual([]);
  });
  it('retains public narration classification across streamed chunks and snapshots', () => {
    let messages = applySideChatText([], {kind:'model_output',message_id:'reply',delta:'先检查',presentation:'commentary'});
    messages = applySideChatText(messages, {kind:'model_output',message_id:'reply',delta:'输入'});
    expect(messages[0].presentation).toBe('commentary');
    expect(messages[0].content).toBe('先检查输入');
    expect(applySideChatText(messages, {kind:'model_output',message_id:'reply',content:'结果',presentation:'final'})[0].presentation).toBe('final');
  });
});
