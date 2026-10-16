"""Manager Analytics: the page's data and its PDF downloads (see analytics.py
for what every figure means, analytics_pdf.py for the documents)."""
import io

from flask import Blueprint, jsonify, request, send_file, session

import analytics
from data_scope import parse_source
from analytics_pdf import build_pdf
from utils import log_activity, role_required

analytics_api_bp = Blueprint('analytics_api', __name__)


@analytics_api_bp.route('/api/analytics', methods=['GET'])
@role_required('admin')
def api_analytics():
    """Everything on the Analytics page for ?days=7|30|90, or ?start=&end= (YYYY-MM-DD)."""
    try:
        start, end = analytics.parse_period(request.args)
        source = parse_source(request.args.get('source', 'real'))
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    return jsonify({'status': 'success', **analytics.build(start, end, source=source)}), 200


@analytics_api_bp.route('/api/analytics/export', methods=['GET'])
@role_required('admin')
def api_analytics_export():
    """A PDF for the same period: ?section=all (the full report), a section
    (demand, operations, customers, staff), or one chart with &block=."""
    section = request.args.get('section', 'all')
    block = request.args.get('block') or None
    if section != 'all' and section not in analytics.SECTIONS:
        return jsonify({'status': 'error', 'message': 'Unknown report section.'}), 400
    if block and (section == 'all' or block not in analytics.SECTIONS[section]['blocks']):
        return jsonify({'status': 'error', 'message': 'Unknown chart for that section.'}), 400
    try:
        start, end = analytics.parse_period(request.args)
        source = parse_source(request.args.get('source', 'real'))
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

    report = analytics.build(start, end, source=source)
    pdf, filename = build_pdf(report, section, block)
    what = analytics.BLOCKS[block][0] if block else ('full analytics report' if section == 'all' else f"{analytics.SECTIONS[section]['title']} report")
    log_activity(session.get('user', 'Manager'), f"Exported {what} PDF ({report['period']['label']})")
    return send_file(io.BytesIO(pdf), as_attachment=True, download_name=filename, mimetype='application/pdf')
