# Shared by the source and bundled maintenance entry points. No device I/O.
function Get-LabRuntimeRoot {
    if ($env:LAB_CONTROL_HOME) { return [IO.Path]::GetFullPath($env:LAB_CONTROL_HOME) }
    return Join-Path $env:LOCALAPPDATA 'TF-LabControl'
}

function ConvertTo-LabProcessArguments {
    param([string[]]$Values)
    # Start-Process joins arrays without quoting. Encode every argv element
    # using the Windows command-line rules, including trailing backslashes.
    return (($Values | ForEach-Object {
        $quoted = [regex]::Replace($_, '(\\*)"', '$1$1\"')
        $quoted = [regex]::Replace($quoted, '(\\+)$', '$1$1')
        '"' + $quoted + '"'
    }) -join ' ')
}

function Get-LabLockFileIdentity {
    param([IO.FileStream]$Stream)
    if (-not ('LabControl.LockFileIdentity' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace LabControl {
    public static class LockFileIdentity {
        [StructLayout(LayoutKind.Sequential)]
        private struct FileInformation {
            public uint Attributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
            public uint VolumeSerialNumber, FileSizeHigh, FileSizeLow, NumberOfLinks;
            public uint FileIndexHigh, FileIndexLow;
        }
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(
            SafeFileHandle handle, out FileInformation information);
        public static string Read(SafeFileHandle handle) {
            FileInformation information;
            if (!GetFileInformationByHandle(handle, out information))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return String.Format("{0:X8}:{1:X8}{2:X8}", information.VolumeSerialNumber,
                information.FileIndexHigh, information.FileIndexLow);
        }
    }
}
'@
    }
    return [LabControl.LockFileIdentity]::Read($Stream.SafeFileHandle)
}

function Enter-LabMaintenanceLease {
    $fixedRoot = Join-Path $env:LOCALAPPDATA 'TF-LabControl'
    $roots = @($fixedRoot, (Get-LabRuntimeRoot)) | Select-Object -Unique
    $streams = [Collections.Generic.List[IO.FileStream]]::new()
    $identities = [Collections.Generic.HashSet[string]]::new()
    try {
        foreach ($root in $roots) {
            [IO.Directory]::CreateDirectory($root) | Out-Null
            $path = Join-Path $root 'hardware.lock'
            # FileStream.Lock uses the same Windows byte-range lock as Python.
            $stream = [IO.File]::Open($path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite,
                ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            try {
                # Distinct AppData paths (or hard links) can name the same file.
                # Deduplicate by its opened handle, while retaining every distinct lock.
                $identity = Get-LabLockFileIdentity -Stream $stream
                if ($identities.Contains($identity)) { $stream.Dispose(); continue }
                $stream.Lock(0, 1)
                $identities.Add($identity) | Out-Null
            } catch { $stream.Dispose(); throw }
            $streams.Add($stream)
        }
        return ,$streams
    } catch {
        foreach ($stream in $streams) { $stream.Dispose() }
        throw ('TF-LabControl hardware is busy or its maintenance lock is unavailable: ' + $_.Exception.Message)
    }
}

function Exit-LabMaintenanceLease {
    param($Lease)
    foreach ($stream in $Lease) { $stream.Dispose() }
}
