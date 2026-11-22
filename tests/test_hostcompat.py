import json
from ego.adapters import hostcompat, hosts


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


def _setup_claude_build_command(tmp_path, monkeypatch, alter_cfg):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hosts.shutil, "which", lambda _cmd: "/usr/bin/claude")
    monkeypatch.setattr(
        hostcompat,
        "inspect_host",
        lambda _host, **_kw: {
            "host": "claude",
            "supported": True,
            "flags": ["--output-format", "--strict-mcp-config", "--mcp-config", "--permission-mode"],
        },
    )
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"alter": alter_cfg}}),
        encoding="utf-8",
    )


def test_build_command_claude_injects_alter_worker_profile(tmp_path, monkeypatch):
    _setup_claude_build_command(tmp_path, monkeypatch, {"command": "alter-mcp", "args": []})

    argv = hosts.build_command("claude", "hello", lean=True, mcp_servers=["alter"], cwd=tmp_path)

    idx = argv.index("--mcp-config")
    cfg = json.loads(argv[idx + 1])
    assert cfg["mcpServers"]["alter"]["env"]["ALTER_MCP_TOOL_PROFILE"] == "ego-worker"


def test_build_command_claude_preserves_user_alter_tool_profile(tmp_path, monkeypatch):
    _setup_claude_build_command(
        tmp_path,
        monkeypatch,
        {"command": "alter-mcp", "args": [], "env": {"ALTER_MCP_TOOL_PROFILE": "custom", "OTHER": "1"}},
    )

    argv = hosts.build_command("claude", "hello", lean=True, mcp_servers=["alter"], cwd=tmp_path)

    idx = argv.index("--mcp-config")
    cfg = json.loads(argv[idx + 1])
    assert cfg["mcpServers"]["alter"]["env"]["ALTER_MCP_TOOL_PROFILE"] == "custom"
    assert cfg["mcpServers"]["alter"]["env"]["OTHER"] == "1"
