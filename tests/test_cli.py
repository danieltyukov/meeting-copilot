from meeting_copilot import cli


def _config(argv, monkeypatch, env=None):
    monkeypatch.delenv("MY_NAME", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    return cli._make_config(cli._build_parser().parse_args(argv))


def test_me_flag_sets_my_name(monkeypatch, tmp_path):
    assert _config([str(tmp_path), "--me", " Daniel "], monkeypatch).my_name == "Daniel"


def test_my_name_from_config_env(monkeypatch, tmp_path):
    conf = tmp_path / "config.env"
    conf.write_text("# keys\nDEEPGRAM_API_KEY=x\nMY_NAME=Daniel Tyukov\n")
    monkeypatch.setattr(cli, "CONFIG_ENV", conf)
    monkeypatch.setattr(cli.os, "environ", {})   # the file is the only source
    cli.load_config_env()
    args = cli._build_parser().parse_args([str(tmp_path)])
    assert cli._make_config(args).my_name == "Daniel Tyukov"


def test_flag_beats_config(monkeypatch, tmp_path):
    cfg = _config([str(tmp_path), "--me", "Dan"], monkeypatch, env={"MY_NAME": "Daniel"})
    assert cfg.my_name == "Dan"


def test_no_name_by_default(monkeypatch, tmp_path):
    assert _config([str(tmp_path)], monkeypatch).my_name is None


def test_people_and_invite_flags(monkeypatch, tmp_path):
    cfg = _config([str(tmp_path), "--people", "Sarah Chen, Marcus Lee", "--invite",
                   "~/meetings/sync.ics"], monkeypatch)
    assert cfg.people == "Sarah Chen, Marcus Lee"
    assert cfg.invite is not None and cfg.invite.name == "sync.ics"
    assert "~" not in str(cfg.invite)                  # expanded
    plain = _config([str(tmp_path)], monkeypatch)
    assert plain.people is None and plain.invite is None
