"""Shared shop hours and bay occupancy rules.

Catalogue minutes reserve scheduled slots. Live estimates may use measured
durations. Model work-ahead is a numeric training feature, not a slot promise.
Pending confirmation keeps both the bay and washer occupied until confirmed.
"""
OPEN_HOUR = 8
CLOSE_HOUR = 21
GAP_MINUTES = 10
OVERDUE_ALLOWANCE = 5
OCCUPIED_STATUSES = ('in_progress', 'pending_confirmation')
