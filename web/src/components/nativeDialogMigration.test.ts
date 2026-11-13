import { describe,it,expect } from 'vitest';
import { readFileSync,readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import ts from 'typescript';

describe('native dialog migration safety boundaries',()=>{
  it('leaves no native confirm/alert/prompt calls in application source',()=>{
    const violations:string[]=[];
    const scan=(folder:string)=>{for(const entry of readdirSync(folder,{withFileTypes:true})){
      const file=resolve(folder,entry.name);
      if(entry.isDirectory()){scan(file);continue;}
      if(!/\.tsx?$/.test(file)||/\.test\./.test(file))continue;
      const source=ts.createSourceFile(file,readFileSync(file,'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
      const visit=(node:ts.Node)=>{
        if(ts.isCallExpression(node)&&ts.isPropertyAccessExpression(node.expression)&&node.expression.expression.getText(source)==='window'&&['confirm','alert','prompt'].includes(node.expression.name.text))violations.push(file);
        ts.forEachChild(node,visit);
      };visit(source);
    }};scan(resolve('src'));expect(violations).toEqual([]);
  });
  it('keeps explicit yes validation, cancel handling and private-export opt-in',()=>{
    const research=readFileSync(resolve('src/components/ResearchWorkspace.tsx'),'utf8');
    expect(research).toContain("if (confirmation === null)");
    expect(research).toContain("confirmation.trim().toLowerCase() === 'yes'");
    const graph=readFileSync(resolve('src/components/OntologyGraph.tsx'),'utf8');
    expect(graph).toContain('const includePrivate = await includePrivateInExport()');
    expect(graph).toContain('await includePrivateInExport() ? displayData() : publicDisplayData()');
    expect(graph).toContain("node.scope === 'public'");
    expect(readFileSync(resolve('src/components/PaperWikiWorkspace.tsx'),'utf8')).toContain("e.preventDefault(); e.returnValue = ''");
  });
});
