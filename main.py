#!/usr/bin/env python3

import argparse
import copy
import logging
import shutil
import tempfile

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image
import vtracer
from fontTools.ttLib import TTFont
from fontTools.pens.basePen import AbstractPen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.transformPen import TransformPen
from fontTools.svgLib.path import parse_path as svg_parse_path
from fontTools.misc.transform import Transform
from fontTools.ttLib.tables import otTables as ot

_PUA_GLYPH_CP = 0xE001  # Private Use Area code point claimed by this tool


def _configure_logging(*, verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(levelname).1s] %(funcName)s: %(message)s")
    )
    logging.root.setLevel(level)
    logging.root.addHandler(handler)
    logging.getLogger("fontTools").setLevel(logging.WARNING)


log = logging.getLogger(__name__)


def img_to_svg(img_path: Path, svg_path: Path) -> None:
    """
    Vectorize an image into an svg.

    Note: If img_path is already an SVG no conversion is performed in this step.
          An implementation detail is leaked here because the svg to ttf process
          will convert all cubic curves to quadratic curves. This path is only
          relevant when an svg is provided. For other image types, vtracer is
          uses polygon mode which handles the linearization. In my testing,
          vtracer spline mode produces less pretty TTF output due to noise.
    """
    if img_path.suffix.lower() in (".svg", ".svgz"):
        shutil.copy(img_path, svg_path)
        log.info("image is already svg, skipping conversion")
        return

    # HACK: transparency is hard. composite onto white background,
    # then convert to grayscale for vtracer.
    # TODO: what's the correct way to preserve alpha in vtracer?
    try:
        with Image.open(img_path) as img:
            rgba = img.convert("RGBA")
    except Image.UnidentifiedImageError as ex:
        log.error("unsupported image type: %s", img_path)
        raise ex
    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    bg.paste(rgba, mask=rgba.split()[3])
    gray = bg.convert("L")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_name = f.name
    try:
        gray.save(tmp_name)
        # vtracer is much faster than pixels2svg and produces cleaner paths.
        # Non-defaults: colormode=binary, mode=polygon.
        # Polygon mode produces much cleaner output for logos I've tested with
        vtracer.convert_image_to_svg_py(
            tmp_name,
            str(svg_path),
            colormode="binary",
            hierarchical="stacked",
            mode="polygon",
            filter_speckle=4,
            color_precision=6,
            corner_threshold=60,
            layer_difference=16,
            length_threshold=4.0,
            max_iterations=10,
            splice_threshold=45,
            path_precision=3,
        )
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def _parse_svg_transform(s: str | None) -> Transform:
    """Parse an svg transform attribute into a fontTools Transform.
    Syntax: https://www.w3.org/TR/css-transforms-1/#funcdef-transform-translate
    Based on https://gist.github.com/anthrotype/0d7cb2fa304004024e793d2cdc1edce5
    """
    if not s:
        return Transform()
    s = s.strip()
    m = re.match(r"translate\(([^,\s)]+)(?:[,\s]+([^)]*))?\)", s)
    if m:
        tx = float(m.group(1))
        ty = float(m.group(2)) if m.group(2) else 0.0
        return Transform().translate(tx, ty)
    m = re.match(r"matrix\(([^)]+)\)", s)
    if not m:
        return Transform()
    points = []
    try:
        parts = re.split(r"[,\s]+", m.group(1).strip())
        points = [float(x) for x in parts]
    except ValueError:
        raise ValueError(f"Invalid matrix transform values: {m.group(1)!r}")
    if len(points) != 6:
        return Transform()
    return Transform(*points)


def _draw_svg_paths(
    root: ET.Element, pen: AbstractPen, transform: Transform | None = None
) -> None:
    """Draw all <path> elements from an SVG root element to pen, composing element transforms."""
    for el in root.findall(".//{http://www.w3.org/2000/svg}path"):
        d = el.get("d", "")
        if not d:
            continue
        t_str = el.get("transform", "")
        if not t_str:
            if transform is None:
                svg_parse_path(d, pen)
            else:
                svg_parse_path(d, TransformPen(pen, transform))
            continue

        composed = _parse_svg_transform(t_str)
        if transform is not None:
            composed = transform.transform(composed)

        svg_parse_path(d, TransformPen(pen, composed))


def svg_to_glyph(
    svg_path: Path, font: TTFont, target_height: int, target_width: int | None = None
) -> tuple:
    """
    Parse svg paths and return (glyph, advance_width) while preserving the
    aspect ratio and fitting within target_width x target_height.
    """
    root = ET.parse(svg_path).getroot()
    bounds_pen = BoundsPen(None)
    _draw_svg_paths(root, bounds_pen)
    if bounds_pen.bounds is None:
        raise ValueError("No paths found in svg, vectorization may have failed.")

    xmin, ymin, xmax, ymax = bounds_pen.bounds
    svg_width = xmax - xmin
    svg_height = ymax - ymin

    if svg_height:
        scale_h = target_height / svg_height
    else:
        scale_h = 1.0

    if target_width and svg_width:
        scale_w = target_width / svg_width
    else:
        scale_w = scale_h

    scale = min(scale_h, scale_w)
    log.debug("svg size: %.1fx%.1f", svg_width, svg_height)
    log.debug(
        "target: %sx%s, scale_h=%.4f, scale_w=%.4f, scale=%.4f",
        target_width,
        target_height,
        scale_h,
        scale_w,
        scale,
    )
    log.debug("scaled output: %.1fx%.1f", svg_width * scale, svg_height * scale)

    # Affine transform: translate to origin, scale, flip Y (SVG down -> font up,
    # TrueType y=0 is baseline: https://learn.microsoft.com/en-us/typography/opentype/spec/ttch01#funits-and-the-grid)
    #   x' = (x - xmin) * scale  ->  xx=scale,  dx=-xmin*scale
    #   y' = (ymax - y) * scale  ->  yy=-scale, dy=ymax*scale
    transform = Transform(scale, 0, 0, -scale, -xmin * scale, ymax * scale)

    tt_pen = TTGlyphPen(font.getGlyphSet())
    # TTF supports only quadratic curves. If a raw SVG was provided, filter any
    # (cu)bic curves and convert them to (qu)adratic curves.
    # This is an approximation using max_err in 1/unitsPerEm.
    # i.e. this is an imperceptible loss of fidelity.
    cubic_pen = Cu2QuPen(tt_pen, max_err=1.0)
    _draw_svg_paths(root, cubic_pen, transform)

    glyph = tt_pen.glyph()
    # The horizontal distance the cursor should advance after drawing the glyph
    advance_width = round(svg_width * scale)
    return glyph, advance_width


def add_glyph_to_font(
    font: TTFont,
    svg_path: Path,
    glyph_name: str,
    max_width: int | None = None,
    scale: float = 1.0,
) -> None:
    os2 = font["OS/2"]
    cap_height = os2.sCapHeight or os2.sTypoAscender
    units_per_em = font["head"].unitsPerEm
    target_height = round(cap_height * scale)
    target_max_width = round((max_width or units_per_em) * scale)

    glyph, advance = svg_to_glyph(
        svg_path, font, target_height=target_height, target_width=target_max_width
    )

    # Register the new glyph name so the proper order is maintained.
    font.getGlyphOrder().append(glyph_name)
    font["glyf"][glyph_name] = glyph

    # Add hmtx entry (advance width, lsb=0)
    font["hmtx"][glyph_name] = (advance or units_per_em, 0)

    # PUA (U+E000–U+F8FF) code point for ligature substitution, no conflict with real chars.
    # https://www.unicode.org/versions/Unicode16.0.0/core-spec/chapter-23/#G19465
    for cmap in font["cmap"].tables:
        if cmap.format in (4, 12) and hasattr(cmap, "cmap"):
            cmap.cmap[_PUA_GLYPH_CP] = glyph_name

    log.info("added glyph '%s' (advance=%s)", glyph_name, advance)


def glyph_name_for_char(cmap: dict, char: str) -> str:
    name = cmap.get(ord(char))
    if name is None:
        raise KeyError(f"No glyph for '{char}' (U+{ord(char):04X}) in font")
    return name


def make_coverage(glyph_names: list[str], font: TTFont) -> ot.Coverage:
    """Return OpenType Coverage table for these glyphs, sorted by glyph ID.

    This tells the font which glyphs a substitution rule applies to.
    Coverage glyphs must be in glyph-ID order or fontTools will warn on serialization.
    Force a rebuild of the glyph map to bypass cache (rebuild=True) and apply
    to this glyph list.
    """
    glyph_id = font.getReverseGlyphMap(rebuild=True)
    cov = ot.Coverage()
    # After much fuss it turns out setting the Format field is a NOP
    # The library will choose the most optimal configuration when we serialize.
    # https://github.com/fonttools/fonttools/blob/c760aaab4abbbb0d069b80f8b10334a429738319/Lib/fontTools/ttLib/tables/otTables.py#L970
    cov.glyphs = sorted(set(glyph_names), key=lambda g: glyph_id.get(g, 0))
    return cov


def get_or_create_marker_glyph(font: TTFont, base_glyph: str) -> str:
    """Return glyph to be used for when ligature must not trigger

    Example: If the sequence is 123 and the shaper sees a123, the presence of
             'a' causes 1 to be replaced with this marker, which the ligature
             rule won't match

    Base on: Fira code conjunction but without messing with .fea source.
    https://github.com/tonsky/FiraCode/blob/e50b177465f32b2f439098c4fcf7451cf70adc6b/features/calt/conj_disj.fea#L2
    """
    marker = base_glyph + ".ctx"
    if marker not in font.getGlyphOrder():
        font.getGlyphOrder().append(marker)
        # glyf is mutable; make a copy to so the marker is not accidentally modified
        font["glyf"][marker] = copy.copy(font["glyf"][base_glyph])
        font["hmtx"][marker] = font["hmtx"][base_glyph]
    return marker


def alphanumeric_glyphs(font: TTFont) -> list[str]:
    """
    Return glyph names for all alphanumeric characters in the font.
    The ligature is suppressed when preceded/followed by a letter or digit.
    """
    cmap = font.getBestCmap() or {}
    return [name for c, name in cmap.items() if chr(c).isalnum()]


def make_lig(glyph_name: str, components: list[str]) -> ot.Ligature:
    lig = ot.Ligature()
    lig.LigGlyph = glyph_name
    lig.Component = components
    return lig


def make_lookup(lookup_type: int, subtables: list) -> ot.Lookup:
    lookup = ot.Lookup()
    lookup.LookupType = lookup_type
    lookup.LookupFlag = 0
    lookup.SubTable = subtables
    lookup.SubTableCount = len(subtables)
    return lookup


def append_lookup(gsub, lookup: ot.Lookup) -> int:
    idx = len(gsub.LookupList.Lookup)
    gsub.LookupList.Lookup.append(lookup)
    gsub.LookupList.LookupCount += 1
    return idx


def add_lig_lookup(gsub) -> tuple[ot.LigatureSubst, int]:
    """Create a fresh Type 4 lookup, wire it into liga, and return (subtable, index)."""
    subtable = ot.LigatureSubst()
    subtable.ligatures = {}
    lookup_type_4 = make_lookup(4, [subtable])
    idx = append_lookup(gsub, lookup_type_4)
    wire_lookup_into_liga(gsub, idx)
    return subtable, idx


def find_or_create_lig_subtable(gsub) -> ot.LigatureSubst:
    """Return the first Type 4 LigatureSubst subtable wired into liga, creating one if needed.

    Unwraps Type 7 (Extension) lookups transparently.
    https://learn.microsoft.com/en-us/typography/opentype/spec/gsub#lookup-type-4-subtable-ligature-substitution
    https://learn.microsoft.com/en-us/typography/opentype/spec/gsub#lookup-type-7-subtable-substitution-subtable-extension
    """
    liga_indices = {
        i
        for fr in gsub.FeatureList.FeatureRecord
        if fr.FeatureTag == "liga"
        for i in fr.Feature.LookupListIndex
    }
    for idx in sorted(liga_indices):
        lookup = gsub.LookupList.Lookup[idx]
        subtables = lookup.SubTable
        if lookup.LookupType == 7:
            subtables = [st.ExtSubTable for st in subtables]
            if not subtables or not hasattr(subtables[0], "ligatures"):
                continue
        elif lookup.LookupType != 4:
            continue
        log.debug(
            "appending to existing liga lookup index %d (type=%d)",
            idx,
            lookup.LookupType,
        )
        return subtables[0]

    subtable, idx = add_lig_lookup(gsub)
    log.debug("created new liga lookup at index %d", idx)
    return subtable


def add_ligature(
    font: TTFont, sequence: str, glyph_name: str, *, context: bool = False
) -> None:
    """
    Add a ligature substitution to the font that maps sequence -> glyph_name.

    When context=True the ligature only fires when the sequence is NOT adjacent
    to other non-whitespace characters. Uses GSUB Type 6 + Type 4.

    Strategy (context=True) two lookups wired into 'liga' in order:
      L_mark (Type 6): when a forbidden neighbor is found, substitute
                       seq[0] -> seq[0].ctx, a visually identical marker glyph.
      L_lig  (Type 4): substitute seq[0] seq[1]... -> glyph_name
                       seq[0].ctx won't match, so the ligature is suppressed.
    """
    if len(sequence) < 2:
        raise ValueError(
            f"Ligature sequence must be at least 2 characters, got {len(sequence)!r}"
        )
    if "GSUB" not in font:
        raise ValueError("Font has no GSUB table; cannot add ligature substitution.")
    gsub = font["GSUB"].table
    if gsub.FeatureList is None or gsub.LookupList is None:
        raise ValueError("Font GSUB table is missing FeatureList or LookupList.")
    cmap = font.getBestCmap()
    if cmap is None:
        raise ValueError("Font has no usable cmap table.")

    glyph_seq = [glyph_name_for_char(cmap, c) for c in sequence]
    first_glyph = glyph_seq[0]

    if context:
        exclusion_glyphs = alphanumeric_glyphs(font)
        log.debug("context exclusion glyphs: %s", exclusion_glyphs)

        if exclusion_glyphs:
            marker_glyph = get_or_create_marker_glyph(font, first_glyph)

            # L_sub1 (Type 1): first_glyph -> marker_glyph
            subst1 = ot.SingleSubst()
            subst1.mapping = {first_glyph: marker_glyph}
            lookup_type_1 = make_lookup(1, [subst1])
            sub1_idx = append_lookup(gsub, lookup_type_1)

            # L_mark (Type 6): two subtables one checks backtrack, one checks lookahead.
            # Each independently marks first_glyph when a forbidden neighbor is found.
            excl_cov = make_coverage(exclusion_glyphs, font)
            input_covs = [make_coverage([g], font) for g in glyph_seq]

            def _chain_rule(backtrack_cov, lookahead_cov):
                rule = ot.ChainContextSubst()
                rule.Format = 3
                rule.BacktrackCoverage = [backtrack_cov] if backtrack_cov else []
                rule.InputCoverage = input_covs
                rule.LookAheadCoverage = [lookahead_cov] if lookahead_cov else []
                slr = ot.SubstLookupRecord()
                slr.SequenceIndex = 0
                slr.LookupListIndex = sub1_idx
                rule.SubstLookupRecord = [slr]
                rule.SubstCount = 1
                return rule

            subtables = [
                _chain_rule(excl_cov, None),  # forbidden char before seq
                _chain_rule(None, excl_cov),  # forbidden char after seq
            ]
            lookup_type_6 = make_lookup(6, subtables)
            mark_idx = append_lookup(gsub, lookup_type_6)
            wire_lookup_into_liga(gsub, mark_idx)
            log.info(
                "ligature context: marker=%s, mark_lookup=%d, sub1_lookup=%d",
                marker_glyph,
                mark_idx,
                sub1_idx,
            )

        # L_lig (Type 4): always a fresh lookup so it's wired after L_mark.
        lig_subst, lig_idx = add_lig_lookup(gsub)
        lig_subst.ligatures = {first_glyph: [make_lig(glyph_name, glyph_seq[1:])]}
        log.info(
            "ligature with context: %s -> %s (lig_lookup=%d)",
            sequence,
            glyph_name,
            lig_idx,
        )

    else:
        log.info("(non-context) ligature: %s = %s", " + ".join(glyph_seq), glyph_name)
        subtable = find_or_create_lig_subtable(gsub)
        lig = make_lig(glyph_name, glyph_seq[1:])
        subtable.ligatures.setdefault(first_glyph, []).append(lig)
        subtable.ligatures[first_glyph].sort(
            key=lambda _lig: len(_lig.Component), reverse=True
        )


def rename_font(font: TTFont, new_name: str) -> None:
    """
    Update the name table entries that determine how the OS and applications
    identify the font.
    """
    # PostScript names: printable ASCII (33-126) excluding [ ] ( ) { } < > / %
    postscript_name = re.sub(r"[^\x21-\x7e]|[\[\](){}<>/%]", "", new_name)
    name_table = font["name"]

    #  https://learn.microsoft.com/en-us/typography/opentype/spec/name#name-ids
    #  1  - Family name
    #  3  - Unique font identifier
    #  4  - Full font name
    #  6  - PostScript name (ASCII 33-126, spaces excluded)
    replacements = {
        1: new_name,
        3: new_name,
        4: new_name,
        6: postscript_name,
    }

    # Update existing records and create any that are missing.
    # Iterate existing records first to preserve platform/encoding/language IDs.
    seen = set()
    for record in name_table.names:
        new = replacements.get(record.nameID)
        if new is not None:
            name_table.setName(
                new, record.nameID, record.platformID, record.platEncID, record.langID
            )
            seen.add(record.nameID)
    for name_id, value in replacements.items():
        if name_id not in seen:
            name_table.setName(value, name_id, 3, 1, 0x409)

    log.info("font renamed to '%s' (PS: %s)", new_name, postscript_name)


def wire_lookup_into_liga(gsub, lookup_index: int) -> None:
    """
    Append lookup_index to every existing liga FeatureRecord.
    If no liga feature exists, create one and wire it into every script/langsys.
    """
    feature_list = gsub.FeatureList

    found = False
    for fr in feature_list.FeatureRecord:
        if fr.FeatureTag == "liga":
            fr.Feature.LookupListIndex.append(lookup_index)
            fr.Feature.LookupCount += 1
            found = True

    if found:
        return

    # No existing liga feature: create one and wire it into every script/langsys.
    feat = ot.Feature()
    feat.FeatureParams = None
    feat.LookupListIndex = [lookup_index]
    feat.LookupCount = 1

    fr = ot.FeatureRecord()
    fr.FeatureTag = "liga"
    fr.Feature = feat

    new_fi = feature_list.FeatureCount
    feature_list.FeatureRecord.append(fr)
    feature_list.FeatureCount += 1

    if gsub.ScriptList is None:
        return
    for script_record in gsub.ScriptList.ScriptRecord:
        script = script_record.Script
        if script.DefaultLangSys:
            script.DefaultLangSys.FeatureIndex.append(new_fi)
            script.DefaultLangSys.FeatureCount += 1
        for lr in script.LangSysRecord:
            lr.LangSys.FeatureIndex.append(new_fi)
            lr.LangSys.FeatureCount += 1


def main():
    parser = argparse.ArgumentParser(
        description="Inject a png logo as a ligature into a ttf.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-f", "--font", required=True, help="Input .ttf file")
    parser.add_argument("-l", "--logo", required=True, help="Logo file (png, svg)")
    parser.add_argument("-o", "--out", required=True, help="Output .ttf file")
    parser.add_argument(
        "-s",
        "--sequence",
        required=True,
        help="Input character sequence (minimum 2 characters).",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Scale factor applied to height.",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=None,
        help="Max glyph width in font units. If omitted, unitsPerEm is used. "
        "Logo is scaled to fit within max-width x cap-height, preserving aspect ratio.",
    )
    parser.add_argument(
        "--family-name",
        required=True,
        help="Override the font family name to avoid conflicts with original font.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v for info, -vv for debug).",
    )
    args = parser.parse_args()
    if len(args.sequence) < 2:
        parser.error("--sequence must be at least 2 characters")
    if Path(args.font).resolve() == Path(args.out).resolve():
        parser.error("--font and --out must be different files")
    _configure_logging(verbosity=args.verbose)
    log.debug("args: %s", args)

    logo_path = Path(args.logo)
    font_path = Path(args.font)
    out_path = Path(args.out)
    glyph_name = "logo_" + re.sub(r"[^A-Za-z0-9._-]", "", args.sequence)

    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as f:
        svg_path = Path(f.name)
    try:
        img_to_svg(logo_path, svg_path)

        font = TTFont(font_path)
        add_glyph_to_font(
            font, svg_path, glyph_name, max_width=args.max_width, scale=args.scale
        )
    finally:
        svg_path.unlink(missing_ok=True)

    add_ligature(font, args.sequence, glyph_name, context=True)

    rename_font(font, args.family_name)

    font.save(out_path)
    log.info("written to %s", out_path)


if __name__ == "__main__":
    main()
