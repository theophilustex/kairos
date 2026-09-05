"""Kairos's test suite.

Run it with::

    python3 -m unittest discover -s tests -v

or just ``make test``.  There is no test framework to install — everything
here is the standard library's :mod:`unittest`.

**This file runs before any test module**, and that matters: it points the
XDG environment variables at a throwaway directory so the tests never touch
your real settings, your real cache, or your real keyring.  :mod:`kairos.config`
reads those variables when it is first imported, so redirecting them has to
happen before ``import kairos`` anywhere.
"""

import atexit
import os
import shutil
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="kairos-tests-")

os.environ["XDG_CONFIG_HOME"] = os.path.join(_SANDBOX, "config")
os.environ["XDG_CACHE_HOME"] = os.path.join(_SANDBOX, "cache")
os.environ["XDG_DATA_HOME"] = os.path.join(_SANDBOX, "data")


def _install_memory_keyring() -> None:
    """Swap in a keyring that only exists for the length of the test run.

    The tests must never read or write the developer's real keyring, but they
    do need a *working* one — otherwise the credential path in
    :mod:`kairos.security` is never exercised, and a test that stores a
    password would silently get nothing back.
    """
    try:
        import keyring
        from keyring.backend import KeyringBackend
    except ImportError:
        return

    class MemoryKeyring(KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def __init__(self):
            super().__init__()
            self._store: dict[tuple, str] = {}

        def get_password(self, service, username):
            return self._store.get((service, username))

        def set_password(self, service, username, password):
            self._store[(service, username)] = password

        def delete_password(self, service, username):
            self._store.pop((service, username), None)

    keyring.set_keyring(MemoryKeyring())


_install_memory_keyring()


@atexit.register
def _cleanup() -> None:
    shutil.rmtree(_SANDBOX, ignore_errors=True)


SANDBOX = _SANDBOX
