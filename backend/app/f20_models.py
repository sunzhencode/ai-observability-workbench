"""Frozen import path for applied migrations. **Do not import this elsewhere.**

The models moved to `registry_models.py` on 2026-07-31, because this file's name
claimed F20 while holding F20 through F27. The path still has to exist: the
`from app.f20_models import ...` lines inside applied migration functions are
covered by those migrations' checksums (`Migration.checksum` hashes
`inspect.getsource(self.apply)`), so rewriting them would make every existing
database refuse to start.

This is not a convenience alias, it is a fossil. New code imports
`app.registry_models`; `tests/test_model_module_boundary.py` fails the build if
anything but `migrations.py` reaches for this one.
"""

from app.registry_models import *  # noqa: F401,F403
