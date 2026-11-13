// Pinned official viewer, loaded only when a structure is opened; never a remote CDN.
import { mkdir, copyFile, cp, stat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
const target = `${root}public/vendor/molstar-5.11.0`;
// Separate lazy viewer budget; the main-app chunk budgets remain unchanged.
if ((await stat(`${root}node_modules/molstar/build/viewer/molstar.js`)).size > 6 * 1024 * 1024) {
  throw new Error('Mol* lazy viewer exceeds its 6 MiB budget');
}
await mkdir(target, { recursive: true });
for (const name of ['molstar.js', 'molstar.css']) {
  await copyFile(`${root}node_modules/molstar/build/viewer/${name}`, `${target}/${name}`);
}
await cp(`${root}node_modules/molstar/build/viewer/images`, `${target}/images`, { recursive: true });
await copyFile(`${root}node_modules/molstar/LICENSE`, `${target}/LICENSE`);
