"""One version number. SERVER_VERSION in fusion_mcp_server.py is the source;
every other copy must match it. The listener lives in Fusion's AddIns folder
and cannot import the server, so this test is what keeps them in lockstep -
ping_fusion warns users whenever they differ."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_listener_matches_server(srv, lst):
    assert lst.LISTENER_VERSION == srv.SERVER_VERSION
    assert lst.PROTOCOL == srv.EXPECTED_PROTOCOL


def test_run_fusion_code_names_the_current_version(srv):
    # the version in this docstring is how users spot a cached tool schema
    assert "[server v{} ".format(srv.SERVER_VERSION) in srv.run_fusion_code.__doc__


def test_changelog_top_entry_is_the_current_version(srv):
    assert re.search(r"^## (\S+)", read("CHANGELOG.md"), re.M).group(1) == srv.SERVER_VERSION


def test_release_notes_and_readme_name_the_current_version(srv):
    v = "v" + srv.SERVER_VERSION
    assert read("RELEASE_NOTES.md").startswith("# " + v)
    assert v in read("README.md")


def test_pdf_renderer_derives_the_version_instead_of_hard_coding_it():
    assert not re.search(r'^VERSION = "', read("docs/render_pdfs.py"), re.M)


def test_requirements_cap_the_sdk_major_version():
    assert re.search(r"^mcp\[cli\]>=[\d.]+,<2$", read("requirements.txt"), re.M)
