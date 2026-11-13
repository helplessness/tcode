---
name: agent-browser
description: "Use when the user needs a real browser: open a website, inspect interactive elements, click, fill forms, type, wait for page changes, take screenshots, save PDFs, download files, or test a web app. This skill uses the native sandboxed browser tool, not host Chrome and not Bash-based browser automation."
allowed-tools: browser
routing:
  domains: [browser-automation]
  intents: [browser-control, web-app-testing, interactive-download]
  aliases: [browser, 浏览器, 打开网页, 点击网页, 填表, 截图, download file, save PDF, web app test]
  positive_examples:
    - 打开网页并点击登录按钮
    - 测试本地 web app 的交互流程
    - 用浏览器下载网页里的 PDF
  negative_examples:
    - 搜索论文并下载 PDF
    - 直接处理本地 PDF
    - 使用 Chrome 用户现有登录态
  required_inputs: [url]
  conflicts_with: [paper-literature-search, pdf]
  skill_role: browser
  priority: 60
---

# Sandboxed Browser Automation

Use the native `browser` tool for all browser work. Do not use `bash`, `npx agent-browser`, host Chrome, CDP ports, or a browser MCP unless the user explicitly asks for a different backend.

The browser runs in a Docker + gVisor Playwright container and stores all state and artifacts under the current workspace:

- `/workspace/.tcode/browser/`
- `/workspace/artifacts/browser/`
- `/workspace/downloads/browser/`

Each named browser session keeps one Chromium context alive between actions. While it is active,
the TCode UI automatically shows an authenticated live view backed by JPEG frames. The user can
take control from that panel, send mouse, scroll, drag and keyboard input, then return control to
the Agent. There is no exposed CDP port. Use the `close` action when the task no longer needs that
session; otherwise it stops after the configured idle timeout.

## Core Workflow

1. Open or navigate to the page:

```json
{"action":"open","url":"https://example.com","session":"default"}
```

2. Observe the rendered page and available elements:

```json
{"action":"observe","session":"default"}
```

The observation returns a `page_revision`, refs like `@e1`, `@e2`, element roles,
accessible names and states. It includes elements from same-origin and cross-origin
iframes, and Playwright pierces open Shadow DOM. `snapshot` remains a compatibility
alias. Prefer refs over CSS selectors and use them only with the revision that produced
them. If a raw selector matches multiple visible elements, observe and choose a ref.

3. Interact:

```json
{"action":"click","ref":"@e1","page_revision":1,"session":"default"}
{"action":"fill","ref":"@e2","page_revision":2,"text":"user@example.com","session":"default"}
{"action":"press","key":"Enter","session":"default"}
```

Every mutating action returns a fresh observation and a new `page_revision`.
Always use the newest returned revision. If an action returns `stale_ref`, use the
recovery observation from that result and retry the intended action at most once.
`page_changed`, `change_reason`, `page_count`, and `frame_count` describe navigation,
new tabs, iframe changes, and interactive DOM changes. Fill, wait, and text extraction
retry once when Playwright reports a transient detached-frame/element error; clicks,
typing, submissions, and downloads are never automatically repeated.

4. Re-observe when page state is unclear or before making a consequential choice:

```json
{"action":"observe","session":"default"}
```

5. Close the persistent session when the browser portion of the task is complete:

```json
{"action":"close","session":"default"}
```

## Human Control And Confirmation

- When the user takes control, stop issuing browser actions and ask them to return control when
  they are finished. Continue from a fresh `observe` after control returns.
- Sensitive actions such as login, payment, submission, sending, deletion, and password entry
  pause automatically for confirmation in the live panel. Do not retry the action or switch to a
  different action to bypass the pause.
- After the user confirms, the original browser action resumes automatically. After rejection or
  confirmation timeout, report that outcome instead of claiming the action completed.
- WebSocket reconnection restores the current control mode and pending confirmation. Do not start
  a replacement browser session merely because the live panel briefly disconnected.

## Common Actions

Navigate:

```json
{"action":"open","url":"https://example.com","wait_until":"networkidle","session":"default"}
```

Click:

```json
{"action":"click","ref":"@e1","page_revision":1,"session":"default"}
```

Fill:

```json
{"action":"fill","ref":"@e2","page_revision":2,"text":"hello","session":"default"}
```

Extract visible text:

```json
{"action":"text","selector":"body","session":"default"}
```

Get current URL or title:

```json
{"action":"get","text":"url","session":"default"}
{"action":"get","text":"title","session":"default"}
```

Screenshot:

```json
{"action":"screenshot","output_path":"artifacts/browser/default/page.png","full_page":true,"session":"default"}
```

Save PDF:

```json
{"action":"pdf","output_path":"artifacts/browser/default/page.pdf","session":"default"}
```

Download by clicking an element:

```json
{"action":"download","ref":"@e3","page_revision":3,"session":"default"}
```

Wait:

```json
{"action":"wait","wait_until":"networkidle","session":"default"}
{"action":"wait","ref":"@e1","page_revision":1,"session":"default"}
{"action":"wait","text":"2000","session":"default"}
```

## Safety Rules

- Never control the user's host browser for untrusted tasks.
- Never ask for or expose browser profile paths, cookies, SSH keys, API keys, or host credentials.
- Keep downloads, screenshots, PDFs, and extracted artifacts in `/workspace`.
- Do not paste secrets into forms unless the user explicitly provided them for this task and approved the site.
- If a site requires login, ask the user for the intended authentication method before proceeding.
- Treat browser networking as high risk; for production, it should go through a controlled proxy or egress gateway.

## Observation And Screenshot Rules

- Use `observe` for normal page understanding. Do not infer a clickable target from an old screenshot.
- `screenshot_mode="auto"` is the default. It attaches a temporary viewport image after navigation,
  clicks, key submission and waits, or when dialogs/visual surfaces require visual verification.
- Use `screenshot_mode="always"` only when layout, overlap, canvas, charts or image content matters.
- Use `screenshot_mode="never"` for text-only steps when minimizing latency is more important.
- Automatic observation screenshots are temporary and are removed when the browser session closes.
  An explicit `screenshot` action creates a persistent artifact instead.
- Password and other editable input values must never be repeated from an observation. Only the
  element's role, accessible name and state may be used for targeting.
- Verify completion from a fresh observation after navigation, submission, or any page-changing action.
