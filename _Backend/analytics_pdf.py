"""The Analytics PDFs: one chart, one section, or the full report.

Built entirely on the server from analytics.build() - the same numbers the
page shows for the same period - with reportlab's own vector charts, so a
PDF never depends on what a browser happened to render.

Every chart comes with its takeaway, the numbers behind it, and how it is
measured. A section PDF opens with a written summary; the full report
opens with an overview page and then every section.
"""
import io
from xml.sax.saxutils import escape

from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Circle, Drawing, Line, Rect, String
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (CondPageBreak, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

from analytics import MIN_SAMPLE, SECTIONS, WEEKDAYS
from config import LOGO_PATH

GOLD = colors.HexColor('#b88f33')
GREY = colors.HexColor('#cbd5e1')
INK = colors.HexColor('#0d1b30')
TEXT = colors.HexColor('#1f2937')
MUTED = colors.HexColor('#6b7280')
RULE = colors.HexColor('#e5e7eb')
HEAD_BG = colors.HexColor('#f3f4f6')
WIDTH = 7.3 * inch

_base = getSampleStyleSheet()
STYLE = {
    'title': ParagraphStyle('t', parent=_base['Normal'], fontName='Helvetica-Bold', fontSize=15, textColor=INK, leading=18),
    'sub': ParagraphStyle('s', parent=_base['Normal'], fontSize=9, textColor=MUTED, leading=12),
    'h2': ParagraphStyle('h2', parent=_base['Normal'], fontName='Helvetica-Bold', fontSize=13, textColor=INK, spaceBefore=4, spaceAfter=4, leading=16),
    'h3': ParagraphStyle('h3', parent=_base['Normal'], fontName='Helvetica-Bold', fontSize=10.5, textColor=INK, spaceAfter=2, leading=13),
    'body': ParagraphStyle('b', parent=_base['Normal'], fontSize=9.5, textColor=TEXT, leading=13.5),
    'take': ParagraphStyle('k', parent=_base['Normal'], fontSize=9.5, textColor=TEXT, leading=13, spaceAfter=6),
    'method': ParagraphStyle('m', parent=_base['Normal'], fontSize=8, textColor=MUTED, leading=10.5, spaceBefore=4),
    'cell': ParagraphStyle('c', parent=_base['Normal'], fontSize=8.5, textColor=TEXT, leading=10.5),
}
CHART_FONT = 'Helvetica'


def _p(text, style='body'):
    return Paragraph(escape(str(text)), STYLE[style])


def _num(value, suffix='', places=0, empty='–'):
    if value is None:
        return empty
    return f"{value:.{places}f}{suffix}" if places else f"{int(round(value))}{suffix}"


# ---------------------------------------------------------------- building blocks
def _table(header, rows, widths, align_right_from=1, zebra=False):
    data = [header] + rows
    table = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
    style = [
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 7.5),
        ('TEXTCOLOR', (0, 0), (-1, 0), MUTED),
        ('BACKGROUND', (0, 0), (-1, 0), HEAD_BG),
        ('FONT', (0, 1), (-1, -1), 'Helvetica', 8.5),
        ('TEXTCOLOR', (0, 1), (-1, -1), TEXT),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, RULE),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('ALIGN', (align_right_from, 0), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]
    table.setStyle(TableStyle(style))
    return table


def _legend(items):
    drawing = Drawing(WIDTH, 12)
    x = 0
    for label, fill in items:
        drawing.add(Rect(x, 2, 7, 7, fillColor=fill, strokeColor=None))
        drawing.add(String(x + 11, 2, label, fontName=CHART_FONT, fontSize=7.5, fillColor=MUTED))
        x += 18 + len(label) * 4.2
    return drawing


def _heat_table(report):
    d = report['demand']
    peak = max([v for row in d['heatmap'] for v in row] + [0]) or 1
    header = [''] + [h.replace(' AM', 'a').replace(' PM', 'p') for h in d['hours']]
    rows = [[WEEKDAYS[wd][:3]] + [f'{v:.1f}' if v else '' for v in row] for wd, row in enumerate(d['heatmap'])]
    table = Table([header] + rows, colWidths=[0.45 * inch] + [(WIDTH - 0.45 * inch) / len(d['hours'])] * len(d['hours']))
    style = [
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 7.5),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 7),
        ('FONT', (0, 1), (0, -1), 'Helvetica-Bold', 7.5),
        ('TEXTCOLOR', (0, 0), (-1, 0), MUTED), ('TEXTCOLOR', (0, 1), (0, -1), MUTED),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (1, 1), (-1, -1), 1.5, colors.white),
    ]
    for wd, row in enumerate(d['heatmap']):
        for i, v in enumerate(row):
            share = v / peak
            a = 0.12 + 0.88 * share  # gold blended into white - no transparency needed in the PDF
            fill = colors.Color(1 - 0.278 * a, 1 - 0.439 * a, 1 - 0.8 * a) if v else colors.HexColor('#f8fafc')
            style.append(('BACKGROUND', (i + 1, wd + 1), (i + 1, wd + 1), fill))
            if share > 0.6:
                style.append(('TEXTCOLOR', (i + 1, wd + 1), (i + 1, wd + 1), colors.white))
    table.setStyle(TableStyle(style))
    return table


def _axis_style(axis, fmt=None):
    axis.labels.fontName = CHART_FONT
    axis.labels.fontSize = 7
    axis.labels.fillColor = MUTED
    axis.strokeColor = RULE
    if fmt:
        axis.labelTextFormat = fmt


def _line(categories, series, height=1.8 * inch, width=WIDTH, label_every=1, y_suffix=''):
    """A category line chart. series: [(values, colour, dashed)] - None values leave a gap."""
    drawing = Drawing(width, height)
    # ReportLab cannot draw a polyline with no points. A source filter may
    # legitimately leave every wait value or moving average unrecorded.
    series = [(values, colour, dashed) for values, colour, dashed in series
              if any(value is not None for value in values)]
    if not series:
        drawing.add(String(12, height / 2, 'No recorded data for this chart.',
                           fontName=CHART_FONT, fontSize=9, fillColor=MUTED))
        return drawing
    chart = HorizontalLineChart()
    chart.x, chart.y, chart.width, chart.height = 30, 22, width - 40, height - 30
    chart.data = [[v if v is not None else None for v in values] for values, _, _ in series]
    for i, (_, colour, dashed) in enumerate(series):
        chart.lines[i].strokeColor = colour
        chart.lines[i].strokeWidth = 1.6
        if dashed:
            chart.lines[i].strokeDashArray = (4, 3)
    chart.joinedLines = True
    chart.categoryAxis.categoryNames = [c if i % label_every == 0 else '' for i, c in enumerate(categories)]
    _axis_style(chart.categoryAxis)
    chart.categoryAxis.tickDown = 0
    _axis_style(chart.valueAxis, lambda v: f'{v:g}{y_suffix}')
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max([v for values, _, _ in series for v in values if v is not None] + [1]) * 1.15
    chart.valueAxis.visibleGrid = True
    chart.valueAxis.gridStrokeColor = RULE
    chart.valueAxis.gridStrokeWidth = 0.4
    chart.valueAxis.strokeColor = None
    drawing.add(chart)
    return drawing


def _xy(series, height=1.8 * inch, width=WIDTH, x_max=None, y_max=None, x_label='', y_label='', y_suffix=''):
    """An x/y plot. series: [(points, colour, 'line'|'dots'|'dashed')]."""
    drawing = Drawing(width, height)
    plot = LinePlot()
    plot.x, plot.y, plot.width, plot.height = 34, 26, width - 46, height - 44  # room above for the y label
    plot.data = [points for points, _, _ in series]
    for i, (_, colour, kind) in enumerate(series):
        # Dots are markers only - no line joining them.
        plot.lines[i].strokeColor = colors.transparent if kind == 'dots' else colour
        plot.lines[i].strokeWidth = 1.6
        if kind == 'dashed':
            plot.lines[i].strokeDashArray = (4, 3)
        if kind == 'dots':
            plot.lines[i].symbol = makeMarker('Circle', size=3, fillColor=colour, strokeColor=None)
    _axis_style(plot.xValueAxis)
    _axis_style(plot.yValueAxis, lambda v: f'{v:g}{y_suffix}')
    plot.xValueAxis.valueMin, plot.yValueAxis.valueMin = 0, 0
    if x_max:
        plot.xValueAxis.valueMax = x_max
    if y_max:
        plot.yValueAxis.valueMax = y_max
    plot.yValueAxis.visibleGrid = True
    plot.yValueAxis.gridStrokeColor = RULE
    plot.yValueAxis.gridStrokeWidth = 0.4
    drawing.add(plot)
    if x_label:
        drawing.add(String(plot.x + plot.width, 2, x_label, fontName=CHART_FONT, fontSize=7, fillColor=MUTED, textAnchor='end'))
    if y_label:
        drawing.add(String(2, height - 8, y_label, fontName=CHART_FONT, fontSize=7, fillColor=MUTED))
    return drawing


def _dumbbell(rows, a_label, b_label, unit=''):
    """rows: [(label, a, b)] - a filled gold dot, b a hollow grey one, joined by a line."""
    row_h, label_w, value_w = 16, 1.9 * inch, 0.9 * inch
    height = row_h * len(rows) + 6
    drawing = Drawing(WIDTH, height)
    track_x, track_w = label_w, WIDTH - label_w - value_w
    top = max([v for _, a, b in rows for v in (a, b) if v is not None] + [1]) * 1.1
    pos = lambda v: track_x + track_w * (v or 0) / top
    for i, (label, a, b) in enumerate(rows):
        y = height - (i + 1) * row_h + 4
        drawing.add(String(0, y - 2.5, label[:34], fontName=CHART_FONT, fontSize=7.5, fillColor=TEXT))
        drawing.add(Line(track_x, y, track_x + track_w, y, strokeColor=RULE, strokeWidth=0.5))
        if a is not None and b is not None:
            drawing.add(Line(pos(min(a, b)), y, pos(max(a, b)), y, strokeColor=GREY, strokeWidth=1.5))
        if b is not None:
            drawing.add(Circle(pos(b), y, 3.4, fillColor=colors.white, strokeColor=colors.HexColor('#94a3b8'), strokeWidth=1.3))
        if a is not None:
            drawing.add(Circle(pos(a), y, 3.6, fillColor=GOLD, strokeColor=colors.white, strokeWidth=0.8))
        fmt = lambda v: '–' if v is None else (f'{v:g}{unit}' if isinstance(v, (int, float)) else str(v))
        drawing.add(String(WIDTH, y - 2.5, f'{fmt(a)} / {fmt(b)}', fontName=CHART_FONT, fontSize=7.5, fillColor=TEXT, textAnchor='end'))
    return [drawing, _legend([(a_label, GOLD), (b_label, colors.HexColor('#94a3b8'))])]


CATEGORICAL = [colors.HexColor(h) for h in ('#2a78d6', '#eb6834', '#1baf7a', '#eda100')]


def _strips(categories, measures):
    """Part-to-whole: one 100% strip per measure. categories: [(name, {measure: share %}, is_other)]."""
    row_h = 18
    drawing = Drawing(WIDTH, row_h * len(measures) + 4)
    label_w = 0.9 * inch
    for m_i, (key, label) in enumerate(measures):
        y = drawing.height - (m_i + 1) * row_h + 4
        drawing.add(String(0, y + 3, label, fontName=CHART_FONT, fontSize=7.5, fillColor=MUTED))
        x = label_w
        for c_i, (name, shares, other) in enumerate(categories):
            share = shares.get(key) or 0
            w = (WIDTH - label_w) * share / 100
            if w > 0.5:
                drawing.add(Rect(x, y, max(w - 1.5, 0.5), 11, fillColor=GREY if other else CATEGORICAL[c_i % 4], strokeColor=None))
            x += w
    return drawing


def _strip_table(categories, measures):
    rows = [[name] + [_num(shares.get(key), '%') for key, _ in measures] for name, shares, _ in categories]
    return _table(['Category'] + [label for _, label in measures], rows, [3.2 * inch] + [1.2 * inch] * len(measures))


# ---------------------------------------------------------------- one block per chart
def _block_daily(r):
    days = r['demand']['daily']
    every = 1 if len(days) <= 10 else 3 if len(days) <= 31 else 10
    tracked = [x for x in days if x['tracked'] and not x['partial']]
    best = max(tracked, key=lambda x: x['cars'], default=None)
    rows = [['Cars served', f"{r['kpis'][0]['value']:,}"],
            ['Days with records', str(r['period']['tracked_days'])],
            ['Busiest day', f"{best['label']} ({best['cars']} cars)" if best and best['cars'] else '–'],
            ['Rainy days', str(sum(1 for x in tracked if x['weather'] == 'Rainy'))]]
    chart = _line([x['label'] for x in days], [([x['cars'] if x['tracked'] and not x['partial'] else None for x in days], GOLD, False),
                                               ([x.get('avg7') for x in days], colors.HexColor('#94a3b8'), True)], label_every=every)
    return [chart, _legend([('Cars per day', GOLD), ('7-day average', colors.HexColor('#94a3b8'))]), Spacer(1, 6),
            _table(['Figure', 'Value'], rows, [2.2 * inch, 2.2 * inch])]


def _block_heatmap(r):
    d = r['demand']
    rows = [[WEEKDAYS[wd], _num(d['weekday_avg'][wd], places=1), f"{d['hours'][row.index(max(row))]} ({max(row):.1f})" if max(row) else '–']
            for wd, row in enumerate(d['heatmap'])]
    return [_heat_table(r), Spacer(1, 3), _p('Average cars arriving per hour - darker is busier.', 'method'), Spacer(1, 6),
            _table(['Weekday', 'Avg cars / day', 'Busiest hour (avg cars)'], rows, [1.6 * inch, 1.6 * inch, 2.2 * inch])]


def _block_weather(r):
    weather = r['demand']['weather']
    if not weather:
        return [_p('No weather was recorded on bookings in this period. Weather is read from OpenWeatherMap when a booking is '
                   'made - it needs WEATHER_API_KEY to be set in the server settings.', 'body')]
    dry = [w for w in weather if w['condition'] != 'Rainy']
    dry_avg = sum(w['avg'] * w['days'] for w in dry) / sum(w['days'] for w in dry) if dry else None
    rows = [[w['condition'], str(w['days']), f"{w['avg']:.1f}",
             f"{round((w['avg'] / dry_avg - 1) * 100):+d}%" if dry_avg and w['condition'] == 'Rainy' else '']
            for w in weather]
    return [_table(['Weather', 'Days', 'Avg cars / day', 'vs dry days'], rows, [1.6 * inch, 1 * inch, 1.4 * inch, 1.2 * inch])]


def _block_staffing(r):
    st = r['demand']['staffing']
    rows = [[WEEKDAYS[wd], _num(st['peak_cars'][wd], places=1), _num(st['needed'][wd]), _num(st['on_duty'][wd], places=1)] for wd in range(7)]
    return _dumbbell([(WEEKDAYS[wd], st['needed'][wd], st['on_duty'][wd]) for wd in range(7)], 'Staff needed', 'On duty (average)') + [
        Spacer(1, 6), _table(['Weekday', 'Busiest hour (avg cars)', 'Staff needed', 'Staff on duty (avg)'], rows,
                             [1.5 * inch, 1.8 * inch, 1.4 * inch, 1.6 * inch])]


def _block_wait(r):
    waits = r['operations']['wait_by_hour']
    lifecycle=r['operations'].get('lifecycle',{})
    return [_p(lifecycle.get('definition','')),
            _p(f"Average queue wait: {lifecycle.get('queue_wait')} min; service duration: {lifecycle.get('service')} min; pickup delay: {lifecycle.get('pickup_delay')} min ({lifecycle.get('pickup_samples',0)} recorded collections)."),
            _line([w['hour'] for w in waits], [([w['minutes'] for w in waits], GOLD, False)], y_suffix=' min'),
            _table(['Arrival hour', 'Cars', 'Avg queue wait'], [[w['hour'], str(w['cars']), _num(w['minutes'], ' min')] for w in waits],
                   [1.6 * inch, 1 * inch, 1.4 * inch])]


def _block_service_times(r):
    times = r['operations']['service_times']
    if not times:
        return [_p('No finished services in this period.')]
    rows = [[x['service'], str(x['jobs']), f"{x['standard']} min", f"{x['actual']} min", f"{x['actual'] - x['standard']:+d} min"] for x in times]
    return _dumbbell([(f"{x['service']} ({x['jobs']})", x['actual'], x['standard']) for x in times], 'Actual (min)', 'Standard (min)') + [
        Spacer(1, 6), _table(['Service', 'Jobs', 'Standard', 'Actual', 'Difference'], rows, [2.6 * inch, 0.8 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch])]


def _block_cancellations(r):
    items = r['operations']['cancellations']
    if not items:
        return [_p('Every booking in this period went ahead.')]
    total = sum(x['count'] for x in items)
    cats = [(f"{x['reason']} ({x['count']})", {'share': x['count'] / total * 100}, False) for x in items[:4]]
    if len(items) > 4:
        cats.append(('Other', {'share': sum(x['count'] for x in items[4:]) / total * 100}, True))
    return [_strips(cats, [('share', f'All {total}')]), Spacer(1, 4), _strip_table(cats, [('share', 'Share')])]


def _block_accuracy(r):
    acc = r['operations']['accuracy']
    validation=r['operations'].get('model_validation')
    validation_text=(f"Latest real-only chronological holdout: {validation['train_samples']} training jobs, {validation['test_samples']} later test jobs. MAE {validation['mae']} min; RMSE {validation['rmse']} min. Training completed by {validation['train_until']}; test interval {validation['test_from']} to {validation['test_until']}." if validation else 'Not enough real records for a chronological holdout result.')
    if r['source']['key'] != 'real':
        validation_text = ''
    if not acc['samples']:
        return [_p(validation_text),_p('No walk-ins with a saved wait estimate in this period.')]
    points = [tuple(p) for p in acc.get('points', [])]
    top = max([v for p in points for v in p] + [30]) * 1.05
    chart = _xy([(points, colors.Color(0.722, 0.561, 0.2, alpha=0.6), 'dots'), ([(0, 0), (top, top)], colors.HexColor('#94a3b8'), 'dashed')],
                height=2.3 * inch, width=3.6 * inch, x_max=top, y_max=top, x_label='predicted (min)', y_label='actual (min)')
    sample_note = (f"Showing the newest {len(points)} of {acc['samples']} samples; metrics use all samples."
                   if len(points) < acc['samples'] else f"Showing all {len(points)} samples.")
    return [_p(acc.get('definition','')),_p(acc.get('evidence_note','')),_p(validation_text),_p(sample_note,'method'),chart, _legend([('Walk-in', GOLD), ('Exact prediction', colors.HexColor('#94a3b8'))]), Spacer(1, 6),
            _table(['Figure', 'Value'], [['Average difference (MAE)', f"±{acc['mae']} min"], ['Root mean square error (RMSE)', f"{acc['rmse']} min"],
                                         ['Within 15 minutes', f"{acc['within_15']}%"],
                                         ['Walk-ins compared', str(acc['samples'])]], [2.6 * inch, 1.4 * inch])]


def _block_services(r):
    services = r['customers']['services']
    if not services:
        return [_p('No finished services in this period.')]
    named = [s for s in services if not s.get('group')][:4]
    rest = [s for s in services if s not in named]
    cats = [(s['service'], {'cars': s['share'], 'work': s['work_share']}, False) for s in named]
    if rest:
        cats.append((f"Other ({sum(s.get('services', 1) for s in rest)} services)",
                     {'cars': sum(s['share'] for s in rest), 'work': sum(s['work_share'] or 0 for s in rest)}, True))
    measures = [('cars', 'Cars'), ('work', 'Work time')]
    return [_strips(cats, measures), Spacer(1, 4), _strip_table(cats, measures)]


def _block_models(r):
    c = r['customers']
    if not c['models']:
        return [_p('No car models recorded on visits in this period.')]
    rows = [[m['model'], str(m['visits'])] for m in c['models']]
    if c['other_models']:
        rows.append([f"{c['other_models']} other models", str(c['other_model_visits'])])
    if c['unrecorded_visits']:
        rows.append(['Model not recorded', str(c['unrecorded_visits'])])
    top = max(m['visits'] for m in c['models'])
    drawing = Drawing(WIDTH, 15 * len(c['models']) + 4)
    for i, m in enumerate(c['models']):
        y = drawing.height - (i + 1) * 15 + 5
        x_end = 1.9 * inch + (WIDTH - 2.4 * inch) * m['visits'] / top
        drawing.add(String(0, y - 2.5, m['model'], fontName=CHART_FONT, fontSize=7.5, fillColor=TEXT))
        drawing.add(Line(1.9 * inch, y, x_end, y, strokeColor=GREY, strokeWidth=1.5))
        drawing.add(Circle(x_end, y, 3.4, fillColor=GOLD, strokeColor=None))
        drawing.add(String(WIDTH, y - 2.5, str(m['visits']), fontName=CHART_FONT, fontSize=7.5, fillColor=MUTED, textAnchor='end'))
    return [drawing, Spacer(1, 6), _table(['Car model', 'Visits'], rows, [3 * inch, 1 * inch])]


def _block_returns(r):
    c = r['customers']
    curve = [(p['days'], p['share']) for p in c.get('return_curve', [])]
    flow = []
    if curve:
        flow += [_xy([(curve, GOLD, 'line')], x_max=60, y_max=100, x_label='days since previous visit', y_label='% of return visits')]
    return flow + [_table(['Time since previous visit', 'Visits'], [[x['label'], str(x['visits'])] for x in c['returns']]
                          + [['Typical gap', _num(c['typical_return_days'], ' days')]], [2.6 * inch, 1.2 * inch])]


def _block_staff(r):
    rows = []
    for s in r['staff']['rows']:
        rating = f"{s['rating']:.1f} ({s['reviews']})" if s['rating'] is not None and s['reviews'] >= MIN_SAMPLE else f"n/a ({s['reviews']})"
        rows.append([str(s['rank'] or '–'), s['name'], str(s['jobs']), _num(s['per_shift'], places=1), _num(s['pace'], '%'),
                     _num(s['on_time'], '%'), str(s['absences']), rating, _num(s['score'], places=3)])
    criteria = [[s['name'], _num(s['speed'], places=1), _num(s['punctuality'], '%', places=1), _num(s['rating'], places=1)] for s in r['staff']['rows']]
    return [_table(['#', 'Staff', 'Jobs', 'Jobs/shift', 'Pace', 'On time', 'Absent', 'Rating (n)', 'MCDA'], rows,
                   [0.3 * inch, 1.55 * inch, 0.55 * inch, 0.75 * inch, 0.65 * inch, 0.7 * inch, 0.6 * inch, 0.95 * inch, 0.7 * inch], align_right_from=2),
            Spacer(1, 8), _p('MCDA criteria (inputs to the score)', 'h3'),
            _table(['Staff', 'Speed (40%)', 'Punctuality (40%)', 'Rating (20%)'], criteria, [2 * inch, 1.3 * inch, 1.5 * inch, 1.3 * inch])]


BLOCK_BUILDERS = {
    'daily': _block_daily, 'heatmap': _block_heatmap, 'weather': _block_weather, 'staffing': _block_staffing,
    'wait': _block_wait, 'service_times': _block_service_times, 'cancellations': _block_cancellations, 'accuracy': _block_accuracy,
    'services': _block_services, 'models': _block_models, 'returns': _block_returns, 'staff': _block_staff,
}


def _block(report, key):
    meta = report['blocks'][key]
    body = BLOCK_BUILDERS[key](report)
    head = [_p(meta['title'], 'h3'), _p(report['takeaways'][key], 'take')]
    # Kept on one page when it fits; a block taller than a page still flows on.
    return [CondPageBreak(2.6 * inch), KeepTogether(head + body + [_p('How this is measured: ' + meta['method'], 'method')]),
            Spacer(1, 16)]


# ---------------------------------------------------------------- documents
def _letterhead(report, title):
    try:
        logo = Image(LOGO_PATH, width=0.55 * inch, height=0.55 * inch)
    except Exception:
        logo = Paragraph('', STYLE['body'])
    text = [Paragraph('MD57 Car Wash &amp; Auto Detailing', STYLE['title']), _p(title, 'sub')]
    right = [_p(f"Period: {report['period']['label']}", 'sub'), _p(f"Generated {report['generated']}", 'sub')]
    table = Table([[logo, text, right]], colWidths=[0.7 * inch, 3.9 * inch, 2.7 * inch])
    table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('ALIGN', (2, 0), (2, 0), 'RIGHT'),
                               ('LINEBELOW', (0, 0), (-1, 0), 1.5, GOLD), ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                               ('LEFTPADDING', (0, 0), (0, 0), 0)]))
    source = report.get('source', {'label': 'All records', 'note': ''})
    caption = source['label'] if source.get('key') == 'all' else source['label'] + '. ' + source['note']
    return [table, Spacer(1, 6), _p(caption, 'method'), Spacer(1, 12)]


def _kpi_strip(report):
    cells = []
    for k in report['kpis']:
        change = ''
        if k['change'] is not None:
            change = f"<br/><font size=7 color='#6b7280'>{'+' if k['change'] >= 0 else ''}{k['change']}% vs previous</font>"
        elif k['note']:
            change = f"<br/><font size=7 color='#6b7280'>{escape(k['note'])}</font>"
        cells.append(Paragraph(f"<font size=6.5 color='#6b7280'>{escape(k['label'].upper())}</font><br/>"
                               f"<font size=12 color='#0d1b30'><b>{escape(k['display'])}</b></font>{change}", STYLE['cell']))
    table = Table([cells], colWidths=[WIDTH / len(cells)] * len(cells))
    table.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), 0.6, RULE), ('LINEAFTER', (0, 0), (-2, -1), 0.6, RULE),
                               ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                               ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    return [table, Spacer(1, 12)]


def _section_summary(report, section):
    return ' '.join(report['takeaways'][key] for key in SECTIONS[section]['blocks'])


def _section(report, section, with_heading=True):
    meta = SECTIONS[section]
    flow = []
    if with_heading:
        flow += [_p(meta['title'], 'h2'), _p(meta['blurb'], 'sub'), Spacer(1, 6)]
    flow += [_p('Summary', 'h3'), _p(_section_summary(report, section)), Spacer(1, 12)]
    for key in meta['blocks']:
        flow += _block(report, key)
    return flow


def _notes(report):
    period = report['period']
    lines = ['Figures cover finished services only; cancelled and no-show bookings are counted separately.',
             'Sales and payments are outside the scope of this system, so no peso amounts are reported.']
    if not period['comparable']:
        lines.append('There are no records for the full period before this one, so no "vs previous" changes are shown.')
    if period['tracked_days'] < period['days']:
        lines.append(f"Records start {period['tracked_from']}; averages use only the {period['tracked_days']} days with records.")
    return [Spacer(1, 4), _p('Notes', 'h3')] + [_p('• ' + line, 'method') for line in lines]


def build_pdf(report, section='all', block=None):
    """PDF bytes and a file name. section: 'all' or a SECTIONS key; block: one chart within it."""
    if block:
        title = f"{report['blocks'][block]['title']} - {SECTIONS[section]['title']}"
        flow = _letterhead(report, title) + _block(report, block)
    elif section == 'all':
        title = 'Analytics Report'
        flow = _letterhead(report, title) + [_p('Overview', 'h2'), Spacer(1, 4)] + _kpi_strip(report)
        for key, meta in SECTIONS.items():
            flow += [_p(meta['title'], 'h3'), _p(_section_summary(report, key)), Spacer(1, 8)]
        flow += _notes(report)
        for key in SECTIONS:
            flow += [PageBreak()] + _section(report, key)
    else:
        title = f"{SECTIONS[section]['title']} Report"
        flow = _letterhead(report, title) + _kpi_strip(report) + _section(report, section, with_heading=False) + _notes(report)

    buffer = io.BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(0.6 * inch, 0.4 * inch, f"MD57 Car Wash  ·  {title}  ·  {report['period']['label']}")
        canvas.drawRightString(letter[0] - 0.6 * inch, 0.4 * inch, f'Page {doc.page}')
        canvas.restoreState()

    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.55 * inch, bottomMargin=0.7 * inch,
                            leftMargin=0.6 * inch, rightMargin=0.6 * inch, title=f'MD57 {title}', author='MD57 Car Wash')
    doc.build(flow, onFirstPage=footer, onLaterPages=footer)
    slug = (block or section).replace('_', '-')
    filename = f"MD57_{'Analytics' if section == 'all' and not block else slug.title().replace('-', '')}_{report['period']['start']}_to_{report['period']['end']}.pdf"
    return buffer.getvalue(), filename
