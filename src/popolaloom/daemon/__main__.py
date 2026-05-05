"""``python -m popolaloom.daemon`` entry point — delegates to :func:`run`.

Splitting this from :mod:`popolaloom.daemon.main` keeps the public ``main()``
async coroutine clean for tests (which need to ``await main()`` in their own
event loop).
"""

from popolaloom.daemon.main import run

if __name__ == "__main__":
    run()
