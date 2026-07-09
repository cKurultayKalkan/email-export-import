from email_export_import.errors import (
    AuthFailed,
    ConnectionFailed,
    MigrationError,
    QuotaExceeded,
)
from email_export_import.models import (
    Account,
    FolderPlan,
    ProviderPreset,
    TransferProgress,
)


def test_provider_preset_defaults():
    p = ProviderPreset(key="x", name="X", host="imap.x.com", port=993, ssl=True)
    assert p.app_password_hint is None
    assert p.skip_folders == ()


def test_transfer_progress_defaults_are_independent():
    a = TransferProgress()
    b = TransferProgress()
    a.failures.append("boom")
    assert b.failures == []
    assert (a.migrated, a.skipped, a.failed) == (0, 0, 0)


def test_folder_plan_fields():
    fp = FolderPlan(source="Work/Projects", dest="Work.Projects", create=True)
    assert fp.source == "Work/Projects"
    assert fp.dest == "Work.Projects"
    assert fp.create is True


def test_account_fields():
    a = Account(host="h", port=993, ssl=True, email="a@x", password="p")
    assert a.email == "a@x"


def test_error_hierarchy():
    assert issubclass(ConnectionFailed, MigrationError)
    assert issubclass(AuthFailed, MigrationError)
    assert issubclass(QuotaExceeded, MigrationError)
