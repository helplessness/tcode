const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const crypto=require('node:crypto');
const {modelSecrets}=require('../model-secrets.cjs');

test('profile-local encrypted vault persists and refuses insecure storage',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'tcode-vault-test-'));
  const key=crypto.randomBytes(32),iv=crypto.randomBytes(16);
  const safe={isEncryptionAvailable:()=>true,encryptString:text=>{
    const cipher=crypto.createCipheriv('aes-256-cbc',key,iv);return Buffer.concat([cipher.update(text),cipher.final()]);
  },decryptString:data=>{const cipher=crypto.createDecipheriv('aes-256-cbc',key,iv);return Buffer.concat([cipher.update(data),cipher.final()]).toString();}};
  try{
    const vault=modelSecrets(root,safe);vault.write({TEST_KEY:'test-only-secret'});
    assert.equal(fs.readFileSync(path.join(root,'model-secrets.bin')).includes(Buffer.from('test-only-secret')),false);
    assert.equal(fs.statSync(path.join(root,'model-secrets.bin')).mode&0o777,0o600);
    assert.equal(modelSecrets(root,safe).read().TEST_KEY,'test-only-secret');
    assert.throws(()=>modelSecrets(root,{...safe,isEncryptionAvailable:()=>false}).write({}),/不可用/);
    assert.throws(()=>modelSecrets(root,{...safe,getSelectedStorageBackend:()=> 'basic_text'}).write({}),/不可用/);
  }finally{fs.rmSync(root,{recursive:true});}
});
