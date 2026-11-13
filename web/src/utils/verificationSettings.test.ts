import { describe, it, expect } from 'vitest';
import { verificationEffectiveLabel, verificationStageStatus } from './verificationSettings';

describe('verification effective status', () => {
  it('does not confuse an account preference with deployment availability', () => {
    expect(verificationEffectiveLabel({enabled:true, mode:'standard'})).toContain('未知');
    expect(verificationStageStatus({enabled:false,reason:'deployment_disabled'})).toBe('服务端未启用');
    expect(verificationStageStatus({enabled:false,reason:'account_disabled'})).toBe('账户已关闭');
    expect(verificationStageStatus({enabled:false,reason:'light_mode'})).toContain('轻量');
  });
  it('labels availability as on-demand rather than verification already performed', () => {
    expect(verificationStageStatus({enabled:true,reason:'available_on_demand'})).toContain('按任务需要');
    expect(verificationStageStatus()).toContain('未知');
  });
});
