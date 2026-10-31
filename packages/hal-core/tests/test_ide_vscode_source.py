"""The VS Code extension source sends only documented message types and has no runtime surprises."""

import json
import re
from pathlib import Path

from halcore.ide.protocol import HOST_TO_IDE, IDE_TO_HOST

EXT = Path(__file__).resolve().parents[3] / "ide" / "vscode"
SOURCES = sorted((EXT / "src").glob("*.ts"))


def test_every_message_type_the_extension_sends_is_documented():
    sent = set()
    for src in SOURCES:
        sent |= set(re.findall(r"\btype:\s*\"([a-z_]+)\"", src.read_text()))
    assert sent and sent <= set(IDE_TO_HOST), sent - set(IDE_TO_HOST)
    assert set(IDE_TO_HOST) <= sent | {"context"}, "a documented type is no longer sent by anything"


def test_the_extension_only_reads_host_types_the_protocol_defines():
    text = "".join(src.read_text() for src in SOURCES)
    read = set(re.findall(r"msg\.type === \"([a-z_]+)\"", text))
    assert read and read <= set(HOST_TO_IDE), read - set(HOST_TO_IDE)


def test_the_extension_has_no_network_or_process_access():
    text = "".join(src.read_text() for src in SOURCES)
    for banned in ("child_process", "from \"http\"", "from \"https\"", "fetch(", "XMLHttpRequest", "eval(", "new Function"):
        assert banned not in text, banned


def test_package_is_mit_licensed_with_no_runtime_dependencies_and_exact_dev_pins():
    pkg = json.loads((EXT / "package.json").read_text())
    assert pkg["license"] == "MIT" and "MIT License" in (EXT / "LICENSE").read_text()
    assert pkg["engines"]["vscode"] == "^1.90.0"
    assert "dependencies" not in pkg
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in pkg["devDependencies"].values())
    assert {"typescript", "@types/vscode"} <= set(pkg["devDependencies"])
    assert (EXT / "package-lock.json").is_file()


def test_the_extension_declares_no_telemetry_and_lists_the_five_commands():
    pkg = json.loads((EXT / "package.json").read_text())
    assert "telemetry" not in json.dumps(pkg).lower()
    names = {c["command"] for c in pkg["contributes"]["commands"]}
    assert names == {"hal.openInTerminal", "hal.addSelection", "hal.sendDiagnostics", "hal.showChanges", "hal.acceptChanges"}


def test_node_modules_and_build_output_are_ignored():
    assert (EXT / ".gitignore").read_text().split() == ["node_modules/", "out/"]
