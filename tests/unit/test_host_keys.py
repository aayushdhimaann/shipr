"""C-10: host-key pinning and confirmed trust-on-first-use (no network)."""

import paramiko
import pytest

from shipr.errors import ProviderError
from shipr.providers.sftp import SFTPProvider, _HostKeyPolicy, describe_key

KEY = paramiko.ECDSAKey.generate()
OTHER = paramiko.ECDSAKey.generate()


class Client:
    def __init__(self):
        self.keys = paramiko.HostKeys()

    def get_host_keys(self):
        return self.keys


def policy(tmp_path, pins=(), trust=False, confirm=None):
    return _HostKeyPolicy(tmp_path / "known_hosts", list(pins), trust, confirm)


def test_fingerprint_is_shown_once_prefixed():
    text = describe_key(KEY)
    assert text.startswith("ecdsa-sha2-nistp256 SHA256:") and "SHA256:SHA256:" not in text


def test_pinned_key_is_accepted_without_known_hosts(tmp_path):
    c = Client()
    policy(tmp_path, pins=[KEY.fingerprint]).missing_host_key(c, "[h]:2222", KEY)
    assert c.keys.lookup("[h]:2222")
    assert not (tmp_path / "known_hosts").exists()  # the pin is the trust anchor


def test_pinned_mismatch_is_refused_even_with_trust(tmp_path):
    p = policy(tmp_path, pins=[OTHER.fingerprint], trust=True, confirm=lambda h, f: True)
    with pytest.raises(ProviderError, match="HOST KEY MISMATCH"):
        p.missing_host_key(Client(), "h", KEY)
    assert not (tmp_path / "known_hosts").exists()


def test_unknown_host_without_trust_shows_fingerprint_and_how_to_pin(tmp_path):
    with pytest.raises(ProviderError) as e:
        policy(tmp_path).missing_host_key(Client(), "h", KEY)
    assert KEY.fingerprint in str(e.value) and "host_key:" in str(e.value)


@pytest.mark.parametrize("confirm", [None, lambda h, f: False])
def test_trust_needs_a_confirmation(tmp_path, confirm):
    with pytest.raises(ProviderError, match="host_key"):
        policy(tmp_path, trust=True, confirm=confirm).missing_host_key(Client(), "h", KEY)
    assert not (tmp_path / "known_hosts").exists()


def test_confirmed_trust_is_saved_once_and_shown_to_the_user(tmp_path):
    seen = []
    p = policy(tmp_path, trust=True, confirm=lambda h, f: seen.append((h, f)) or True)
    p.missing_host_key(Client(), "[h]:2222", KEY)
    p.missing_host_key(Client(), "[h]:2222", KEY)
    assert seen[0] == ("[h]:2222", describe_key(KEY))
    lines = (tmp_path / "known_hosts").read_text().splitlines()
    assert lines == [f"[h]:2222 {KEY.get_name()} {KEY.get_base64()}"]


def test_pinned_key_is_checked_after_connecting_to_a_known_host():
    p = SFTPProvider({"host": "h", "username": "u", "host_key": OTHER.fingerprint})
    with pytest.raises(ProviderError, match="HOST KEY MISMATCH"):
        p._check_pin(KEY)
    SFTPProvider({"host": "h", "username": "u", "host_key": [OTHER.fingerprint,
                                                               KEY.fingerprint]})._check_pin(KEY)


@pytest.mark.parametrize("bad", ["abc", "MD5:aa:bb", "SHA256:short", ["SHA256:" + "x" * 43, 5]])
def test_invalid_pins_are_config_errors(bad):
    with pytest.raises(ProviderError, match="host_key"):
        SFTPProvider({"host": "h", "username": "u", "host_key": bad})


def test_credentials_never_appear_in_reprs():
    p = SFTPProvider({"host": "h", "username": "u", "password": "hunter2-pw",
                      "passphrase": "hunter2-pp"})
    assert "hunter2" not in repr(p.cfg) and "hunter2" not in str(p.cfg.model_dump())
    assert p.cfg.password.get_secret_value() == "hunter2-pw"


def test_appending_never_glues_onto_a_hand_edited_last_line(tmp_path):
    store = tmp_path / "known_hosts"
    store.write_text(f"other {OTHER.get_name()} {OTHER.get_base64()}")  # no trailing newline
    policy(tmp_path, trust=True, confirm=lambda h, f: True).missing_host_key(Client(), "h", KEY)
    keys = paramiko.HostKeys(str(store))
    assert keys.lookup("other") and keys.lookup("h")
