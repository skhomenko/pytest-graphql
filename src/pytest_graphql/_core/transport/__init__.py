"""HTTP protocol and transport (SPEC 5.6, C3, C13, C19, C23, C59).

``base.py`` holds the public ``Transport`` protocol, the ``DerivableTransportBase``
abstract class derivation is opted into by inheriting, and ``RawResponse``, the
value a transport's ``send()`` returns. ``httpx_transport.py`` holds
``HttpxTransport``: request encoding, C3 response classification, the C13
operational limits, and the connect-only retry rule.
"""

from __future__ import annotations
