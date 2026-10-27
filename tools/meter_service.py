"""Narrow systemd write exceptions needed by the existing meter/core contract."""
from pathlib import PurePosixPath


def activity_log_directory(settings):
    if not settings.get('activity_enabled'):
        return None
    value = settings.get('access_log', '/var/log/kenxu/access.log')
    if not isinstance(value, str) or PurePosixPath(value) != PurePosixPath('/var/log/kenxu/access.log') or '..' in value.split('/'):
        raise ValueError('Review nonstandard activity log sandbox explicitly')
    return '/var/log/kenxu'


def flow_meter_dropin(settings):
    paths = ['/var/lib/kenxu-flow']
    directory = activity_log_directory(settings)
    if directory:
        paths.append(directory)
    return '[Service]\nReadWritePaths=' + ' '.join(paths) + '\n'
