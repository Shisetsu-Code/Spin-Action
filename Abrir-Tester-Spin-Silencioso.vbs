Option Explicit

Dim shell, fso, scriptDir, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
command = "cmd.exe /c """ & scriptDir & "\Abrir-Tester-Spin.cmd"""
shell.Run command, 0, False
