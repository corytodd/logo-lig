#!/usr/bin/env python3

import argparse
import logging
import tempfile

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageOps
import vtracer
from fontTools.ttLib import TTFont
from fontTools.pens.basePen import AbstractPen
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


def png_to_svg(png_path: Path, svg_path: Path, *, invert: bool = False) -> None:
    """
    Vectorize a png into an svg.
    """
    # HACK: transparency is hard. composite onto white background,
    # then convert to grayscale for vtracer.
    # TODO: what's the correct way to preserve alpha in vtracer?
    with Image.open(png_path) as img:
        rgba = img.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    bg.paste(rgba, mask=rgba.split()[3])
    gray = bg.convert("L")
    if invert:
        gray = ImageOps.invert(gray)

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
    parts = []
    for x in re.split(r"[,\s]+", m.group(1).strip()):
        parts.append(float(x))
    return Transform(*parts)


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
    _draw_svg_paths(root, tt_pen, transform)

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


def add_ligature(font: TTFont, sequence: str, glyph_name: str) -> None:
    """
    Add a ligature substitution to the font that maps sequence -> glyph_name.
    """
    cmap = font.getBestCmap()
    glyph_seq = []
    for c in sequence:
        glyph_seq.append(glyph_name_for_char(cmap, c))
    first_glyph = glyph_seq[0]
    rest_glyphs = glyph_seq[1:]
    log.info("ligature: %s = %s", " + ".join(glyph_seq), glyph_name)

    if "GSUB" not in font:
        # TODO: can we create this ourselves?
        raise ValueError("Font has no GSUB table; cannot add ligature substitution.")
    gsub = font["GSUB"].table
    if gsub.FeatureList is None or gsub.LookupList is None:
        # TODO: can we create this ourselves?
        raise ValueError("Font GSUB table is missing FeatureList or LookupList.")

    # Find all lookup indices referenced by any liga FeatureRecord
    liga_lookup_indices = set()
    for fr in gsub.FeatureList.FeatureRecord:
        if fr.FeatureTag == "liga":
            liga_lookup_indices.update(fr.Feature.LookupListIndex)

    # Find the first LookupType 4 among them, unwrapping Type 7 (Extension) if needed.
    # https://learn.microsoft.com/en-us/typography/opentype/spec/gsub#lookup-type-4-subtable-ligature-substitution
    # https://learn.microsoft.com/en-us/typography/opentype/spec/gsub#lookup-type-7-subtable-substitution-subtable-extension
    target_subtables = None
    for idx in sorted(liga_lookup_indices):
        lookup = gsub.LookupList.Lookup[idx]
        subtables = lookup.SubTable

        if lookup.LookupType == 7:
            subtables = [st.ExtSubTable for st in subtables]
            if not hasattr(subtables[0], "ligatures"):
                continue
        elif lookup.LookupType != 4:
            continue

        target_subtables = subtables
        log.debug(
            "appending to existing liga lookup index %d (type=%d)",
            idx,
            lookup.LookupType,
        )
        break

    if target_subtables is None:
        # No existing liga LookupType 4: create one from scratch and wire it in.
        new_subtable = ot.LigatureSubst()
        new_subtable.Format = 1
        new_subtable.ligatures = {}

        new_lookup = ot.Lookup()
        new_lookup.LookupType = 4
        new_lookup.LookupFlag = 0
        new_lookup.SubTable = [new_subtable]
        new_lookup.SubTableCount = 1

        lookup_index = len(gsub.LookupList.Lookup)
        gsub.LookupList.Lookup.append(new_lookup)
        gsub.LookupList.LookupCount += 1
        target_subtables = [new_subtable]

        _wire_lookup_into_liga(gsub, lookup_index)
        log.debug("created new liga lookup at index %d", lookup_index)

    # Build the Ligature record and append to the first subtable
    lig = ot.Ligature()
    lig.LigGlyph = glyph_name
    lig.Component = rest_glyphs

    subtable = target_subtables[0]
    if first_glyph in subtable.ligatures:
        subtable.ligatures[first_glyph].append(lig)
    else:
        subtable.ligatures[first_glyph] = [lig]


def rename_font(font: TTFont, new_name: str) -> None:
    """
    Update the name table entries that determine how the OS and applications
    identify the font.
    """
    postscript_name = new_name.replace(" ", "")
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

    # Update every platform/encoding record for each name ID
    for record in name_table.names:
        new = replacements.get(record.nameID)
        if new is not None:
            name_table.setName(
                new, record.nameID, record.platformID, record.platEncID, record.langID
            )

    log.info("font renamed to '%s' (PS: %s)", new_name, postscript_name)


def _wire_lookup_into_liga(gsub, lookup_index: int) -> None:
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
    parser.add_argument("-p", "--png", required=True, help="Logo png file")
    parser.add_argument("-o", "--out", required=True, help="Output .ttf file")
    parser.add_argument(
        "-s", "--sequence", required=True, help="Input character sequence"
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
        "-i",
        "--invert",
        action="store_true",
        help="Invert PNG before tracing.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v for info, -vv for debug).",
    )
    args = parser.parse_args()
    _configure_logging(verbosity=args.verbose)
    log.debug("args: %s", args)

    png_path = Path(args.png)
    font_path = Path(args.font)
    out_path = Path(args.out)
    glyph_name = "logo_" + re.sub(r"[^A-Za-z0-9._-]", "", args.sequence)

    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as f:
        svg_path = Path(f.name)
    try:
        png_to_svg(png_path, svg_path, invert=args.invert)

        font = TTFont(font_path)
        add_glyph_to_font(
            font, svg_path, glyph_name, max_width=args.max_width, scale=args.scale
        )
    finally:
        svg_path.unlink(missing_ok=True)

    add_ligature(font, args.sequence, glyph_name)

    rename_font(font, args.family_name)

    font.save(out_path)
    log.info("written to %s", out_path)


if __name__ == "__main__":
    main()
