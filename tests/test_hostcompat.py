from ego.adapters import hostcompat


def test_flag_scan_keeps_camel_case_switches_from_real_claude_help():
    # `claude --help` lists the camelCase switch with its hyphenated alias on one line.
    help_text = "  --allowedTools, --allowed-tools <tools...>   Comma or space-separated list of tool names"
    flags = set(hostcompat.FLAG_RE.findall(help_text))
    assert "--allowedTools" in flags
    assert "--allowed-tools" in flags


def test_require_flags_accepts_camel_case_switch():
    info = {"host": "claude", "supported": True,
            "flags": sorted(set(hostcompat.FLAG_RE.findall("--allowedTools, --permission-mode")))}
    hostcompat.require_flags(info, "--allowedTools", "--permission-mode")


def test_inspect_host_handles_empty_version_output(tmp_path, monkeypatch):
    # Arrange
    monkeypatch.setenv("EGO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(hostcompat.shutil, "which", lambda _cmd: "/usr/bin/claude")
    monkeypatch.setattr(hostcompat, "_bounded", lambda *a, **kw: (True, ""))

    # Act
    info = hostcompat.inspect_host("claude", refresh=True)

    # Assert
    assert info["version"] is None
    assert info["supported"] is False
