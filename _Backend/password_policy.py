"""One policy for newly chosen passwords; existing logins remain compatible."""
import unicodedata

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 16
PASSWORD_HINT = 'Use 8 to 16 characters, including at least one special character (such as !, @, # or $).'
# HTML patterns use Unicode sets; punctuation and symbols count as special.
PASSWORD_PATTERN = r'(?=.*[\p{P}\p{S}])[^\p{C}]{8,16}'


def password_error(password):
    if (not isinstance(password, str)
            or not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH
            or any(unicodedata.category(char).startswith('C') for char in password)
            or not any(unicodedata.category(char)[0] in ('P', 'S') for char in password)):
        return PASSWORD_HINT
    return None
