"""PDF export for the final report.

Tries WeasyPrint first (best fidelity). Falls back to xhtml2pdf (pure-Python,
no GTK/Pango system libraries required) so the export works on Windows without
a GTK install. CSS is always embedded inline so both renderers find it.
"""
from __future__ import annotations

import html
import io
import re
from typing import Any

try:
    from weasyprint import HTML, CSS
    _WEASY_OK = True
except Exception:                               # noqa: BLE001
    HTML = CSS = None  # type: ignore
    _WEASY_OK = False

try:
    from xhtml2pdf import pisa as _pisa
    _XHTML2PDF_OK = True
except Exception:                               # noqa: BLE001
    _pisa = None  # type: ignore
    _XHTML2PDF_OK = False


_BASE_CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif; color: #1a1f2c; line-height: 1.5; font-size: 10.5pt; }
h1 { font-size: 18pt; border-bottom: 2px solid #4f9dff; padding-bottom: 6pt; margin-bottom: 14pt; margin-top: 0; }
h2 { font-size: 13pt; color: #1e293b; margin-top: 20pt; margin-bottom: 6pt; border-left: 4px solid #4f9dff; padding-left: 8pt; }
h3 { font-size: 11pt; color: #334155; margin-top: 12pt; margin-bottom: 4pt; }
p { margin: 4pt 0; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; }
th, td { border: 1px solid #d0d4dc; padding: 4pt 8pt; font-size: 9.5pt; text-align: left; vertical-align: top; }
th { background: #f1f5f9; font-weight: 600; }
tr:nth-child(even) { background: #f8fafc; }
.badge { display: inline-block; padding: 2pt 7pt; border-radius: 10pt; font-size: 9pt; font-weight: 700; }
.pass { background: #d1fae5; color: #065f46; }
.fail { background: #fee2e2; color: #991b1b; }
.advisory { background: #fef3c7; color: #92400e; }
.chart { margin: 10pt 0 14pt 0; }
.chart img { max-width: 100%; border: 1px solid #1a2235; border-radius: 4pt; }
.cover { text-align: left; padding: 10pt 0 20pt 0; border-bottom: 2px solid #4f9dff; margin-bottom: 20pt; }
.cover h1 { border: none; font-size: 22pt; margin-bottom: 4pt; }
.cover .subtitle { color: #64748b; font-size: 10.5pt; margin-bottom: 8pt; }
.footer { color: #94a3b8; font-size: 8pt; border-top: 1px solid #d0d4dc; padding-top: 6pt; margin-top: 20pt; }
.narrative p { margin-bottom: 8pt; }
.narrative ul { margin: 4pt 0 8pt 16pt; }
.narrative li { margin-bottom: 2pt; }
blockquote { border-left: 3px solid #f1c40f; background: #fef9e7; padding: 6pt 10pt; margin: 8pt 0; border-radius: 0 4pt 4pt 0; }
code { background: #f1f5f9; padding: 1pt 4pt; border-radius: 3pt; font-size: 9pt; }
.drop-summary { font-size: 10pt; color: #1e293b; margin: 6pt 0 10pt 0; }
"""

# Fields shown in the User Inputs table, in order
_CS_LABELS: dict[str, str] = {
    "packaging_family":       "Packaging Family",
    "packaging_type":         "Packaging Type",
    "product_type":           "Product Type",
    "product_category":       "Product Category",
    "objective":              "Objective",
    "material":               "Material",
    "wall_thickness_mm":      "Wall Thickness (mm)",
    "capacity_ml":            "Capacity (ml)",
    "fill_level_pct":         "Fill Level (%)",
    "gross_weight_g":         "Gross Weight (g)",
    "closure_type":           "Closure Type",
    "bottle_subtype":         "Bottle Subtype",
    "transit_modes":          "Transit Modes",
    "road_condition":         "Road Condition",
    "has_secondary_carton":   "Secondary Carton",
    "carton_type":            "Carton Type",
    "carton_board_grade":     "Carton Board Grade",
    "carton_pack_count":      "Pack Count",
    "carton_stack_height":    "Stack Height",
    # Packet
    "packet_type":            "Packet Type",
    "laminate_structure":     "Laminate Structure",
    "total_thickness_micron": "Total Thickness (µm)",
    "seal_type":              "Seal Type",
    "fill_weight_g":          "Fill Weight (g)",
    # Brush
    "brush_pack_type":        "Brush Pack Type",
    "brush_count":            "Brush Count",
    "product_weight_g":       "Product Weight (g)",
    "primary_pack_material":  "Primary Pack Material",
}
_CS_SKIP = {
    "routing_target", "identified_packaging", "geometry_is_proxy",
    "has_geometry", "geometry_upload_prompted", "identification_confidence",
    "stacking_method", "packet_style", "packets_per_carton",
}


def _e(s: Any) -> str:
    return html.escape(str(s)) if s is not None else ""


def _fmt_val(v: Any) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, list):
        return ", ".join(str(x).replace("_", " ").title() for x in v)
    if isinstance(v, bool):
        return "Yes" if v else "No"
    s = str(v)
    return s if s.replace(".", "").isdigit() else s.replace("_", " ").title()


def _md_to_html(md: str) -> str:
    """Small Markdown subset → HTML (no external dep)."""
    lines = md.splitlines()
    out: list[str] = []
    in_table = False
    in_list = False
    table_rows: list[str] = []

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def emit_table():
        nonlocal in_table, table_rows
        if not table_rows:
            in_table = False
            return
        out.append("<table>")
        header, _sep, *rows = table_rows
        cells = [c.strip() for c in header.strip("|").split("|")]
        out.append("<thead><tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr></thead>")
        out.append("<tbody>")
        for r in rows:
            rc = [c.strip() for c in r.strip("|").split("|")]
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in rc) + "</tr>")
        out.append("</tbody></table>")
        in_table = False
        table_rows.clear()

    def inline(s: str) -> str:
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
        return s

    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            close_list()
            in_table = True
            table_rows.append(stripped)
            continue
        if in_table:
            emit_table()
        if stripped.startswith("# "):
            close_list(); out.append(f"<h3>{inline(stripped[2:])}</h3>")
        elif stripped.startswith("## "):
            close_list(); out.append(f"<h3>{inline(stripped[3:])}</h3>")
        elif stripped.startswith("### "):
            close_list(); out.append(f"<h3>{inline(stripped[4:])}</h3>")
        elif stripped.startswith("> "):
            close_list(); out.append(f"<blockquote>{inline(stripped[2:])}</blockquote>")
        elif stripped.startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{inline(stripped[2:])}</li>")
        elif stripped == "":
            close_list(); out.append("")
        else:
            close_list(); out.append(f"<p>{inline(stripped)}</p>")
    if in_table:
        emit_table()
    close_list()
    return "\n".join(out)


def _chart_img(b64: str, label: str) -> str:
    if not b64:
        return ""
    return (
        f'<div class="chart">'
        f'<img src="data:image/png;base64,{b64}" alt="{_e(label)}" />'
        "</div>"
    )


# ---------------------------------------------------------------- md cleaner

def _strip_eng_calcs(md: str) -> str:
    """Remove the Engineering Calculations section from body_markdown."""
    lines = md.splitlines()
    result: list[str] = []
    in_calcs = False
    for line in lines:
        bare = line.strip().lstrip("#").strip()
        if re.match(r"^Engineering Calculations\s*$", bare, re.IGNORECASE):
            in_calcs = True
            continue
        if in_calcs:
            # A new markdown heading ends the section
            if re.match(r"^#{1,4}\s+\w", line) or re.match(r"^\*\*[A-Z]", line):
                in_calcs = False
            else:
                continue
        result.append(line)
    return "\n".join(result)


# ---------------------------------------------------------------- HTML builder

def _build_html(
    *,
    title: str,
    case_summary: dict,
    transit: dict | None,
    ista2a: dict | None,
    ista6a: dict | None,
    report_md: str,
    charts: dict[str, str],
    generated_on: str = "",
) -> str:
    parts: list[str] = []

    # ---- Cover
    fam = case_summary.get("packaging_family") or case_summary.get("packaging_type") or ""
    ista_verdict = (ista2a or {}).get("overall_verdict", "")
    badge = ""
    if ista_verdict:
        cls = "pass" if ista_verdict.lower() == "pass" else ("advisory" if "adv" in ista_verdict.lower() else "fail")
        badge = f' <span class="badge {cls}">{_e(ista_verdict.upper())}</span>'
    parts.append(
        '<div class="cover">'
        f"<h1>{_e(title)}{badge}</h1>"
        f'<div class="subtitle">{_e(fam.replace("_"," ").title())} Packaging'
        " · PackTwin.AI Engineering Report"
        + (f" · {_e(generated_on)}" if generated_on else "")
        + "</div></div>"
    )

    # ---- 1. User Inputs
    parts.append("<h2>1. User Inputs</h2>")
    rows_html = ""
    for key, label in _CS_LABELS.items():
        if key in _CS_SKIP:
            continue
        val = case_summary.get(key)
        if val is None or val == "" or val == []:
            continue
        rows_html += f"<tr><th>{_e(label)}</th><td>{_e(_fmt_val(val))}</td></tr>"
    # Catch-all for unknown keys
    known = set(_CS_LABELS) | _CS_SKIP
    for key, val in case_summary.items():
        if key in known or val is None or val == "" or val == []:
            continue
        label = key.replace("_", " ").title()
        rows_html += f"<tr><th>{_e(label)}</th><td>{_e(_fmt_val(val))}</td></tr>"
    if rows_html:
        parts.append(f"<table><tbody>{rows_html}</tbody></table>")

    # ---- 2. Transit Envelope
    parts.append("<h2>2. Transit Envelope</h2>")
    if transit:
        rows_html = ""
        mode_mix = transit.get("mode_mix") or {}
        if mode_mix:
            mix_str = ", ".join(
                f"{k.replace('_',' ').title()} {int(v * 100)}%"
                for k, v in mode_mix.items() if v
            )
            rows_html += f"<tr><th>Mode Mix</th><td>{_e(mix_str)}</td></tr>"
        if transit.get("vibration_level") is not None:
            rows_html += (
                f"<tr><th>Vibration Level</th>"
                f"<td>{transit['vibration_level']:.3f} g<sub>rms</sub></td></tr>"
            )
        if transit.get("drop_height_m") is not None:
            rows_html += f"<tr><th>Drop Height</th><td>{transit['drop_height_m']:.2f} m</td></tr>"
        if transit.get("compression_load_n") is not None:
            rows_html += f"<tr><th>Compression Load</th><td>{int(transit['compression_load_n'])} N</td></tr>"
        if transit.get("handling_fraction") is not None:
            rows_html += f"<tr><th>Handling Fraction</th><td>{transit['handling_fraction']:.0%}</td></tr>"
        if transit.get("notes"):
            rows_html += f"<tr><th>Notes</th><td>{_e(transit['notes'])}</td></tr>"
        if rows_html:
            parts.append(f"<table><tbody>{rows_html}</tbody></table>")


    # ---- 3. ISTA 2A Results
    if ista2a:
        parts.append("<h2>3. ISTA 2A Results</h2>")
        overall = ista2a.get("overall_verdict", "")
        drops = ista2a.get("drops") or []
        n_drops = len(drops)
        n_pass = sum(1 for d in drops if str(d.get("verdict", "")).lower() == "pass")

        # Summary line
        cls = "pass" if overall.lower() == "pass" else "fail"
        stack_v = ista2a.get("stack_verdict") or ista2a.get("compression_verdict") or ""
        stack_part = f" Stack compression: {stack_v.lower()}." if stack_v else ""
        parts.append(
            f'<p class="drop-summary">'
            f'Overall verdict: <span class="badge {cls}">{_e(overall.upper())}</span>'
            f"&nbsp;&nbsp;{n_pass} of {n_drops} drop orientations cleared.{_e(stack_part)}"
            "</p>"
        )

        # Drop cards (table layout — xhtml2pdf safe, no flexbox)
        if drops:
            card_cells = []
            for d in drops:
                ori = str(d.get("orientation", "")).replace("_", " ").title()
                sf  = d.get("safety_factor")
                sf_str = f"{sf:.2f}" if sf is not None else "—"
                v   = d.get("impact_velocity_m_s")
                sig = d.get("impact_pressure_mpa")
                kt  = d.get("stress_concentration_kt")
                verd = str(d.get("verdict", "")).upper()
                vbg = "#d1fae5" if verd == "PASS" else "#fee2e2"
                vfg = "#065f46" if verd == "PASS" else "#991b1b"
                cell = (
                    '<td style="width:33%;padding:8pt;border:1px solid #d0d4dc;'
                    'border-radius:4pt;background:#f8fafc;vertical-align:top;">'
                    f'<div style="font-size:8pt;color:#64748b;font-weight:700;'
                    f'text-transform:uppercase;border-bottom:1px solid #e2e8f0;'
                    f'padding-bottom:3pt;margin-bottom:5pt;">Drop · {_e(ori)}</div>'
                    f'<div style="font-size:16pt;font-weight:700;color:#1e293b;line-height:1.1;">{_e(sf_str)}</div>'
                    '<div style="font-size:7pt;color:#94a3b8;margin-bottom:5pt;">Safety Factor</div>'
                    f'<div style="font-size:8pt;color:#475569;">v&nbsp;&nbsp;<b>{_e(str(v) if v is not None else "—")} m/s</b></div>'
                    f'<div style="font-size:8pt;color:#475569;">σ&nbsp;&nbsp;<b>{_e(str(sig) if sig is not None else "—")} MPa</b></div>'
                    f'<div style="font-size:8pt;color:#475569;">K<sub>t</sub>&nbsp;<b>{_e(str(kt) if kt is not None else "—")}</b></div>'
                    f'<div style="margin-top:5pt;background:{vbg};color:{vfg};'
                    f'padding:2pt 7pt;border-radius:8pt;font-size:8pt;font-weight:700;'
                    f'display:inline-block;">{_e(verd)}</div>'
                    "</td>"
                )
                card_cells.append(cell)
            # Pad to multiple of 3 so the row is always full-width
            while len(card_cells) % 3 != 0:
                card_cells.append('<td style="border:none;"></td>')
            rows_html = ""
            for i in range(0, len(card_cells), 3):
                rows_html += "<tr>" + "".join(card_cells[i:i + 3]) + "</tr>"
            parts.append(
                '<table style="border:none;border-collapse:separate;'
                'border-spacing:6pt;width:100%;">'
                f"<tbody>{rows_html}</tbody></table>"
            )

        # Drop verdict bar chart (from build_charts — already a beautiful dark chart)
        drop_chart = charts.get("drop_verdict_bar", "")
        if drop_chart:
            parts.append(_chart_img(drop_chart, "ISTA 2A – Safety Factor by Orientation"))

    # ---- 4. ISTA 6A Results
    if ista6a:
        parts.append("<h2>4. ISTA 6A Results</h2>")
        v6 = ista6a.get("verdict", "")
        if v6:
            cls = "pass" if v6.lower() == "pass" else ("advisory" if "adv" in v6.lower() else "fail")
            parts.append(f'<p>Overall verdict: <span class="badge {cls}">{_e(v6.upper())}</span></p>')
        adv = ista6a.get("advisory") or ista6a.get("notes") or ""
        if isinstance(adv, list):
            adv_html = "<ul>" + "".join(f"<li>{_e(a)}</li>" for a in adv) + "</ul>"
            parts.append(adv_html)
        elif adv:
            parts.append(f"<blockquote>{_e(adv)}</blockquote>")

    # ---- 5. Engineering Narrative (Engineering Calculations stripped)
    clean_md = _strip_eng_calcs(report_md or "")
    if clean_md.strip():
        parts.append("<h2>5. Engineering Narrative</h2>")
        parts.append(f'<div class="narrative">{_md_to_html(clean_md)}</div>')

    return "\n".join(parts)


# --------------------------------------------------------------- public API

def render_pdf(
    *,
    title: str,
    case_summary: dict,
    transit: dict | None = None,
    ista2a: dict | None = None,
    ista6a: dict | None = None,
    risk_zones: list[dict] | None = None,  # accepted for API compat; not rendered in PDF
    report_md: str = "",
    charts: dict[str, str] | None = None,
    generated_on: str = "",
) -> bytes:
    """Render a structured A4 PDF mirroring the in-app Report & Analysis tabs.

    Sections:
        1. User Inputs
        2. Transit Envelope (stress-over-time, shock events, ship telemetry charts)
        3. ISTA 2A Results (drop cards + SF bar chart)
        4. ISTA 6A Results
        5. Engineering Narrative (Engineering Calculations section stripped)

    Tries WeasyPrint first; falls back to xhtml2pdf on Windows (no GTK needed).
    """
    _ = risk_zones  # parameter kept for caller compat; not rendered
    if not _WEASY_OK and not _XHTML2PDF_OK:
        raise RuntimeError(
            "No PDF renderer available. "
            "Install xhtml2pdf (pip install xhtml2pdf) or WeasyPrint with GTK."
        )

    body_html = _build_html(
        title=title,
        case_summary=case_summary or {},
        transit=transit,
        ista2a=ista2a,
        ista6a=ista6a,
        report_md=report_md,
        charts=charts or {},
        generated_on=generated_on,
    )

    full = f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>{_e(title)}</title>
<style>{_BASE_CSS}</style>
</head>
<body>
{body_html}
<div class="footer">Generated by PackTwin.AI &mdash; CPG Packaging Engineering Platform.</div>
</body></html>"""

    if _WEASY_OK:
        return HTML(string=full).write_pdf()

    buf = io.BytesIO()
    result = _pisa.CreatePDF(io.StringIO(full), dest=buf)
    if result.err:
        raise RuntimeError(f"xhtml2pdf render error (code {result.err})")
    return buf.getvalue()
