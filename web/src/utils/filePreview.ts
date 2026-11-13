const extension = (path: string | null | undefined) => {
  const cleanPath = String(path || '').trim().toLowerCase().split(/[?#]/, 1)[0];
  return cleanPath.includes('.') ? cleanPath.split('.').pop() || '' : '';
};

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'bmp']);
const OFFICE_EXTENSIONS = new Set(['docx', 'xlsx', 'pptx']);

export const isImageFile = (path: string | null | undefined) => IMAGE_EXTENSIONS.has(extension(path));
export const isPdfFile = (path: string | null | undefined) => extension(path) === 'pdf';
export const isHtmlFile = (path: string | null | undefined) => ['html', 'htm'].includes(extension(path));
export const isOfficeFile = (path: string | null | undefined) => OFFICE_EXTENSIONS.has(extension(path));

