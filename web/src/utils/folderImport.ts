export function planFolderImport<T extends { webkitRelativePath: string; size: number }>(incoming: T[]) {
  const files: T[] = [];
  let skipped = 0;
  let bytes = 0;
  for (const file of incoming) {
    const path = file.webkitRelativePath;
    const parts = path.split('/');
    if (!path || path.includes('\\') || parts.some(part => !part || part === '.' || part === '..')) throw new Error('文件夹中存在无效相对路径');
    if (parts.some(part => part.startsWith('.') || ['node_modules', '__pycache__'].includes(part))) { skipped++; continue; }
    if (file.size > 100 * 1024 * 1024) throw new Error(`${path} 超过单文件 100 MB 限制`);
    files.push(file);
    bytes += file.size;
    if (files.length > 200 || bytes > 1024 ** 3) throw new Error('一次最多导入 200 个文件、共 1 GB，请选择较小的子目录');
  }
  if (!files.length) throw new Error('没有可导入的文件；隐藏文件和依赖缓存目录默认跳过');
  return { files, skipped, bytes };
}
