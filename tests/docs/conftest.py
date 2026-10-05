"""Guards for the documentation tests.

``docs/reference/DESIGN_DECISIONS.md`` section 10 says unit tests never open a
non-loopback socket. The documentation examples run client code, so they get
the same guard as the unit tests. It is imported rather than copied, so there
is one guard to keep correct. A subprocess, such as the site build in
``test_site.py``, runs outside it and never touches the network.
"""

from tests.unit.conftest import _no_non_loopback_sockets  # noqa: F401
