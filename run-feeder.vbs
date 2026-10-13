' Runs the cluster dashboard feeder once without a console window. The scheduled task "Cluster dashboard feeder"
' (install-task.ps1) starts it every 5 minutes.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
sh.Run """C:\Program Files\nodejs\node.exe"" """ & here & "\feeder.mjs""", 0, False
