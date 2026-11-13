"""Free layout fragments with local, platform-owned interactions only."""
import re
from html import escape
from html.parser import HTMLParser

TAGS = set("div span p section article header footer main aside nav h1 h2 h3 h4 h5 h6 strong em small b i ul ol li dl dt dd details summary button label input select option output progress table thead tbody tr th td caption figure figcaption svg g path rect circle ellipse line polyline polygon text title br hr".split())
VOID = {"input", "br", "hr"}
ATTRS = set("id class role title style aria-label aria-labelledby aria-describedby aria-expanded aria-pressed aria-live aria-hidden tabindex type value min max step checked disabled selected for hidden open width height viewbox d x y x1 y1 x2 y2 cx cy r rx ry points fill stroke stroke-width opacity transform text-anchor data-ui-action data-ui-target data-ui-value".split())


def validate_css(value: str) -> str:
    # No imports, URLs or escaped spellings; resources cannot leave the sandbox.
    if re.search(r"url\s*\(|@import|@namespace|expression\s*\(|</|[\\\x00]", value, re.I):
        raise ValueError("unsafe_html_css")
    if re.search(r"--ui-[a-z-]+\s*:", value, re.I):
        raise ValueError("reserved_html_theme_variable")
    return value


class Fragment(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []
        self.ids = set()
        self.targets = []
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if tag not in TAGS or len(self.stack) > 40 or self.nodes > 2000:
            raise ValueError("unsupported_html_tag")
        names = set()
        safe = []
        for name, value in attrs:
            if name not in ATTRS or name in names:
                raise ValueError("unsupported_html_attribute")
            names.add(name)
            value = value or ""
            if name == "style": validate_css(value)
            if name == "id":
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,95}", value) or value in self.ids:
                    raise ValueError("invalid_html_id")
                self.ids.add(value)
            if name == "data-ui-action" and value not in {"toggle", "select", "input"}:
                raise ValueError("unsupported_html_action")
            if name == "data-ui-target": self.targets.append(value)
            if tag == "input" and name == "type" and value not in {"range", "number", "text", "checkbox", "radio"}:
                raise ValueError("unsupported_html_input")
            safe.append(f' {name}="{escape(value, quote=True)}"')
        self.parts.append("<" + tag + "".join(safe) + ">")
        if tag not in VOID: self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID: self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("unbalanced_html")
        self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        self.parts.append(escape(data))

    def handle_decl(self, decl):
        raise ValueError("html_fragment_required")


def validate_html(value: str) -> str:
    parser = Fragment()
    parser.feed(value)
    parser.close()
    if parser.stack or not parser.parts:
        raise ValueError("unbalanced_html")
    if any(target not in parser.ids for target in parser.targets):
        raise ValueError("invalid_html_target")
    return "".join(parser.parts)
