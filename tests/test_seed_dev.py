from scripts import seed_dev


def test_seed_refuses_anything_but_local_sqlite(monkeypatch):
    settings = seed_dev.get_settings()
    monkeypatch.setenv("SEED_PASSWORD", "long-enough-password")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h:5432/db")
    assert seed_dev.main() == 1


def test_seed_refuses_on_railway(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    monkeypatch.setenv("SEED_PASSWORD", "long-enough-password")
    assert seed_dev.main() == 1


def test_seed_requires_a_password(monkeypatch):
    monkeypatch.setenv("SEED_PASSWORD", "short")
    assert seed_dev.main() == 1
