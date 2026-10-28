# Native background operations only. No activation, keyboard or mouse APIs.
if (-not ('C1FocusWatch' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Threading;
public sealed class C1FocusWatch : IDisposable {
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int l,t,r,b; }
    [StructLayout(LayoutKind.Sequential)] struct GUI { public uint size,flags; public IntPtr active,focus,capture,menu,move,caret; public RECT rect; }
    [StructLayout(LayoutKind.Sequential)] struct MSG { public IntPtr hwnd; public uint msg; public UIntPtr wp; public IntPtr lp; public uint time; public int x,y; public uint extra; }
    delegate void EventProc(IntPtr hook,uint evt,IntPtr hwnd,int obj,int child,uint thread,uint time);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h,out uint pid);
    [DllImport("user32.dll")] static extern bool GetGUIThreadInfo(uint t,ref GUI g);
    [DllImport("user32.dll")] static extern IntPtr SetWinEventHook(uint min,uint max,IntPtr mod,EventProc cb,uint pid,uint thread,uint flags);
    [DllImport("user32.dll")] static extern bool UnhookWinEvent(IntPtr h);
    [DllImport("user32.dll")] static extern bool PeekMessage(out MSG msg,IntPtr h,uint min,uint max,uint remove);
    [DllImport("user32.dll")] static extern bool TranslateMessage(ref MSG msg);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref MSG msg);
    public sealed class Sample { public long foreground,focus,active; public uint pid; public bool readable; }
    public sealed class Change { public string reason; public Sample state; public uint evt; public long eventHwnd; }
    public Sample Before,After;
    public bool HooksInstalled;
    public int Samples;
    public readonly List<Change> Changes=new List<Change>();
    readonly object gate=new object();
    readonly ManualResetEvent ready=new ManualResetEvent(false);
    volatile bool stop;
    Thread worker;
    EventProc callback;
    public static Sample Read() {
        var s=new Sample(); IntPtr fg=GetForegroundWindow(); uint pid;
        uint tid=GetWindowThreadProcessId(fg,out pid); var g=new GUI();g.size=(uint)Marshal.SizeOf(typeof(GUI));
        s.foreground=fg.ToInt64();s.pid=pid;s.readable=fg!=IntPtr.Zero && tid!=0 && GetGUIThreadInfo(tid,ref g);
        s.focus=g.focus.ToInt64();s.active=g.active.ToInt64();return s;
    }
    void Observe(string reason,uint evt,long eventHwnd) {
        var s=Read();lock(gate){Samples++;After=s;
            bool changed=!s.readable || s.foreground!=Before.foreground || s.focus!=Before.focus || s.active!=Before.active;
            if(evt==3 && eventHwnd!=Before.foreground) changed=true;
            if(changed && Changes.Count<128) Changes.Add(new Change{reason=reason,state=s,evt=evt,eventHwnd=eventHwnd});
        }
    }
    public C1FocusWatch() {
        Before=Read();After=Before;
        callback=(h,e,w,o,c,t,time)=>Observe("win_event",e,w.ToInt64());
        worker=new Thread(()=>{
            IntPtr fg=SetWinEventHook(3,3,IntPtr.Zero,callback,0,0,0);
            IntPtr focus=SetWinEventHook(0x8005,0x8005,IntPtr.Zero,callback,0,0,0);
            HooksInstalled=fg!=IntPtr.Zero && focus!=IntPtr.Zero;ready.Set();
            try { while(!stop){ MSG msg;while(PeekMessage(out msg,IntPtr.Zero,0,0,1)){TranslateMessage(ref msg);DispatchMessage(ref msg);}Observe("sample",0,0);Thread.Sleep(15);} }
            finally {if(fg!=IntPtr.Zero)UnhookWinEvent(fg);if(focus!=IntPtr.Zero)UnhookWinEvent(focus);}
        });worker.IsBackground=true;worker.Start();ready.WaitOne(2000);
    }
    public bool Unchanged { get { lock(gate){return HooksInstalled && Before.readable && Changes.Count==0;} } }
    public void Dispose(){stop=true;if(worker!=null)worker.Join(2000);Observe("final",0,0);}
}
'@
}

function Start-FocusWatch {
    $watch=New-Object C1FocusWatch
    if (-not $watch.HooksInstalled -or -not $watch.Before.readable) { $watch.Dispose(); Fail 'FOCUS_MONITOR_UNAVAILABLE' 'Cannot monitor the foreground and keyboard focus; no action dispatched.' }
    return $watch
}
function Start-BackgroundWatch {
    $watch=Start-FocusWatch
    if ($watch.Before.pid -eq $script:WindowInfo.pid) { $watch.Dispose(); Fail 'BACKGROUND_REQUIRED' 'Capture One must remain in the background for this operation.' }
    return $watch
}
function Watch-Report($Watch) {
    return @{before=$Watch.Before;after=$Watch.After;unchanged=$Watch.Unchanged;hooks_installed=$Watch.HooksInstalled;samples=$Watch.Samples;changes=@($Watch.Changes.ToArray());sample_interval_ms=15}
}
function Assert-BackgroundWatch($Watch) {
    if (-not $Watch.Unchanged) { Fail 'FOCUS_CHANGED' 'Foreground or keyboard focus changed. No further action was dispatched; inspect the outcome before continuing.' (Watch-Report $Watch) }
}
function Start-LayerWatch {
    if($script:BackgroundOnly){return Start-BackgroundWatch}
    return Start-FocusWatch
}
function Assert-LayerWatch($Watch) {
    if($script:BackgroundOnly){Assert-BackgroundWatch $Watch}
}
function Get-LayersRoot {
    return Resolve @{automation_id='LayersListControl';control_type='Tree';class_name='TreeView'}
}
function Open-LayersTool($Request) {
    Check-Guard (Field $Request 'guard')
    $header=Resolve @{automation_id='HeaderSite';control_type='Button';ancestors=@(@{automation_id='expander';class_name='Expander'},@{class_name='LocalAdjustmentsTool'})}
    $patternObject=Pattern $header 'Toggle'
    if($null -eq $patternObject){Fail 'TOOL_TOGGLE_UNAVAILABLE' 'No native TogglePattern for the observed layer expander.'}
    $before=[string]$patternObject.Current.ToggleState
    if($before -notin @('On','Off')){Fail 'TOOL_TOGGLE_UNAVAILABLE' 'Indeterminate layer expander state.'}
    $watch=Start-LayerWatch
    try {
        Check-Guard (Field $Request 'guard');Assert-LayerWatch $watch
        if($before -eq 'Off'){$script:MayHaveMutated=$true;$patternObject.Toggle()}
        Start-Sleep -Milliseconds 250;Assert-LayerWatch $watch
        if([string]$patternObject.Current.ToggleState -ne 'On'){Fail 'TOOL_NOT_EXPANDED' 'Native toggle did not expand the layer tool.'}
        $layers=Get-LayerState
        Check-Guard (Field $Request 'guard')
    }finally{$watch.Dispose()}
    Assert-LayerWatch $watch
    return @{window=$script:WindowInfo;before=$before;after='On';changed=($before -eq 'Off');layers=$layers;focus=(Watch-Report $watch);image_modified=$false}
}
function Get-LayerState {
    $tree=Get-LayersRoot
    $condition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::TreeItem)
    $items=$tree.FindAll([System.Windows.Automation.TreeScope]::Children,$condition)
    if ($items.Count -gt 64) { Fail 'LAYER_LIST_TOO_LARGE' 'Unexpected layer count.' }
    $rows=@()
    for($i=0;$i -lt $items.Count;$i++) {
        $item=$items.Item($i)
        $texts=$item.FindAll([System.Windows.Automation.TreeScope]::Children,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Text)))
        $labels=@();foreach($text in $texts){if(-not [string]::IsNullOrWhiteSpace($text.Current.Name)){$labels+= [string]$text.Current.Name}}
        # Adjustment rows also expose opacity as Text. NameEditBox identifies
        # the real name without guessing from order or percentage formatting.
        # Hidden WPF editors are omitted by FindAll in C1 16.8. Use the raw
        # read-only tree; never focus, realize or open an editor to read a name.
        $editors=@();$walker=[System.Windows.Automation.TreeWalker]::RawViewWalker
        $queue=New-Object 'System.Collections.Generic.Queue[System.Windows.Automation.AutomationElement]'
        $queue.Enqueue($item);$visited=0
        while($queue.Count -gt 0){
            $parent=$queue.Dequeue();$child=$walker.GetFirstChild($parent)
            while($null -ne $child){
                $visited++;if($visited -gt 128){Fail 'LAYER_NAME_UNREADABLE' 'Layer raw tree exceeds the bounded name lookup.'}
                if($child.Current.AutomationId -ceq 'NameEditBox'){$editors+=,$child}
                if($child.Current.ControlType -ne [System.Windows.Automation.ControlType]::TreeItem){$queue.Enqueue($child)}
                $child=$walker.GetNextSibling($child)
            }
        }
        if($editors.Count -eq 1){
            $valuePattern=Pattern $editors[0] 'Value'
            if($null -eq $valuePattern){Fail 'LAYER_NAME_UNREADABLE' 'Name editor has no readable value.'}
            $label=[string]$valuePattern.Current.Value
            if([string]::IsNullOrWhiteSpace($label) -or ($labels.Count -gt 0 -and $label -cnotin $labels)){Fail 'LAYER_NAME_UNREADABLE' 'Layer editor and label disagree.' @{labels=$labels;value=$label}}
        }elseif($editors.Count -eq 0 -and $labels.Count -eq 1){$label=$labels[0]}
        else{Fail 'LAYER_NAME_UNREADABLE' 'Expected an unambiguous native layer name.' @{labels=$labels;editors=$editors.Count}}
        $sel=Pattern $item 'SelectionItem';if($null -eq $sel){Fail 'LAYER_SELECTION_UNREADABLE' 'Layer lacks selection state.'}
        $rows+=@{runtime_id=@($item.GetRuntimeId());name=$label;selected=[bool]$sel.Current.IsSelected;enabled=[bool]$item.Current.IsEnabled;is_image_layer=($editors.Count -eq 0 -and $label -in @(([string][char]0x56fe+[char]0x50cf+[char]0x56fe+[char]0x5c42),'Image Layer','Background'))}
    }
    return @{layers=$rows;count=$rows.Count;selected=@($rows|Where-Object{$_.selected})}
}
function Find-LayerByRuntime($Runtime) {
    if($null -eq $Runtime -or @($Runtime).Count -lt 2){Fail 'LAYER_REFERENCE_REQUIRED' 'A current layer runtime_id is required.'}
    $tree=Get-LayersRoot
    $items=$tree.FindAll([System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)
    $found=@();foreach($item in $items){if(Same @($item.GetRuntimeId()) @($Runtime)){$found+=,$item}}
    if($found.Count -ne 1){Fail 'LAYER_NOT_FOUND' 'The exact observed layer no longer exists.'}
    return $found[0]
}
function Get-LayerActionButton([string]$Action) {
    if($Action -eq 'create_empty') { return Resolve @{automation_id='MainButton';control_type='Button';ancestors=@(@{automation_id='AddButton';class_name='ButtonWithChevron'},@{class_name='LocalAdjustmentsView'},@{class_name='Expander'},@{class_name='LocalAdjustmentsTool'})} }
    if($Action -eq 'remove_created') { return Resolve @{automation_id='RemoveButton';control_type='Button';ancestors=@(@{class_name='LocalAdjustmentsView'},@{class_name='Expander'},@{class_name='LocalAdjustmentsTool'})} }
    $label=switch($Action){'subject_mask'{[string][char]0x4e3b+[char]0x4f53};'background_mask'{[string][char]0x80cc+[char]0x666f};default{Fail 'INVALID_LAYER_ACTION' 'Unsupported fixed layer action.'}}
    $panel=Resolve @{class_name='LocalAdjustmentsTool';control_type='Custom'}
    $condition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,$label)
    $matches=$panel.FindAll([System.Windows.Automation.TreeScope]::Descendants,$condition)
    if($matches.Count -ne 1){Fail 'AMBIGUOUS_LAYER_ACTION' 'Expected one exact AI mask label.'}
    $button=[System.Windows.Automation.TreeWalker]::RawViewWalker.GetParent($matches.Item(0))
    if($button.Current.ControlType -ne [System.Windows.Automation.ControlType]::Button -or $button.Current.ClassName -cne 'Button'){Fail 'INVALID_LAYER_ACTION' 'Observed AI label does not belong to a native button.'}
    return $button
}
function Invoke-BackgroundLayer($Request) {
    foreach($key in (Names $Request)){if($key -notin @('op','pid','hwnd','background_only','guard','post_guard','action','layer_runtime_id','expected_layers')){Fail 'INVALID_REQUEST' 'Unexpected background layer argument.'}}
    $action=[string](Field $Request 'action')
    if($action -notin @('create_empty','subject_mask','background_mask','select_layer','remove_created')){Fail 'INVALID_LAYER_ACTION' 'Unsupported fixed layer action.'}
    $before=Get-LayerState
    if(-not (Same $before.layers (Field $Request 'expected_layers'))){Fail 'STALE_LAYERS' 'Layer list changed since the operation was prepared.'}
    if($before.selected.Count -ne 1){Fail 'SINGLE_LAYER_REQUIRED' 'Exactly one layer must be selected.'}
    $reference=Field $Request 'layer_runtime_id'
    if($action -eq 'select_layer') {
        $element=Find-LayerByRuntime $reference;$patternObject=Pattern $element 'SelectionItem'
    }else{
        if($action -eq 'remove_created') {
            $selected=$before.selected[0]
            if($selected.is_image_layer -or -not (Same $selected.runtime_id $reference)){Fail 'LAYER_DELETE_REFUSED' 'Only the exact selected created adjustment layer may be removed.'}
        }
        $element=Get-LayerActionButton $action;$patternObject=Pattern $element 'Invoke'
    }
    if($null -eq $patternObject -or -not $element.Current.IsEnabled){Fail 'CONTROL_UNAVAILABLE' 'Required native pattern is unavailable or disabled.'}
    Check-Guard (Field $Request 'guard')
    $script:MayHaveMutated=$true
    if($action -eq 'select_layer'){$patternObject.Select()}else{$patternObject.Invoke()}
    $deadline=[DateTime]::UtcNow.AddSeconds(60);$matched=$false;$newRows=@()
    $oldKeys=@($before.layers|ForEach-Object{$_.runtime_id -join ','})
    $oldNames=[Collections.Generic.Dictionary[string,int]]::new([StringComparer]::Ordinal)
    foreach($row in $before.layers){$key=([string]$row.is_image_layer)+'|'+$row.name;if($oldNames.ContainsKey($key)){$oldNames[$key]++}else{$oldNames[$key]=1}}
    do {
        Start-Sleep -Milliseconds 150
        while($true){
            try{$after=Get-LayerState;break}
            catch{
                if($_.Exception.Data['BridgeCode'] -ne 'LAYER_NAME_UNREADABLE' -or [DateTime]::UtcNow -ge $deadline){throw}
                Start-Sleep -Milliseconds 150
            }
        }
        $newRows=@($after.layers|Where-Object{($_.runtime_id -join ',') -notin $oldKeys})
        if($action -eq 'select_layer'){$matched=($after.selected.Count -eq 1 -and (Same $after.selected[0].runtime_id $reference) -and $after.count -eq $before.count)}
        elseif($action -eq 'remove_created'){$matched=($after.count -eq ($before.count-1) -and $after.selected.Count -eq 1 -and @($after.layers|Where-Object{Same $_.runtime_id $reference}).Count -eq 0)}
        else{
            $newNames=[Collections.Generic.Dictionary[string,int]]::new([StringComparer]::Ordinal)
            foreach($row in $after.layers){$key=([string]$row.is_image_layer)+'|'+$row.name;if($newNames.ContainsKey($key)){$newNames[$key]++}else{$newNames[$key]=1}}
            $preserved=$true;foreach($key in $oldNames.Keys){if(-not $newNames.ContainsKey($key) -or $newNames[$key] -lt $oldNames[$key]){$preserved=$false;break}}
            $newRows=@()
            if($after.selected.Count -eq 1 -and -not $after.selected[0].is_image_layer){
                $name=$after.selected[0].name
                if(@($before.layers|Where-Object{$_.name -ceq $name}).Count -eq 0 -and @($after.layers|Where-Object{$_.name -ceq $name}).Count -eq 1){$newRows=@($after.selected[0])}
            }
            $matched=($after.count -eq ($before.count+1) -and $preserved -and $newRows.Count -eq 1)
        }
        if($matched){break}
    }while([DateTime]::UtcNow -lt $deadline)
    if(-not $matched){Fail 'LAYER_RESULT_TIMEOUT' 'Native action returned but the expected layer result was not observed.' @{before=$before;after=$after}}
    Check-Guard (Field $Request 'post_guard')
    return @{window=$script:WindowInfo;accepted=$true;verified=$true;before=$before;after=$after;created=$newRows;mask_pixels_verified=$false;render_verified=$false}
}
