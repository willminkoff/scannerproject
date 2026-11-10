"""Stop a test from reaching a real radio.

This repo's modules talk to live hardware as a side effect of being used. The
chirp command bus is a UDP socket (``sendto``) and dongle power is an HTTP POST
(``urlopen``); neither confirms, neither dry-runs, and both are reachable from
module level. On 2026-11-10 that cost two outages in one morning:

  * ``tests/test_tune_snapshot_and_logging.py`` ran against a build whose
    airband host was still a hardcoded constant. ``_tune_chirp_band`` removes
    every channel and adds one called ``vfo``, so the live airband pool went
    from 10 channels to a lone ``vfo@119.35``. Three times.
  * A new guard suite was run against unfixed code to prove it failed first.
    One test called ``_venus_power("off")`` without mocking the transport, on
    the reasoning that the refusal branch made it unreachable -- but the
    refusal branch was the thing being added. It POSTed a real ``/power/off``
    and took the airband radio, op25 and two icecast mounts down for three
    minutes.

Both commits that followed (3106e18, 19a2c58) moved one address into the
environment. That fixes two addresses. This fixes the category: with the guard
armed, a socket to anything that is not this machine fails the test instead of
arriving.

Deliberately NOT guarded: ``bind`` (listening harms nothing), DNS, and
subprocesses. A test that shells out to ``ssh`` or ``curl`` escapes this
entirely -- see ``tests/integration/conftest.py``, which does exactly that on
purpose. The guard covers sockets opened by this process, which is where both
outages came from.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import traceback

ENV_OPT_OUT = "SB3_TESTS_ALLOW_NETWORK"
MARKER = "allow_network"

# Names that mean "this machine". Anything else is off-box -- including a name
# that would resolve to loopback, because resolving it is itself a network
# call, and a guard that phones out to decide whether to block is not a guard.
_LOCAL_NAMES = frozenset(
    {"", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NetworkUseInTest(BaseException):
    """A test tried to reach something off this machine.

    Derived from BaseException, NOT Exception, on purpose. The code under test
    is full of ``except Exception: return {"ok": False, ...}`` -- see
    ``_venus_power`` in sb3/ui/routes.py -- so an Exception raised here would
    be caught by the very function being tested. The call would be blocked,
    the function would report a tidy failure, the assertion would still pass,
    and the suite would go green with the guard completely invisible. This repo
    has twice shipped a green suite over broken code; a guard that can be
    swallowed by a bare ``except Exception`` is a third way to do it.
    """


_lock = threading.Lock()
_installed = False
_allow_depth = 0
_violations: list[dict] = []
_originals: dict[str, object] = {}


# --------------------------------------------------------------------------
# address classification
# --------------------------------------------------------------------------


def is_local(family, address) -> bool:
    """True if *address* cannot leave this machine.

    Unknown shapes return False: a guard decides by what it recognises, and
    fails closed on what it does not.
    """
    # AF_UNIX, AF_BLUETOOTH, AF_NETLINK and friends are not IP and do not
    # route. The project does use Bluetooth audio, so blocking them would
    # break real tests for no safety gain.
    if family not in (socket.AF_INET, socket.AF_INET6):
        return True

    host = address
    if isinstance(address, (tuple, list)):
        host = address[0] if address else ""
    if isinstance(host, (bytes, bytearray)):
        host = host.decode("utf-8", "replace")
    if not isinstance(host, str):
        return False

    # fe80::1%en0 -- strip the scope before parsing.
    bare = host.strip().split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(bare)
    except ValueError:
        return host.strip().lower() in _LOCAL_NAMES

    if addr.is_loopback or addr.is_unspecified:
        return True
    # ::ffff:127.0.0.1 is loopback wearing an IPv6 hat.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None and (mapped.is_loopback or mapped.is_unspecified):
        return True
    return False


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _blame() -> list[str]:
    """The repo's own frames, innermost last. Points at the line that did it
    rather than at socket.py."""
    out = []
    for frame in traceback.extract_stack():
        path = os.path.abspath(frame.filename)
        if not path.startswith(_REPO_ROOT):
            continue
        if path == os.path.abspath(__file__):
            continue
        rel = os.path.relpath(path, _REPO_ROOT)
        out.append(f"{rel}:{frame.lineno} in {frame.name}")
    return out[-5:]


def describe(violation: dict) -> str:
    frames = "\n".join(f"    {f}" for f in violation["frames"]) or "    (no repo frames)"
    return (
        f"  {violation['op']}({violation['address']!r})\n{frames}"
    )


def format_violations(items) -> str:
    head = (
        "a test tried to reach a host that is not this machine.\n\n"
        + "\n\n".join(describe(v) for v in items)
        + "\n\n"
    )
    return head + (
        "Addresses like this belong to real radios -- the chirp command bus on\n"
        "7400/7401 and dongle power on 9200 -- and they act on arrival. Mock the\n"
        "transport at EVERY call site, even where the code 'cannot get there':\n\n"
        "    with mock.patch(\"urllib.request.urlopen\") as urlopen:\n"
        "        ...\n"
        "    urlopen.assert_not_called()\n\n"
        f"If this test genuinely needs the network, mark it @pytest.mark.{MARKER},\n"
        f"or run the suite with {ENV_OPT_OUT}=1 to disarm the guard entirely."
    )


def _refuse(op: str, family, address):
    violation = {
        "op": op,
        "family": int(family) if family is not None else None,
        "address": address,
        "frames": _blame(),
    }
    with _lock:
        _violations.append(violation)
    raise NetworkUseInTest(
        format_violations([violation])
    )


# --------------------------------------------------------------------------
# the patches
# --------------------------------------------------------------------------


def _check(sock, op: str, address):
    if _allow_depth > 0:
        return
    family = getattr(sock, "family", socket.AF_INET)
    if is_local(family, address):
        return
    _refuse(op, family, address)


def _wrap_first_arg(name, real):
    def guarded(self, address, *args, **kwargs):
        _check(self, name, address)
        return real(self, address, *args, **kwargs)

    guarded.__name__ = name
    guarded.__doc__ = f"netguard-wrapped socket.{name}; see tests/netguard.py"
    return guarded


def _wrap_sendto(real):
    # sendto(data, address) and sendto(data, flags, address) -- the address is
    # always last. This is the airband wipe's exact path.
    def guarded(self, *args, **kwargs):
        address = kwargs.get("address", args[-1] if args else None)
        _check(self, "sendto", address)
        return real(self, *args, **kwargs)

    guarded.__name__ = "sendto"
    return guarded


def _wrap_sendmsg(real):
    # sendmsg(buffers, ancdata, flags, address). address=None means the socket
    # is already connected, and connect() was checked.
    def guarded(self, *args, **kwargs):
        address = kwargs.get("address", args[3] if len(args) > 3 else None)
        if address is not None:
            _check(self, "sendmsg", address)
        return real(self, *args, **kwargs)

    guarded.__name__ = "sendmsg"
    return guarded


def opted_out() -> bool:
    return (os.environ.get(ENV_OPT_OUT) or "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def install() -> bool:
    """Arm the guard. Idempotent. Returns True if it is now armed.

    Called from the root conftest at import time, which is before pytest
    imports any test module -- so a module that reaches the network while
    merely being imported is caught too. That is not hypothetical: importing
    sb3/ui/routes.py used to be enough to be able to cut power to the radios.
    """
    global _installed
    with _lock:
        if _installed:
            return True
        if opted_out():
            return False
        for name in ("connect", "connect_ex"):
            real = getattr(socket.socket, name, None)
            if real is not None:
                _originals[name] = real
                setattr(socket.socket, name, _wrap_first_arg(name, real))
        real = getattr(socket.socket, "sendto", None)
        if real is not None:
            _originals["sendto"] = real
            socket.socket.sendto = _wrap_sendto(real)
        real = getattr(socket.socket, "sendmsg", None)
        if real is not None:
            _originals["sendmsg"] = real
            socket.socket.sendmsg = _wrap_sendmsg(real)
        _installed = True
        return True


def uninstall() -> None:
    """Restore the real methods. For the guard's own tests."""
    global _installed
    with _lock:
        for name, real in _originals.items():
            setattr(socket.socket, name, real)
        _originals.clear()
        _installed = False


def is_installed() -> bool:
    return _installed


class allowed:
    """Lift the guard for a block. Backs @pytest.mark.allow_network."""

    def __enter__(self):
        global _allow_depth
        with _lock:
            _allow_depth += 1
        return self

    def __exit__(self, *exc):
        global _allow_depth
        with _lock:
            _allow_depth -= 1
        return False


def violations() -> list[dict]:
    with _lock:
        return list(_violations)


def violation_count() -> int:
    with _lock:
        return len(_violations)
