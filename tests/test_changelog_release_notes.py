# Copyright (C) 2024-2026 jango_blockchained
#
# This file is part of pynescript.
#
# pynescript is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pynescript is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pynescript.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GitHub Release notes are the matching CHANGELOG.md section."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.changelog_release_notes import build_release_notes
from scripts.changelog_release_notes import extract_changelog_section
from scripts.changelog_release_notes import main
from scripts.changelog_release_notes import normalize_version


_ROOT = Path(__file__).resolve().parents[1]
_CHANGELOG = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_normalize_version_strips_v_prefix() -> None:
    assert normalize_version("v0.6.13") == "0.6.13"
    assert normalize_version("0.6.13") == "0.6.13"


def test_extract_em_dash_dated_heading() -> None:
    section = extract_changelog_section(_CHANGELOG, "0.6.13")
    assert section.startswith("## [0.6.13] — 2026-10-18\n")
    assert "**LSP signature help**" in section
    assert "## [0.6.12]" not in section
    assert "## [Unreleased]" not in section


def test_extract_hyphen_dated_heading() -> None:
    section = extract_changelog_section(_CHANGELOG, "v0.6.8")
    assert section.startswith("## [0.6.8] - 2026-09-26\n")
    assert "## [0.6.6]" not in section


def test_missing_version_explains_unreleased_move() -> None:
    with pytest.raises(ValueError, match=r"no ## \[9\.9\.9\] section"):
        extract_changelog_section(_CHANGELOG, "9.9.9")


def test_unreleased_is_not_a_release_section() -> None:
    with pytest.raises(ValueError, match="Unreleased"):
        extract_changelog_section(_CHANGELOG, "Unreleased")


def test_build_release_notes_puts_changelog_before_install() -> None:
    notes = build_release_notes(_CHANGELOG, "0.6.13")
    assert notes.index("## [0.6.13]") == 0
    assert notes.index("**LSP signature help**") < notes.index("## Install")
    assert 'pip install "hoox-pyne==0.6.13"' in notes


def test_cli_writes_out(tmp_path: Path) -> None:
    out = tmp_path / "notes.md"
    assert main(["--version", "0.6.13", "--changelog", str(_ROOT / "CHANGELOG.md"), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("## [0.6.13]")
    assert "hoox-pyne==0.6.13" in text
