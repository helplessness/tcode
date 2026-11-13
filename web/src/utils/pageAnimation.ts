import type { ModelAnimation } from '../api/intelligentPage';

export function animationValues(animation: ModelAnimation, progress: number, values: Record<string, number>) {
  const frame = { ...values }, position = Math.max(0, Math.min(1, progress));
  for (const track of animation.tracks) frame[track.parameter_id] = track.start + (track.end - track.start) * position;
  return frame;
}

// One bounded clock per model. No generated code, autoplay, looping or background catch-up.
export function createPlayback(durationSeconds: number, publish: (progress: number, playing: boolean) => void,
  request: (callback: FrameRequestCallback) => number = callback => requestAnimationFrame(callback),
  cancel: (id: number) => void = id => cancelAnimationFrame(id)) {
  let progress = 0, playing = false, frame = 0, generation = 0;
  const stop = () => {
    playing = false; generation++; if (frame) cancel(frame); frame = 0;
  };
  const pause = () => { stop(); publish(progress, false); };
  const seek = (value: number) => {
    if (!Number.isFinite(value)) return;
    stop(); progress = Math.max(0, Math.min(1, value)); publish(progress, false);
  };
  return {
    pause, seek, reset: () => seek(0),
    dispose: stop,
    play: () => {
      if (playing) return;
      if (progress >= 1) progress = 0;
      playing = true; const token = ++generation;
      let previous: number | undefined, lastPaint = -Infinity;
      publish(progress, true);
      const tick: FrameRequestCallback = now => {
        if (!playing || generation !== token) return;
        if (previous !== undefined) progress = Math.min(1, progress + Math.max(0, Math.min(100, now - previous)) / (durationSeconds * 1000));
        previous = now;
        if (progress >= 1) { playing = false; frame = 0; publish(1, false); return; }
        if (now - lastPaint >= 1000 / 30) { lastPaint = now; publish(progress, true); }
        frame = request(tick);
      };
      frame = request(tick);
    },
  };
}
