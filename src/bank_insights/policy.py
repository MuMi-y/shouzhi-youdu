"""Trusted startup configuration; no model-controlled paths or mutation API."""
import copy
import hashlib
import json
import stat


KEY = 'analysis_policy_config'
FILENAME = 'insight-policy.json'
MAX_BYTES = 16384


class PolicyError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def invalid():
    raise PolicyError('INVALID_INSIGHT_POLICY', '洞察规则配置结构、版本或参数无效；未启用新配置。')


def keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        invalid()


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        invalid()


def default_policy():
    return {'category_increase': {'minimum_increase_percent': 20,
                'minimum_absolute_increase_minor': 10000, 'previous_debit_must_be_positive': True},
            'recurring_price_increase': {'minimum_increase_percent': 10,
                'previous_observations_must_be_fixed': True}}


def validate_policy(value):
    keys(value, ('category_increase', 'recurring_price_increase'))
    category, recurring = value['category_increase'], value['recurring_price_increase']
    keys(category, ('minimum_increase_percent', 'minimum_absolute_increase_minor', 'previous_debit_must_be_positive'))
    keys(recurring, ('minimum_increase_percent', 'previous_observations_must_be_fixed'))
    integer(category['minimum_increase_percent'], 1, 1000)
    integer(category['minimum_absolute_increase_minor'], 1, 100000000)
    integer(recurring['minimum_increase_percent'], 1, 1000)
    if category['previous_debit_must_be_positive'] is not True or recurring['previous_observations_must_be_fixed'] is not True:
        invalid()
    return copy.deepcopy(value)


def validate_config(value):
    keys(value, ('schema_version', 'revision', 'thresholds'))
    integer(value['schema_version'], 1, 1)
    integer(value['revision'], 1, 2147483647)
    return {'schema_version': 1, 'revision': value['revision'],
            'thresholds': validate_policy(value['thresholds'])}


def default_config():
    return {'schema_version': 1, 'revision': 1, 'thresholds': default_policy()}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            invalid()
        value[key] = item
    return value


def decode_config(raw):
    try:
        value = json.loads(raw, object_pairs_hook=unique_object,
                           parse_constant=lambda value: invalid())
    except (ValueError, UnicodeError, RecursionError):
        invalid()
    return validate_config(value)


def read_config(directory):
    path = directory / FILENAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise PolicyError('INSIGHT_POLICY_UNREADABLE', '洞察规则配置无法读取。') from None
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > MAX_BYTES:
        invalid()
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            invalid()
        return decode_config(raw.decode('utf-8'))
    except OSError:
        raise PolicyError('INSIGHT_POLICY_UNREADABLE', '洞察规则配置无法读取。') from None
    except UnicodeError:
        invalid()


def choose_config(stored, requested):
    if stored is None:
        return requested if requested is not None else default_config()
    if requested is None:
        return stored
    if requested['revision'] < stored['revision']:
        raise PolicyError('INSIGHT_POLICY_ROLLBACK', '规则配置版本低于已生效版本；未降级。')
    if requested['revision'] == stored['revision'] and requested != stored:
        raise PolicyError('INSIGHT_POLICY_VERSION_CONFLICT', '同一规则配置版本不能更改内容。')
    return requested


def config_metadata(config):
    raw = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'schema_version': config['schema_version'], 'revision': config['revision'],
            'sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest()}
