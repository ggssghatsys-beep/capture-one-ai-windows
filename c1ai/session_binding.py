"""In-memory binding to a native Session. Does not write machine configuration."""
from __future__ import annotations

import os
from pathlib import Path
import uuid


class SessionBinding:
    def __init__(self, config):
        self.mode = config.get('session_binding', 'auto')
        if self.mode not in {'auto', 'pinned'}:
            raise ValueError('session_binding must be auto or pinned')
        self.pinned = config.get('session_path')
        if self.mode == 'pinned' and not self.pinned:
            raise ValueError('A pinned binding requires session_path')
        self.current = None

    def accept(self, native, instance):
        from .windows_backend import NativeError
        path = native.get('document_path')
        identifier = native.get('document_uuid')
        if not isinstance(path, str) or not Path(path).is_absolute() or Path(path).suffix.lower() != '.cosessiondb':
            raise NativeError('SESSION_REQUIRED', 'Open a Session in the C1 main window; Catalog binding is not enabled')
        try:
            identifier = str(uuid.UUID(identifier))
            if uuid.UUID(identifier).int == 0:
                raise ValueError('empty document UUID')
        except (ValueError, TypeError, AttributeError) as error:
            raise NativeError('SESSION_IDENTITY_UNAVAILABLE', 'C1 did not provide a valid document UUID') from error
        path = os.path.normcase(os.path.abspath(path))
        if self.mode == 'pinned' and path != os.path.normcase(os.path.abspath(self.pinned)):
            raise NativeError('DOCUMENT_CHANGED', 'C1 is showing a different Session from the pinned configuration')
        identity = {'path': path, 'uuid': identifier,
                    'pid': instance['pid'], 'started_at': instance['started_at']}
        previous = {k: self.current[k] for k in identity} if self.current else None
        if previous != identity:
            self.current = {**identity, 'generation': uuid.uuid4().hex}
        return dict(self.current)
