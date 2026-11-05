import csv
import io
import json
from collections import Counter
from xml.sax.saxutils import escape


def render(rows, format="json"):
    fields = ["_id", "acs_id", "model", "manufacturer", "firmware", "status", "enrollment", "last_inform"]
    data = [{k: row.get(k) for k in fields} for row in rows]
    if format == "json":
        return json.dumps(data, default=str, indent=2).encode(), "application/json"
    if format == "csv":
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        for row in data:
            # Guard spreadsheet formula injection from device-supplied labels.
            writer.writerow(
                {
                    k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v
                    for k, v in row.items()
                }
            )
        return out.getvalue().encode(), "text/csv"
    counts = Counter(row.get("status", "UNKNOWN") for row in data)
    if format == "png":
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        fig = Figure(figsize=(8, 4), layout="constrained")
        FigureCanvasAgg(fig)
        ax = fig.subplots()
        ax.bar(list(counts), list(counts.values()), color="#278d9d")
        ax.set(title=f"Fleet status — {len(data)} devices", ylabel="Device count")
        out = io.BytesIO()
        fig.savefig(out, format="png")
        return out.getvalue(), "image/png"
    if format == "pdf":
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        out = io.BytesIO()
        styles = getSampleStyleSheet()
        story = [
            Paragraph("GenieACS Fleet Report", styles["Title"]),
            Paragraph(f"{len(data)} devices. Activity is inferred from Inform timestamps.", styles["Normal"]),
            Spacer(1, 16),
        ]
        table = Table(
            [["State", "Devices"]] + [[k, str(v)] for k, v in sorted(counts.items())], colWidths=[330, 130]
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dceff1")),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                ]
            )
        )
        story += [table, Spacer(1, 16)]
        for row in data:
            story.append(
                Paragraph(escape(str(row["acs_id"])) + " — " + escape(str(row["status"])), styles["Normal"])
            )
        SimpleDocTemplate(out).build(story)
        return out.getvalue(), "application/pdf"
    raise ValueError("Use json, csv, pdf or png")


def chart(rows, path):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    points = {}
    is_counter = path.endswith(("BytesReceived", "BytesSent"))
    for row in rows:
        for m in row["measurements"]:
            value = m.get("bps") if is_counter else m.get("value")
            if m["path"] == path and type(value) in (int, float) and m.get("sample_at"):
                points[m["sample_at"]] = value / 1e6 if is_counter else value
    if not points:
        raise ValueError("No measured history for this parameter; counter charts need two distinct samples")
    fig = Figure(figsize=(10, 4), layout="constrained")
    FigureCanvasAgg(fig)
    ax = fig.subplots()
    ordered = sorted(points.items())
    ax.plot([p[0] for p in ordered], [p[1] for p in ordered], marker=".", color="#278d9d")
    ax.set(
        title=path,
        ylabel="Mbps" if is_counter else "Reported value (device units)",
        xlabel="Sample time (UTC)",
    )
    fig.autofmt_xdate()
    out = io.BytesIO()
    fig.savefig(out, format="png")
    return out.getvalue()
