"""Prove the root AGENTS.md points a coding agent at the three documents (issue #267)."""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCUMENTS = ["CONTRIBUTING.md", "ARCHITECTURE.md", "OPERATIONS.md"]


def test_agents_md_exists_in_the_root():
    assert (ROOT / "AGENTS.md").is_file()


@pytest.mark.parametrize("document", DOCUMENTS)
def test_agents_md_names_each_document_and_the_document_exists(document):
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert document in text, f"AGENTS.md does not name {document}"
    assert (ROOT / document).is_file(), f"AGENTS.md names {document}, which is not in the root"
