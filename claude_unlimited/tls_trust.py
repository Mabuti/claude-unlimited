"""Make HTTPS work on a Python that ships with an empty CA store.

The python.org macOS installer bundles OpenSSL but no CA certificates until
its "Install Certificates.command" is run, so every HTTPS call this program
makes (the proxy's upstream connection, account resolution, login, usage
probe, updater...) fails with CERTIFICATE_VERIFY_FAILED. The operating
system already has a perfectly good bundle; this module points Python at it,
and only when Python's own store is empty. A healthy system is left exactly
as it was, including one whose trust comes from the user's own
SSL_CERT_FILE / SSL_CERT_DIR.

A directory of hashed CA files is loaded lazily, so it reads as empty until a
handshake needs it, and OpenSSL finds a file there only if its name equals
the certificate's subject hash. The standard library cannot compute that
hash, so a hash-named file is assumed to carry the right one. Because that
cannot be verified, an available OS bundle is preferred over such a
directory: adding it is purely additive, so a mis-named directory can never
leave HTTPS broken. The directory decides only when no bundle loads, or when
the user pointed SSL_CERT_DIR at it.

The hook is `ssl._create_default_https_context`, the PEP 476 indirection
that both `http.client.HTTPSConnection` and urllib's default HTTPS handler
call when the caller passes no context. Wrapping it covers every call site
without touching them. It has to happen BEFORE the first urlopen: from
Python 3.12 urllib's HTTPSHandler builds its context when the handler is
constructed, and the global opener that holds it is cached on the first
urlopen, so a context built before the wrap keeps the old empty trust for
the life of the process (before 3.12 each connection calls the hook, but the
ordering costs nothing there). `cli.main()` is the one process entrypoint
(the console scripts, `python -m claude_unlimited` and the background
service all go through it), so it calls this first.

Standard library only; no platform branches. On Windows the default store is
read from the system store and is never empty, so the fallback never fires.
"""

from __future__ import annotations

import os
import re
import ssl
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

# OS-provided bundles, first readable one that actually parses wins.
_CANDIDATE_BUNDLES = (
    "/etc/ssl/cert.pem",                   # macOS; also Alpine, Arch, BSD
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",    # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",              # openSUSE
)

# A user who sets these has chosen their own trust; we never override it.
_USER_TRUST_ENV = ("SSL_CERT_FILE", "SSL_CERT_DIR")

_MARKER = "_claude_unlimited_ca_fallback"


# OpenSSL finds a CA in a directory only by its subject-hash name, e.g.
# "5ad8a5d6.0" (CRLs are ".r0" and do not match). A valid CA under any other
# name, such as "my-ca.pem", is invisible to a handshake.
_HASHED_CA_NAME = re.compile(r"^[0-9a-f]{8}\.[0-9]+$")

# Names are filtered for free, so every entry is looked at; only the number
# of files actually parsed per directory is capped.
_DIRECTORY_LOAD_ATTEMPTS = 10


@dataclass(frozen=True)
class TlsStatus:
    # "default": Python's own store has CAs. "env": the user's SSL_CERT_FILE /
    # SSL_CERT_DIR (named in `env`) supplies them. "directory": trust comes
    # from the hashed CA directory `path`. "fallback": Python's store was
    # empty and `path` is the OS bundle now added to every default HTTPS
    # context. "none": nothing provides CAs and nothing could be added.
    source: str
    path: Optional[str] = None
    # Which of SSL_CERT_FILE / SSL_CERT_DIR are set (for "none") or actually
    # supply CAs (for "env"). Setting either one, whether or not it works,
    # means no fallback is ever attempted: the user chose their trust.
    env: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.source != "none"


_status: Optional[TlsStatus] = None


def _default_ca_count() -> int:
    return ssl.create_default_context().cert_store_stats().get("x509_ca", 0)


def _set_env() -> Tuple[str, ...]:
    return tuple(name for name in _USER_TRUST_ENV if os.environ.get(name))


def _file_has_ca(path: str) -> bool:
    try:
        probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        probe.load_verify_locations(cafile=path)
        return probe.cert_store_stats().get("x509_ca", 0) > 0
    except (ssl.SSLError, OSError, ValueError):
        return False


def _directory_has_ca(directory: str) -> bool:
    """A directory of hashed CA files reports zero CAs until a handshake
    looks one up, so `cert_store_stats()` cannot see it. Check offline that
    an entry OpenSSL would actually find (hash-named) parses as a CA. Never
    raises."""
    attempts = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not _HASHED_CA_NAME.match(entry.name):
                    continue
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if _file_has_ca(entry.path):
                    return True
                attempts += 1
                if attempts >= _DIRECTORY_LOAD_ATTEMPTS:
                    break
    except (OSError, ValueError):
        pass
    return False


def _credited_env(names: Tuple[str, ...]) -> Tuple[str, ...]:
    # Only a variable that really supplies CAs gets credit for the trust; one
    # aimed at a missing, empty or CA-less path does nothing for OpenSSL.
    def supplies_cas(name: str) -> bool:
        value = os.environ.get(name, "")
        if name == "SSL_CERT_FILE":
            return _file_has_ca(value)
        return any(_directory_has_ca(d) for d in value.split(os.pathsep) if d)
    return tuple(name for name in names if supplies_cas(name))


def _ca_directories() -> List[str]:
    # What OpenSSL will search: SSL_CERT_DIR if set (it may list several),
    # else the build's default. `capath` is None when that directory is absent.
    override = os.environ.get("SSL_CERT_DIR")
    if override:
        return [d for d in override.split(os.pathsep) if d]
    capath = ssl.get_default_verify_paths().capath
    return [capath] if capath else []


def _usable_ca_directory() -> Optional[str]:
    for directory in _ca_directories():
        if _directory_has_ca(directory):
            return directory
    return None


def _bundle_ca_count(original: Callable[..., ssl.SSLContext], path: str) -> int:
    ctx = original()
    ctx.load_verify_locations(cafile=path)
    return ctx.cert_store_stats().get("x509_ca", 0)


def _install(path: str) -> None:
    original = ssl._create_default_https_context

    def _create_https_context_with_os_bundle(*args, **kwargs):
        ctx = original(*args, **kwargs)
        try:
            ctx.load_verify_locations(cafile=path)
        except (ssl.SSLError, OSError):
            # The bundle was verified when it was chosen; if it has since
            # gone bad, fail the handshake normally rather than the call.
            pass
        return ctx

    setattr(_create_https_context_with_os_bundle, _MARKER, path)
    _create_https_context_with_os_bundle.__wrapped__ = original
    ssl._create_default_https_context = _create_https_context_with_os_bundle


def ensure_ca_bundle() -> TlsStatus:
    """Idempotent and never raises. Call before any network use."""
    global _status
    if _status is not None:
        return _status
    try:
        _status = _ensure()
    except Exception:
        # TLS setup must never stop the process from starting.
        _status = TlsStatus("none")
    return _status


def _install_first_bundle() -> Optional[TlsStatus]:
    original = ssl._create_default_https_context
    for path in _CANDIDATE_BUNDLES:
        try:
            if _bundle_ca_count(original, path) > 0:
                _install(path)
                return TlsStatus("fallback", path)
        except (ssl.SSLError, OSError, ValueError):
            continue
    return None


def _ensure() -> TlsStatus:
    env = _set_env()
    credited = _credited_env(env)
    if _default_ca_count() > 0:
        return TlsStatus("env", env=credited) if credited else TlsStatus("default")
    if env:
        # The user chose their trust: never add ours, whatever it yields.
        directory = _usable_ca_directory()
        if directory:
            return TlsStatus("env", env=credited) if credited else TlsStatus("directory", directory)
        return TlsStatus("none", env=env)
    bundled = _install_first_bundle()
    if bundled:
        return bundled
    directory = _usable_ca_directory()
    if directory:
        return TlsStatus("directory", directory)
    return TlsStatus("none")
