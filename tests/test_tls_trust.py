"""A Python whose default CA store is empty (the python.org macOS installer
before "Install Certificates.command") must still reach HTTPS endpoints, and
`doctor` must say which trust source is in use. Nothing here touches the
network: the empty store is simulated, and the "OS bundle" is a throwaway CA
generated for the test."""

import datetime
import os
import ssl
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import claude_unlimited.cli as cli
import claude_unlimited.daemon_installer as daemon_installer
import claude_unlimited.tls_trust as tls_trust


def _ca_pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _empty_context(*args, **kwargs) -> ssl.SSLContext:
    # What ssl._create_default_https_context yields on a Python with no CAs.
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _ca_count(ctx: ssl.SSLContext) -> int:
    return ctx.cert_store_stats()["x509_ca"]


def _default_capath(monkeypatch, capath):
    # The build's default CA directory (None when it does not exist).
    monkeypatch.setattr(ssl, "get_default_verify_paths",
                        lambda: SimpleNamespace(cafile=None, capath=capath))


@pytest.fixture
def ca_dir(tmp_path):
    """A hashed CA directory like /etc/ssl/certs: files named <hash>.0, one
    of them a real CA, next to entries that are not. The stdlib cannot compute
    OpenSSL's subject hash, so the name is only pattern-checked by the code
    under test (and is not the certificate's real hash here)."""
    directory = tmp_path / "certs"
    directory.mkdir()
    (directory / "5ad8a5d6.0").write_bytes(_ca_pem())
    (directory / "README").write_text("not a certificate\n")
    (directory / "subdir").mkdir()
    return directory


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "bundle.pem"
    path.write_bytes(_ca_pem())
    return path


@pytest.fixture
def empty_store(monkeypatch, bundle):
    """Python's own store is empty and the only OS bundle is `bundle`."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(tls_trust, "_status", None)
    monkeypatch.setattr(tls_trust, "_default_ca_count", lambda: 0)
    monkeypatch.setattr(tls_trust, "_CANDIDATE_BUNDLES", (str(bundle),))
    _default_capath(monkeypatch, None)
    # monkeypatch restores the real hook after the test.
    monkeypatch.setattr(ssl, "_create_default_https_context", _empty_context)
    return bundle


def test_healthy_store_installs_nothing(monkeypatch, bundle):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(tls_trust, "_status", None)
    monkeypatch.setattr(tls_trust, "_default_ca_count", lambda: 5)
    monkeypatch.setattr(tls_trust, "_CANDIDATE_BUNDLES", (str(bundle),))
    monkeypatch.setattr(ssl, "_create_default_https_context", _empty_context)

    status = tls_trust.ensure_ca_bundle()

    assert status.source == "default" and status.ok
    assert ssl._create_default_https_context is _empty_context


def test_empty_store_uses_the_os_bundle(empty_store):
    assert _ca_count(ssl._create_default_https_context()) == 0

    status = tls_trust.ensure_ca_bundle()

    assert (status.source, status.path) == ("fallback", str(empty_store))
    assert _ca_count(ssl._create_default_https_context()) > 0


def test_empty_store_fallback_reaches_http_client(empty_store):
    # HTTPSConnection asks the hook for a context when given none. Its
    # `_context` attribute is assigned in __init__ the same way on every
    # supported version (3.10 through 3.14). urllib's handler is
    # deliberately not inspected:
    # before 3.12 it holds no context at all and asks per connection.
    import http.client

    tls_trust.ensure_ca_bundle()

    assert _ca_count(http.client.HTTPSConnection("example.invalid")._context) > 0


def test_empty_store_without_any_bundle_reports_none(empty_store, monkeypatch, tmp_path):
    monkeypatch.setattr(tls_trust, "_CANDIDATE_BUNDLES", (str(tmp_path / "missing.pem"),))

    status = tls_trust.ensure_ca_bundle()

    assert (status.source, status.ok) == ("none", False)
    assert ssl._create_default_https_context is _empty_context


def test_unparseable_candidate_is_skipped(empty_store, monkeypatch, tmp_path):
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate\n")
    monkeypatch.setattr(tls_trust, "_CANDIDATE_BUNDLES", (str(junk), str(empty_store)))

    status = tls_trust.ensure_ca_bundle()

    assert status.path == str(empty_store)
    assert _ca_count(ssl._create_default_https_context()) > 0


def test_second_call_does_not_wrap_again(empty_store):
    first = tls_trust.ensure_ca_bundle()
    hook = ssl._create_default_https_context
    second = tls_trust.ensure_ca_bundle()

    assert second == first
    assert ssl._create_default_https_context is hook
    assert hook.__wrapped__ is _empty_context


@pytest.mark.parametrize("name", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_user_chosen_trust_is_respected(empty_store, monkeypatch, tmp_path, name):
    monkeypatch.setenv(name, str(tmp_path / "theirs"))

    status = tls_trust.ensure_ca_bundle()

    assert (status.source, status.env) == ("none", (name,))
    assert ssl._create_default_https_context is _empty_context


def test_directory_trust_is_a_healthy_store(empty_store, monkeypatch, ca_dir):
    # A hashed directory reads as 0 CAs until a handshake; with no OS bundle
    # to prefer, it must not be mistaken for an empty store.
    monkeypatch.setattr(tls_trust, "_CANDIDATE_BUNDLES", ())
    _default_capath(monkeypatch, str(ca_dir))
    probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    probe.load_verify_locations(capath=str(ca_dir))
    assert _ca_count(probe) == 0

    status = tls_trust.ensure_ca_bundle()

    assert (status.source, status.path) == ("directory", str(ca_dir))
    assert ssl._create_default_https_context is _empty_context


def test_bundle_is_preferred_over_a_usable_looking_directory(empty_store, monkeypatch, ca_dir):
    # A hash-named file cannot be checked against its subject hash, so the
    # additive OS bundle wins whenever one loads.
    _default_capath(monkeypatch, str(ca_dir))

    status = tls_trust.ensure_ca_bundle()

    assert (status.source, status.path) == ("fallback", str(empty_store))
    assert _ca_count(ssl._create_default_https_context()) > 0


def test_directory_without_a_ca_falls_through_to_the_bundle(empty_store, monkeypatch, tmp_path):
    junk = tmp_path / "junkdir"
    junk.mkdir()
    (junk / "aaaaaaaa.0").write_text("garbage\n")
    (junk / "bbbbbbbb.0").write_bytes(b"")
    _default_capath(monkeypatch, str(junk))

    status = tls_trust.ensure_ca_bundle()

    assert (status.source, status.path) == ("fallback", str(empty_store))


def test_junk_directory_and_no_bundle_reports_none(empty_store, monkeypatch, tmp_path):
    junk = tmp_path / "junkdir"
    junk.mkdir()
    (junk / "aaaaaaaa.0").write_text("garbage\n")
    _default_capath(monkeypatch, str(junk))
    monkeypatch.setattr(tls_trust, "_CANDIDATE_BUNDLES", ())

    assert tls_trust.ensure_ca_bundle().source == "none"


def test_ssl_cert_dir_is_used_instead_of_the_default_directory(empty_store, monkeypatch, ca_dir, tmp_path):
    _default_capath(monkeypatch, str(tmp_path / "elsewhere"))
    monkeypatch.setenv("SSL_CERT_DIR", str(ca_dir))

    status = tls_trust.ensure_ca_bundle()

    assert (status.source, status.env) == ("env", ("SSL_CERT_DIR",))
    assert ssl._create_default_https_context is _empty_context


def test_directory_scan_looks_at_every_entry(empty_store, monkeypatch, tmp_path):
    monkeypatch.setattr(tls_trust, "_CANDIDATE_BUNDLES", ())
    crowded = tmp_path / "crowded"
    crowded.mkdir()
    for n in range(40):
        (crowded / f"junk-{n:03d}.txt").write_text("garbage\n")
    (crowded / "5ad8a5d6.0").write_bytes(_ca_pem())
    _default_capath(monkeypatch, str(crowded))

    assert tls_trust.ensure_ca_bundle().source == "directory"


def test_ca_under_a_non_hash_name_is_not_a_directory_store(empty_store, monkeypatch, tmp_path):
    # OpenSSL would never find it during a handshake.
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    (wrong / "my-ca.pem").write_bytes(_ca_pem())
    (wrong / "5ad8a5d6.r0").write_bytes(_ca_pem())  # a CRL slot, not a CA
    _default_capath(monkeypatch, str(wrong))

    status = tls_trust.ensure_ca_bundle()

    assert (status.source, status.path) == ("fallback", str(empty_store))


def test_directory_parse_attempts_are_capped(monkeypatch, tmp_path):
    crowded = tmp_path / "crowded"
    crowded.mkdir()
    for n in range(30):
        (crowded / f"{n:08x}.0").write_text("garbage\n")
    tried = []
    monkeypatch.setattr(tls_trust, "_file_has_ca", lambda path: tried.append(path) or False)

    assert tls_trust._directory_has_ca(str(crowded)) is False
    assert len(tried) == tls_trust._DIRECTORY_LOAD_ATTEMPTS


def test_setup_failure_never_raises(monkeypatch):
    monkeypatch.setattr(tls_trust, "_status", None)

    def boom():
        raise RuntimeError("no ssl today")

    monkeypatch.setattr(tls_trust, "_default_ca_count", boom)

    assert tls_trust.ensure_ca_bundle().source == "none"


def test_main_sets_up_trust_before_anything_else(monkeypatch):
    calls = []
    monkeypatch.setattr(tls_trust, "ensure_ca_bundle", lambda: calls.append("tls"))
    monkeypatch.setattr(cli, "status", lambda: calls.append("status") or 0)

    assert cli.main(["status"]) == 0
    assert calls == ["tls", "status"]


@pytest.fixture
def doctor_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli.updater, "ensure_cli_aliases", lambda *a, **kw: None)
    monkeypatch.setattr(daemon_installer, "status",
                        lambda: {"installed": True, "running": True, "pid": 1})


def test_doctor_reports_default_store(doctor_env, monkeypatch, capsys):
    monkeypatch.setattr(tls_trust, "_status", tls_trust.TlsStatus("default"))
    cli.doctor()
    assert "TLS certificates: OK — system default store" in capsys.readouterr().out


def test_doctor_reports_a_ca_directory(doctor_env, empty_store, monkeypatch, ca_dir, capsys):
    monkeypatch.setattr(tls_trust, "_CANDIDATE_BUNDLES", ())
    _default_capath(monkeypatch, str(ca_dir))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)

    cli.doctor()

    out = capsys.readouterr().out
    assert f"TLS certificates: OK — system CA directory {ca_dir}" in out
    assert "MISSING" not in out


def test_doctor_names_the_env_var_that_provides_trust(doctor_env, monkeypatch, bundle, capsys):
    monkeypatch.setattr(tls_trust, "_status", None)
    monkeypatch.setattr(tls_trust, "_default_ca_count", lambda: 5)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    cli.doctor()

    assert "TLS certificates: OK — set by SSL_CERT_FILE\n" in capsys.readouterr().out


def test_doctor_env_caused_missing_has_an_env_hint(doctor_env, empty_store, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "nothing.pem"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)

    rc = cli.doctor()
    out = capsys.readouterr().out

    assert "TLS certificates: MISSING — SSL_CERT_FILE provides no CA certificates" in out
    assert "Point SSL_CERT_FILE at a valid CA bundle file, or unset it." in out
    assert "Install Certificates.command" not in out
    assert "otherwise set SSL_CERT_FILE" not in out
    assert rc == 1


def test_doctor_dir_variable_hint_names_a_directory(doctor_env, empty_store, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "nothing"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)

    cli.doctor()

    assert ("Point SSL_CERT_DIR at a valid CA certificate directory, or unset it."
            in capsys.readouterr().out)


def test_doctor_two_variable_missing_uses_the_plural(doctor_env, empty_store, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "nothing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "nothing"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)

    rc = cli.doctor()
    out = capsys.readouterr().out

    assert "MISSING — SSL_CERT_FILE and SSL_CERT_DIR provide no CA certificates" in out
    assert ("Point SSL_CERT_FILE at a valid CA bundle file and SSL_CERT_DIR at a valid "
            "CA certificate directory, or unset them.") in out
    assert "or unset them." in out
    assert rc == 1


def test_env_var_pointing_nowhere_gets_no_credit_when_default_is_healthy(
        doctor_env, monkeypatch, tmp_path, bundle, capsys):
    monkeypatch.setattr(tls_trust, "_status", None)
    monkeypatch.setattr(tls_trust, "_default_ca_count", lambda: 5)
    monkeypatch.setattr(tls_trust, "_CANDIDATE_BUNDLES", (str(bundle),))
    monkeypatch.setattr(ssl, "_create_default_https_context", _empty_context)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "nonexistent"))

    cli.doctor()

    assert "TLS certificates: OK — system default store" in capsys.readouterr().out
    assert tls_trust.ensure_ca_bundle().source == "default"
    assert ssl._create_default_https_context is _empty_context


def test_only_variables_that_supply_cas_are_credited(monkeypatch, tmp_path, bundle, ca_dir):
    both = ("SSL_CERT_FILE", "SSL_CERT_DIR")
    empty = tmp_path / "empty.pem"
    empty.write_bytes(b"")
    monkeypatch.setenv("SSL_CERT_FILE", str(empty))
    monkeypatch.setenv("SSL_CERT_DIR", os.pathsep.join([str(tmp_path / "gone"), str(ca_dir)]))
    assert tls_trust._credited_env(both) == ("SSL_CERT_DIR",)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    assert tls_trust._credited_env(both) == both


def test_env_dir_without_a_hashed_ca_gets_no_credit(doctor_env, monkeypatch, tmp_path, bundle, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "my-ca.pem").write_bytes(_ca_pem())
    monkeypatch.setattr(tls_trust, "_status", None)
    monkeypatch.setattr(tls_trust, "_default_ca_count", lambda: 5)
    monkeypatch.setattr(tls_trust, "_CANDIDATE_BUNDLES", (str(bundle),))
    monkeypatch.setattr(ssl, "_create_default_https_context", _empty_context)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", str(plain))

    cli.doctor()

    assert "TLS certificates: OK — system default store" in capsys.readouterr().out
    assert ssl._create_default_https_context is _empty_context


def test_doctor_credits_only_the_variable_that_works(doctor_env, empty_store, monkeypatch, tmp_path, ca_dir, capsys):
    empty = tmp_path / "empty.pem"
    empty.write_bytes(b"")
    monkeypatch.setenv("SSL_CERT_FILE", str(empty))
    monkeypatch.setenv("SSL_CERT_DIR", str(ca_dir))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)

    rc = cli.doctor()
    out = capsys.readouterr().out

    assert "TLS certificates: OK — set by SSL_CERT_DIR\n" in out
    assert rc == 0
    assert ssl._create_default_https_context is _empty_context


def test_doctor_reports_the_fallback_bundle(doctor_env, empty_store, capsys):
    cli.doctor()
    out = capsys.readouterr().out
    assert f"TLS certificates: OK — using {empty_store} (Python's own store is empty)" in out


def test_doctor_flags_missing_certificates(doctor_env, monkeypatch, capsys):
    monkeypatch.setattr(tls_trust, "_status", tls_trust.TlsStatus("none"))
    # Launcher lookup is environment-dependent and also feeds the result.
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)

    rc = cli.doctor()
    out = capsys.readouterr().out

    assert "TLS certificates: MISSING — Python has no CA certificates" in out
    assert "Install Certificates.command" in out and "SSL_CERT_FILE" in out
    assert "NEEDS ATTENTION" in out and rc == 1


def test_doctor_is_ready_when_trust_is_fine(doctor_env, monkeypatch, capsys):
    monkeypatch.setattr(tls_trust, "_status", tls_trust.TlsStatus("default"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(cli.hud_installer, "is_supported", lambda: False)

    assert cli.doctor() == 0
    assert "Result: READY" in capsys.readouterr().out
