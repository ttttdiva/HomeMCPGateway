param([Parameter(Mandatory=$true)][string]$Directory)
$ErrorActionPreference = 'Stop'
# PowerShell targets an older .NET Framework; opt this fixture into the installed
# WinForms accessibility providers before creating any controls. No host config
# or user applications are changed (Microsoft .NET accessibility switches).
foreach ($Suffix in @('', '.2', '.3', '.4', '.5')) {
    [AppContext]::SetSwitch(('Switch.UseLegacyAccessibilityFeatures' + $Suffix), $false)
}
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class FixtureDpi {
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
}
'@
[void][FixtureDpi]::SetProcessDpiAwarenessContext([IntPtr](-4))
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing @'
using System;
using System.Drawing;
using System.Windows.Forms;
public class FixtureScroll : Panel {
    public int VerticalEvents, HorizontalEvents;
    public FixtureScroll() {
        AutoScroll = true; AutoScrollMinSize = new Size(1500, 1000);
        BackColor = Color.AliceBlue; TabStop = true;
    }
    protected override void OnMouseWheel(MouseEventArgs e) {
        VerticalEvents++; base.OnMouseWheel(e); Invalidate();
    }
    protected override void WndProc(ref Message m) {
        if (m.Msg == 0x020E) {
            HorizontalEvents++;
            int delta = (short)(((long)m.WParam >> 16) & 0xffff);
            int next = Math.Max(0, Math.Min(HorizontalScroll.Maximum - HorizontalScroll.LargeChange + 1,
                HorizontalScroll.Value + delta / 120 * 60));
            AutoScrollPosition = new Point(next, VerticalScroll.Value); Invalidate();
        }
        base.WndProc(ref m);
    }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e);
        for (int i=0; i<30; i++) e.Graphics.DrawString("Scrollable row " + i + " -- horizontal content extends to the right", Font,
            Brushes.Navy, 12 + AutoScrollPosition.X, 12 + i * 32 + AutoScrollPosition.Y);
    }
}
public class FixtureDrag : Panel {
    public int RightClicks, DoubleClicks, Drags, Moves;
    public Point LastStart, LastEnd;
    private bool dragging;
    private Point box = new Point(35,35);
    public FixtureDrag() { BackColor = Color.Honeydew; TabStop = true; DoubleBuffered = true; }
    protected override void OnMouseDown(MouseEventArgs e) {
        base.OnMouseDown(e); Focus();
        if (e.Button == MouseButtons.Right) RightClicks++;
        if (e.Button == MouseButtons.Left) { dragging = true; LastStart = e.Location; Capture = true; }
    }
    protected override void OnMouseMove(MouseEventArgs e) {
        base.OnMouseMove(e); Moves++;
        if (dragging) { box = new Point(e.X - 20, e.Y - 20); Invalidate(); }
    }
    protected override void OnMouseUp(MouseEventArgs e) {
        base.OnMouseUp(e);
        if (dragging && e.Button == MouseButtons.Left) {
            LastEnd = e.Location;
            if (Math.Abs(LastEnd.X - LastStart.X) + Math.Abs(LastEnd.Y - LastStart.Y) > 15) Drags++;
            dragging = false; Capture = false; Invalidate();
        }
    }
    protected override void OnMouseDoubleClick(MouseEventArgs e) { DoubleClicks++; base.OnMouseDoubleClick(e); }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e);
        e.Graphics.DrawRectangle(Pens.DarkGreen, 500, 25, 180, 100);
        e.Graphics.DrawString("Drag target", Font, Brushes.DarkGreen, 530, 60);
        e.Graphics.FillRectangle(Brushes.RoyalBlue, box.X, box.Y, 90, 65);
        e.Graphics.DrawString("Drag me", Font, Brushes.White, box.X+6, box.Y+20);
    }
}
'@
[System.Windows.Forms.Application]::EnableVisualStyles()
$Form = New-Object System.Windows.Forms.Form
# Only this disposable QA window is topmost. Prevent existing background apps
# from covering coordinate targets during the test; do not modify user windows
# or block physical input. Normal focus/restore is still exercised through MCP.
$Form.TopMost = $true
$Form.Text = 'Home MCP native QA fixture'
$Form.Name = 'HomeMCPFixture'
$Form.StartPosition = 'Manual'
$Form.Location = New-Object System.Drawing.Point(100,100)
$Form.ClientSize = New-Object System.Drawing.Size(920,650)
$Form.Font = New-Object System.Drawing.Font('Segoe UI',11)
$Form.KeyPreview = $true
$Script:Clicks = 0
$Script:Shortcuts = 0
$Script:AltShortcuts = 0
$Script:Keys = New-Object System.Collections.Generic.List[string]
function Add-Control($Control, $Name, $Label, $X, $Y, $Width, $Height) {
    $Control.Name = $Name; $Control.AccessibleName = $Label
    $Control.Location = New-Object System.Drawing.Point($X,$Y)
    $Control.Size = New-Object System.Drawing.Size($Width,$Height)
    $Form.Controls.Add($Control)
    return $Control
}
$Button = Add-Control (New-Object System.Windows.Forms.Button) 'applyButton' 'Apply' 20 20 130 38
$Button.Text = 'Apply'
$Button.Add_Click({ $Script:Clicks++ })
$Entry = Add-Control (New-Object System.Windows.Forms.TextBox) 'literalText' 'Literal text' 170 20 720 48
$Entry.Multiline = $true
$Check = Add-Control (New-Object System.Windows.Forms.CheckBox) 'optionCheck' 'Option' 20 85 130 36
$Check.Text = 'Option'
$Combo = Add-Control (New-Object System.Windows.Forms.ComboBox) 'choiceCombo' 'Choice' 170 85 280 36
$Combo.DropDownStyle = 'DropDownList'
[void]$Combo.Items.AddRange(@('Alpha','Beta','Gamma'))
$Combo.SelectedIndex = 0
$List = Add-Control (New-Object System.Windows.Forms.ListBox) 'itemsList' 'Items' 20 145 250 180
foreach($N in 1..40) { [void]$List.Items.Add(('Item {0:d2}' -f $N)) }
$Scroll = Add-Control (New-Object FixtureScroll) 'scrollRegion' 'Scroll region' 300 145 590 190
$Drag = Add-Control (New-Object FixtureDrag) 'dragRegion' 'Drag region' 20 365 870 165
$Result = Add-Control (New-Object System.Windows.Forms.Label) 'resultLabel' 'Operation results' 20 550 870 85
$Result.BorderStyle = 'FixedSingle'
$Form.Add_KeyDown({
    param($Sender,$Event)
    $Script:Keys.Add($Event.KeyData.ToString())
    if ($Script:Keys.Count -gt 60) { $Script:Keys.RemoveAt(0) }
    if ($Event.Control -and $Event.Shift -and $Event.KeyCode -eq 'F12') { $Script:Shortcuts++; $Event.SuppressKeyPress=$true }
    if ($Event.Alt -and $Event.KeyCode -eq 'F11') { $Script:AltShortcuts++; $Event.SuppressKeyPress=$true }
})
function Get-Bounds($Control) {
    $P = $Control.PointToScreen([System.Drawing.Point]::Empty)
    return @{ x=$P.X; y=$P.Y; width=$Control.Width; height=$Control.Height; cx=$P.X+[int]($Control.Width/2); cy=$P.Y+[int]($Control.Height/2) }
}
$Timer = New-Object System.Windows.Forms.Timer
$Timer.Interval = 80
$Timer.Add_Tick({
    if (Test-Path (Join-Path $Directory 'close')) { $Form.Close(); return }
    if (Test-Path (Join-Path $Directory 'minimize')) {
        Remove-Item (Join-Path $Directory 'minimize')
        $Form.WindowState = 'Minimized'
    }
    $Place = Join-Path $Directory 'position.json'
    if (Test-Path $Place) {
        try {
            $Position = [IO.File]::ReadAllText($Place) | ConvertFrom-Json
            $Form.Location = New-Object System.Drawing.Point([int]$Position.x,[int]$Position.y)
            Remove-Item $Place
        } catch { }
    }
    $Result.Text = "Clicks=$Script:Clicks  Checked=$($Check.Checked)  Shortcut=$Script:Shortcuts  Alt=$Script:AltShortcuts`r`nText=$($Entry.Text)`r`nScroll V=$($Scroll.VerticalEvents) H=$($Scroll.HorizontalEvents)  Drag=$($Drag.Drags) Right=$($Drag.RightClicks) Double=$($Drag.DoubleClicks)"
    $State = @{
        pid=$PID; hwnd=$Form.Handle.ToInt64(); timestamp=[DateTimeOffset]::UtcNow.ToString('o');
        clicks=$Script:Clicks; checked=$Check.Checked; text=$Entry.Text; selection_length=$Entry.SelectionLength; entry_focused=$Entry.Focused; form_focused=$Form.ContainsFocus; topmost=$Form.TopMost;
        shortcuts=$Script:Shortcuts; alt_shortcuts=$Script:AltShortcuts; keys=@($Script:Keys.ToArray());
        list_selected=$List.SelectedItem; combo_selected=$Combo.SelectedItem;
        vertical_events=$Scroll.VerticalEvents; horizontal_events=$Scroll.HorizontalEvents;
        vertical_value=$Scroll.VerticalScroll.Value; horizontal_value=$Scroll.HorizontalScroll.Value;
        drags=$Drag.Drags; right_clicks=$Drag.RightClicks; double_clicks=$Drag.DoubleClicks; moves=$Drag.Moves;
        drag_start=@{x=$Drag.LastStart.X;y=$Drag.LastStart.Y}; drag_end=@{x=$Drag.LastEnd.X;y=$Drag.LastEnd.Y};
        minimized=($Form.WindowState -eq 'Minimized');
        controls=@{button=(Get-Bounds $Button);entry=(Get-Bounds $Entry);check=(Get-Bounds $Check);combo=(Get-Bounds $Combo);list=(Get-Bounds $List);scroll=(Get-Bounds $Scroll);drag=(Get-Bounds $Drag)}
    }
    try {
        $Json = $State | ConvertTo-Json -Depth 8 -Compress
        [IO.File]::WriteAllText((Join-Path $Directory 'state.tmp'),$Json,[Text.UTF8Encoding]::new($false))
        Move-Item -Force (Join-Path $Directory 'state.tmp') (Join-Path $Directory 'state.json')
    } catch { }
})
$Form.Add_Shown({ $Timer.Start(); $Form.Activate() })
try { [System.Windows.Forms.Application]::Run($Form) }
finally { $Timer.Stop(); $Timer.Dispose(); $Form.Dispose() }
