"""Block list and Do Not Disturb for incoming calls on this Mac.

Blocked callers are declined with SIP 603 (the caller hears the call rejected).
While Do Not Disturb is on, calls are answered with 480 Temporarily Unavailable,
so this Mac stays silent; other devices on the line are not affected.
Both kinds are still written to the call log.
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta

from contacts import contact_key
from registration import private_json

MAX_BLOCKED = 2000
REPEAT_WINDOW = 180  # seconds: a second call from the same number gets through
DEFAULT_DND = {'enabled': False, 'until': None, 'scheduleEnabled': False,
               'start': '22:00', 'end': '07:00', 'repeatCallers': True, 'skipUntil': None}


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('Times must look like 22:00')
    hours, minutes = value.split(':')
    return int(hours) * 60 + int(minutes)


def in_window(minute, start, end):
    """True if minute-of-day is inside [start, end), which may cross midnight."""
    if start == end:
        return False
    return start <= minute < end if start < end else minute >= start or minute < end


class CallScreening:
    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        self.lock = threading.Lock()
        self.data = None
        self.recent_silenced = {}

    def _load(self):
        if self.data is None:
            try:
                data = json.loads(self.path.read_text())
                blocked = {k: v for k, v in data.get('blocked', {}).items()
                           if isinstance(k, str) and isinstance(v, dict)}
                dnd = {**DEFAULT_DND, **{k: v for k, v in data.get('dnd', {}).items() if k in DEFAULT_DND}}
                self.data = {'blocked': blocked, 'dnd': dnd}
            except (OSError, ValueError, AttributeError):
                self.data = {'blocked': {}, 'dnd': dict(DEFAULT_DND)}
        return self.data

    def _save(self):
        private_json(self.path, self.data)

    # ---- Do Not Disturb ----

    def _dnd_state(self, now):
        dnd = self._load()['dnd']
        until = dnd.get('until')
        if until and until <= now:
            dnd['until'] = None
            dnd['enabled'] = False
            until = None
        if until:
            return True, 'timer', until
        if dnd['enabled']:
            return True, 'manual', None
        if dnd['scheduleEnabled']:
            local = datetime.fromtimestamp(now)
            start, end = parse_time(dnd['start']), parse_time(dnd['end'])
            minute = local.hour * 60 + local.minute
            if in_window(minute, start, end):
                # When the scheduled quiet time ends next (for display).
                finish = local.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
                if finish <= local:
                    finish += timedelta(days=1)
                if (dnd.get('skipUntil') or 0) > now:
                    return False, None, None  # turned off for tonight
                return True, 'schedule', finish.timestamp()
        return False, None, None

    def dnd_status(self):
        with self.lock:
            now = self.clock()
            active, reason, until = self._dnd_state(now)
            return {**self._load()['dnd'], 'active': active, 'activeReason': reason, 'activeUntil': until}

    def update_dnd(self, changes):
        if not isinstance(changes, dict):
            raise ValueError('Expected settings')
        with self.lock:
            dnd = dict(self._load()['dnd'])
            now = self.clock()
            if 'enabled' in changes:
                active, reason, finish = self._dnd_state(now)
                dnd['enabled'] = bool(changes['enabled'])
                dnd['until'] = None
                # Turning off during scheduled quiet hours skips the rest of tonight's window.
                dnd['skipUntil'] = finish if not dnd['enabled'] and reason == 'schedule' else None
            if 'minutes' in changes:  # on for a while, then off by itself
                minutes = changes['minutes']
                if not isinstance(minutes, int) or not 1 <= minutes <= 7 * 24 * 60:
                    raise ValueError('Choose between 1 minute and 7 days')
                dnd['enabled'] = False
                dnd['until'] = now + minutes * 60
            if 'until' in changes:
                until = changes['until']
                if until is not None and (not isinstance(until, (int, float)) or not now < until <= now + 7 * 86400):
                    raise ValueError('Choose a time within the next 7 days')
                dnd['until'] = until
                if until:
                    dnd['enabled'] = False
            for key in ('start', 'end'):
                if key in changes:
                    parse_time(changes[key])
                    dnd[key] = changes[key]
            for key in ('scheduleEnabled', 'repeatCallers'):
                if key in changes:
                    dnd[key] = bool(changes[key])
            self._load()['dnd'] = dnd
            self._save()
        return self.dnd_status()

    # ---- Block list ----

    def blocked(self):
        with self.lock:
            items = [{'key': k, **v} for k, v in self._load()['blocked'].items()]
        return sorted(items, key=lambda item: item.get('addedAt', 0), reverse=True)

    def is_blocked(self, number):
        key = contact_key(number)
        with self.lock:
            return bool(key) and key in self._load()['blocked']

    def block(self, number):
        key = contact_key(number)
        if not 3 <= len(key) <= 15:
            raise ValueError('Enter a valid telephone number')
        with self.lock:
            blocked = self._load()['blocked']
            if key not in blocked and len(blocked) >= MAX_BLOCKED:
                raise ValueError('The block list is full')
            blocked.setdefault(key, {'number': re.sub(r'[^\d+]', '', str(number))[:16], 'addedAt': self.clock()})
            self._save()
        return {'key': key, 'blocked': True}

    def unblock(self, key_or_number):
        key = contact_key(key_or_number)
        with self.lock:
            found = self._load()['blocked'].pop(key, None) is not None
            if found:
                self._save()
        return found

    # ---- Decision for an incoming call ----

    def decide(self, number):
        """None to ring normally, or 'blocked' / 'silenced'."""
        if self.is_blocked(number):
            return 'blocked'
        with self.lock:
            now = self.clock()
            active, _, _ = self._dnd_state(now)
            if not active:
                return None
            key = contact_key(number)
            if self._load()['dnd']['repeatCallers'] and key:
                last = self.recent_silenced.get(key)
                if last and now - last <= REPEAT_WINDOW:
                    self.recent_silenced.pop(key, None)
                    return None  # urgent: the same person called again within 3 minutes
                self.recent_silenced = {k: t for k, t in self.recent_silenced.items() if now - t <= REPEAT_WINDOW}
                self.recent_silenced[key] = now
            return 'silenced'

    def status(self):
        return {'blocked': self.blocked(), 'dnd': self.dnd_status()}

    def clear(self):
        with self.lock:
            self.data = {'blocked': {}, 'dnd': dict(DEFAULT_DND)}
            self.recent_silenced = {}
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
