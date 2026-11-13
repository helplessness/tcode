function createRecoveryController({ healthCheck, restart, notify = () => {}, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let timer;
  let inFlight;
  let failures = 0;

  function cancel() {
    if (timer) clearTimer(timer);
    timer = undefined;
  }

  async function recover(reason = 'unknown') {
    if (inFlight) return inFlight;
    cancel();
    inFlight = (async () => {
      try {
        if (await healthCheck()) {
          failures = 0;
          notify({ state: 'ready', reason });
          return 'healthy';
        }
      } catch {}
      notify({ state: 'recovering', reason });
      try {
        await restart();
        failures = 0;
        notify({ state: 'ready', reason });
        return 'restarted';
      } catch (error) {
        failures += 1;
        schedule(reason);
        throw error;
      }
    })().finally(() => { inFlight = undefined; });
    return inFlight;
  }

  function schedule(reason = 'backend-exit') {
    if (timer) return;
    const delay = Math.min(30_000, 1_000 * (2 ** Math.min(failures, 5)));
    timer = setTimer(() => {
      timer = undefined;
      void recover(reason).catch(() => {});
    }, delay);
  }

  return { recover, schedule, cancel };
}

module.exports = { createRecoveryController };
