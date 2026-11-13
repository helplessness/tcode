const shortLivedGetCache = new Map<string, { expiresAt: number; value: unknown }>();
const inflightGetRequests = new Map<string, Promise<unknown>>();
let cacheEpoch = 0;

export async function withShortLivedGetCache<T>(
  key: string,
  ttlMs: number,
  loader: () => Promise<T>,
): Promise<T> {
  const now = Date.now();
  const cached = shortLivedGetCache.get(key);
  if (cached && cached.expiresAt > now) {
    return cached.value as T;
  }

  const inflight = inflightGetRequests.get(key);
  if (inflight) {
    return inflight as Promise<T>;
  }

  const requestEpoch = cacheEpoch;
  const promise = loader()
    .then((value) => {
      if (ttlMs > 0 && requestEpoch === cacheEpoch) {
        shortLivedGetCache.set(key, {
          expiresAt: Date.now() + ttlMs,
          value,
        });
      }
      return value;
    })
    .finally(() => {
      if (inflightGetRequests.get(key) === promise) {
        inflightGetRequests.delete(key);
      }
    });

  inflightGetRequests.set(key, promise as Promise<unknown>);
  return promise;
}

export function invalidateSessionGetCache(sessionId: string) {
  invalidateGetCacheByPrefix(`session:${sessionId}:`);
}

export function invalidateWorkflowRunCache() {
  invalidateGetCacheByPrefix('workflow:runs:');
}

export function invalidateWorkflowTemplateCache() {
  invalidateGetCacheByPrefix('workflow:templates');
}

export function resetShortLivedGetCache() {
  cacheEpoch += 1;
  shortLivedGetCache.clear();
  inflightGetRequests.clear();
}

function invalidateGetCacheByPrefix(prefix: string) {
  for (const key of Array.from(shortLivedGetCache.keys())) {
    if (key.startsWith(prefix)) {
      shortLivedGetCache.delete(key);
    }
  }
  for (const key of Array.from(inflightGetRequests.keys())) {
    if (key.startsWith(prefix)) {
      inflightGetRequests.delete(key);
    }
  }
}
