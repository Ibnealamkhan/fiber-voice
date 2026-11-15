"""Private, on-disk call log (recents and missed calls)."""
import json
import threading
import time

from registration import private_json

LIMIT = 500


def classify(call):
    """Summarize a finished call as one user-facing result."""
    if getattr(call, 'screened', None):
        return call.screened  # 'blocked' or 'silenced' (Do Not Disturb)
    if call.connected:
        return 'answered'
    if call.direction == 'incoming':
        return 'declined' if getattr(call, 'user_declined', False) else 'missed'
    code = call.last_sip or 0
    cause = getattr(call, 'failure_cause', None)
    if call.state == 'cancelled':
        return 'cancelled'
    if code in (486, 600) or cause == 17:
        return 'busy'
    if code == 603 or cause == 21:
        return 'declined'
    if code in (404, 484) or cause in (1, 22, 28):
        return 'invalid'
    if cause in (18, 19, 102) or code in (408, 487):
        return 'no_answer'
    if code == 480 or cause in (20, 27):
        return 'unavailable'  # switched off / out of coverage: it never rang
    return 'failed'


class CallHistory:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = None

    def _load(self):
        if self.data is None:
            try:
                data = json.loads(self.path.read_text())
                if not isinstance(data.get('entries'), list):
                    raise ValueError
                self.data = data
            except (OSError, ValueError, AttributeError):
                self.data = {'entries': [], 'seenMissedAt': 0}
        return self.data

    def _save(self):
        private_json(self.path, self.data)

    def add(self, call):
        entry = {'id': call.id, 'direction': call.direction, 'number': call.number,
                 'result': classify(call), 'detail': call.error if call.state == 'failed' else None, 'startedAt': round(call.created, 3),
                 'connectedAt': round(call.connected, 3) if call.connected else None,
                 'endedAt': round(call.ended or time.time(), 3),
                 'duration': int((call.ended or time.time()) - call.connected) if call.connected else 0}
        with self.lock:
            data = self._load()
            data['entries'] = [e for e in data['entries'] if e.get('id') != call.id]
            data['entries'].insert(0, entry)
            del data['entries'][LIMIT:]
            self._save()
        return entry

    def missed_unseen(self):
        data = self._load()
        seen = data.get('seenMissedAt', 0)
        return sum(1 for e in data['entries'] if e.get('result') == 'missed' and e.get('endedAt', 0) > seen)

    def list(self, limit=200):
        with self.lock:
            data = self._load()
            return {'entries': data['entries'][:limit], 'missedUnseen': self.missed_unseen()}

    def mark_seen(self):
        with self.lock:
            self._load()['seenMissedAt'] = time.time()
            self._save()
            return {'missedUnseen': 0}

    def delete(self, entry_id):
        with self.lock:
            data = self._load()
            before = len(data['entries'])
            data['entries'] = [e for e in data['entries'] if e.get('id') != entry_id]
            if len(data['entries']) != before:
                self._save()
            return before != len(data['entries'])

    def clear(self):
        with self.lock:
            self.data = {'entries': [], 'seenMissedAt': time.time()}
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
