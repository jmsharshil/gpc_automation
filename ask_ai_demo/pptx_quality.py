"""
crm_chat/pptx_quality.py
========================
Two jobs, both run INSIDE your Django process with python-pptx (no Azure call,
no data leaves your server):

1. describe_template(bytes) -> str
       A compact report about an attached template (slide size, theme fonts/colours,
       layouts with their SAFE AREA and placeholders, and the "keep-out" decorations).
       It is put in the system prompt BEFORE the model writes code, so the model never
       has to guess coordinates.

2. check_deck(bytes) -> list[str]
       Deterministic layout check of a generated deck.  Finds the problems you can see
       on screen: shapes outside the slide, text/cards overlapping the template's bands,
       logos or footer, cards overlapping each other, text that does not fit its box,
       invisible text colour, empty placeholders.  chat_agent.py sends these messages
       back to the model (before the file is delivered) so it fixes its own script.

The model needs no special helper functions: it writes plain python-pptx, using the
geometry from the report, and the checker catches whatever still goes wrong.
"""
import io
import logging

logger = logging.getLogger(__name__)

EMU = 914400
TOL = int(0.06 * EMU)            # geometry tolerance (0.06 in)
MAX_ISSUES = 12



# --------------------------------------------------------------------------
# template geometry (server side only)
# --------------------------------------------------------------------------
CHAR_W = 0.52        # average glyph width as a fraction of the font size
LINE_H = 1.2         # line height as a multiple of the font size
WRAP_LOSS = 0.92     # real word-wrapping wastes some space


def text_height_pt(paragraphs, size_pt, width_pt, bold=False):
    """Estimated height (points) of wrapped paragraphs."""
    cw = size_pt * CHAR_W * (1.08 if bold else 1.0)
    cpl = max(1, int(width_pt / cw * WRAP_LOSS))
    lines = sum(max(1, -(-len(str(p)) // cpl)) for p in paragraphs)
    return lines * size_pt * LINE_H


def safe_area(prs, layout, margin_in=0.2):
    """(left, top, width, height) in EMU of the area NOT covered by the layout/master decorations."""
    W, Hh = prs.slide_width, prs.slide_height
    left, right, top, bottom = 0, W, 0, Hh
    for s in list(layout.shapes) + list(layout.slide_master.shapes):
        if s.is_placeholder or s.width is None or s.height is None:
            continue
        l, t, w, h = s.left, s.top, s.width, s.height
        r, b = l + w, t + h
        if h > 0.6 * Hh and r < 0.25 * W:
            left = max(left, r)
        elif h > 0.6 * Hh and l > 0.5 * W:
            right = min(right, l)
        elif t < 0.25 * Hh and b < 0.45 * Hh:
            top = max(top, b)
        elif t > 0.8 * Hh:
            bottom = min(bottom, t)
    for ph in layout.placeholders:
        if ph.placeholder_format.type is not None and "TITLE" in str(ph.placeholder_format.type) and ph.top is not None:
            top = max(top, ph.top + ph.height)
    m = int(margin_in * EMU)
    left, top, right, bottom = left + m, top + m, right - m, bottom - m
    return left, top, max(right - left, EMU), max(bottom - top, EMU)


def layout_kind(prs, lay):
    """'cover' = layout with a large decorative graphic at the right (cover/closing design), else 'content'."""
    W, Hh = prs.slide_width, prs.slide_height
    for s in lay.shapes:
        if s.is_placeholder or s.width is None or s.height is None:
            continue
        if s.height > 0.6 * Hh and s.left > 0.5 * W:
            return "cover"
    return "content"


def pick_layout(prs, kind="content"):
    """Best layout index of the requested kind (needs a title; prefers a body and a large safe area)."""
    best, best_score = None, -1
    for i, lay in enumerate(prs.slide_layouts):
        if layout_kind(prs, lay) != kind:
            continue
        types = [str(p.placeholder_format.type) for p in lay.placeholders]
        score = (3 if any("TITLE" in t for t in types) else 0) + (2 if any(("BODY" in t or "OBJECT" in t) for t in types) else 0)
        sa = safe_area(prs, lay)
        score += sa[2] * sa[3] / float(prs.slide_width * prs.slide_height)
        if score > best_score:
            best, best_score = i, score
    return best if best is not None else 0


def theme_info(prs):
    """{'major': heading font, 'minor': body font, 'colors': {'accent1': '1F3864', ...}}"""
    from lxml import etree
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    out = {"major": None, "minor": None, "colors": {}}
    try:
        root = etree.fromstring(prs.slide_master.part.part_related_by(RT.THEME).blob)
        for tag, key in (("majorFont", "major"), ("minorFont", "minor")):
            el = root.find(".//a:fontScheme/a:%s/a:latin" % tag, ns)
            if el is not None:
                out[key] = el.get("typeface")
        cs = root.find(".//a:clrScheme", ns)
        if cs is not None:
            for child in cs:
                inner = child[0] if len(child) else None
                if inner is None:
                    continue
                val = inner.get("val") if etree.QName(inner).localname == "srgbClr" else inner.get("lastClr")
                if val:
                    out["colors"][etree.QName(child).localname] = val.upper()
    except Exception:
        pass
    return out

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _inch(v):
    return v / float(EMU)


def _box(shape):
    try:
        l, t, w, h = shape.left, shape.top, shape.width, shape.height
    except Exception:
        return None
    if None in (l, t, w, h):
        return None
    return int(l), int(t), int(w), int(h)


def _area(b):
    return max(0, b[2]) * max(0, b[3])


def _inter(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return max(0, x2 - x1) * max(0, y2 - y1)


def _contains(outer, inner, tol=TOL):
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol and
            inner[0] + inner[2] <= outer[0] + outer[2] + tol and
            inner[1] + inner[3] <= outer[1] + outer[3] + tol)


def _label(shape):
    try:
        if shape.has_text_frame and shape.text_frame.text.strip():
            t = " ".join(shape.text_frame.text.split())
            return "'" + (t[:32] + "…" if len(t) > 32 else t) + "'"
    except Exception:
        pass
    return f"'{getattr(shape, 'name', 'shape')}'"


def _lum(hex6):
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(h1, h2):
    a, b = _lum(h1), _lum(h2)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _solid_rgb(fill):
    try:
        from pptx.enum.dml import MSO_FILL, MSO_COLOR_TYPE
        if fill.type == MSO_FILL.SOLID and fill.fore_color.type == MSO_COLOR_TYPE.RGB:
            return str(fill.fore_color.rgb).upper()
    except Exception:
        pass
    return None


def _text_rgb(shape):
    try:
        from pptx.enum.dml import MSO_COLOR_TYPE
        for p in shape.text_frame.paragraphs:
            for r in p.runs:
                if r.text.strip() and r.font.color is not None and r.font.color.type == MSO_COLOR_TYPE.RGB:
                    return str(r.font.color.rgb).upper()
    except Exception:
        pass
    return None


def _is_content(shape):
    """Shapes that carry visible content (text, picture, table, chart)."""
    try:
        if shape.has_text_frame and shape.text_frame.text.strip():
            return True
    except Exception:
        pass
    if getattr(shape, "has_table", False) or getattr(shape, "has_chart", False):
        return True
    try:
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        return shape.shape_type == MSO_SHAPE_TYPE.PICTURE
    except Exception:
        return False


def _is_picture(shape):
    try:
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        return shape.shape_type == MSO_SHAPE_TYPE.PICTURE
    except Exception:
        return False


def _is_title(shape):
    try:
        return shape.is_placeholder and ("TITLE" in str(shape.placeholder_format.type) or shape.placeholder_format.idx == 0)
    except Exception:
        return False


def _decorations(slide, W, H):
    """Non-placeholder shapes of the slide's layout + master (bands, bars, logos, footer text)."""
    out = []
    try:
        sources = list(slide.slide_layout.shapes) + list(slide.slide_layout.slide_master.shapes)
    except Exception:
        return out
    for s in sources:
        try:
            if s.is_placeholder:
                continue
        except Exception:
            pass
        b = _box(s)
        if not b:
            continue
        frac = _area(b) / float(W * H)
        if frac < 0.004 or frac > 0.85:      # hairlines / full-slide backgrounds are not keep-out zones
            continue
        out.append((b, _solid_rgb(s.fill) if hasattr(s, "fill") else None))
    return out


def _region(b, W, H):
    l, t, w, h = b
    if w > 0.75 * W and t < 0.3 * H and t + h < 0.55 * H:
        return "top band"
    if w > 0.75 * W and t > 0.7 * H:
        return "footer band"
    if h > 0.6 * H and l + w < 0.3 * W:
        return "left side bar"
    if h > 0.6 * H and l > 0.6 * W:
        return "right side panel"
    return f"template graphic/logo at ({_inch(l):.1f}, {_inch(t):.1f}) in, size {_inch(w):.1f} x {_inch(h):.1f} in"


def _para_size(p, default):
    sizes = [r.font.size.pt for r in p.runs if r.font.size is not None]
    if sizes:
        return max(sizes)
    if p.font.size is not None:
        return p.font.size.pt
    return default


def _overflow(shape):
    """(needed_pt, available_pt) or None if it cannot be estimated reliably."""
    try:
        tf = shape.text_frame
    except Exception:
        return None
    if not tf.text.strip():
        return None
    box = _box(shape)
    if not box:
        return None
    placeholder = False
    try:
        placeholder = shape.is_placeholder
    except Exception:
        pass
    default = None if placeholder else 18.0
    ml = (tf.margin_left if tf.margin_left is not None else 91440)
    mr = (tf.margin_right if tf.margin_right is not None else 91440)
    mt = (tf.margin_top if tf.margin_top is not None else 45720)
    mb = (tf.margin_bottom if tf.margin_bottom is not None else 45720)
    width_pt = max(10.0, (box[2] - ml - mr) / 12700.0)
    avail_pt = (box[3] - mt - mb) / 12700.0
    needed, known = 0.0, False
    for p in tf.paragraphs:
        txt = "".join(r.text for r in p.runs) or p.text
        size = _para_size(p, default)
        if size is None:
            continue
        known = True
        bold = any(r.font.bold for r in p.runs)
        needed += text_height_pt([txt], size, width_pt, bold)
        try:
            if p.space_after is not None:
                needed += p.space_after.pt
        except Exception:
            pass
    if not known:
        return None
    return needed, avail_pt


def _nowrap_overrun(shape):
    """
    python-pptx text boxes do NOT wrap by default (wrap="none"), so a long line runs straight through
    the card and off the slide.  Returns (overrun_in, line_chars, size_pt) when a non-wrapping line is
    clearly wider than its box, else None.
    """
    try:
        tf = shape.text_frame
        if tf.word_wrap is not False or not tf.text.strip():
            return None
        box = _box(shape)
        if not box:
            return None
        inner_w = box[2] - (tf.margin_left if tf.margin_left is not None else 91440) \
            - (tf.margin_right if tf.margin_right is not None else 91440)
        worst = 0.0
        for p in tf.paragraphs:
            size = _para_size(p, 18.0)
            txt = "".join(r.text for r in p.runs) or p.text
            bold = any(r.font.bold for r in p.runs)
            width = len(txt) * size * CHAR_W * (1.08 if bold else 1.0) * 12700.0
            worst = max(worst, width)
        over = worst - inner_w
        if over > 0.3 * EMU and worst > 1.1 * inner_w:
            return _inch(over)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# 1) template report for the system prompt
# --------------------------------------------------------------------------
def describe_template(data: bytes, filename: str = "template.pptx") -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    W, Hh = prs.slide_width, prs.slide_height
    theme = theme_info(prs)
    lines = [f"\n[TEMPLATE REPORT for /tmp/inputs/{filename}]"]
    lines.append(f"Slide size: {_inch(W):.2f} x {_inch(Hh):.2f} in. ALL coordinates must stay inside this. "
                 f"The file already contains {len(prs.slides)} sample slide(s): delete them before adding your own.")
    if theme["major"] or theme["minor"]:
        lines.append(f"Theme fonts: headings = {theme['major']}, body = {theme['minor']} (do not set other fonts).")
    if theme["colors"]:
        lines.append("Theme colours (hex): " + ", ".join(f"{k} {v}" for k, v in theme["colors"].items()))
    lines.append("Layouts (index: name | kind | SAFE AREA left,top,width,height in inches | placeholders):")
    for i, lay in enumerate(prs.slide_layouts):
        try:
            l, t, w, h = safe_area(prs, lay)
            kind = layout_kind(prs, lay)
        except Exception:
            continue
        phs = []
        for ph in lay.placeholders:
            try:
                phs.append(f"{str(ph.placeholder_format.type).split('.')[-1].split(' ')[0].lower()}#{ph.placeholder_format.idx}")
            except Exception:
                pass
        lines.append(f"  {i}: {lay.name} | {kind} | {_inch(l):.2f},{_inch(t):.2f},{_inch(w):.2f},{_inch(h):.2f} | {', '.join(phs)}")
    try:
        best_c, best_cov = pick_layout(prs, "content"), pick_layout(prs, "cover")
        lines.append(f"Best layouts: content slides -> layout {best_c}; cover/closing slide -> layout {best_cov}.")
        deco = _decorations_for_layout(prs.slide_layouts[best_c], W, Hh)
        if deco:
            lines.append("Keep-out template decorations on content slides (do not place your own text or cards on them): " + "; ".join(deco))
    except Exception:
        pass
    return "\n".join(lines) + "\n"


def _decorations_for_layout(layout, W, Hh):
    out = []
    try:
        shapes = list(layout.shapes) + list(layout.slide_master.shapes)
    except Exception:
        return out
    for s in shapes:
        try:
            if s.is_placeholder:
                continue
        except Exception:
            pass
        b = _box(s)
        if not b:
            continue
        frac = _area(b) / float(W * Hh)
        if frac < 0.004 or frac > 0.85:
            continue
        out.append(_region(b, W, Hh))
    return out[:8]


# --------------------------------------------------------------------------
# 2) deck checker
# --------------------------------------------------------------------------
def check_deck(data: bytes) -> list:
    """Returns a list of human-readable layout problems ([] = looks fine). Never raises."""
    try:
        return _check_deck(data)
    except Exception as exc:                    # a checker bug must never break the chat
        logger.warning("check_deck failed: %s", exc)
        return []


def _check_deck(data: bytes) -> list:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    W, Hh = prs.slide_width, prs.slide_height
    issues = []

    for n, slide in enumerate(prs.slides, start=1):
        decos = _decorations(slide, W, Hh)
        shapes = []
        for s in slide.shapes:
            b = _box(s)
            if b:
                shapes.append((s, b))

        # a) outside the slide
        for s, b in shapes:
            if not _is_content(s) and _area(b) < 0.03 * W * Hh:
                continue
            l, t, w, h = b
            over_r, over_b = (l + w) - W, (t + h) - Hh
            if over_r > TOL or over_b > TOL or l < -TOL or t < -TOL:
                bits = []
                if over_r > TOL:
                    bits.append(f"{_inch(over_r):.1f} in past the RIGHT edge (slide is {_inch(W):.2f} in wide)")
                if over_b > TOL:
                    bits.append(f"{_inch(over_b):.1f} in past the BOTTOM edge (slide is {_inch(Hh):.2f} in tall)")
                if l < -TOL:
                    bits.append(f"{_inch(-l):.1f} in past the LEFT edge")
                if t < -TOL:
                    bits.append(f"{_inch(-t):.1f} in past the TOP edge")
                issues.append((n, 0, f"Slide {n}: {_label(s)} extends " + " and ".join(bits) + "."))

        # b) content over template decorations (band / bars / logo / footer)
        for s, b in shapes:
            if _is_title(s) or _is_picture(s) or not _is_content(s):
                continue
            try:
                if s.is_placeholder:
                    continue
            except Exception:
                pass
            for d, d_fill in decos:
                inter_d = _inter(b, d)
                frac = inter_d / float(max(1, _area(b)))
                covers_deco = inter_d / float(max(1, _area(d))) >= 0.25      # e.g. a card sitting on the logo
                if frac < 0.12 and not covers_deco:
                    continue
                inside = _contains(d, b)
                txt = _text_rgb(s)
                poor = bool(txt and d_fill and _contrast(txt, d_fill) < 3.0)
                if inside and not poor:
                    continue                        # readable text deliberately placed on a band: fine
                why = f"text colour #{txt} is almost invisible on #{d_fill}" if poor else "it covers part of it"
                issues.append((n, 1, f"Slide {n}: {_label(s)} overlaps the template's {_region(d, W, Hh)} ({why}). "
                                     f"Keep content inside the layout's safe area from the template report."))
                break

        # c) content shapes overlapping each other (partially)
        content = [(s, b) for s, b in shapes if _is_content(s)]
        for i in range(len(content)):
            for j in range(i + 1, len(content)):
                (s1, b1), (s2, b2) = content[i], content[j]
                if _contains(b1, b2) or _contains(b2, b1):
                    continue
                inter = _inter(b1, b2)
                smaller = float(max(1, min(_area(b1), _area(b2))))
                if inter / smaller > 0.15:
                    issues.append((n, 2, f"Slide {n}: {_label(s1)} and {_label(s2)} overlap each other "
                                         f"({int(100 * inter / smaller)}% of the smaller one)."))

        # d) text that does not fit its box
        for s, b in shapes:
            if _is_title(s):
                continue
            over = _nowrap_overrun(s)
            if over:
                issues.append((n, 3, f"Slide {n}: text in {_label(s)} has word wrap turned OFF and runs about "
                                     f"{over:.1f} in past its box (text boxes do not wrap by default). "
                                     f"Set text_frame.word_wrap = True on every text frame."))
                continue
            est = _overflow(s)
            if est and est[0] > est[1] * 1.25 and est[0] - est[1] > 14:
                issues.append((n, 3, f"Slide {n}: text in {_label(s)} needs about {est[0] / 72:.1f} in of height but the box "
                                     f"has {est[1] / 72:.1f} in. Shorten the text, use a smaller font or a bigger box."))

        # e) leftover empty placeholders
        for s, b in shapes:
            try:
                if s.is_placeholder and s.has_text_frame and not s.text_frame.text.strip():
                    issues.append((n, 4, f"Slide {n}: empty placeholder '{s.name}' (fill it or delete it before saving)."))
            except Exception:
                pass

    issues.sort(key=lambda x: (x[0], x[1]))
    msgs, seen = [], set()
    for _, _, m in issues:
        if m not in seen:
            seen.add(m)
            msgs.append(m)
    if len(msgs) > MAX_ISSUES:
        extra = len(msgs) - MAX_ISSUES
        msgs = msgs[:MAX_ISSUES] + [f"(+{extra} more similar problems)"]
    return msgs


def format_issues(issues: list) -> str:
    return (
        "The deck was generated but NOT delivered yet because of layout problems:\n- "
        + "\n- ".join(issues)
        + "\n\nFix ALL of them and call run_python_code again with the COMPLETE corrected script "
          "(re-open the template, keep all slide content). Compute every shape's position from the layout's "
          "safe area in the template report, size fonts so the text fits its box, and leave the title "
          "placeholder's font, size and position untouched."
    )
