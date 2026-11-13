"""Sandboxed browser automation tool backed by Playwright in Docker/gVisor."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shlex
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from ...browser import browser_live_paths, safe_browser_session_id
from ..base import ParameterSchema, Tool, ToolContext
from ...runtime.sandbox import (
    build_tool_sandbox_spec,
    get_sandbox_runtime,
    load_sandbox_config,
    sandbox_session_enabled,
)
from ...runtime.sandbox.base import SandboxJob
from ...types import ToolResult


BrowserAction = Literal[
    "open",
    "navigate",
    "observe",
    "snapshot",
    "click",
    "click_at",
    "move",
    "drag",
    "scroll",
    "key_down",
    "key_up",
    "fill",
    "type",
    "press",
    "wait",
    "screenshot",
    "pdf",
    "text",
    "get",
    "download",
    "close",
]


def _browser_job_ref(runtime_info: dict[str, Any]) -> SandboxJob:
    return SandboxJob(
        sandbox_job_id=str(runtime_info.get("sandbox_job_id") or ""),
        container_id=str(runtime_info.get("container_id") or ""),
        container_name=str(runtime_info.get("container_name") or ""),
        command="",
        metadata={"execution_id": str(runtime_info.get("execution_id") or "")},
    )


class BrowserParameters(ParameterSchema):
    action: BrowserAction
    url: str | None = None
    selector: str | None = None
    ref: str | None = None
    page_revision: int | None = None
    text: str | None = None
    key: str | None = None
    x: float | None = None
    y: float | None = None
    end_x: float | None = None
    end_y: float | None = None
    delta_x: float | None = None
    delta_y: float | None = None
    session: str | None = None
    output_path: str | None = None
    full_page: bool | None = True
    wait_until: str | None = "networkidle"
    timeout: int | None = 60
    width: int | None = 1440
    height: int | None = 1000
    screenshot_mode: Literal["auto", "always", "never"] | None = "auto"


class BrowserActionError(RuntimeError):
    """Structured worker failure that keeps recovery observations available."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = dict(payload or {})
        super().__init__(str(self.payload.get("error") or "browser action failed"))


def _context_workdir(context: ToolContext) -> str:
    if context and isinstance(getattr(context, "extra", None), dict):
        value = str(
            context.extra.get("control_workdir")
            or context.extra.get("workdir", "")
            or ""
        ).strip()
        if value:
            return value
    return str(os.environ.get("TCODE_WORKDIR") or os.getcwd())


def _browser_runtime_context(context: ToolContext) -> ToolContext:
    """Bind the long-lived browser to the owned control workspace.

    Browser Agent tool calls can execute from a per-task isolated worktree.  The
    runner owner policy and the authenticated live-view endpoint both operate on
    the managed control workspace, so browser state and artifacts must use that
    same root.
    """
    if not context or not isinstance(getattr(context, "extra", None), dict):
        return context
    workdir = _context_workdir(context)
    if not workdir:
        return context
    extra = dict(context.extra)
    extra["workdir"] = workdir
    return replace(context, extra=extra)


def _safe_session_name(value: str) -> str:
    return safe_browser_session_id(value)


def _safe_relative_path(value: str, default: str) -> str:
    text = str(value or "").strip() or default
    text = text.replace("\\", "/").lstrip("/")
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        return default
    return path.as_posix()


def _validate_browser_url(value: str) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Browser URL is invalid.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Browser only allows http:// and https:// URLs.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Browser URL credentials are not allowed.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Browser URL port is invalid.")

    hostname = parsed.hostname.rstrip(".").lower()
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
        or hostname == "metadata.google.internal"
    ):
        raise ValueError("Browser cannot access local or private-network URLs.")
    try:
        address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        return url
    if not address.is_global:
        raise ValueError("Browser cannot access local or private-network URLs.")
    return url


def _browser_image() -> str:
    return str(
        os.environ.get("TCODE_BROWSER_SANDBOX_IMAGE")
        or os.environ.get("TCODE_SANDBOX_BROWSER_IMAGE")
        or "tcode-browser:playwright"
    ).strip()


def _browser_network_override() -> str:
    raw = os.environ.get("TCODE_BROWSER_SANDBOX_NETWORK")
    return str(raw or "").strip() if raw is not None else ""


def _browser_profile_name() -> str:
    return str(os.environ.get("TCODE_BROWSER_SANDBOX_PROFILE", "browser") or "browser").strip()


def _browser_node_path() -> str:
    return str(os.environ.get("TCODE_BROWSER_NODE_PATH", "") or "").strip()


def _browser_browsers_path() -> str:
    return str(os.environ.get("TCODE_BROWSER_PLAYWRIGHT_BROWSERS_PATH", "") or "").strip()


def _browser_allowed_agents() -> set[str]:
    return {
        item.strip().lower()
        for item in str(
            os.environ.get("TCODE_BROWSER_ALLOWED_AGENTS", "browser") or "browser"
        ).split(",")
        if item.strip()
    }


def _browser_agent_allowed(context: ToolContext) -> bool:
    allowed = _browser_allowed_agents()
    candidates = {str(getattr(context, "agent", "") or "").strip().lower()}
    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        candidates.add(str(extra.get("agent_name", "") or "").strip().lower())
    return bool(allowed.intersection(candidates))


def _browser_egress_proxy_env() -> dict[str, str]:
  proxy = str(load_sandbox_config().egress_proxy or "").strip()
  if not proxy:
    return {}
  return {
    "HTTP_PROXY": proxy,
    "HTTPS_PROXY": proxy,
    "ALL_PROXY": proxy,
    "http_proxy": proxy,
    "https_proxy": proxy,
    "all_proxy": proxy,
    "NO_PROXY": "127.0.0.1,localhost",
    "no_proxy": "127.0.0.1,localhost",
  }


def _browser_worker_node_script() -> str:
    return r"""
const fs = require("fs");
const path = require("path");

function atomicWrite(target, value) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const tmp = `${target}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmp, value);
  fs.renameSync(tmp, target);
}

function readJson(target, fallback = {}) {
  try {
    const value = JSON.parse(fs.readFileSync(target, "utf8"));
    return value && typeof value === "object" && !Array.isArray(value) ? value : fallback;
  } catch (_) {
    return fallback;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function main() {
  const config = JSON.parse(process.env.TCODE_BROWSER_WORKER_CONFIG || "{}");
  const { chromium } = require("playwright");
  const workspace = process.env.TALL_WORKSPACE || "/workspace";
  const session = config.session || "default";
  const browserRoot = path.join(workspace, ".tcode", "browser");
  const liveRoot = path.join(browserRoot, "live", session);
  const requestsDir = path.join(liveRoot, "requests");
  const responsesDir = path.join(liveRoot, "responses");
  const observationsDir = path.join(liveRoot, "observations");
  const confirmationPath = path.join(liveRoot, "confirmation.json");
  const statePath = path.join(liveRoot, "state.json");
  const framePath = path.join(liveRoot, "latest.jpg");
  const readyPath = path.join(liveRoot, "ready.json");
  const userDataDir = path.join(browserRoot, "profiles", session);
  const downloadsDir = path.join(workspace, "downloads", "browser", session);
  const artifactsDir = path.join(workspace, "artifacts", "browser", session);
  fs.mkdirSync(requestsDir, { recursive: true });
  fs.mkdirSync(responsesDir, { recursive: true });
  fs.mkdirSync(observationsDir, { recursive: true });
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.mkdirSync(downloadsDir, { recursive: true });
  fs.mkdirSync(artifactsDir, { recursive: true });

  const timeoutMs = Math.max(1000, Math.min(Number(config.timeout || 60) * 1000, 10 * 60 * 1000));
  const idleMs = Math.max(30000, Math.min(Number(config.idle_seconds || 1800) * 1000, 24 * 60 * 60 * 1000));
  const confirmationTimeoutMs = Math.max(30000, Math.min(Number(config.confirmation_timeout_seconds || 300) * 1000, 30 * 60 * 1000));
  const frameFps = Math.max(1, Math.min(Number(config.frame_fps || 5), 12));
  const frameIntervalMs = Math.floor(1000 / frameFps);
  const frameQuality = Math.max(25, Math.min(Number(config.frame_quality || 60), 85));
  const viewport = {
    width: Math.max(320, Math.min(Number(config.width || 1440), 3840)),
    height: Math.max(320, Math.min(Number(config.height || 1000), 2160)),
  };
  const liveViewport = {
    width: Math.min(viewport.width, 1920),
    height: Math.min(viewport.height, 1200),
  };
  const proxyServer = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || "";
  const launchOptions = {
    headless: true,
    acceptDownloads: true,
    downloadsPath: downloadsDir,
    viewport,
    args: ["--no-sandbox", "--no-first-run", "--disable-sync"],
  };
  if (proxyServer) {
    launchOptions.args.push(`--proxy-server=${proxyServer}`);
    launchOptions.proxy = {
      server: proxyServer,
      bypass: "127.0.0.1,localhost",
    };
  }
  const context = await chromium.launchPersistentContext(userDataDir, launchOptions);
  context.setDefaultTimeout(timeoutMs);
  let page = context.pages()[0] || await context.newPage();
  let lastObservation = null;
  const nativeDialogs = [];
  const watchedPages = new WeakSet();
  function watchPage(candidate) {
    if (!candidate || watchedPages.has(candidate)) return;
    watchedPages.add(candidate);
    candidate.on("dialog", async (dialog) => {
      nativeDialogs.push({ type: dialog.type(), message: String(dialog.message() || "").slice(0, 160) });
      if (nativeDialogs.length > 5) nativeDialogs.shift();
      await dialog.dismiss().catch(() => {});
    });
  }
  for (const candidate of context.pages()) watchPage(candidate);
  context.on("page", watchPage);
  let cdp = null;
  let running = true;
  let stopRequested = false;
  let frameWriting = false;
  let frameSeq = 0;
  let lastFrameAt = 0;
  let lastActivityAt = Date.now();
  let pageRevision = 0;
  let controlMode = "agent";
  let state = {
    ...readJson(statePath, {}),
    browser_session_id: session,
    agent_session_id: String(config.agent_session_id || ""),
    workspace_id: String(config.workspace_id || ""),
    status: "starting",
    frame_mime: "image/jpeg",
    frame_seq: 0,
    worker_pid: process.pid,
    started_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
  pageRevision = Math.max(0, Number(state.page_revision || 0));
  controlMode = ["agent", "human", "paused"].includes(String(state.control_mode || ""))
    ? String(state.control_mode)
    : "agent";

  function writeState(extra = {}) {
    state = {
      ...state,
      ...extra,
      browser_session_id: session,
      frame_mime: "image/jpeg",
      frame_seq: frameSeq,
      page_revision: pageRevision,
      control_mode: controlMode,
      updated_at: new Date().toISOString(),
    };
    atomicWrite(statePath, JSON.stringify(state, null, 2));
  }

  let sensitiveCleanupComplete = false;
  function cleanupSensitiveLiveState() {
    if (sensitiveCleanupComplete) return;
    sensitiveCleanupComplete = true;
    try { fs.unlinkSync(framePath); } catch (error) {
      if (!error || error.code !== "ENOENT") {
        process.stderr.write(`Unable to remove browser live frame: ${String(error)}\n`);
      }
    }
    try { fs.unlinkSync(confirmationPath); } catch (_) {}
    try {
      for (const name of fs.readdirSync(observationsDir)) {
        if (!name.endsWith(".jpg")) continue;
        try { fs.unlinkSync(path.join(observationsDir, name)); } catch (_) {}
      }
      fs.rmdirSync(observationsDir);
    } catch (_) {}
    const currentStatus = String(state.status || "");
    const terminalStatus = ["closed", "idle_timeout", "stopped"].includes(currentStatus)
      ? currentStatus
      : "stopped";
    state = {
      ...state,
      status: terminalStatus,
      url: "",
      title: "",
      refs: {},
      error: "",
      frame_error: "",
      last_frame_at: "",
      frame_available: false,
      observation_screenshot_path: "",
      observation_screenshot_available: false,
      pending_confirmation: null,
      control_mode: "agent",
      updated_at: new Date().toISOString(),
    };
    try {
      atomicWrite(statePath, JSON.stringify(state, null, 2));
    } catch (error) {
      process.stderr.write(`Unable to redact browser live state: ${String(error)}\n`);
    }
  }
  process.on("exit", cleanupSensitiveLiveState);

  async function refreshPageState(extra = {}) {
    writeState({
      ...extra,
      url: page && !page.isClosed() ? page.url() : String(state.url || ""),
      title: page && !page.isClosed() ? await page.title().catch(() => "") : String(state.title || ""),
    });
  }

  async function attachScreencast() {
    if (cdp) {
      await cdp.send("Page.stopScreencast").catch(() => {});
      await cdp.detach().catch(() => {});
    }
    const sessionClient = await context.newCDPSession(page);
    cdp = sessionClient;
    await sessionClient.send("Page.enable");
    sessionClient.on("Page.screencastFrame", async (event) => {
      await sessionClient.send("Page.screencastFrameAck", { sessionId: event.sessionId }).catch(() => {});
      const now = Date.now();
      if (frameWriting || now - lastFrameAt < frameIntervalMs) return;
      frameWriting = true;
      lastFrameAt = now;
      try {
        atomicWrite(framePath, Buffer.from(event.data, "base64"));
        frameSeq += 1;
        writeState({
          last_frame_at: new Date().toISOString(),
          frame_available: true,
        });
      } catch (error) {
        writeState({ frame_error: String(error && error.message ? error.message : error) });
      } finally {
        frameWriting = false;
      }
    });
    await sessionClient.send("Page.startScreencast", {
      format: "jpeg",
      quality: frameQuality,
      maxWidth: liveViewport.width,
      maxHeight: liveViewport.height,
      everyNthFrame: 1,
    });
  }

  async function useNewestPage() {
    const pages = context.pages().filter((candidate) => !candidate.isClosed());
    const newest = pages[pages.length - 1];
    if (newest && newest !== page) {
      page = newest;
      watchPage(page);
      await attachScreencast();
    }
  }

  function safeWorkspaceOutput(value, fallback) {
    const raw = String(value || fallback || "").replace(/\\/g, "/").replace(/^\/+/, "");
    const target = path.resolve(workspace, raw);
    const prefix = path.resolve(workspace) + path.sep;
    if (!target.startsWith(prefix)) throw new Error("browser output path escaped the workspace");
    return target;
  }

  async function ensurePage(payload) {
    await useNewestPage();
    const targetUrl = payload.url || state.url;
    if (targetUrl && page.url() !== targetUrl) {
      await page.goto(targetUrl, { waitUntil: payload.wait_until || "networkidle", timeout: timeoutMs });
    }
  }

  function selectorFor(el) {
    return el.evaluate((node) => {
      function esc(value) {
        if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
        return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
      }
      if (node.id) return "#" + esc(node.id);
      const parts = [];
      let cur = node;
      while (cur && cur.nodeType === 1 && parts.length < 5) {
        let part = cur.nodeName.toLowerCase();
        if (cur.getAttribute("data-testid")) part += `[data-testid="${cur.getAttribute("data-testid")}"]`;
        else if (cur.getAttribute("name")) part += `[name="${cur.getAttribute("name")}"]`;
        else {
          const parent = cur.parentElement;
          if (parent) {
            const same = Array.from(parent.children).filter((child) => child.nodeName === cur.nodeName);
            if (same.length > 1) part += `:nth-of-type(${same.indexOf(cur) + 1})`;
          }
        }
        parts.unshift(part);
        cur = cur.parentElement;
      }
      return parts.join(" > ");
    });
  }

  function staleRefError(reason) {
    const error = new Error(
      `stale_ref: ${reason}. Run browser observe again and retry once with the new page_revision.`
    );
    error.code = "stale_ref";
    error.currentRevision = pageRevision;
    return error;
  }

  async function firstUsableLocator(selector) {
    const candidates = [];
    let onlyCandidate = null;
    let total = 0;
    for (const frame of page.frames()) {
      const locator = frame.locator(selector);
      const count = Math.min(await locator.count(), 20);
      for (let index = 0; index < count; index += 1) {
        const candidate = locator.nth(index);
        total += 1;
        onlyCandidate = candidate;
        if (await candidate.isVisible().catch(() => false)) candidates.push(candidate);
      }
    }
    if (candidates.length === 1) return candidates[0];
    if (candidates.length > 1) {
      const error = new Error(`ambiguous_selector: ${selector} matched ${candidates.length} visible elements; observe and use a ref`);
      error.code = "ambiguous_selector";
      throw error;
    }
    if (total === 1) return onlyCandidate;
    if (total > 1) {
      const error = new Error(`ambiguous_selector: ${selector} matched ${total} elements; observe and use a ref`);
      error.code = "ambiguous_selector";
      throw error;
    }
    throw new Error(`selector did not resolve to a visible element: ${selector}`);
  }

  async function resolveLocator(payload) {
    if (payload.selector) return firstUsableLocator(payload.selector);
    if (!payload.ref) throw new Error("selector or ref is required");
    const entry = state.refs && state.refs[payload.ref];
    if (!entry) throw staleRefError(`ref ${payload.ref} is no longer available`);
    const selector = typeof entry === "string" ? entry : String(entry.selector || "");
    const entryRevision = Number(
      typeof entry === "string" ? state.page_revision : entry.revision
    );
    const requestedRevision = Number(payload.page_revision);
    if (!Number.isInteger(requestedRevision) || requestedRevision <= 0) {
      throw staleRefError("page_revision is required when using a ref");
    }
    if (requestedRevision !== entryRevision || requestedRevision !== pageRevision) {
      throw staleRefError(
        `requested revision ${requestedRevision} does not match current revision ${pageRevision}`
      );
    }
    if (!selector) throw staleRefError(`ref ${payload.ref} has no selector`);
    const frames = page.frames();
    const frameIndex = Math.max(0, Number(typeof entry === "object" ? entry.frame_index : 0) || 0);
    const frame = frames[frameIndex];
    if (!frame || (entry.frame_url && frame.url() !== entry.frame_url)) {
      throw staleRefError(`ref ${payload.ref} frame is no longer available`);
    }
    const selectorIndex = Math.max(0, Number(typeof entry === "object" ? entry.selector_index : 0) || 0);
    const locator = frame.locator(selector).nth(selectorIndex);
    if (await locator.count() < 1) {
      throw staleRefError(`ref ${payload.ref} no longer resolves to an element`);
    }
    const expectedSignature = typeof entry === "object" ? String(entry.signature || "") : "";
    if (expectedSignature) {
      const currentSignature = await locator.evaluate((node) => {
        const clean = (value) => String(value || "").trim().replace(/\s+/g, " ").slice(0, 160);
        const tag = node.tagName.toLowerCase();
        const type = clean(node.getAttribute("type")).toLowerCase();
        const explicitRole = clean(node.getAttribute("role")).toLowerCase();
        const implicitRoles = {
          a: node.hasAttribute("href") ? "link" : "",
          button: "button",
          select: "combobox",
          textarea: "textbox",
        };
        let role = explicitRole || implicitRoles[tag] || "";
        if (!role && tag === "input") {
          role = ["button", "submit", "reset"].includes(type)
            ? "button"
            : ["checkbox", "radio"].includes(type)
              ? type
              : "textbox";
        }
        const labels = node.labels ? Array.from(node.labels).map((label) => label.innerText).join(" ") : "";
        const labelledBy = clean(node.getAttribute("aria-labelledby"));
        const labelledText = labelledBy
          ? labelledBy.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" ")
          : "";
        const name = clean(
          node.getAttribute("aria-label") || labelledText || labels ||
          node.getAttribute("placeholder") || node.getAttribute("title") ||
          node.getAttribute("name") || node.innerText
        );
        const text = clean(
          tag === "input" && ["button", "submit", "reset"].includes(type)
            ? node.getAttribute("value")
            : node.innerText
        );
        return JSON.stringify([tag, type, role, name, text]);
      });
      if (currentSignature !== expectedSignature) {
        throw staleRefError(`ref ${payload.ref} now points to a different element`);
      }
    }
    return locator;
  }

  async function withLocatorRecovery(payload, operation) {
    let locator = await resolveLocator(payload);
    try {
      return await operation(locator);
    } catch (error) {
      const detail = String(error && error.message ? error.message : error);
      if (!/(?:detached|not attached|execution context was destroyed|frame was detached)/i.test(detail)) throw error;
      await useNewestPage();
      locator = await resolveLocator(payload);
      return operation(locator);
    }
  }

  async function visualSummary() {
    const summaries = await Promise.all(page.frames().map((frame) => frame.evaluate(() => {
      const visible = (node) => {
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      };
      const countVisible = (selector) => Array.from(document.querySelectorAll(selector)).filter(visible).length;
      return {
        images: countVisible("img"),
        canvases: countVisible("canvas"),
        svgs: countVisible("svg"),
        videos: countVisible("video"),
        dialogs: countVisible("dialog,[role=dialog],[aria-modal=true]"),
      };
    }).catch(() => ({ images: 0, canvases: 0, svgs: 0, videos: 0, dialogs: 0 }))));
    return summaries.reduce((total, summary) => ({
      images: total.images + summary.images,
      canvases: total.canvases + summary.canvases,
      svgs: total.svgs + summary.svgs,
      videos: total.videos + summary.videos,
      dialogs: total.dialogs + summary.dialogs,
    }), { images: 0, canvases: 0, svgs: 0, videos: 0, dialogs: 0 });
  }

  async function snapshot() {
    const elements = [];
    const refs = {};
    const nextRevision = pageRevision + 1;
    const selectorCounts = new Map();
    const frames = page.frames();
    for (let frameIndex = 0; frameIndex < frames.length && elements.length < 80; frameIndex += 1) {
      const frame = frames[frameIndex];
      const locators = await frame.locator(
        "a,button,input,textarea,select,[role=button],[role=link],[role=textbox],[onclick],[tabindex]"
      ).elementHandles().catch(() => []);
      for (const el of locators) {
        if (elements.length >= 80) break;
      try {
        const visible = await el.isVisible();
        if (!visible) continue;
        const item = await el.evaluate((node) => {
          const clean = (value) => String(value || "").trim().replace(/\s+/g, " ").slice(0, 160);
          const tag = node.tagName.toLowerCase();
          const type = clean(node.getAttribute("type")).toLowerCase();
          const explicitRole = clean(node.getAttribute("role")).toLowerCase();
          const implicitRoles = {
            a: node.hasAttribute("href") ? "link" : "",
            button: "button",
            select: "combobox",
            textarea: "textbox",
          };
          let role = explicitRole || implicitRoles[tag] || "";
          if (!role && tag === "input") {
            role = ["button", "submit", "reset"].includes(type)
              ? "button"
              : ["checkbox", "radio"].includes(type)
                ? type
                : "textbox";
          }
          const labels = node.labels ? Array.from(node.labels).map((label) => label.innerText).join(" ") : "";
          const labelledBy = clean(node.getAttribute("aria-labelledby"));
          const labelledText = labelledBy
            ? labelledBy.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" ")
            : "";
          const name = clean(
            node.getAttribute("aria-label") || labelledText || labels ||
            node.getAttribute("placeholder") || node.getAttribute("title") ||
            node.getAttribute("name") || node.innerText
          );
          const text = clean(
            tag === "input" && ["button", "submit", "reset"].includes(type)
              ? node.getAttribute("value")
              : node.innerText
          );
          const enabled = !node.disabled && node.getAttribute("aria-disabled") !== "true";
          const checkable = tag === "input" && ["checkbox", "radio"].includes(type);
          const editable = tag === "textarea" || (tag === "input" && !["button", "submit", "reset", "checkbox", "radio", "file"].includes(type)) || node.isContentEditable;
          return {
            tag,
            type,
            role,
            name,
            text,
            visible: true,
            enabled,
            editable,
            checked: checkable ? Boolean(node.checked) : null,
            shadow_dom: typeof ShadowRoot !== "undefined" && node.getRootNode() instanceof ShadowRoot,
            signature: JSON.stringify([tag, type, role, name, text]),
          };
        });
        const ref = `@e${elements.length + 1}`;
        const selector = await selectorFor(el);
        const selectorKey = `${frameIndex}:${selector}`;
        const selectorIndex = selectorCounts.get(selectorKey) || 0;
        selectorCounts.set(selectorKey, selectorIndex + 1);
        refs[ref] = {
          selector,
          selector_index: selectorIndex,
          frame_index: frameIndex,
          frame_url: frame.url(),
          revision: nextRevision,
          signature: item.signature,
        };
        const { signature, ...publicItem } = item;
        elements.push({
          ref,
          selector,
          frame_index: frameIndex,
          frame_url: frame.url(),
          ...publicItem,
        });
      } catch (_) {}
      }
    }
    pageRevision = nextRevision;
    state.refs = refs;
    state.page_revision = pageRevision;
    const title = await page.title().catch(() => "");
    const currentObservation = {
      url: page.url(),
      title,
      page_count: context.pages().filter((candidate) => !candidate.isClosed()).length,
      frame_count: frames.length,
      element_signature: elements.map((item) => `${item.frame_index}:${refs[item.ref]?.signature || ""}:${item.selector}`).join("\n"),
    };
    const pageChanged = Boolean(lastObservation) && JSON.stringify(lastObservation) !== JSON.stringify(currentObservation);
    let changeReason = "";
    if (pageChanged) {
      if (lastObservation.url !== currentObservation.url) changeReason = "url_changed";
      else if (lastObservation.page_count !== currentObservation.page_count) changeReason = "page_count_changed";
      else if (lastObservation.frame_count !== currentObservation.frame_count) changeReason = "frame_count_changed";
      else if (lastObservation.title !== currentObservation.title) changeReason = "title_changed";
      else changeReason = "interactive_dom_changed";
    }
    lastObservation = currentObservation;
    return {
      page_revision: pageRevision,
      viewport,
      elements,
      page_changed: pageChanged,
      change_reason: changeReason,
      page_count: currentObservation.page_count,
      frame_count: currentObservation.frame_count,
      native_dialogs: nativeDialogs.splice(0),
      visual_summary: await visualSummary(),
    };
  }

  function observationScreenshotReason(action, mode, observation) {
    const normalized = String(mode || "auto").toLowerCase();
    if (normalized === "never") return "";
    if (normalized === "always") return "requested";
    if (["open", "navigate", "click", "press", "wait"].includes(action)) {
      return `after_${action}`;
    }
    const visual = observation && observation.visual_summary ? observation.visual_summary : {};
    if (Number(visual.dialogs || 0) > 0) return "dialog_visible";
    if (Number(visual.canvases || 0) + Number(visual.svgs || 0) + Number(visual.videos || 0) > 0) {
      return "visual_surface_visible";
    }
    return "";
  }

  async function captureObservationScreenshot(action, mode, observation) {
    const reason = observationScreenshotReason(action, mode, observation);
    if (!reason || !observation || !Number(observation.page_revision || 0)) {
      return { path: "", reason: "", error: "" };
    }
    const target = path.join(
      observationsDir,
      `revision-${observation.page_revision}-${Date.now()}.jpg`
    );
    try {
      const image = await page.screenshot({
        type: "jpeg",
        quality: frameQuality,
        fullPage: false,
      });
      atomicWrite(target, image);
      const candidates = fs.readdirSync(observationsDir)
        .filter((name) => name.endsWith(".jpg"))
        .map((name) => {
          const candidatePath = path.join(observationsDir, name);
          let modifiedAt = 0;
          try { modifiedAt = fs.statSync(candidatePath).mtimeMs; } catch (_) {}
          return { name, modifiedAt };
        })
        .sort((left, right) => right.modifiedAt - left.modifiedAt);
      for (const candidate of candidates.slice(3)) {
        try { fs.unlinkSync(path.join(observationsDir, candidate.name)); } catch (_) {}
      }
      return { path: target, reason, error: "" };
    } catch (error) {
      const detail = String(error && error.message ? error.message : error);
      if (String(mode || "auto").toLowerCase() === "always") throw error;
      return { path: "", reason: "", error: detail.slice(0, 1000) };
    }
  }

  async function executeAction(payload) {
    const action = payload.action || "snapshot";
    const origin = String(payload.origin || "agent");
    if (action === "set_control") {
      const requested = String(payload.mode || "");
      if (!["agent", "human", "paused"].includes(requested)) throw new Error("invalid browser control mode");
      controlMode = requested;
      await refreshPageState({ status: "ready", action, control_mode: controlMode });
      return { ok: true, action, session, control_mode: controlMode, url: state.url || "", title: state.title || "", frame_seq: frameSeq };
    }
    if (origin !== "human" && controlMode !== "agent") {
      const error = new Error(`browser_controlled_by_${controlMode}`);
      error.code = "browser_control_paused";
      throw error;
    }
    const actionTimeoutMs = Math.max(1000, Math.min(Number(payload.timeout || config.timeout || 60) * 1000, 10 * 60 * 1000));
    context.setDefaultTimeout(actionTimeoutMs);
    let artifactPath = "";
    let text = "";
    let elements = [];
    let downloads = [];
    let observation = null;
    let observationScreenshot = { path: "", reason: "", error: "" };
    writeState({ status: "running", action, error: "" });

    const requireConfirmation = async (summary) => {
      const confirmationId = `confirm-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const publicSummary = String(summary || "Sensitive browser action").slice(0, 160);
      try { fs.unlinkSync(confirmationPath); } catch (_) {}
      writeState({ status: "paused", action, pending_confirmation: { confirmation_id: confirmationId, action, summary: publicSummary } });
      const deadline = Date.now() + confirmationTimeoutMs;
      while (Date.now() < deadline) {
        const decision = readJson(confirmationPath, {});
        if (String(decision.confirmation_id || "") === confirmationId) {
          try { fs.unlinkSync(confirmationPath); } catch (_) {}
          writeState({ status: "running", action, pending_confirmation: null, error: "" });
          if (decision.approved === true) return;
          const rejected = new Error(`confirmation_rejected: ${publicSummary}`);
          rejected.code = "confirmation_rejected";
          throw rejected;
        }
        await sleep(100);
      }
      writeState({ status: "paused", action, pending_confirmation: null });
      const timeout = new Error(`confirmation_timeout: ${publicSummary}`);
      timeout.code = "confirmation_timeout";
      throw timeout;
    };

    if (action === "open" || action === "navigate") {
      if (!payload.url) throw new Error("url is required for browser open/navigate");
      if (!/^https?:\/\//i.test(String(payload.url))) throw new Error("browser only allows http and https URLs");
      await page.goto(payload.url, { waitUntil: payload.wait_until || "networkidle", timeout: actionTimeoutMs });
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "observe" || action === "snapshot") {
      await ensurePage(payload);
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "click") {
      await ensurePage(payload);
      const locator = await resolveLocator(payload);
      const summary = await locator.evaluate((node) => String(
        node.getAttribute("aria-label") || node.getAttribute("title") || node.innerText || node.getAttribute("value") || ""
      ).trim().replace(/\s+/g, " ").slice(0, 160));
      if (/(?:pay|purchase|buy|checkout|submit|send|delete|remove|login|sign in|付款|支付|购买|结账|提交|发送|删除|登录)/i.test(summary)) {
        await requireConfirmation(summary || "Sensitive click");
      }
      await locator.click();
      await page.waitForLoadState(payload.wait_until || "networkidle", { timeout: actionTimeoutMs }).catch(() => {});
      await useNewestPage();
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "click_at") {
      await ensurePage(payload);
      if (![payload.x, payload.y].every(Number.isFinite)) throw new Error("x and y are required for click_at");
      if (origin !== "human") {
        const summary = await page.evaluate(({ x, y }) => {
          const node = document.elementFromPoint(x, y);
          return String(node && (node.getAttribute("aria-label") || node.getAttribute("title") || node.innerText || node.getAttribute("value")) || "")
            .trim().replace(/\s+/g, " ").slice(0, 160);
        }, { x: Number(payload.x), y: Number(payload.y) });
        if (/(?:pay|purchase|buy|checkout|submit|send|delete|remove|login|sign in|付款|支付|购买|结账|提交|发送|删除|登录)/i.test(summary)) {
          await requireConfirmation(summary || "Sensitive coordinate click");
        }
      }
      await page.mouse.click(Number(payload.x), Number(payload.y));
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "move") {
      await ensurePage(payload);
      if (![payload.x, payload.y].every(Number.isFinite)) throw new Error("x and y are required for move");
      await page.mouse.move(Number(payload.x), Number(payload.y));
    } else if (action === "drag") {
      await ensurePage(payload);
      if (![payload.x, payload.y, payload.end_x, payload.end_y].every(Number.isFinite)) throw new Error("start and end coordinates are required for drag");
      await page.mouse.move(Number(payload.x), Number(payload.y));
      await page.mouse.down();
      await page.mouse.move(Number(payload.end_x), Number(payload.end_y), { steps: 12 });
      await page.mouse.up();
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "scroll") {
      await ensurePage(payload);
      await page.mouse.wheel(Number(payload.delta_x || 0), Number(payload.delta_y || 0));
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "key_down") {
      await page.keyboard.down(payload.key || payload.text || "");
    } else if (action === "key_up") {
      await page.keyboard.up(payload.key || payload.text || "");
    } else if (action === "fill") {
      await ensurePage(payload);
      const locator = await resolveLocator(payload);
      const field = await locator.evaluate((node) => ({
        type: String(node.getAttribute("type") || "").toLowerCase(),
        name: String(node.getAttribute("aria-label") || node.getAttribute("name") || node.getAttribute("placeholder") || "").slice(0, 160),
      }));
      if (field.type === "password" || /password|密码/i.test(field.name)) {
        await requireConfirmation(`填写敏感字段：${field.name || "password"}`);
      }
      await withLocatorRecovery(payload, (target) => target.fill(payload.text || ""));
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "type") {
      await ensurePage(payload);
      await (await resolveLocator(payload)).pressSequentially(payload.text || "");
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "press") {
      await ensurePage(payload);
      await page.keyboard.press(payload.key || payload.text || "Enter");
      await page.waitForLoadState(payload.wait_until || "networkidle", { timeout: actionTimeoutMs }).catch(() => {});
      await useNewestPage();
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "wait") {
      await ensurePage(payload);
      if (payload.selector || payload.ref) {
        await withLocatorRecovery(payload, (target) => target.waitFor({ timeout: actionTimeoutMs }));
      }
      else if (payload.text && /^\d+$/.test(String(payload.text))) await page.waitForTimeout(Number(payload.text));
      else await page.waitForLoadState(payload.wait_until || "networkidle", { timeout: actionTimeoutMs });
      await useNewestPage();
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "screenshot") {
      await ensurePage(payload);
      const out = payload.output_path || `artifacts/browser/${session}/screenshot-${Date.now()}.png`;
      artifactPath = safeWorkspaceOutput(out, "");
      fs.mkdirSync(path.dirname(artifactPath), { recursive: true });
      await page.screenshot({ path: artifactPath, fullPage: payload.full_page !== false });
      observation = await snapshot();
      elements = observation.elements;
      observationScreenshot = {
        path: artifactPath,
        reason: "explicit_screenshot",
        error: "",
      };
    } else if (action === "pdf") {
      await ensurePage(payload);
      const out = payload.output_path || `artifacts/browser/${session}/page-${Date.now()}.pdf`;
      artifactPath = safeWorkspaceOutput(out, "");
      fs.mkdirSync(path.dirname(artifactPath), { recursive: true });
      await page.pdf({ path: artifactPath, printBackground: true });
    } else if (action === "text") {
      await ensurePage(payload);
      if (payload.selector || payload.ref) {
        text = String(await withLocatorRecovery(payload, async (target) => (
          await target.innerText({ timeout: actionTimeoutMs }).catch(() => target.textContent())
        )) || "").trim();
      } else {
        text = ((await page.locator("body").innerText({ timeout: actionTimeoutMs }).catch(async () => await page.textContent("body"))) || "").trim();
      }
    } else if (action === "get") {
      await ensurePage(payload);
      const what = String(payload.text || "").toLowerCase();
      if (what === "url") text = page.url();
      else if (what === "title") text = await page.title();
      else text = JSON.stringify({ url: page.url(), title: await page.title() }, null, 2);
    } else if (action === "download") {
      await ensurePage(payload);
      const locator = await resolveLocator(payload);
      const [download] = await Promise.all([
        page.waitForEvent("download", { timeout: actionTimeoutMs }),
        locator.click(),
      ]);
      const suggested = path.basename(download.suggestedFilename() || `download-${Date.now()}`);
      const out = payload.output_path || `downloads/browser/${session}/${suggested}`;
      artifactPath = safeWorkspaceOutput(out, "");
      fs.mkdirSync(path.dirname(artifactPath), { recursive: true });
      await download.saveAs(artifactPath);
      downloads.push(artifactPath);
      await useNewestPage();
      observation = await snapshot();
      elements = observation.elements;
    } else if (action === "close") {
      running = false;
      state.refs = {};
    } else {
      throw new Error(`unsupported browser action: ${action}`);
    }

    if (observation && !observationScreenshot.path) {
      observationScreenshot = await captureObservationScreenshot(
        action,
        payload.screenshot_mode || "auto",
        observation
      );
    }

    await refreshPageState({ status: action === "close" ? "closed" : "ready", action });
    return {
      ok: true,
      action,
      session,
      url: state.url || "",
      title: state.title || "",
      text,
      elements,
      page_revision: observation ? observation.page_revision : pageRevision,
      viewport: observation ? observation.viewport : viewport,
      visual_summary: observation ? observation.visual_summary : {},
      page_changed: observation ? observation.page_changed : false,
      change_reason: observation ? observation.change_reason : "",
      page_count: observation ? observation.page_count : context.pages().filter((candidate) => !candidate.isClosed()).length,
      frame_count: observation ? observation.frame_count : page.frames().length,
      native_dialogs: observation ? observation.native_dialogs : [],
      observation_screenshot_path: observationScreenshot.path,
      observation_screenshot_mime: observationScreenshot.path
        ? (observationScreenshot.path.endsWith(".png") ? "image/png" : "image/jpeg")
        : "",
      observation_screenshot_reason: observationScreenshot.reason,
      observation_screenshot_error: observationScreenshot.error,
      artifact_path: artifactPath,
      downloads,
      state_path: statePath,
      live_frame_path: framePath,
      frame_seq: frameSeq,
    };
  }

  process.on("SIGTERM", () => { stopRequested = true; });
  process.on("SIGINT", () => { stopRequested = true; });
  await attachScreencast();
  await refreshPageState({ status: "ready" });
  atomicWrite(readyPath, JSON.stringify({ ok: true, session, pid: process.pid, started_at: new Date().toISOString() }));

  while (running && !stopRequested) {
    const names = fs.readdirSync(requestsDir).filter((name) => name.endsWith(".json")).sort();
    if (names.length === 0) {
      if (Date.now() - lastActivityAt >= idleMs) {
        writeState({ status: "idle_timeout" });
        break;
      }
      await sleep(40);
      continue;
    }
    for (const name of names) {
      const requestPath = path.join(requestsDir, name);
      const responsePath = path.join(responsesDir, name);
      const payload = readJson(requestPath, {});
      try { fs.unlinkSync(requestPath); } catch (_) {}
      lastActivityAt = Date.now();
      try {
        const result = await executeAction(payload);
        atomicWrite(responsePath, JSON.stringify(result));
      } catch (error) {
        const detail = String(error && error.stack ? error.stack : error);
        let recoveryObservation = null;
        if (error && ["stale_ref", "ambiguous_selector"].includes(error.code)) {
          try { recoveryObservation = await snapshot(); } catch (_) {}
        }
        const recoverable = Boolean(error && ["stale_ref", "ambiguous_selector"].includes(error.code));
        const confirmationRequired = Boolean(error && error.code === "confirmation_required");
        await refreshPageState({
          status: recoverable ? "ready" : (confirmationRequired ? "paused" : "error"),
          action: payload.action || "",
          error: recoverable ? "" : detail.slice(0, 4000),
        });
        atomicWrite(responsePath, JSON.stringify({
          ok: false,
          action: payload.action || "",
          session,
          error: detail,
          error_code: String(error && error.code ? error.code : "browser_action_failed"),
          action_summary: String(error && error.actionSummary ? error.actionSummary : ""),
          current_revision: pageRevision,
          page_revision: recoveryObservation ? recoveryObservation.page_revision : pageRevision,
          elements: recoveryObservation ? recoveryObservation.elements : [],
          viewport: recoveryObservation ? recoveryObservation.viewport : viewport,
          visual_summary: recoveryObservation ? recoveryObservation.visual_summary : {},
          page_changed: recoveryObservation ? recoveryObservation.page_changed : false,
          change_reason: recoveryObservation ? recoveryObservation.change_reason : "",
          page_count: recoveryObservation ? recoveryObservation.page_count : context.pages().filter((candidate) => !candidate.isClosed()).length,
          frame_count: recoveryObservation ? recoveryObservation.frame_count : page.frames().length,
          native_dialogs: recoveryObservation ? recoveryObservation.native_dialogs : [],
          url: state.url || "",
          title: state.title || "",
          state_path: statePath,
          live_frame_path: framePath,
          frame_seq: frameSeq,
        }));
      }
      if (!running || stopRequested) break;
    }
  }

  if (cdp) await cdp.send("Page.stopScreencast").catch(() => {});
  await context.close().catch(() => {});
  if (state.status !== "closed" && state.status !== "idle_timeout") writeState({ status: "stopped" });
  cleanupSensitiveLiveState();
}

main().catch((err) => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""


_BROWSER_SESSION_LOCKS: dict[str, asyncio.Lock] = {}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name, default) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _container_workspace_path_to_host(value: Any, spec: Any) -> str:
    container_path = str(value or "").strip()
    container_workspace = str(spec.pathmap.container_workspace or "").rstrip("/")
    prefix = container_workspace + "/"
    if not container_path.startswith(prefix):
        return ""
    relative = container_path[len(prefix) :]
    workspace = spec.pathmap.host_workspace.expanduser().resolve(strict=False)
    candidate = (workspace / relative).resolve(strict=False)
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return ""
    return candidate.as_posix()


def _session_lock(workspace: Path, session: str) -> asyncio.Lock:
    key = f"{workspace.resolve(strict=False).as_posix()}::{session}"
    lock = _BROWSER_SESSION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _BROWSER_SESSION_LOCKS[key] = lock
    return lock


async def _emit_browser_session_event(
    context: ToolContext,
    *,
    browser_session_id: str,
    status: str,
    action: str,
    data: dict[str, Any] | None = None,
) -> None:
    extra = getattr(context, "extra", None)
    emitter = extra.get("__agent_event_emit__") if isinstance(extra, dict) else None
    if not callable(emitter):
        return
    details = dict(data or {})
    event = {
        "kind": "browser_session",
        "session_id": str(getattr(context, "session_id", "") or ""),
        "agent_name": str(getattr(context, "agent", "") or "browser"),
        "browser_session_id": browser_session_id,
        "workspace_id": str((extra or {}).get("workspace_id") or ""),
        "status": status,
        "action": action,
        "url": str(details.get("url") or ""),
        "title": str(details.get("title") or ""),
        "frame_seq": int(details.get("frame_seq") or 0),
        "page_revision": int(details.get("page_revision") or 0),
        "message": {
            "closed": "浏览器实时画面已关闭",
            "ready": "浏览器实时画面已连接",
            "error": "浏览器实时画面启动或操作失败",
        }.get(status, "正在启动浏览器实时画面"),
    }
    try:
        result = emitter(event)
        if hasattr(result, "__await__"):
            await result
    except Exception:
        pass


class BrowserTool(Tool):
    def available_to_agent(self, agent_name: str) -> bool:
        # 与 execute 中的专用浏览器 Agent 检查一致，发现不能扩大调用边界。
        return str(agent_name or "").strip().lower() in _browser_allowed_agents()

    @property
    def id(self) -> str:
        return "browser"

    @property
    def description(self) -> str:
        if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
            return """Control Tcode's own isolated Chromium browser window, never the user's local browser.
Supported actions: open/navigate (url required), observe/snapshot/screenshot, fill/type (text plus selector or ref), click (selector or ref), press (key), scroll (delta_y), back/forward/reload, close.
Start with open, inspect returned text/elements and screenshot, then act. Refs require the exact returned page_revision; observe again if stale. Search inputs are supported; sensitive actions require user takeover. Downloads, popups and local/private-network URLs are blocked. Inspect the actual result after every action; never describe a planned action as completed.
This is a sandboxed Chromium renderer with an isolated temporary profile, not a Linux container. Other actions in the shared schema are unsupported on desktop."""
        return """Control a sandboxed browser using Playwright.

Use this for web interaction that needs a real browser: navigate, inspect interactive elements,
click, fill, type, wait, screenshot, save PDF, extract text, and download files. For visual
surfaces, use click_at, move, drag, scroll, key_down, and key_up with viewport coordinates.
Prefer observe for structured page state. Refs returned by an observation are valid only with
that observation's page_revision; observe again after stale_ref. Observations include iframe and
open Shadow DOM elements, reject ambiguous raw selectors, and report page/frame/interactive DOM
changes. screenshot_mode controls whether a bounded visual observation is also provided to the
model. Sensitive actions pause for explicit user confirmation; do not bypass that pause by changing
action type.
This tool is designed for Docker + gVisor browser sandboxes and does not control the host browser."""

    @property
    def parameters(self) -> type:
        return BrowserParameters

    def _build_live_worker_spec(
        self,
        *,
        context: ToolContext,
        session: str,
        parsed: BrowserParameters,
        sandbox_config,
    ):
        idle_seconds = _env_int(
            "TCODE_BROWSER_SESSION_IDLE_SECONDS",
            1800,
            minimum=30,
            maximum=24 * 3600,
        )
        worker_config = {
            "session": session,
            "agent_session_id": str(getattr(context, "session_id", "") or ""),
            "workspace_id": str(((context.extra or {}) if context else {}).get("workspace_id") or ""),
            "timeout": parsed.timeout or 60,
            "idle_seconds": idle_seconds,
            "confirmation_timeout_seconds": _env_int(
                "TCODE_BROWSER_CONFIRMATION_TIMEOUT_SECONDS",
                300,
                minimum=30,
                maximum=1800,
            ),
            "frame_fps": _env_int(
                "TCODE_BROWSER_LIVE_FPS", 5, minimum=1, maximum=12
            ),
            "frame_quality": _env_int(
                "TCODE_BROWSER_LIVE_JPEG_QUALITY", 60, minimum=25, maximum=85
            ),
            "width": parsed.width or 1440,
            "height": parsed.height or 1000,
        }
        extra_env = {
            "TCODE_BROWSER_WORKER_CONFIG": json.dumps(
                worker_config, ensure_ascii=False
            )
        }
        if _browser_node_path():
            extra_env["NODE_PATH"] = _browser_node_path()
        if _browser_browsers_path():
            extra_env["PLAYWRIGHT_BROWSERS_PATH"] = _browser_browsers_path()
        network_override = _browser_network_override()
        if network_override.strip().lower() == "egress":
            extra_env.update(_browser_egress_proxy_env())
        command = "node <<'NODE'\n" + _browser_worker_node_script() + "\nNODE"
        runtime_context = _browser_runtime_context(context)
        spec = build_tool_sandbox_spec(
            tool_name=self.id,
            command=command,
            requested_workdir=None,
            timeout_seconds=idle_seconds + 120,
            context=runtime_context,
            extra_env=extra_env,
            sandbox_profile=_browser_profile_name(),
            metadata={
                "browser_action": "live_worker",
                "browser_session": session,
                "browser_live": True,
            },
        )
        base_image = sandbox_config.image
        if (
            not spec.image
            or not bool(spec.metadata.get("sandbox_profile_found"))
            or spec.image == base_image
        ):
            spec = replace(spec, image=_browser_image())
        if network_override:
            spec = replace(spec, network=network_override)
        return replace(
            spec,
            lifecycle="persistent",
            home=f"{spec.pathmap.container_workspace}/.tcode-runtime/browser-home",
            idle_stop_seconds=idle_seconds,
            retention_seconds=max(idle_seconds, 3600),
            mounts=[],
        )

    async def _ensure_live_worker(
        self,
        *,
        context: ToolContext,
        session: str,
        parsed: BrowserParameters,
        sandbox_config,
    ):
        spec = self._build_live_worker_spec(
            context=context,
            session=session,
            parsed=parsed,
            sandbox_config=sandbox_config,
        )
        workspace = spec.pathmap.host_workspace.expanduser().resolve(strict=False)
        paths = browser_live_paths(workspace, session)
        paths.requests.mkdir(parents=True, exist_ok=True)
        paths.responses.mkdir(parents=True, exist_ok=True)
        runtime = get_sandbox_runtime()
        async with _session_lock(workspace, session):
            runtime_info = await asyncio.to_thread(_read_json_file, paths.runtime)
            existing_job_id = str(runtime_info.get("sandbox_job_id") or "").strip()
            if existing_job_id:
                try:
                    status = await runtime.job_status(_browser_job_ref(runtime_info))
                    if status.status == "running" and paths.ready.is_file():
                        return runtime, spec, runtime_info, paths
                except Exception:
                    pass

            for stale_path in (paths.ready, paths.frame):
                try:
                    stale_path.unlink()
                except FileNotFoundError:
                    pass
            for stale_dir in (paths.requests, paths.responses):
                for stale_path in stale_dir.glob("*.json"):
                    try:
                        stale_path.unlink()
                    except FileNotFoundError:
                        pass
            await asyncio.to_thread(
                _atomic_write_json,
                paths.state,
                {
                    "browser_session_id": session,
                    "agent_session_id": str(
                        getattr(context, "session_id", "") or ""
                    ),
                    "workspace_id": str((context.extra or {}).get("workspace_id") or ""),
                    "status": "starting",
                    "frame_seq": 0,
                    "frame_mime": "image/jpeg",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            job = await runtime.start_job(spec)
            runtime_info = {
                "sandbox_job_id": job.sandbox_job_id,
                "container_id": job.container_id,
                "container_name": job.container_name,
                "runtime_id": str((job.metadata or {}).get("runtime_id") or ""),
                "execution_id": str((job.metadata or {}).get("execution_id") or ""),
                "started_at": time.time(),
            }
            await asyncio.to_thread(_atomic_write_json, paths.runtime, runtime_info)

            startup_timeout = _env_int(
                "TCODE_BROWSER_LIVE_STARTUP_TIMEOUT_SECONDS",
                45,
                minimum=5,
                maximum=120,
            )
            deadline = time.monotonic() + startup_timeout
            last_status_check = 0.0
            while time.monotonic() < deadline:
                if paths.ready.is_file():
                    return runtime, spec, runtime_info, paths
                now = time.monotonic()
                if now - last_status_check >= 1.0:
                    last_status_check = now
                    status = await runtime.job_status(job)
                    if status.status != "running":
                        output = await runtime.job_output(job, lines=200)
                        raise RuntimeError(
                            output.strip()
                            or f"browser worker stopped during startup: {status.status}"
                        )
                await asyncio.sleep(0.1)

            try:
                output = await runtime.job_output(job, lines=200)
                await runtime.terminate_job(job)
            except Exception:
                output = ""
            raise RuntimeError(
                output.strip()
                or f"browser worker did not become ready within {startup_timeout} seconds"
            )

    async def _execute_live_request(
        self,
        *,
        runtime,
        runtime_info: dict[str, Any],
        paths,
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        request_id = f"req-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
        request_path = paths.requests / f"{request_id}.json"
        response_path = paths.responses / f"{request_id}.json"
        await asyncio.to_thread(_atomic_write_json, request_path, payload)
        deadline = time.monotonic() + max(1, timeout_seconds)
        last_status_check = 0.0
        while time.monotonic() < deadline:
            if response_path.is_file():
                data = await asyncio.to_thread(_read_json_file, response_path)
                try:
                    response_path.unlink()
                except FileNotFoundError:
                    pass
                if not data.get("ok"):
                    raise BrowserActionError(data)
                if str(payload.get("action") or "") == "close":
                    job_id = str(runtime_info.get("sandbox_job_id") or "")
                    if job_id:
                        try:
                            await runtime.terminate_job(_browser_job_ref(runtime_info))
                        except Exception:
                            pass
                    try:
                        paths.ready.unlink()
                    except FileNotFoundError:
                        pass
                return data
            now = time.monotonic()
            if now - last_status_check >= 1.0:
                last_status_check = now
                job_id = str(runtime_info.get("sandbox_job_id") or "")
                if job_id:
                    status = await runtime.job_status(_browser_job_ref(runtime_info))
                    if status.status != "running":
                        output = await runtime.job_output(_browser_job_ref(runtime_info), lines=200)
                        raise RuntimeError(
                            output.strip()
                            or f"browser worker stopped: {status.status}"
                        )
            await asyncio.sleep(0.05)
        raise TimeoutError(
            f"browser action timed out after {max(1, timeout_seconds)} seconds"
        )

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = BrowserParameters(**args)
        if not _browser_agent_allowed(context):
            return ToolResult(
                title=f"browser {parsed.action}",
                output="",
                error=(
                    "Browser tool is restricted to the dedicated browser agent. "
                    "Delegate this request with subagent_type=agent-browser."
                ),
                metadata={
                    "browser_agent_required": True,
                    "execution_skipped": True,
                    "execution_environment": {
                        "location": "not_executed",
                        "label": "Not executed",
                        "sandboxed": False,
                    },
                    "required_execution_environment": {
                        "location": "sandbox",
                        "sandboxed": True,
                        "agent": "browser",
                        "profile": _browser_profile_name(),
                        "image": _browser_image(),
                        "network": _browser_network_override() or "egress",
                    },
                },
            )
        if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
            from ...browser.desktop import execute_desktop_browser
            return await execute_desktop_browser(parsed.model_dump(), context)
        if not sandbox_session_enabled(context):
            return ToolResult(
                title="browser",
                output="",
                error=(
                    "Browser tool requires sandbox mode. Set TCODE_SANDBOX_ENABLED=true "
                    "on the Linux server and configure a Sandbox Runner with a Playwright browser image."
                ),
                metadata={"sandbox_required": True},
            )

        if parsed.url:
            try:
                _validate_browser_url(parsed.url)
            except ValueError as exc:
                return ToolResult(
                    title=f"browser {parsed.action}",
                    output="",
                    error=str(exc),
                    metadata={"sandbox_required": True, "browser_url_blocked": True},
                )

        sandbox_config = load_sandbox_config()
        require_runner = str(
            os.environ.get("TCODE_BROWSER_REQUIRE_RUNNER", "true") or "true"
        ).strip().lower() not in {"0", "false", "no", "off"}
        if require_runner and not sandbox_config.runner_url:
            return ToolResult(
                title=f"browser {parsed.action}",
                output="",
                error=(
                    "Browser requires the isolated sandbox runner, but "
                    "TCODE_SANDBOX_RUNNER_URL is empty."
                ),
                metadata={"sandbox_required": True, "runner_required": True},
            )

        session = _safe_session_name(parsed.session or getattr(context, "session_id", "") or "default")
        timestamp = int(time.time() * 1000)
        default_ext = "pdf" if parsed.action == "pdf" else "png"
        default_output = f"artifacts/browser/{session}/{parsed.action}-{timestamp}.{default_ext}"
        if parsed.action in {"screenshot", "pdf"}:
            output_path = _safe_relative_path(parsed.output_path or "", default_output)
        elif parsed.action == "download":
            output_path = _safe_relative_path(parsed.output_path, "") if parsed.output_path else ""
        else:
            output_path = ""
        payload = {
            "action": parsed.action,
            "url": parsed.url,
            "selector": parsed.selector,
            "ref": parsed.ref,
            "page_revision": parsed.page_revision,
            "text": parsed.text,
            "key": parsed.key,
            "x": parsed.x,
            "y": parsed.y,
            "end_x": parsed.end_x,
            "end_y": parsed.end_y,
            "delta_x": parsed.delta_x,
            "delta_y": parsed.delta_y,
            "origin": "agent",
            "session": session,
            "output_path": output_path,
            "full_page": parsed.full_page,
            "wait_until": parsed.wait_until,
            "timeout": parsed.timeout,
            "width": parsed.width,
            "height": parsed.height,
            "screenshot_mode": parsed.screenshot_mode,
        }
        spec = None
        runtime_info: dict[str, Any] = {}
        paths = browser_live_paths(_context_workdir(context), session)
        await _emit_browser_session_event(
            context,
            browser_session_id=session,
            status="starting",
            action=parsed.action,
        )
        try:
            runtime, spec, runtime_info, paths = await self._ensure_live_worker(
                context=context,
                session=session,
                parsed=parsed,
                sandbox_config=sandbox_config,
            )
            data = await self._execute_live_request(
                runtime=runtime,
                runtime_info=runtime_info,
                paths=paths,
                payload=payload,
                timeout_seconds=max(
                    1,
                    int(parsed.timeout or 60),
                    _env_int(
                        "TCODE_BROWSER_CONFIRMATION_TIMEOUT_SECONDS",
                        300,
                        minimum=30,
                        maximum=1800,
                    ) + 5,
                ),
            )
        except Exception as exc:
            error_text = str(exc)
            failure_data = (
                dict(exc.payload)
                if isinstance(exc, BrowserActionError)
                else {}
            )
            recoverable = failure_data.get("error_code") in {
                "stale_ref",
                "ambiguous_selector",
            }
            confirmation_required = failure_data.get("error_code") == "confirmation_required"
            try:
                failed_state = await asyncio.to_thread(_read_json_file, paths.state)
                await asyncio.to_thread(
                    _atomic_write_json,
                    paths.state,
                    {
                        **failed_state,
                        "browser_session_id": session,
                        "agent_session_id": str(
                            getattr(context, "session_id", "") or ""
                        ),
                        "workspace_id": str(
                            (context.extra or {}).get("workspace_id") or ""
                        ),
                        "status": "ready" if recoverable else ("paused" if confirmation_required else "error"),
                        "action": parsed.action,
                        "error": "" if recoverable else error_text[:4000],
                        "updated_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    },
                )
            except Exception:
                pass
            await _emit_browser_session_event(
                context,
                browser_session_id=session,
                status="ready" if recoverable else ("paused" if confirmation_required else "error"),
                action=parsed.action,
                data={**failure_data, "error": error_text},
            )
            effective_image = spec.image if spec is not None else _browser_image()
            effective_network = (
                spec.network
                if spec is not None
                else (_browser_network_override() or "egress")
            )
            profile = _browser_profile_name()
            return ToolResult(
                title=f"browser {parsed.action}",
                output=self._format_output(failure_data) if failure_data else "",
                error=f"Browser sandbox execution failed: {error_text}",
                metadata={
                    "sandbox": True,
                    "browser": True,
                    "image": effective_image,
                    "network": effective_network,
                    "sandbox_image": effective_image,
                    "sandbox_network": effective_network,
                    "sandbox_profile": profile,
                    "sandbox_runner_url": sandbox_config.runner_url,
                    "browser_live": True,
                    "browser_session_id": session,
                    "browser_error_code": str(
                        failure_data.get("error_code") or "browser_action_failed"
                    ),
                    "human_review_required": failure_data.get("error_code") == "confirmation_required",
                    "browser_action_summary": str(failure_data.get("action_summary") or ""),
                    "page_revision": int(
                        failure_data.get("page_revision")
                        or failure_data.get("current_revision")
                        or 0
                    ),
                    "execution_environment": {
                        "location": "sandbox",
                        "label": "Sandbox",
                        "sandboxed": True,
                        "image": effective_image,
                        "network": effective_network,
                        "profile": profile,
                        "runner_url": sandbox_config.runner_url,
                    },
                },
            )

        await _emit_browser_session_event(
            context,
            browser_session_id=session,
            status="closed" if parsed.action == "close" else "ready",
            action=parsed.action,
            data=data,
        )

        output = "\n".join(
            [
                "execution_environment: sandbox",
                f"image: {spec.image}",
                f"network: {spec.network}",
                self._format_output(data),
            ]
        ).strip()
        artifact_path = str(data.get("artifact_path") or "")
        host_output_path = _container_workspace_path_to_host(artifact_path, spec)
        observation_screenshot_path = str(
            data.get("observation_screenshot_path") or ""
        )
        host_observation_screenshot_path = _container_workspace_path_to_host(
            observation_screenshot_path,
            spec,
        )

        return ToolResult(
            title=f"browser {parsed.action}",
            output=output,
            output_path=host_output_path or None,
            metadata={
                "sandbox": True,
                "browser": True,
                "image": spec.image,
                "network": spec.network,
                "sandbox_job_id": str(runtime_info.get("sandbox_job_id") or ""),
                "container_id": str(runtime_info.get("container_id") or ""),
                "container_name": str(runtime_info.get("container_name") or ""),
                "session": session,
                "browser_session_id": session,
                "browser_live": True,
                "browser_live_frame_path": str(data.get("live_frame_path") or ""),
                "browser_live_frame_seq": int(data.get("frame_seq") or 0),
                "page_revision": int(data.get("page_revision") or 0),
                "url": data.get("url", ""),
                "title": data.get("title", ""),
                "artifact_path": artifact_path,
                "host_artifact_path": host_output_path,
                "observation_screenshot_path": observation_screenshot_path,
                "host_observation_screenshot_path": host_observation_screenshot_path,
                "observation_screenshot_mime": str(
                    data.get("observation_screenshot_mime") or ""
                ),
                "observation_screenshot_reason": str(
                    data.get("observation_screenshot_reason") or ""
                ),
                "model_image_path": host_observation_screenshot_path,
                "model_image_mime": str(
                    data.get("observation_screenshot_mime") or ""
                ),
                "state_path": data.get("state_path", ""),
                "sandbox_image": spec.image,
                "sandbox_network": spec.network,
                "sandbox_profile": _browser_profile_name(),
                "sandbox_runner_url": sandbox_config.runner_url,
            },
        )

    def _format_output(self, data: dict[str, Any]) -> str:
        lines = [
            f"action: {data.get('action', '')}",
            f"url: {data.get('url', '')}",
            f"title: {data.get('title', '')}",
            f"page_revision: {int(data.get('page_revision') or data.get('current_revision') or 0)}",
        ]
        error_code = str(data.get("error_code") or "").strip()
        if error_code:
            lines.append(f"error_code: {error_code}")
        viewport = data.get("viewport")
        if isinstance(viewport, dict) and viewport:
            lines.append(
                "viewport: "
                f"{int(viewport.get('width') or 0)}x{int(viewport.get('height') or 0)}"
            )
        if data.get("page_changed"):
            lines.append(
                f"page_changed: true ({str(data.get('change_reason') or 'changed')})"
            )
        page_count = int(data.get("page_count") or 0)
        frame_count = int(data.get("frame_count") or 0)
        if page_count or frame_count:
            lines.append(f"pages: {page_count}, frames: {frame_count}")
        native_dialogs = data.get("native_dialogs")
        if isinstance(native_dialogs, list) and native_dialogs:
            lines.append(
                "native_dialogs: "
                + "; ".join(
                    f"{str(item.get('type') or 'dialog')}={shlex.quote(str(item.get('message') or ''))}"
                    for item in native_dialogs[:5]
                    if isinstance(item, dict)
                )
            )
        visual_summary = data.get("visual_summary")
        if isinstance(visual_summary, dict) and visual_summary:
            lines.append(
                "visual_summary: "
                + ", ".join(
                    f"{key}={int(visual_summary.get(key) or 0)}"
                    for key in ("images", "canvases", "svgs", "videos", "dialogs")
                )
            )
        artifact_path = str(data.get("artifact_path") or "")
        if artifact_path:
            lines.append(f"artifact_path: {artifact_path}")
        observation_path = str(data.get("observation_screenshot_path") or "")
        if observation_path:
            lines.append(f"observation_screenshot_path: {observation_path}")
            lines.append(
                "observation_screenshot_reason: "
                f"{data.get('observation_screenshot_reason', '')}"
            )
        observation_error = str(data.get("observation_screenshot_error") or "")
        if observation_error:
            lines.append(f"observation_screenshot_error: {observation_error[:1000]}")
        text = str(data.get("text") or "")
        if text:
            lines.append("")
            lines.append(text[:12000])
        elements = data.get("elements")
        if isinstance(elements, list) and elements:
            lines.append("")
            lines.append("interactive_elements:")
            for item in elements[:80]:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("name") or item.get("text") or item.get("role") or "").strip()
                tag = str(item.get("tag") or "").strip()
                typ = str(item.get("type") or "").strip()
                role = str(item.get("role") or "").strip()
                ref = str(item.get("ref") or "").strip()
                selector = str(item.get("selector") or "").strip()
                frame_index = int(item.get("frame_index") or 0)
                type_text = f" type={typ}" if typ else ""
                role_text = f" role={role}" if role else ""
                label_text = f" {shlex.quote(label)}" if label else ""
                states = []
                if item.get("enabled") is False:
                    states.append("disabled")
                if item.get("editable") is True:
                    states.append("editable")
                if item.get("checked") is True:
                    states.append("checked")
                state_text = f" [{' '.join(states)}]" if states else ""
                frame_text = f" frame={frame_index}" if frame_index else ""
                shadow_text = " shadow" if item.get("shadow_dom") else ""
                lines.append(
                    f"- {ref} {tag}{type_text}{role_text}{state_text}{frame_text}{shadow_text}"
                    f"{label_text} selector={selector}"
                )
        return "\n".join(lines).strip()

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
