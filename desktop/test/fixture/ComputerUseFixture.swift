import AppKit

final class FixtureDelegate: NSObject, NSApplicationDelegate {
    private let query = NSTextField(string: "")
    private let status = NSTextField(labelWithString: "idle")
    private let interactionStatus = NSTextField(labelWithString: "interaction:idle")
    private let focusStatus = NSTextField(labelWithString: "focus:unknown")
    private var window: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 420),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "TCode Computer Fixture"
        self.window = window
        window.center()

        query.frame = NSRect(x: 40, y: 330, width: 320, height: 28)
        query.placeholderString = "fixture query"
        query.setAccessibilityIdentifier("fixture-query")
        query.target = self
        query.action = #selector(searchQuery)

        let submit = NSButton(title: "Apply", target: self, action: #selector(applyQuery))
        submit.frame = NSRect(x: 375, y: 328, width: 100, height: 32)
        submit.setAccessibilityIdentifier("fixture-apply")

        status.frame = NSRect(x: 40, y: 285, width: 435, height: 28)
        status.setAccessibilityIdentifier("fixture-status")

        let modal = NSButton(title: "Show modal", target: self, action: #selector(showModal))
        modal.frame = NSRect(x: 40, y: 55, width: 120, height: 32)
        modal.setAccessibilityIdentifier("fixture-modal")

        let reset = NSButton(title: "Reset", target: self, action: #selector(reset))
        reset.frame = NSRect(x: 175, y: 55, width: 100, height: 32)
        reset.setAccessibilityIdentifier("fixture-reset")

        let selectable = NSButton(title: "Selectable item", target: self, action: #selector(selectItem))
        selectable.frame = NSRect(x: 40, y: 230, width: 160, height: 32)
        selectable.setAccessibilityIdentifier("fixture-selectable")
        let contextMenu = NSMenu(title: "Fixture actions")
        let enabled = contextMenu.addItem(withTitle: "Choose enabled", action: #selector(chooseContextItem), keyEquivalent: "")
        enabled.target = self
        let disabled = contextMenu.addItem(withTitle: "Unavailable", action: nil, keyEquivalent: "")
        disabled.isEnabled = false
        let more = NSMenuItem(title: "More", action: nil, keyEquivalent: "")
        let submenu = NSMenu(title: "More")
        let nested = submenu.addItem(withTitle: "Nested choice", action: #selector(chooseNestedItem), keyEquivalent: "")
        nested.target = self
        contextMenu.setSubmenu(submenu, for: more)
        contextMenu.addItem(more)
        selectable.menu = contextMenu

        let popup = NSPopUpButton(frame: NSRect(x: 225, y: 230, width: 160, height: 32), pullsDown: false)
        popup.addItems(withTitles: ["First option", "Second option", "Third option"])
        popup.target = self
        popup.action = #selector(selectPopup(_:))
        popup.setAccessibilityIdentifier("fixture-popup")

        interactionStatus.frame = NSRect(x: 40, y: 185, width: 435, height: 28)
        interactionStatus.setAccessibilityIdentifier("fixture-interaction-status")
        focusStatus.frame = NSRect(x: 40, y: 145, width: 435, height: 28)
        focusStatus.setAccessibilityIdentifier("fixture-focus-status")
        Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
            self.focusStatus.stringValue = "front_pid:\(pid)|key:\(window.isKeyWindow)|query_focused:\(self.query.currentEditor() != nil)"
        }

        let spinner = NSProgressIndicator(frame: NSRect(x: 420, y: 230, width: 24, height: 24))
        spinner.style = .spinning
        spinner.setAccessibilityIdentifier("fixture-background-animation")
        spinner.startAnimation(nil)

        for view in [query, submit, status, selectable, popup, interactionStatus, focusStatus, spinner, modal, reset] {
            window.contentView?.addSubview(view)
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func applyQuery() {
        status.stringValue = "verified:\(query.stringValue)"
        window?.title = "TCode Computer Fixture — verified:\(query.stringValue)"
    }
    @objc private func searchQuery() {
        status.stringValue = "results:\(query.stringValue)"
        window?.title = "TCode Computer Fixture — results:\(query.stringValue)"
    }
    @objc private func reset() {
        query.stringValue = ""; status.stringValue = "idle"
        interactionStatus.stringValue = "interaction:idle"
        window?.title = "TCode Computer Fixture"
    }
    @objc private func selectItem() {
        let clicks = NSApp.currentEvent?.clickCount ?? 1
        interactionStatus.stringValue = clicks >= 2 ? "interaction:opened" : "interaction:selected"
    }
    @objc private func selectPopup(_ sender: NSPopUpButton) {
        interactionStatus.stringValue = "dropdown:\(sender.titleOfSelectedItem ?? "unknown")"
    }
    @objc private func chooseContextItem() {
        interactionStatus.stringValue = "context:enabled"
    }
    @objc private func chooseNestedItem() {
        interactionStatus.stringValue = "context:nested"
    }
    @objc private func showModal() {
        let alert = NSAlert()
        alert.messageText = "Fixture modal"
        alert.informativeText = "No external side effects"
        alert.addButton(withTitle: "Close")
        alert.runModal()
    }
}

let app = NSApplication.shared
let delegate = FixtureDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
