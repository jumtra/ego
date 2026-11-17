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
