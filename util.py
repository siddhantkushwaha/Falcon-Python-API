import json
import os

from viper.customLogging import get_logger

import params

logger = get_logger('falcon', log_level=25, path=params.root_dir)


def log(msg):
    logger.log(25, msg)


def error(msg):
    logger.error(msg)


def save_mail_to_cache(mail):
    mail_id = mail['id']
    pt = os.path.join(params.root_dir, 'dump', f'{mail_id}.json')
    os.makedirs(os.path.dirname(pt), exist_ok=True)
    with open(pt, 'w') as fp:
        fp.write(json.dumps(mail))


def get_mail_from_cache(mail_id):
    pt = os.path.join(params.root_dir, 'dump', f'{mail_id}.json')
    if os.path.exists(pt):
        with open(pt, 'rb') as fp:
            mail = json.load(fp)
            return mail
    return None


def get_key(obj, keys, if_none_val=None):
    for key in keys:
        if obj is None:
            break
        obj = obj.get(key, None)

    if obj is None and if_none_val is not None:
        obj = if_none_val

    return obj


def set_key(obj, keys, val):
    if obj is None:
        raise Exception('Root object cannot be none for inplace op.')

    for key in keys[:-1]:
        child = obj.get(key, None)
        if child is None:
            child = {}
            obj[key] = child
        obj = child

    obj[keys[-1]] = val
