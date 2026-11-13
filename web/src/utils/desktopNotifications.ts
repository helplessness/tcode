/** Keep OS banners independent of unread state; reading remains an explicit action. */
export function desktopNotificationLedger(identity: string, storage: Pick<Storage, 'getItem' | 'setItem'>) {
  const key = `tcode-desktop-notifications-v1:${identity}`;
  let seen: Set<string>;
  try {
    const saved: unknown = JSON.parse(storage.getItem(key) || '[]');
    seen = new Set(Array.isArray(saved) ? saved.filter((id): id is string => typeof id === 'string').slice(-1000) : []);
  } catch { seen = new Set(); }
  return {
    has: (id: string) => seen.has(id),
    remember(id: string) {
      seen.add(id);
      if (seen.size > 1000) seen.delete(seen.values().next().value!);
      try { storage.setItem(key, JSON.stringify([...seen])); } catch { /* In-memory dedupe still works. */ }
    },
  };
}
