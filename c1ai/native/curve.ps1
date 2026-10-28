# Fixed Curve tool operations. Normalized canvas points only; no desktop-coordinate or DB writes.
function Curve-Labels {
    return @{rgb='RGB';red=([string][char]0x7ea2+[char]0x8272);green=([string][char]0x7eff+[char]0x8272);
        blue=([string][char]0x84dd+[char]0x8272);luma=([string][char]0x4eae+[char]0x5ea6)}
}
function Curve-Tool {
    return Resolve @{class_name='CurvesTool';control_type='Custom'}
}
function Curve-One($Root, $Condition, [string]$Description) {
    $items=$Root.FindAll([System.Windows.Automation.TreeScope]::Descendants,$Condition)
    if($items.Count -ne 1){Fail 'CURVE_CONTROL_AMBIGUOUS' ('Expected one '+$Description)}
    $element=$items.Item(0)
    if($element.Current.ProcessId -ne $script:WindowInfo.pid){Fail 'CURVE_OWNER_CHANGED' 'Curve control belongs to another process.'}
    return $element
}
function Curve-IdCondition([string]$Id) {
    return [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty,$Id)
}
function Curve-NameTypeCondition([string]$Name,$Type) {
    $nameCondition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,$Name)
    $typeCondition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,$Type)
    return [System.Windows.Automation.AndCondition]::new($nameCondition,$typeCondition)
}
function Curve-Rect($Element) {
    $rect=$Element.Current.BoundingRectangle
    return @{x=$rect.X;y=$rect.Y;width=$rect.Width;height=$rect.Height}
}
function Curve-State {
    $tool=Curve-Tool;$labels=Curve-Labels
    $typeCondition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::RadioButton)
    $radios=$tool.FindAll([System.Windows.Automation.TreeScope]::Descendants,$typeCondition)
    if($radios.Count -ne 5){Fail 'CURVE_CHANNELS_UNVERIFIED' 'Exactly five native Curve channel radios are required.'}
    $rows=@();$selected=@();$seen=@{}
    foreach($radio in $radios){
        $name=[string]$radio.Current.Name;$channels=@($labels.Keys|Where-Object{$labels[$_] -ceq $name})
        if($channels.Count -ne 1 -or $seen.ContainsKey($channels[0])){Fail 'CURVE_CHANNELS_UNVERIFIED' 'Curve channel labels must be unique and qualified.'}
        $channel=$channels[0];$seen[$channel]=$true
        $p=Pattern $radio 'SelectionItem'
        if($null -eq $p){Fail 'CURVE_CHANNELS_UNVERIFIED' 'Curve channel has no native selection state.'}
        $isSelected=[bool]$p.Current.IsSelected
        if($isSelected){$selected+=,$channel}
        $rows+=@{channel=$channel;name=$name;runtime_id=@($radio.GetRuntimeId());selected=$isSelected;enabled=[bool]$radio.Current.IsEnabled}
    }
    if($selected.Count -ne 1){Fail 'CURVE_CHANNELS_UNVERIFIED' 'Exactly one Curve channel must be selected.'}
    $canvas=Curve-One $tool (Curve-IdCondition 'PointsCanvas') 'Curve PointsCanvas'
    if($canvas.Current.ControlType -ne [System.Windows.Automation.ControlType]::List -or $canvas.Current.ClassName -cne 'ItemsControl'){
        Fail 'CURVE_CANVAS_UNVERIFIED' 'Unexpected Curve canvas provider.'
    }
    $dataCondition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::DataItem)
    $points=$canvas.FindAll([System.Windows.Automation.TreeScope]::Children,$dataCondition)
    if($points.Count -lt 2 -or $points.Count -gt 64){Fail 'CURVE_POINTS_UNVERIFIED' 'Expected 2-64 directly exposed Curve points.'}
    $pointRows=@()
    foreach($point in $points){
        if($point.Current.ProcessId -ne $script:WindowInfo.pid){Fail 'CURVE_OWNER_CHANGED' 'Curve point owner changed.'}
        $pointRows+=@{name=[string]$point.Current.Name;runtime_id=@($point.GetRuntimeId());rect=(Curve-Rect $point)}
    }
    $active=@($radios|Where-Object{$_.Current.Name -ceq $labels[$selected[0]]})[0]
    if(-not (Pattern $active 'SelectionItem').Current.IsSelected){Fail 'CURVE_CHANNEL_CHANGED' 'Curve channel changed during read.'}
    return @{selected_channel=$selected[0];radios=$rows;canvas=@{runtime_id=@($canvas.GetRuntimeId());rect=(Curve-Rect $canvas)};points=$pointRows}
}
function Get-CurveState {
    return @{window=$script:WindowInfo;state=(Curve-State);source='native_curve_geometry';numeric_value_api=$false}
}
function Curve-AssertRequest($Request, $ExtraKeys) {
    foreach($key in (Names $Request)){
        if($key -notin (@('op','pid','hwnd','background_only','guard','channel','expected_curve')+$ExtraKeys)){
            Fail 'INVALID_CURVE_REQUEST' 'Unexpected Curve operation argument.'
        }
    }
    $labels=Curve-Labels;$channel=[string](Field $Request 'channel')
    if(-not $labels.ContainsKey($channel)){Fail 'INVALID_CURVE_CHANNEL' 'Unsupported fixed Curve channel.'}
    Check-Guard (Field $Request 'guard')
    $current=Curve-State
    if(-not (Same $current (Field $Request 'expected_curve'))){Fail 'STALE_CURVE' 'Curve channel, geometry or point inventory changed.'}
    return $current
}
function Select-CurveChannel($Request) {
    $before=Curve-AssertRequest $Request @();$channel=[string](Field $Request 'channel')
    if($before.selected_channel -ceq $channel){return @{window=$script:WindowInfo;accepted=$true;changed=$false;state=$before}}
    $tool=Curve-Tool;$label=(Curve-Labels)[$channel]
    $condition=Curve-NameTypeCondition $label ([System.Windows.Automation.ControlType]::RadioButton)
    $radio=Curve-One $tool $condition 'requested Curve radio'
    if($radio.Current.ControlType -ne [System.Windows.Automation.ControlType]::RadioButton -or -not $radio.Current.IsEnabled){Fail 'CURVE_CHANNEL_UNAVAILABLE' 'Native Curve channel radio unavailable.'}
    $p=Pattern $radio 'SelectionItem';if($null -eq $p){Fail 'CURVE_CHANNEL_UNAVAILABLE' 'Native Curve radio has no SelectionItemPattern.'}
    Check-Guard (Field $Request 'guard')
    $script:MayHaveMutated=$true;$p.Select()
    Check-Guard (Field $Request 'guard')
    $after=Curve-State
    if($after.selected_channel -cne $channel){Fail 'CURVE_CHANNEL_UNKNOWN' 'Requested Curve channel was not read back.'}
    return @{window=$script:WindowInfo;accepted=$true;changed=$true;state=$after;image_modified=$false}
}
function Add-CurvePoint($Request) {
    $before=Curve-AssertRequest $Request @('x','y');$x=Field $Request 'x';$y=Field $Request 'y'
    if($x -is [bool] -or $y -is [bool] -or $x -isnot [ValueType] -or $y -isnot [ValueType] -or [double]::IsNaN([double]$x) -or [double]::IsNaN([double]$y) -or [double]::IsInfinity([double]$x) -or [double]::IsInfinity([double]$y) -or $x -le 0 -or $x -ge 1 -or $y -lt 0 -or $y -gt 1){Fail 'INVALID_CURVE_POINT' 'A finite internal normalized x and y within 0..1 are required.'}
    if($before.selected_channel -cne [string](Field $Request 'channel') -or $before.points.Count -ne 2){Fail 'CURVE_POINT_SCOPE' 'Only the selected two-point linear curve is supported.'}
    $canvas=Curve-One (Curve-Tool) (Curve-IdCondition 'PointsCanvas') 'Curve PointsCanvas';$r=$before.canvas.rect
    if(-not $canvas.Current.IsEnabled -or $canvas.Current.IsOffscreen -or $r.width -lt 64 -or $r.height -lt 64){Fail 'CURVE_CANVAS_UNAVAILABLE' 'Canvas must be enabled and visible.'}
    $endpoints=@($before.points|Sort-Object {$_.rect.x})
    $linear=[math]::Abs($endpoints[0].rect.x+$endpoints[0].rect.width/2-$r.x) -le 3 -and [math]::Abs($endpoints[0].rect.y+$endpoints[0].rect.height/2-($r.y+$r.height)) -le 3 -and [math]::Abs($endpoints[1].rect.x+$endpoints[1].rect.width/2-($r.x+$r.width)) -le 3 -and [math]::Abs($endpoints[1].rect.y+$endpoints[1].rect.height/2-$r.y) -le 3
    if(-not $linear){Fail 'CURVE_POINT_SCOPE' 'The two existing points must be linear endpoints.'}
    $hit=New-Object C1WindowNative+POINT;$hit.X=[int][math]::Round($r.x+[double]$x*$r.width);$hit.Y=[int][math]::Round($r.y+(1-[double]$y)*$r.height)
    $bounds=$script:RootElement.Current.BoundingRectangle;$hwnd=[IntPtr]$script:WindowInfo.hwnd
    if($hit.X -le $r.x+2 -or $hit.X -ge $r.x+$r.width-2 -or $hit.Y -lt $r.y -or $hit.Y -ge $r.y+$r.height -or -not $bounds.Contains([double]$hit.X,[double]$hit.Y)){Fail 'CURVE_POINT_BOUNDS' 'The computed point must remain inside the canvas and C1 window.'}
    Require-No-Held-Input;Check-Guard (Field $Request 'guard')
    if([C1WindowNative]::GetForegroundWindow() -ne $hwnd){$script:MayHaveMutated=$true;if(-not [C1WindowNative]::SetForegroundWindow($hwnd)){Fail 'FOREGROUND_REFUSED' 'C1 foreground activation refused.'}}
    Curve-ClickElement (Resolve @{automation_id='labelTitle';control_type='Text'}) (Field $Request 'guard')
    $escape=[C1WindowNative]::CreateNavigationInputs('escape')
    [void][C1WindowNative]::SendInput([uint32]$escape.Length,$escape,[Runtime.InteropServices.Marshal]::SizeOf([type][C1WindowNative+INPUT]))
    Start-Sleep -Milliseconds 80
    $hitWindow=[C1WindowNative]::WindowFromPoint($hit);$owner=0;[void][C1WindowNative]::GetWindowThreadProcessId($hitWindow,[ref]$owner)
    if([C1WindowNative]::GetForegroundWindow() -ne $hwnd -or $owner -ne $script:WindowInfo.pid -or ($hitWindow -ne $hwnd -and -not [C1WindowNative]::IsChild($hwnd,$hitWindow))){Fail 'CURVE_POINT_OCCLUDED' 'The canvas point is covered or C1 is not foreground.' @{hit_hwnd=$hitWindow.ToInt64();hit_pid=$owner;foreground=[C1WindowNative]::GetForegroundWindow().ToInt64();point=@{x=$hit.X;y=$hit.Y};canvas=$r}}
    Require-No-Held-Input;Check-Guard (Field $Request 'guard')
    if(-not (Same (Curve-State) $before)){Fail 'STALE_CURVE' 'Canvas or point inventory changed before input.'}
    $inputs=[C1WindowNative]::CreateLeftClickInputs($hit.X,$hit.Y,[C1WindowNative]::GetSystemMetrics(76),[C1WindowNative]::GetSystemMetrics(77),[C1WindowNative]::GetSystemMetrics(78),[C1WindowNative]::GetSystemMetrics(79))
    if([C1WindowNative]::GetForegroundWindow() -ne $hwnd -or [C1WindowNative]::WindowFromPoint($hit) -ne $hitWindow){Fail 'CURVE_POINT_OCCLUDED' 'The input target changed before dispatch.'}
    Require-No-Held-Input
    $script:MayHaveMutated=$true;$sent=[C1WindowNative]::SendInput([uint32]$inputs.Length,$inputs,[Runtime.InteropServices.Marshal]::SizeOf([type][C1WindowNative+INPUT]))
    if($sent -ne $inputs.Length){Fail 'INPUT_INCOMPLETE' 'Curve point input outcome is unknown; do not repeat.'}
    Start-Sleep -Milliseconds 150;Check-Guard (Field $Request 'guard');$after=Curve-State
    return @{window=$script:WindowInfo;accepted=$true;point_count_verified=($after.selected_channel -ceq $before.selected_channel -and $after.points.Count -eq 3);state=$after;actual_point_geometry=$after.points;requested_normalized=@{x=$x;y=$y};numeric_value_api=$false;model_verified=$false;render_verified=$false}
}
function Move-CurvePoint($Request) {
    $before=Curve-AssertRequest $Request @('x','y');$x=[double](Field $Request 'x');$y=[double](Field $Request 'y')
    if($x -le 0 -or $x -ge 1 -or $y -lt 0 -or $y -gt 1 -or $before.points.Count -ne 3){Fail 'CURVE_POINT_SCOPE' 'This route moves the single interior point.'}
    $points=@($before.points|Sort-Object {$_.rect.x});$p=$points[1].rect;$r=$before.canvas.rect
    $from=New-Object C1WindowNative+POINT;$from.X=[int][math]::Round($p.x+$p.width/2);$from.Y=[int][math]::Round($p.y+$p.height/2)
    $to=New-Object C1WindowNative+POINT;$to.X=[int][math]::Round($r.x+$x*$r.width);$to.Y=[int][math]::Round($r.y+(1-$y)*$r.height)
    Require-No-Held-Input;Check-Guard (Field $Request 'guard')
    foreach($point in @($from,$to)){
        $owner=0;[void][C1WindowNative]::GetWindowThreadProcessId([C1WindowNative]::WindowFromPoint($point),[ref]$owner)
        if($owner -ne $script:WindowInfo.pid){Fail 'CURVE_POINT_OCCLUDED' 'Curve point is covered.'}
    }
    $a=[C1WindowNative]::CreateLeftClickInputs($from.X,$from.Y,[C1WindowNative]::GetSystemMetrics(76),[C1WindowNative]::GetSystemMetrics(77),[C1WindowNative]::GetSystemMetrics(78),[C1WindowNative]::GetSystemMetrics(79))
    $b=[C1WindowNative]::CreateLeftClickInputs($to.X,$to.Y,[C1WindowNative]::GetSystemMetrics(76),[C1WindowNative]::GetSystemMetrics(77),[C1WindowNative]::GetSystemMetrics(78),[C1WindowNative]::GetSystemMetrics(79))
    $script:MayHaveMutated=$true;$down=[C1WindowNative+INPUT[]]@($a[0],$a[1]);$move=[C1WindowNative+INPUT[]]@($b[0]);$up=[C1WindowNative+INPUT[]]@($b[2])
    $size=[Runtime.InteropServices.Marshal]::SizeOf([type][C1WindowNative+INPUT]);$downSent=0;$moveSent=0;$upSent=0
    try{
        $downSent=[C1WindowNative]::SendInput(2,$down,$size)
        if($downSent -ne 2){Fail 'INPUT_INCOMPLETE' 'Curve drag start unknown.'}
        Start-Sleep -Milliseconds 120
        $moveSent=[C1WindowNative]::SendInput(1,$move,$size)
        if($moveSent -ne 1){Fail 'INPUT_INCOMPLETE' 'Curve drag movement unknown.'}
        Start-Sleep -Milliseconds 150
    }finally{
        $upSent=[C1WindowNative]::SendInput(1,$up,$size)
        if($upSent -ne 1){Fail 'INPUT_INCOMPLETE' 'Curve mouse-up was not accepted; button state is unknown.' @{mouse_up_sent=$false;input_sent=$upSent}}
    }
    Start-Sleep -Milliseconds 150;Check-Guard (Field $Request 'guard')
    return @{window=$script:WindowInfo;accepted=$true;state=(Curve-State);numeric_value_api=$false;mouse_up_sent=($upSent -eq 1);input_batches=@{down=$downSent;move=$moveSent;up=$upSent}}
}
function Curve-ClickElement($Element,$Guard) {
    if(-not $Element.Current.IsEnabled -or $Element.Current.IsOffscreen){Fail 'CURVE_ELEMENT_UNAVAILABLE' 'Curve click target must be visible.'}
    $point=New-Object System.Windows.Point
    if(-not $Element.TryGetClickablePoint([ref]$point)){Fail 'CURVE_ELEMENT_UNAVAILABLE' 'Curve click target has no clickable point.'}
    $hwnd=[IntPtr]$script:WindowInfo.hwnd
    if([C1WindowNative]::GetForegroundWindow() -ne $hwnd){
        if(-not [C1WindowNative]::SetForegroundWindow($hwnd)){Fail 'FOREGROUND_REFUSED' 'C1 element activation refused.'}
    }
    $hit=New-Object C1WindowNative+POINT;$hit.X=[int][math]::Round($point.X);$hit.Y=[int][math]::Round($point.Y)
    $hitWindow=[C1WindowNative]::WindowFromPoint($hit);$owner=0
    [void][C1WindowNative]::GetWindowThreadProcessId($hitWindow,[ref]$owner)
    if($owner -ne $script:WindowInfo.pid){Fail 'CURVE_ELEMENT_UNAVAILABLE' 'Curve click target is covered by another process.'}
    Require-No-Held-Input;Check-Guard $Guard
    $inputs=[C1WindowNative]::CreateLeftClickInputs($hit.X,$hit.Y,[C1WindowNative]::GetSystemMetrics(76),[C1WindowNative]::GetSystemMetrics(77),[C1WindowNative]::GetSystemMetrics(78),[C1WindowNative]::GetSystemMetrics(79))
    $script:MayHaveMutated=$true
    $sent=[C1WindowNative]::SendInput([uint32]$inputs.Length,$inputs,[Runtime.InteropServices.Marshal]::SizeOf([type][C1WindowNative+INPUT]))
    if($sent -ne $inputs.Length){Fail 'INPUT_INCOMPLETE' 'Curve element click outcome unknown.'}
}
