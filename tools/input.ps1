# Posts a mouse click or key press straight to a process's main window, without moving the real
# cursor or stealing focus. Click coordinates are in the space of the screenshots shot.ps1 makes
# (the window rect scaled down to 1280 wide).
#   tools/input.ps1 -ProcessId 1234 -X 205 -Y 197
#   tools/input.ps1 -ProcessId 1234 -X 205 -Y 197 -Right   (right click)
#   tools/input.ps1 -ProcessId 1234 -X 205 -Y 197 -Move    (hover only)
#   tools/input.ps1 -ProcessId 1234 -X 205 -Y 197 -Wheel -120   (mouse wheel there; negative = down)
#   tools/input.ps1 -ProcessId 1234 -Key 0x0D        (virtual-key code, e.g. Enter)
#   tools/input.ps1 -ProcessId 1234 -Text "mods"     (typed characters)
param(
    [Parameter(Mandatory)][int]$ProcessId,
    [int]$X = -1,
    [int]$Y = -1,
    [int]$Key = -1,
    [string]$Text = "",
    [switch]$Right,
    [switch]$Move,
    [int]$Wheel = 0
)

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class InputNative {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
}
"@

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class WinFind {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    // The game can own several top-level windows; the real one is the biggest visible one
    public static IntPtr Largest(uint pid) {
        IntPtr best = IntPtr.Zero; long bestArea = 0;
        EnumWindows((h, l) => {
            uint owner; GetWindowThreadProcessId(h, out owner);
            RECT r;
            if (owner == pid && IsWindowVisible(h) && GetWindowRect(h, out r)) {
                long area = (long)(r.R - r.L) * (r.B - r.T);
                if (area > bestArea) { bestArea = area; best = h; }
            }
            return true;
        }, IntPtr.Zero);
        return best;
    }
}
"@
$hwnd = [WinFind]::Largest([uint32]$ProcessId)
if ($hwnd -eq [IntPtr]::Zero) { throw "process $ProcessId has no main window" }

if ($X -ge 0) {
    $r = New-Object InputNative+RECT
    [void][InputNative]::GetWindowRect($hwnd, [ref]$r)
    $scale = [Math]::Min(1.0, 1280 / ($r.R - $r.L))
    $p = New-Object InputNative+POINT
    $p.X = $r.L + [int]($X / $scale)
    $p.Y = $r.T + [int]($Y / $scale)
    [void][InputNative]::ScreenToClient($hwnd, [ref]$p)
    $lp = [IntPtr](($p.Y -shl 16) -bor ($p.X -band 0xFFFF))
    [void][InputNative]::PostMessage($hwnd, 0x0200, [IntPtr]0, $lp)   # WM_MOUSEMOVE
    if ($Move) { "moved to client ($($p.X), $($p.Y))"; return }
    if ($Wheel -ne 0) {
        # WM_MOUSEWHEEL: delta in the high word of wParam, screen coordinates in lParam
        Start-Sleep -Milliseconds 150
        $sx = $r.L + [int]($X / $scale); $sy = $r.T + [int]($Y / $scale)
        $wp = [IntPtr]([int64](($Wheel -band 0xFFFF) -shl 16))
        $slp = [IntPtr](($sy -shl 16) -bor ($sx -band 0xFFFF))
        [void][InputNative]::PostMessage($hwnd, 0x020A, $wp, $slp)
        "wheel $Wheel at client ($($p.X), $($p.Y))"; return
    }
    Start-Sleep -Milliseconds 150
    if ($Right) {
        [void][InputNative]::PostMessage($hwnd, 0x0204, [IntPtr]2, $lp)   # WM_RBUTTONDOWN
        Start-Sleep -Milliseconds 80
        [void][InputNative]::PostMessage($hwnd, 0x0205, [IntPtr]0, $lp)   # WM_RBUTTONUP
    } else {
        [void][InputNative]::PostMessage($hwnd, 0x0201, [IntPtr]1, $lp)   # WM_LBUTTONDOWN
        Start-Sleep -Milliseconds 80
        [void][InputNative]::PostMessage($hwnd, 0x0202, [IntPtr]0, $lp)   # WM_LBUTTONUP
    }
    "clicked client ($($p.X), $($p.Y))"
}
if ($Key -ge 0) {
    [void][InputNative]::PostMessage($hwnd, 0x0100, [IntPtr]$Key, [IntPtr]1)            # WM_KEYDOWN
    Start-Sleep -Milliseconds 80
    [void][InputNative]::PostMessage($hwnd, 0x0101, [IntPtr]$Key, [IntPtr]0xC0000001)   # WM_KEYUP
    "pressed key $Key"
}
if ($Text) {
    foreach ($ch in $Text.ToCharArray()) {
        [void][InputNative]::PostMessage($hwnd, 0x0102, [IntPtr][int]$ch, [IntPtr]1)   # WM_CHAR
        Start-Sleep -Milliseconds 30
    }
    "typed '$Text'"
}
