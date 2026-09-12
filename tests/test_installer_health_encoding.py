import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1 encoding")
def test_installer_reads_utf8_readiness_in_windows_powershell(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "ready.log").write_text(
        "飞书长连接已就绪；正在监听受控群的指令。\n", encoding="utf-8"
    )
    probe = tmp_path / "check.ps1"
    probe.write_text(r'''
param([string]$Installer, [string]$Log)
$ErrorActionPreference = 'Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Installer,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Installer syntax error' }
$fn=$ast.Find({param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Test-OopzGatewayReady'
},$true)
if (-not $fn) { throw 'Missing readiness function' }
# Execute only the pure helper, never the installation or process actions.
Invoke-Expression $fn.Extent.Text
$reader=[IO.StreamReader]::new($Log,[Text.Encoding]::UTF8,$true)
try { $text=$reader.ReadToEnd() } finally { $reader.Dispose() }
if (-not (Test-OopzGatewayReady $text)) { throw 'UTF8 readiness was missed' }
if (Test-OopzGatewayReady '') { throw 'Empty log accepted' }
if (Test-OopzGatewayReady 'Connecting; HTTP 401') { throw 'Failure log accepted' }
$calls=@($ast.FindAll({param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and
    $node.GetCommandName() -eq 'Test-OopzGatewayReady'
},$true))
if ($calls.Count -ne 1 -or -not $calls[0].Extent.Text.Contains('$newLog')) {
    throw ('Health calls: ' + $calls.Count + ' text: ' + $calls[0].Extent.Text)
}
Write-Output 'Windows PowerShell readiness checks passed'
''', encoding="ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(probe), "-Installer", str(root / "scripts/install_release.ps1"),
         "-Log", str(tmp_path / "ready.log")],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Windows PowerShell readiness checks passed" in result.stdout
