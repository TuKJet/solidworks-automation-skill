[CmdletBinding()]
param(
    [ValidateSet('install','check','update')][string]$Command = 'install',
    [string]$Target = '',
    [string]$Source = 'https://github.com/TuKJet/solidworks-automation-skill.git',
    [switch]$Dependencies
)

$ErrorActionPreference = 'Stop'
$python = Get-Command py.exe -ErrorAction SilentlyContinue
if ($python) { $pythonArgs = @('-3') } else {
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $python) { throw '未找到 Python 3（py.exe 或 python.exe）。' }
    $pythonArgs = @()
}
$manager = Join-Path $PSScriptRoot 'scripts\skill_manager.py'
$argsList = @($pythonArgs + @($manager, $Command))
if ($Target) { $argsList += @('--target', $Target) }
if ($Source) { $argsList += @('--source', $Source) }
if ($Dependencies) { $argsList += '--dependencies' }
& $python.Source @argsList
exit $LASTEXITCODE
