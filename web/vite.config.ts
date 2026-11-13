import { defineConfig, type Plugin } from 'vite';
import solid from 'vite-plugin-solid';
import tailwindcss from '@tailwindcss/vite';

const ENTRY_CHUNK_BUDGET_BYTES = 250 * 1024;
const ASYNC_CHUNK_BUDGET_BYTES = 500 * 1024;
// Mermaid 11.17 将新图表语法生成器合并成一个不可继续拆分的模块。
// 该块仍只在渲染 Mermaid 时按需加载；其余异步块继续执行 500 KiB 上限。
const MERMAID_PARSER_CHUNK_BUDGET_BYTES = 700 * 1024;
// PPTX 渲染器由 Open File Viewer 在打开 Office 文件时按需加载。上游以单个
// 不可拆分的 ESM 文件发布；它不会进入首屏或普通文件预览的加载链。
const PPTX_RENDERER_CHUNK_BUDGET_BYTES = 1200 * 1024;

const enforceJavaScriptChunkBudget = (): Plugin => ({
  name: 'tcode-javascript-chunk-budget',
  apply: 'build',
  generateBundle(_options, bundle) {
    const oversized = Object.entries(bundle).flatMap(([fileName, output]) => {
      if (output.type !== 'chunk' || typeof output.code !== 'string') return [];
      const size = new TextEncoder().encode(output.code).byteLength;
      const budget = output.isEntry
        ? ENTRY_CHUNK_BUDGET_BYTES
        : fileName.startsWith('assets/aiden0z-pptx-renderer.')
          ? PPTX_RENDERER_CHUNK_BUDGET_BYTES
        : fileName.startsWith('assets/mermaid-parser-')
          ? MERMAID_PARSER_CHUNK_BUDGET_BYTES
          : ASYNC_CHUNK_BUDGET_BYTES;
      return size > budget ? [`${fileName}: ${size} > ${budget} bytes`] : [];
    });
    if (oversized.length > 0) {
      throw new Error(`JavaScript 分块超过预算：\n${oversized.join('\n')}`);
    }
  },
});

export default defineConfig(({ mode }) => ({
  resolve: { dedupe: ['@codemirror/state', '@codemirror/view'] },
  optimizeDeps: { include: ['@codemirror/state', '@codemirror/view', '@codemirror/autocomplete', '@codemirror/commands', '@codemirror/language', '@codemirror/lang-markdown', '@codemirror/search'] },
  plugins: [
    solid({ ssr: mode === 'test' }),
    tailwindcss(),
    enforceJavaScriptChunkBudget(),
  ],
  build: {
    modulePreload: {
      resolveDependencies(_filename, dependencies, context) {
        if (context.hostType !== 'html') return dependencies;
        // Mermaid 解析器只服务动态加载的图表渲染。manualChunks 会让 Rollup
        // 把这些共享块列为入口候选预加载，这里阻止首屏提前下载；当真正执行
        // import('mermaid') 时，Vite 仍会通过 JS 动态导入预加载完整依赖链。
        const onDemandPrefixes = [
          'assets/mermaid-parser',
          'assets/lodash-es-',
        ];
        return dependencies.filter(
          (dependency) => !onDemandPrefixes.some(
            (prefix) => dependency.startsWith(prefix),
          ),
        );
      },
    },
    rollupOptions: {
      input: { main: 'index.html', desktop: 'desktop.html', molstar: 'molstar.html' },
      output: {
        manualChunks(id) {
          const moduleId = id.replace(/\\/g, '/');
          // Solid scenes load on demand; keep Three's core and renderer below the existing chunk budget.
          if(moduleId.includes('/node_modules/three/build/three.core.js')) return 'solid-three-core';
          if(moduleId.includes('/node_modules/three/build/three.module.js')) return 'solid-three-renderer';

          // Vite 的动态导入预加载帮助器必须保持独立；否则 Rollup 可能把它
          // 合并进 Mermaid 解析器，并让应用入口为了一个小帮助器加载整套解析器。
          if (moduleId.includes('vite/preload-helper')) {
            return 'vite-runtime';
          }

          // 多个 CommonJS 依赖共享 Rollup 运行时；单独放置可避免该运行时被
          // 吞入某个业务块，进而让不相关的动态功能互相产生静态依赖。
          if (moduleId.includes('commonjsHelpers')) {
            return 'commonjs-helpers';
          }

          // CodeMirror 原先被强制合成一个约 555 KB 的文件。按核心、语言包和
          // 可选功能拆分后，打开 Wiki 编辑器时仍并行加载，但不再出现单个大块。
          if (
            /\/node_modules\/@codemirror\/(?:autocomplete|commands|search)\//.test(moduleId)
          ) {
            return 'codemirror-features';
          }
          if (
            /\/node_modules\/@codemirror\/lang-/.test(moduleId)
            || /\/node_modules\/@lezer\/(?:markdown|lr|html|css|javascript)\//.test(moduleId)
          ) {
            return 'codemirror-languages';
          }
          if (
            moduleId.includes('/node_modules/@codemirror/')
            || moduleId.includes('/node_modules/@lezer/')
            || /\/node_modules\/(?:style-mod|w3c-keyname|crelt|@marijn\/find-cluster-break)\//.test(moduleId)
          ) {
            return 'codemirror-core';
          }

          // MarkdownContent 只注册常用语言；把核心与语言定义留在同一个小型
          // 按需块中，避免完整 highlight.js 的约 969 KB 全语言包。
          if (moduleId.includes('/node_modules/highlight.js/')) {
            return 'syntax-highlight';
          }

          // Mermaid 的图表实现本身已经动态加载，这里进一步拆分解析器、D3
          // 与渲染依赖，避免 Wardley 和 mermaid.core 各自形成 500 KB 大块。
          if (
            moduleId.includes('/node_modules/chevrotain/')
            || moduleId.includes('/node_modules/@chevrotain/')
            || moduleId.includes('/node_modules/chevrotain-allstar/')
          ) {
            return 'mermaid-parser-chevrotain';
          }
          if (moduleId.includes('/node_modules/langium/')) {
            return 'mermaid-parser-langium';
          }
          if (moduleId.includes('/node_modules/vscode-')) {
            return 'mermaid-parser-vscode';
          }
          if (moduleId.includes('/node_modules/@mermaid-js/parser/')) {
            return 'mermaid-parser';
          }
          if (/\/node_modules\/(?:d3-|internmap|delaunator|robust-predicates)/.test(moduleId)) {
            return 'mermaid-d3';
          }
          if (moduleId.includes('/node_modules/roughjs/')) {
            return 'mermaid-rough';
          }
          if (moduleId.includes('/node_modules/lodash-es/')) {
            return 'lodash-es';
          }
          if (
            moduleId.includes('/node_modules/@iconify/')
            || moduleId.includes('/node_modules/khroma/')
            || moduleId.includes('/node_modules/stylis/')
            || moduleId.includes('/node_modules/dayjs/')
            || moduleId.includes('/node_modules/@braintree/sanitize-url/')
            || moduleId.includes('/node_modules/ts-dedent/')
          ) {
            return 'mermaid-support';
          }

          // G6 仅在本体关系探索页按需加载。其渲染、布局和运行时依赖分别成块，
          // 避免完整图引擎进入页面业务块并突破 500 KiB 异步预算。
          if (moduleId.includes('/node_modules/@antv/layout/')) {
            return 'antv-graph-layout';
          }
          if (moduleId.includes('/node_modules/@antv/g-canvas/') || moduleId.includes('/node_modules/@antv/g-plugin-')) {
            return 'antv-graph-canvas';
          }
          if (moduleId.includes('/node_modules/@antv/g/')) {
            return 'antv-graph-renderer';
          }
          if (moduleId.includes('/node_modules/@antv/graphlib/') || moduleId.includes('/node_modules/@antv/algorithm/')) {
            return 'antv-graph-algorithm';
          }
          if (moduleId.includes('/node_modules/@antv/g6/')) {
            return 'antv-graph-runtime';
          }
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.TCODE_DEV_PROXY || 'http://localhost:8080',
        changeOrigin: true,
      },
      '/ws': {
        target: process.env.TCODE_DEV_PROXY || 'ws://localhost:8080',
        ws: true,
      },
    },
  },
}));
