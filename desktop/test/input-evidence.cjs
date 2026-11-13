// Opt-in source-desktop test evidence. Never enabled in packaged/normal runs.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function inputEvidence({ directory, bundleId, driver }) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 }); fs.chmodSync(directory, 0o700);
  let taskId = ''; let frames = 0; let lastImage = null; const files = [];
  const expiresAt = Date.now() + 60 * 60_000;
  const cleanup = () => { for (const file of files) try { fs.unlinkSync(file); } catch (e) { if (e.code !== 'ENOENT') throw e; } };
  const timer = setTimeout(cleanup, 60 * 60_000); timer.unref();
  process.once('exit', cleanup);
  return async ({ operation, payload, result, error, capture, stage = 'after', frontmostPid = null }) => {
    if (Date.now() >= expiresAt || payload.app?.bundle_id !== bundleId) return;
    if (!taskId) taskId = payload.task_id;
    if (taskId !== payload.task_id) return;
    if (stage === 'after') try { frontmostPid = (await driver.call('list_apps', { session: 'tcode-test-evidence' })).apps?.find(a => a.active)?.pid || null; } catch { /* record unavailable, not guessed */ }
    const row = { at: new Date().toISOString(), task_id: taskId, expires_at: new Date(expiresAt).toISOString(), operation, stage, preceding_frame: frames || null,
      target_pid: payload.app?.pid || null, window_id: payload.window?.window_id || null, frontmost_pid: frontmostPid,
      coordinates: payload.x !== undefined ? { x: payload.x, y: payload.y,
        reference_width: capture?.delivered_width, reference_height: capture?.delivered_height,
        space: 'delivered_pixels', native_transform_owner: 'official_driver' } : undefined,
      key: operation === 'press_key' ? payload.key : undefined, modifiers: operation === 'press_key' ? payload.modifiers : undefined,
      effect: error?.evidence?.effect || result?.effect || result?.action?.effect || null,
      delivery: error?.evidence?.actual_delivery || result?.delivery?.mode || payload.delivery_mode || null,
      error_code: error?.safeCode || error?.code || null };
    if (row.coordinates && lastImage?.bounds) row.coordinates.derived_screen_point = {
      x: lastImage.bounds.x + payload.x * lastImage.bounds.width / lastImage.width,
      y: lastImage.bounds.y + payload.y * lastImage.bounds.height / lastImage.height,
      evidence: 'derived_from_exact_window_image_not_native_event_readback',
    };
    if (operation === 'observe' && result?._image && frames < 12) {
      const png = Buffer.from(result._image.data, 'base64');
      if (png.length <= 8 * 1024 * 1024 && png.length >= 24) {
        const name = `${String(++frames).padStart(2, '0')}.png`; const file = path.join(directory, name);
        fs.writeFileSync(file, png, { mode: 0o600 }); files.push(file);
        row.image = { file: name, sha256: crypto.createHash('sha256').update(png).digest('hex'),
          width: png.readUInt32BE(16), height: png.readUInt32BE(20) };
        row.window_bounds = result.window_bounds; row.backing_scale = result.screenshot_scale;
        lastImage = { bounds: result.window_bounds, width: row.image.width, height: row.image.height };
      }
    }
    const journal = path.join(directory, 'evidence.jsonl');
    fs.appendFileSync(journal, `${JSON.stringify(row)}\n`, { mode: 0o600 });
  };
}
module.exports = { inputEvidence };
