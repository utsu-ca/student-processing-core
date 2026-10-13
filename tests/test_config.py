import argparse
import json

import pytest

from utsu_std.utils import ConfigError, load_and_merge_config, load_config_file


def parser_for(path):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(path))
    parser.add_argument("--seed", type=int, default=2026)
    return parser


def test_an_unreadable_config_is_never_overwritten(tmp_path):
    path = tmp_path / "config.json"
    broken = '{"db": "x.sqlite", "smtp": {"host": "smtp.example.ca",}}'
    path.write_text(broken, encoding="utf-8")
    parser = parser_for(path)
    with pytest.raises(ConfigError):
        load_and_merge_config(parser.parse_args([]), parser)
    assert path.read_text(encoding="utf-8") == broken
    assert not (tmp_path / "config.json.tmp").exists()


def test_a_config_that_is_not_an_object_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config_file(str(path), strict=True)
    assert load_config_file(str(path)) == {}  # the tolerant form, used by the mail tools, still returns {}


def test_a_missing_config_is_created_and_other_sections_are_kept(tmp_path):
    path = tmp_path / "config.json"
    parser = parser_for(path)
    assert load_and_merge_config(parser.parse_args([]), parser)["seed"] == 2026
    assert json.loads(path.read_text(encoding="utf-8"))["seed"] == 2026

    path.write_text(json.dumps({"seed": 7, "smtp": {"host": "h"}}), encoding="utf-8-sig")
    conf = load_and_merge_config(parser.parse_args([]), parser)
    assert conf["seed"] == 7 and json.loads(path.read_text(encoding="utf-8"))["smtp"] == {"host": "h"}
