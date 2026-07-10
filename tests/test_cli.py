from typer.testing import CliRunner

from email_export_import import connection
from email_export_import.cli import app
from tests.fakes import FakeIMAPClient, make_message

runner = CliRunner()


def install_hosts(monkeypatch, by_host):
    def factory(host, port=993, ssl=True):
        return by_host[host]

    monkeypatch.setattr(connection, "IMAPClient", factory)


def base_args(extra=()):
    return [
        "--src-host", "src.test", "--src-email", "a@x.com",
        "--dst-host", "dst.test", "--dst-email", "b@y.com",
        "--yes",
        *extra,
    ]


def test_non_interactive_end_to_end(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )

    assert result.exit_code == 0, result.output
    assert len(dst.folders["INBOX"]) == 1
    assert "Migrated" in result.output


def test_missing_password_env_prompts_are_avoided_with_env(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={"INBOX": []})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert "password" not in result.output.lower()  # no prompt leaked


def test_skip_option_excludes_folder(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={
        "INBOX": [make_message(uid=1, message_id="<a@x>")],
        "Noise": [make_message(uid=2, message_id="<b@x>")],
    })
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path), "--skip", "Noise"]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert not dst.folder_exists("Noise")


def test_auth_failure_non_interactive_exits_1(monkeypatch, tmp_path):
    from imapclient.exceptions import LoginError

    src = FakeIMAPClient()
    src.login_error = LoginError("AUTHENTICATIONFAILED")
    dst = FakeIMAPClient()
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "bad", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 1
    assert "rejected the login" in result.output


def test_preset_fills_host(monkeypatch, tmp_path):
    gmail = FakeIMAPClient(folders={"INBOX": []})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"imap.gmail.com": gmail, "dst.test": dst})

    result = runner.invoke(
        app,
        [
            "--src-preset", "gmail", "--src-email", "a@gmail.com",
            "--dst-host", "dst.test", "--dst-email", "b@y.com",
            "--yes", "--state-dir", str(tmp_path),
        ],
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    # The factory only maps "imap.gmail.com" — a zero exit code proves the
    # preset filled in the host (any other host would KeyError inside invoke).
    assert result.exit_code == 0, result.output


def test_yes_without_host_fails_cleanly_without_prompt(tmp_path):
    result = runner.invoke(
        app,
        ["--src-email", "a@x.com", "--dst-host", "dst.test", "--dst-email", "b@y.com",
         "--yes", "--state-dir", str(tmp_path)],
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 1
    assert "required with --yes" in result.output
    assert "Choose" not in result.output  # no preset menu leaked


def test_yes_without_password_env_fails_cleanly(tmp_path):
    result = runner.invoke(
        app,
        ["--src-host", "src.test", "--src-email", "a@x.com",
         "--dst-host", "dst.test", "--dst-email", "b@y.com",
         "--yes", "--state-dir", str(tmp_path)],
        env={"EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 1
    assert "EEI_SRC_PASSWORD" in result.output


def test_unknown_preset_fails_cleanly(tmp_path):
    result = runner.invoke(
        app,
        ["--src-preset", "gmial", "--src-email", "a@x.com",
         "--dst-host", "dst.test", "--dst-email", "b@y.com",
         "--yes", "--state-dir", str(tmp_path)],
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 1
    assert "unknown preset 'gmial'" in result.output


def test_cert_error_non_interactive_exits_with_hint(monkeypatch, tmp_path):
    import ssl as ssl_mod

    def factory(host, port=993, ssl=True, **kwargs):
        raise ssl_mod.SSLCertVerificationError(
            1, "certificate verify failed: self-signed certificate"
        )

    monkeypatch.setattr(connection, "IMAPClient", factory)
    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 1
    assert "--no-src-verify-ssl" in result.output


def test_no_verify_ssl_flag_skips_verification(monkeypatch, tmp_path):
    import ssl as ssl_mod

    src = FakeIMAPClient(folders={"INBOX": []})
    dst = FakeIMAPClient(folders={"INBOX": []})
    captured = {}

    def factory(host, port=993, ssl=True, **kwargs):
        if host == "src.test":
            captured.update(kwargs)
            return src
        return dst

    monkeypatch.setattr(connection, "IMAPClient", factory)
    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path), "--no-src-verify-ssl"]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert captured["ssl_context"].verify_mode == ssl_mod.CERT_NONE
    assert "verification disabled" in result.output


def test_wizard_offers_retry_without_verification(monkeypatch, tmp_path):
    import ssl as ssl_mod

    src = FakeIMAPClient(folders={"INBOX": []})
    dst = FakeIMAPClient(folders={"INBOX": []})

    def factory(host, port=993, ssl=True, **kwargs):
        if host == "src.test":
            if "ssl_context" not in kwargs:
                raise ssl_mod.SSLCertVerificationError(
                    1, "certificate verify failed: self-signed certificate"
                )
            return src
        return dst

    monkeypatch.setattr(connection, "IMAPClient", factory)
    result = runner.invoke(
        app,
        ["--src-host", "src.test", "--src-port", "993", "--src-email", "a@x.com",
         "--dst-host", "dst.test", "--dst-port", "993", "--dst-email", "b@y.com",
         "--state-dir", str(tmp_path)],
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
        input="y\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "man-in-the-middle" in result.output
    assert "verification disabled" in result.output
