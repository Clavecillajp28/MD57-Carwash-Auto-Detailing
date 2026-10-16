"""Shared JSON shape and scalar validation before route-specific business rules."""
import math

TEXT_FIELDS = set('username fullname email notify_email password new_password current_password confirm_password otp code role customer_name name plate_number vehicle_type vehicle_model customer_email booking_source booking_date booking_time service_type service_name service_category default_bay model_name from_name staff_name date_str status time_in time_out shift_date shift_note unavailable_date reason comment concern subject message action purpose challenge_id event message-id'.split())


def validate_object(body):
    if not isinstance(body, dict):
        return 'A JSON object is required.'
    for key, value in body.items():
        if key in TEXT_FIELDS and value is not None and not isinstance(value, str):
            return f'{key} must be text.'
        if isinstance(value, str) and len(value) > (10000 if key in ('comment','concern','message') else 512):
            return f'{key} is too long.'
        if isinstance(value, float) and not math.isfinite(value):
            return f'{key} must be finite.'
        if isinstance(value, (dict, list)) and key not in ('prices','steps','tags'):
            return f'{key} must be a scalar value.'
        if key in ('prices','steps') and value is not None:
            if not isinstance(value, dict) or len(value)>100:
                return f'{key} must be an object with at most 100 entries.'
            if any(isinstance(item,(dict,list)) for item in value.values()):
                return f'{key} contains an invalid value.'
    return None
