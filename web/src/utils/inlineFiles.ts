const WORKSPACE_FILE_EXTENSION_PATTERN = /\.(?:png|jpe?g|webp|gif|bmp|pdf|txt|md|json|csv|tsv|html?|css|scss|js|jsx|ts|tsx|py|sh|ya?ml|xml|sql|pdb|cif|mmcif|sdf|mol2)(?:[?#].*)?$/i;
const IMAGE_FILE_EXTENSION_PATTERN = /\.(?:png|jpe?g|webp|gif|bmp)(?:[?#].*)?$/i;
const STANDALONE_IMAGE_PATH_PATTERN = /^(?:(?:\/workspace)?\/|\.{1,2}\/)?[A-Za-z0-9_.@%+~()-]+(?:\/[A-Za-z0-9_.@%+~()-]+)*\.(?:png|jpe?g|webp|gif|bmp)(?:[?#][^\s]*)?$/i;

export const isLocalFileRef = (value: string) => {
  const text = String(value || '').trim();
  if (!text) return false;
  if (/^(?:https?:|data:|blob:|mailto:|#)/i.test(text)) return false;
  return (
    text.startsWith('/')
    || text.startsWith('./')
    || text.startsWith('../')
    || WORKSPACE_FILE_EXTENSION_PATTERN.test(text)
  );
};

export const isImageFileRef = (value: string) => (
  isLocalFileRef(value) && IMAGE_FILE_EXTENSION_PATTERN.test(String(value || '').trim())
);

export const normalizeInlineFilePath = (path: string) => {
  const text = String(path || '').trim();
  // Sandbox tools expose the current host workspace at /workspace. The file API
  // resolves from that host workspace, so only the virtual sandbox root is removed.
  return text.startsWith('/workspace/') ? text.slice('/workspace/'.length) : text;
};

export const getInlineFileUrl = (path: string, workspaceId = '') => {
  const search = new URLSearchParams({ path: normalizeInlineFilePath(path) });
  const normalizedWorkspaceId = String(workspaceId || '').trim();
  if (normalizedWorkspaceId) search.set('workspace_id', normalizedWorkspaceId);
  return `/api/files/inline?${search.toString()}`;
};

export const toInlineHref = (value: string, workspaceId = '') => {
  const text = String(value || '').trim();
  return isLocalFileRef(text) ? getInlineFileUrl(text, workspaceId) : text;
};

const markdownImageTarget = (path: string) => (
  /\s/.test(path) ? `<${path}>` : path
);

const escapeMarkdownImageAlt = (value: string) => (
  String(value || '图片').replace(/([\\[\]])/g, '\\$1')
);

/** Build a local-image Markdown fragment that survives spaces in workspace paths. */
export const toInlineImageMarkdown = (path: string, alt = '图片') => {
  const target = String(path || '').trim();
  if (!isImageFileRef(target)) return '';
  return `![${escapeMarkdownImageAlt(alt)}](${markdownImageTarget(target)})`;
};

const standaloneImagePath = (line: string) => {
  let candidate = String(line || '').trim();
  if (!candidate || candidate.startsWith('![') || candidate.startsWith('[')) return '';
  candidate = candidate.replace(/^[-*+]\s+/, '').trim();
  if (candidate.startsWith('`') && candidate.endsWith('`') && candidate.length > 2) {
    candidate = candidate.slice(1, -1).trim();
  }
  return isImageFileRef(candidate) && STANDALONE_IMAGE_PATH_PATTERN.test(candidate)
    ? candidate
    : '';
};

/**
 * Compatibility for agents that put a complete image path on its own line but
 * forget the Markdown image marker. Existing Markdown and fenced code stay intact.
 */
export const autoEmbedStandaloneImagePaths = (content: string) => {
  let inFence = false;
  return String(content || '')
    .split('\n')
    .map((line) => {
      if (/^\s*```/.test(line)) {
        inFence = !inFence;
        return line;
      }
      if (inFence) return line;
      const path = standaloneImagePath(line);
      if (!path) return line;
      const name = normalizeInlineFilePath(path).split('/').pop() || '图片';
      return toInlineImageMarkdown(path, name);
    })
    .join('\n');
};
