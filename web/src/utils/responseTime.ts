const dateFormat = new Intl.DateTimeFormat('en-CA', {
  year: 'numeric', month: 'numeric', day: 'numeric', timeZone: 'Asia/Shanghai',
});
const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai',
});

export function formatResponseTime(timestamp: number, now = Date.now()): string {
  if (!Number.isFinite(timestamp) || timestamp <= 0 || !Number.isFinite(new Date(timestamp).getTime())) return '';
  const date = Object.fromEntries(dateFormat.formatToParts(timestamp).map(part => [part.type, part.value]));
  const today = Object.fromEntries(dateFormat.formatToParts(now).map(part => [part.type, part.value]));
  const time = timeFormat.format(timestamp);
  if (date.year === today.year && date.month === today.month && date.day === today.day) return time;
  return `${date.year === today.year ? '' : `${date.year}年`}${Number(date.month)}月${Number(date.day)}日 ${time}`;
}
