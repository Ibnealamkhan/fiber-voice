"""Contacts kept by the app on this Mac: names given to numbers, and favourites.

Names from the Mac's own Contacts are read by the native app and never stored here.
"""
import json
import re
import threading

from registration import private_json

MAX_NAME = 80
MAX_CONTACTS = 5000
MAX_FAVORITES = 50


def contact_key(number):
    """Match the same line however it was written: +91 98765 43210, 09876543210, 9876543210."""
    digits = re.sub(r'\D', '', str(number or ''))
    return digits[-10:] if len(digits) >= 10 else digits


def dialable(number):
    return re.sub(r'[^\d+]', '', str(number or ''))[:16]


def clean_name(name):
    if not isinstance(name, str):
        raise ValueError('Name must be text')
    name = re.sub(r'[\x00-\x1f\x7f]', '', name).strip()
    name = re.sub(r'\s+', ' ', name)
    if len(name) > MAX_NAME:
        raise ValueError(f'Names can be at most {MAX_NAME} characters')
    return name


def valid_key(number):
    key = contact_key(number)
    if not 3 <= len(key) <= 15:
        raise ValueError('Enter a valid telephone number')
    return key


class LocalContacts:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = None

    def _load(self):
        if self.data is None:
            try:
                raw = json.loads(self.path.read_text())
                strings = lambda d: {k: v for k, v in (d or {}).items() if isinstance(k, str) and isinstance(v, str)}
                self.data = {'contacts': strings(raw.get('contacts')), 'numbers': strings(raw.get('numbers')),
                             'favorites': [k for k in raw.get('favorites', []) if isinstance(k, str)]}
            except (OSError, ValueError, AttributeError):
                self.data = {'contacts': {}, 'numbers': {}, 'favorites': []}
        return self.data

    def _save(self):
        private_json(self.path, self.data)

    def snapshot(self):
        with self.lock:
            data = self._load()
            return {'contacts': dict(data['contacts']), 'numbers': dict(data['numbers']),
                    'favorites': list(data['favorites'])}

    def set(self, number, name):
        key = valid_key(number)
        name = clean_name(name)
        with self.lock:
            data = self._load()
            if name:
                if key not in data['contacts'] and len(data['contacts']) >= MAX_CONTACTS:
                    raise ValueError('Too many saved names')
                data['contacts'][key] = name
                data['numbers'][key] = dialable(number) or key
            else:
                data['contacts'].pop(key, None)
                if key not in data['favorites']:
                    data['numbers'].pop(key, None)
            self._save()
        return {'key': key, 'name': name or None}

    def favorite(self, number, on):
        key = valid_key(number)
        with self.lock:
            data = self._load()
            favorites = [k for k in data['favorites'] if k != key]
            if on:
                if len(favorites) >= MAX_FAVORITES:
                    raise ValueError(f'You can have up to {MAX_FAVORITES} favourites')
                favorites.append(key)
                data['numbers'].setdefault(key, dialable(number) or key)
            elif key not in data['contacts']:
                data['numbers'].pop(key, None)
            data['favorites'] = favorites
            self._save()
            return list(favorites)

    def clear(self):
        with self.lock:
            self.data = {'contacts': {}, 'numbers': {}, 'favorites': []}
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
