from __future__ import annotations

from scripts.security_export_scan import scan


def test_security_export_scan_is_clean() -> None:
    result = scan()
    assert result["status"] == "passed"
    assert result["forbidden_provider_matches"] == []
    assert result["secret_matches"] == []
    assert result["forbidden_export_names"] == []
