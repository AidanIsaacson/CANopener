"""settings.json shared by two programs (the handheld UI and canopener-cli): neither loses the
other's changes."""

from canopener.core.settings import Settings


def test_a_save_keeps_what_another_program_wrote(tmp_path):
    f = str(tmp_path / "settings.json")
    app = Settings(f)
    app.set("units", "metric")
    cli = Settings(f)
    cli.update({"upload.target": "server", "upload.url": "https://logs.example.com"})
    app.set("sound.volume", 40)                     # the app saves something unrelated
    on_disk = Settings(f)
    assert on_disk.get("upload.target") == "server" and on_disk.get("upload.url") == "https://logs.example.com"
    assert on_disk.get("units") == "metric" and on_disk.get("sound.volume") == 40
    assert app.get("upload.url") == "https://logs.example.com"      # and the app has it now


def test_get_notices_another_programs_change(tmp_path):
    f = str(tmp_path / "settings.json")
    app = Settings(f)
    app.RECHECK = 0
    app.set("upload.target", "github")
    Settings(f).set("upload.target", "server")
    assert app.get("upload.target") == "server"


def test_our_own_change_wins_for_the_same_key_and_reset_removes_only_that_key(tmp_path):
    f = str(tmp_path / "settings.json")
    app, cli = Settings(f), Settings(f)
    cli.update({"units": "metric", "profile": "obd2"})
    app.set("units", "imperial")
    assert Settings(f).get("units") == "imperial" and Settings(f).get("profile") == "obd2"
    Settings(f).set("sound.volume", 10)
    app.reset("profile")
    fresh = Settings(f)
    assert "profile" not in fresh._values and fresh.get("sound.volume") == 10


def test_a_missing_or_broken_file_is_still_fine(tmp_path):
    f = tmp_path / "settings.json"
    s = Settings(str(f))
    s.set("units", "metric")
    f.write_text("{not json")
    s.set("sound.volume", 5)
    assert Settings(str(f)).get("units") == "metric" and Settings(str(f)).get("sound.volume") == 5
