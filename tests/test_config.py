from pathlib import Path

from domain_i.config import read_config


def test_all_env_files_merged_in_name_order(tmp_path: Path) -> None:
    (tmp_path / "b.env").write_text("DOMAIN_I_TLDS=io\nONLY_B=1\n")
    (tmp_path / "a.env").write_text("DOMAIN_I_TLDS=dev\nONLY_A=1\n")
    (tmp_path / "notes.txt").write_text("DOMAIN_I_TLDS=zzz\n")
    assert read_config({}, tmp_path) == {"DOMAIN_I_TLDS": "io", "ONLY_A": "1", "ONLY_B": "1"}


def test_symlinked_env_file_is_read(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "secret-file"
    target.parent.mkdir()
    target.write_text("SPACESHIP_API_KEY=k\n")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "credentials.env").symlink_to(target)
    assert read_config({}, cfg) == {"SPACESHIP_API_KEY": "k"}


def test_domain_i_env_reads_only_that_file(tmp_path: Path) -> None:
    (tmp_path / "a.env").write_text("X=from-dir\n")
    single = tmp_path / "single"
    single.write_text("Y=1\n")
    assert read_config({"DOMAIN_I_ENV": str(single)}, tmp_path) == {"Y": "1"}


def test_missing_config_dir_is_empty(tmp_path: Path) -> None:
    assert read_config({}, tmp_path / "nope") == {}
