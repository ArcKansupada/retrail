"""Guard: user-facing text must survive a cp1252 console.

This bit twice. `retrail list` crashed outright on Windows because of box
drawing characters, and em-dashes in help text and error messages rendered as
garbage. Both are trivially avoidable and neither should be caught by hand
again, so the rule is enforced rather than remembered.

The one sanctioned exception is `_glyphs()`, which contains box-drawing
characters *and* the encodability check that keeps them from ever being printed
to a console that can't take them.
"""

import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).parent.parent / "retrail"
SANCTIONED = {"cli.py": {"_glyphs"}}


def source_files():
    return sorted(PACKAGE.glob("*.py"))


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_source_is_ascii_except_the_guarded_glyphs(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    inside_glyphs = False
    offenders = []

    for number, line in enumerate(lines, 1):
        if line.startswith("def "):
            inside_glyphs = any(
                line.startswith(f"def {name}(")
                for name in SANCTIONED.get(path.name, ())
            )
        if inside_glyphs:
            continue
        if any(ord(char) > 127 for char in line):
            offenders.append(number)

    assert not offenders, (
        f"{path.name} has non-ASCII on line(s) {offenders}. These garble on a "
        "cp1252 console. Use '-' instead of an em-dash, and plain quotes."
    )


def test_every_string_the_cli_can_print_encodes_as_cp1252():
    """Belt and braces: the whole module must round-trip through cp1252."""
    source = (PACKAGE / "cli.py").read_text(encoding="utf-8")
    non_ascii = {c for c in source if ord(c) > 127}
    # Only the box-drawing set, and only because _glyphs() gates it.
    assert non_ascii <= set("└─├│")


def test_glyphs_are_actually_gated():
    """If the guard is ever removed, the exception above must stop applying."""
    source = (PACKAGE / "cli.py").read_text(encoding="utf-8")
    assert "except (UnicodeEncodeError, LookupError):" in source


# --- model output is not ours to police ------------------------------------
#
# The rules above cover text WE wrote. Everything below covers text the MODEL
# wrote, which can be any Unicode at all. A real Opus run returned an emoji and
# killed a plain `print` — and retrail prints model text in diff (final
# answers), bisect (probe answers), and log (summaries), so on a cp1252 console
# an emoji in an ANSWER would take down the command reporting it.


class FakeConsole:
    """A writable stream reporting a specific terminal encoding.

    Not an io.StringIO subclass: its `encoding` is read-only, which is the very
    attribute under test. And it must be genuinely writable, since click.echo
    writes to the stream rather than just inspecting it.
    """

    def __init__(self, encoding):
        self.encoding = encoding
        self._chunks = []

    def write(self, text):
        self._chunks.append(text)
        return len(text)

    def flush(self):
        pass

    def getvalue(self):
        return "".join(self._chunks)


def _echo_to(monkeypatch, encoding, text):
    from retrail.cli import echo

    console = FakeConsole(encoding)
    monkeypatch.setattr("retrail.cli.sys.stdout", console)
    monkeypatch.setattr("click.utils._default_text_stdout", lambda: console)
    echo(text)
    return console.getvalue()


def test_echo_survives_model_output_a_cp1252_console_cannot_encode(monkeypatch):
    # Check mark, euro sign, airplane - the shape of a real Opus booking reply.
    out = _echo_to(monkeypatch, "cp1252", "Booked ✅ for €450 \U0001f6eb")
    assert "Booked" in out and "450" in out  # the message survived
    out.encode("cp1252")  # and it is printable on this console


def test_echo_leaves_encodable_text_untouched(monkeypatch):
    out = _echo_to(monkeypatch, "cp1252", "Confirmed: AUS-SFO booked for $450.")
    assert out == "Confirmed: AUS-SFO booked for $450.\n"


def test_echo_passes_unicode_through_on_a_utf8_console(monkeypatch):
    """Degrading is a last resort, not the default. A capable terminal must
    still get the real characters."""
    out = _echo_to(monkeypatch, "utf-8", "Booked ✅")
    assert "✅" in out


def test_every_cli_print_goes_through_echo():
    """One raw click.echo would reopen the hole. Only echo() itself may call it."""
    source = (PACKAGE / "cli.py").read_text(encoding="utf-8")
    raw = [
        number
        for number, line in enumerate(source.splitlines(), 1)
        if "click.echo(" in line and "click.echo(text)" not in line
    ]
    assert not raw, (
        f"cli.py calls click.echo directly on line(s) {raw}. Use echo() so model "
        "output can't crash the command printing it."
    )
