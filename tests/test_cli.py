from typer.testing import CliRunner

from email_export_import import connection
from email_export_import.cli import app
from tests.fakes import FakeIMAPClient, make_message

runner = CliRunner()


def install_hosts(monkeypatch, by_host):
    def factory(host, port=993, ssl=True, **kwargs):
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
        input="y\n\ny\n",  # retry w/o verification, spool default, start
    )
    assert result.exit_code == 0, result.output
    assert "man-in-the-middle" in result.output
    assert "verification disabled" in result.output


def test_namespace_prefix_used_for_created_folders(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={"Gelen Kutusu": [make_message(uid=1, message_id="<g@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []}, delimiter=".", namespace_prefix="INBOX.")
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert dst.folder_exists("INBOX.Gelen Kutusu")
    assert len(dst.folders["INBOX.Gelen Kutusu"]) == 1


def test_dst_email_prompt_defaults_to_src_email(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={"INBOX": []})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        ["--src-host", "src.test", "--src-port", "993", "--src-email", "a@x.com",
         "--dst-host", "dst.test", "--dst-port", "993",
         "--state-dir", str(tmp_path)],
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
        input="\n\ny\n",  # dst email default, spool default, start
    )
    assert result.exit_code == 0, result.output
    assert "connected to dst.test as a@x.com" in result.output


def test_workers_flag_end_to_end(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path), "--workers", "2"]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert len(dst.folders["INBOX"]) == 1


def _session_config(workers=1):
    return {
        "src": {"host": "src.test", "port": 993, "ssl": True, "verify_ssl": True,
                "email": "a@x.com"},
        "dst": {"host": "dst.test", "port": 993, "ssl": True, "verify_ssl": True,
                "email": "b@y.com"},
        "skip": [],
        "workers": workers,
    }


def test_wizard_offers_resume_of_unfinished_session(monkeypatch, tmp_path):
    from email_export_import.state import MigrationState

    prior = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    prior.set_config(_session_config())
    prior.flush()

    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        ["--state-dir", str(tmp_path)],  # no connection flags -> resume offer
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
        input="\ny\n",  # accept default session 1, then start migration
    )
    assert result.exit_code == 0, result.output
    assert "a@x.com" in result.output  # session table shown
    assert len(dst.folders["INBOX"]) == 1

    after = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    assert after.status == "completed"


def test_successful_run_saves_config_and_marks_completed(monkeypatch, tmp_path):
    from email_export_import.state import MigrationState

    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output

    saved = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    assert saved.status == "completed"
    assert saved.config["src"]["host"] == "src.test"
    assert "password" not in str(saved.config).lower()


def test_run_with_failures_stays_resumable(monkeypatch, tmp_path):
    from imapclient.exceptions import IMAPClientError

    from email_export_import import connection as conn_mod
    from email_export_import.state import MigrationState

    monkeypatch.setattr(conn_mod.time, "sleep", lambda s: None)
    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    dst.append_error = IMAPClientError("APPEND rejected")
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output

    saved = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    assert saved.status != "completed"  # failed run must stay offered for resume
    assert "retry" in result.output.lower()


def test_resume_reports_already_migrated_count(monkeypatch, tmp_path):
    from email_export_import.state import MigrationState

    prior = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    prior.set_config(_session_config())
    prior.mark_migrated("INBOX", "<a@x>", 1)
    prior.flush()

    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        ["--state-dir", str(tmp_path)],
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
        input="\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "already migrated" in result.output
    assert len(dst.folders["INBOX"]) == 0  # deduped, not re-uploaded


def test_spool_flag_saved_and_spool_dir_used(monkeypatch, tmp_path):
    from email_export_import.state import MigrationState

    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path), "--spool"]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "spool" / "a@x.com__b@y.com").is_dir()

    saved = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    assert saved.config["spool"] is True


def test_spool_default_off(monkeypatch, tmp_path):
    from email_export_import.state import MigrationState

    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "spool").exists()

    saved = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    assert saved.config["spool"] is False


def test_planning_survives_dropped_source_connection(monkeypatch, tmp_path):
    from imapclient.exceptions import IMAPClientAbortError

    from email_export_import import connection as conn_mod

    monkeypatch.setattr(conn_mod.time, "sleep", lambda s: None)

    healthy = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    broken = FakeIMAPClient(folders={"INBOX": []})

    def dead_list(*a, **k):
        raise IMAPClientAbortError("socket error: [Errno 32] Broken pipe")

    broken.list_folders = dead_list
    src_clients = [broken]
    dst = FakeIMAPClient(folders={"INBOX": []})

    def factory(host, port=993, ssl=True, **kwargs):
        if host == "src.test":
            return src_clients.pop(0) if src_clients else healthy
        return dst

    monkeypatch.setattr(connection, "IMAPClient", factory)
    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert len(dst.folders["INBOX"]) == 1


def test_remember_passwords_saves_to_keychain(monkeypatch, tmp_path):
    from email_export_import import secrets_store

    saved = {}
    monkeypatch.setattr(secrets_store, "save_password",
                        lambda h, e, r, pw: saved.__setitem__((h, e, r), pw) or True)
    monkeypatch.setattr(secrets_store, "get_password", lambda h, e, r: None)

    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path), "--remember-passwords"]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert saved[("src.test", "a@x.com", "source")] == "p1"
    assert saved[("dst.test", "b@y.com", "dest")] == "p2"


def test_rate_limit_flag_accepted_and_shows_stats(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path), "--rate-limit", "2"]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert "Duration:" in result.output
    assert "Per folder" in result.output


def test_resume_reads_password_from_keychain(monkeypatch, tmp_path):
    # A saved session whose password lives in the keychain resumes without a
    # prompt and without the env var.
    from email_export_import import secrets_store
    from email_export_import.state import MigrationState

    store = {("src.test", "a@x.com", "source"): "p1",
             ("dst.test", "b@y.com", "dest"): "p2"}
    monkeypatch.setattr(secrets_store, "get_password",
                        lambda h, e, r: store.get((h, e, r)))

    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    # first run creates the session
    runner.invoke(app, base_args(["--state-dir", str(tmp_path)]),
                  env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"})
    # non-interactive resume of the same pair, NO env vars — keychain supplies them
    result = runner.invoke(
        app,
        ["--src-host", "src.test", "--src-email", "a@x.com",
         "--dst-host", "dst.test", "--dst-email", "b@y.com", "--yes",
         "--state-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
