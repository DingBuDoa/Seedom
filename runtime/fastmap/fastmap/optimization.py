"""Bounded optimization policy, independent of CUDA and the desktop UI."""
import json
from pathlib import Path

_data = json.loads(Path(__file__).with_name('optimization_profiles.json').read_text(encoding='utf-8'))
REVISION = _data['revision']
PROFILES = _data['profiles']
_profile = 'balanced'


def configure(profile='balanced'):
    global _profile
    if profile not in PROFILES:
        raise ValueError(f'Unknown optimization profile: {profile}')
    _profile = profile
    return policy()


def policy():
    return dict(PROFILES[_profile], profile=_profile, revision=REVISION)


def report_stop(stage, reason, **details):
    print('[POINTSTUDIO_OPTIMIZATION] ' + json.dumps(
        dict(stage=stage, reason=reason, profile=_profile, **details), allow_nan=False), flush=True)
