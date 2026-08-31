import logging
from unittest import mock

import pytest

import src.parser as parser_module
from src.parser import parse


@pytest.fixture(autouse=True)
def _clear_projects_cache():
    # The mtime/size cache is module-level (parse() is called per Discord
    # message and must not re-read the file every time), so tests need a
    # clean slate to exercise cache-miss vs. cache-hit behavior reliably.
    parser_module._cache.clear()
    yield
    parser_module._cache.clear()


def test_session_command_extracts_resume_id_and_prompt():
    cmd = parse("@sess-abc123 계속 진행")
    assert cmd.session_id == "sess-abc123"
    assert cmd.prompt == "계속 진행"
    assert cmd.workdir is None


def test_no_config_file_disables_project_tags(tmp_path, monkeypatch):
    # Backward compat: without PROJECTS_FILE (or a projects.toml next to the
    # repo), no project tags are recognized -- the text just passes through.
    monkeypatch.setenv("PROJECTS_FILE", str(tmp_path / "does-not-exist.toml"))
    cmd = parse("@book 원고를 점검해줘")
    assert cmd.prompt == "@book 원고를 점검해줘"
    assert cmd.workdir is None
    assert cmd.system_hint is None


def test_project_tag_loaded_from_config(tmp_path, monkeypatch):
    config = tmp_path / "projects.toml"
    config.write_text(
        '[book]\ndir = "book"\nhint = "한국어로 답변"\n',
        encoding="utf-8",
    )
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("PROJECTS_FILE", str(config))
    monkeypatch.setenv("PROJECT_ROOT", str(root))

    cmd = parse("@book 원고를 점검해줘")

    assert cmd.prompt == "원고를 점검해줘"
    assert cmd.workdir == str(root / "book")
    assert cmd.system_hint == "한국어로 답변"


def test_corrupt_toml_does_not_crash(tmp_path, monkeypatch, caplog):
    config = tmp_path / "projects.toml"
    config.write_text("this is not valid [[[ toml", encoding="utf-8")
    monkeypatch.setenv("PROJECTS_FILE", str(config))

    with caplog.at_level(logging.ERROR):
        cmd = parse("@book 원고를 점검해줘")

    # Falls back to "no tags" instead of raising, and logs the problem.
    assert cmd.prompt == "@book 원고를 점검해줘"
    assert cmd.workdir is None
    assert any("projects file" in record.message for record in caplog.records)


def test_entry_missing_required_dir_is_skipped(tmp_path, monkeypatch, caplog):
    config = tmp_path / "projects.toml"
    config.write_text('[book]\nhint = "no dir here"\n', encoding="utf-8")
    monkeypatch.setenv("PROJECTS_FILE", str(config))

    with caplog.at_level(logging.ERROR):
        cmd = parse("@book 원고를 점검해줘")

    assert cmd.workdir is None
    assert any("missing required" in record.message for record in caplog.records)


def test_entry_with_wrong_type_is_skipped(tmp_path, monkeypatch, caplog):
    config = tmp_path / "projects.toml"
    config.write_text("book = 1\n", encoding="utf-8")
    monkeypatch.setenv("PROJECTS_FILE", str(config))

    with caplog.at_level(logging.ERROR):
        cmd = parse("@book 원고를 점검해줘")

    assert cmd.workdir is None
    assert any("is not a table" in record.message for record in caplog.records)


def test_project_root_applies_to_relative_dir_from_config(tmp_path, monkeypatch):
    config = tmp_path / "projects.toml"
    config.write_text('[book]\ndir = "book"\n', encoding="utf-8")
    root = tmp_path / "root"
    monkeypatch.setenv("PROJECTS_FILE", str(config))
    monkeypatch.setenv("PROJECT_ROOT", str(root))

    cmd = parse("@book 수정")

    assert cmd.workdir == str(root / "book")


def test_absolute_dir_in_config_bypasses_project_root(tmp_path, monkeypatch):
    absolute_dir = tmp_path / "elsewhere" / "book"
    config = tmp_path / "projects.toml"
    config.write_text(f'[book]\ndir = "{absolute_dir}"\n', encoding="utf-8")
    monkeypatch.setenv("PROJECTS_FILE", str(config))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "root"))

    cmd = parse("@book 수정")

    assert cmd.workdir == str(absolute_dir)


def test_config_is_not_reparsed_when_file_unchanged(tmp_path, monkeypatch):
    config = tmp_path / "projects.toml"
    config.write_text('[book]\ndir = "book"\n', encoding="utf-8")
    monkeypatch.setenv("PROJECTS_FILE", str(config))

    with mock.patch.object(
        parser_module, "_parse_projects_file", wraps=parser_module._parse_projects_file
    ) as parse_spy:
        parse("@book 1")
        parse("@book 2")
        parse("아무 태그도 없는 메시지")

    # Three parse() calls, one file read+parse -- the rest are cache hits.
    assert parse_spy.call_count == 1


def test_changed_config_is_reparsed(tmp_path, monkeypatch):
    config = tmp_path / "projects.toml"
    config.write_text('[book]\ndir = "book"\n', encoding="utf-8")
    monkeypatch.setenv("PROJECTS_FILE", str(config))

    first = parse("@book 수정")
    assert first.workdir is not None and first.workdir.endswith("book")

    # Different length -> guaranteed different st_size even if the
    # filesystem's mtime resolution can't tell the two writes apart.
    config.write_text('[book]\ndir = "renamed-book"\n', encoding="utf-8")
    second = parse("@book 수정")

    assert second.workdir is not None and second.workdir.endswith("renamed-book")
    assert first.workdir != second.workdir


def test_file_created_after_absent_is_picked_up(tmp_path, monkeypatch):
    config = tmp_path / "projects.toml"
    monkeypatch.setenv("PROJECTS_FILE", str(config))

    before = parse("@book 수정")
    assert before.workdir is None

    config.write_text('[book]\ndir = "book"\n', encoding="utf-8")
    after = parse("@book 수정")

    assert after.workdir is not None and after.workdir.endswith("book")


def test_session_command_unaffected_by_project_config(tmp_path, monkeypatch):
    # @sess- must keep working regardless of whether/how project tags are
    # configured -- it's handled before project_definitions() is consulted.
    config = tmp_path / "projects.toml"
    config.write_text('[book]\ndir = "book"\n', encoding="utf-8")
    monkeypatch.setenv("PROJECTS_FILE", str(config))

    cmd = parse("@sess-abc123 계속 진행")

    assert cmd.session_id == "sess-abc123"
    assert cmd.prompt == "계속 진행"
    assert cmd.workdir is None


def test_non_utf8_toml_does_not_crash(tmp_path, monkeypatch, caplog):
    # UnicodeDecodeError is a ValueError, so it is caught by neither OSError
    # nor TOMLDecodeError. _parse_projects_file promises "Never raises", and
    # parse() runs on every Discord message -- an escape here means the bot
    # stops answering anything until the file is fixed by hand.
    config = tmp_path / "projects.toml"
    config.write_bytes(b'[book]\ndir = "\xff\xfe\xfa"\nhint = ""\n')
    monkeypatch.setenv("PROJECTS_FILE", str(config))

    with caplog.at_level(logging.ERROR, logger="src.parser"):
        cmd = parse("@book 확인해줘")

    assert cmd.workdir is None
    assert cmd.prompt == "@book 확인해줘"
    assert any("projects file" in record.message for record in caplog.records)
