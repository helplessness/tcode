"""SVG art with closed, output-driven bindings; no generated executable code."""
import math
import re

from .canvas import NS, validate_svg

NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"


def path_tokens(value: str, *, morph=False):
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|" + NUMBER, value)
    if not tokens or re.sub(r"[\s,]", "", value) != "".join(tokens) or tokens[0] not in ('M', 'm'):
        raise ValueError('invalid_vector_path')
    command = None
    count = 0
    arity = {'M': 2, 'L': 2, 'H': 1, 'V': 1, 'C': 6, 'S': 4, 'Q': 4, 'T': 2, 'A': 7, 'Z': 0}
    for token in [*tokens, 'M']:
        if token.isalpha():
            if command is not None:
                size = arity[command.upper()]
                if (size and (not count or count % size)) or (not size and count):
                    raise ValueError('invalid_vector_path')
            command, count = token, 0
            if morph and token.upper() == 'A':
                raise ValueError('arc_morph_not_supported')
        else:
            value_number = float(token)
            if not math.isfinite(value_number) or abs(value_number) > 100000:
                raise ValueError('invalid_vector_path_number')
            if command.upper() == 'A' and count % 7 in (3, 4) and value_number not in (0, 1):
                raise ValueError('invalid_vector_arc_flag')
            count += 1
    if len(tokens) > 2000:
        raise ValueError('vector_path_limit')
    return tokens


def vector_source(source: str):
    normalized, root = validate_svg(source)
    if len(list(root.iter())) > 256:
        raise ValueError('vector_element_limit')
    view = [float(v) for v in re.split(r'[\s,]+', root.get('viewBox', '').strip()) if v]
    if len(view) != 4 or view[2] <= 0 or view[3] <= 0:
        raise ValueError('vector_viewbox_required')
    for node in root.iter():
        if node.get('d'):
            path_tokens(node.get('d'))
    return normalized, root


def vector_tree(source: str):
    _, root = vector_source(source)
    def convert(node):
        return {'tag': node.tag.removeprefix('{' + NS + '}'), 'attrs': dict(node.attrib),
                'text': node.text or '', 'tail': node.tail or '', 'children': [convert(child) for child in node]}
    return convert(root)
