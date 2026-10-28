# Native Basic Color Editor: ValuePattern writes and bounded Enter commits; no Styles or reflection.
function Mixer-Labels {
    return @{basic=([string][char]0x57fa+[char]0x672c);hue=([string][char]0x8272+[char]0x76f8);
        saturation=([string][char]0x9971+[char]0x548c+[char]0x5ea6);lightness=([string][char]0x4eae+[char]0x5ea6)}
}
function Mixer-One($Root,$Condition,[string]$Description) {
    $items=$Root.FindAll([System.Windows.Automation.TreeScope]::Descendants,$Condition)
    if($items.Count -ne 1){Fail 'MIXER_CONTROL_UNVERIFIED' ('Expected one '+$Description)}
    $element=$items.Item(0)
    if($element.Current.ProcessId -ne $script:WindowInfo.pid){Fail 'MIXER_OWNER_CHANGED' 'Mixer control belongs to another process.'}
    return $element
}
function Mixer-Property($Property,$Value) {return [System.Windows.Automation.PropertyCondition]::new($Property,$Value)}
function Mixer-Rect($Element) {
    $r=$Element.Current.BoundingRectangle
    if($r.IsEmpty -or $r.Width -le 0 -or $r.Height -le 0){Fail 'MIXER_GEOMETRY_UNAVAILABLE' 'Patch geometry is unavailable.'}
    return @{x=$r.X;y=$r.Y;width=$r.Width;height=$r.Height}
}
function Mixer-Basic {
    $root=Resolve @{class_name='ColorEditorView';control_type='Custom'}
    $condition=[System.Windows.Automation.AndCondition]::new(
        (Mixer-Property ([System.Windows.Automation.AutomationElement]::ControlTypeProperty) ([System.Windows.Automation.ControlType]::TabItem)),
        (Mixer-Property ([System.Windows.Automation.AutomationElement]::NameProperty) (Mixer-Labels).basic))
    $basic=Mixer-One $root $condition 'Basic Color Editor tab'
    $selection=Pattern $basic 'SelectionItem'
    if($null -eq $selection -or -not $selection.Current.IsSelected){Fail 'MIXER_BASIC_REQUIRED' 'Select Basic Color Editor before this qualified route.'}
    return $basic
}
function Mixer-Edit($Basic,[string]$Field) {
    $labels=Mixer-Labels
    if($Field -cnotin @('hue','saturation','lightness')){Fail 'INVALID_MIXER_FIELD' 'Unsupported Basic mixer component.'}
    $condition=Mixer-Property ([System.Windows.Automation.AutomationElement]::AutomationIdProperty) 'ToolSliderDecimalUpDown'
    $items=$Basic.FindAll([System.Windows.Automation.TreeScope]::Children,$condition);$matches=@()
    foreach($item in $items){
        if($item.Current.ControlType -ne [System.Windows.Automation.ControlType]::Edit -or $item.Current.ClassName -cne 'TextBox'){continue}
        $label=[System.Windows.Automation.TreeWalker]::RawViewWalker.GetPreviousSibling($item)
        if($null -ne $label -and $label.Current.AutomationId -ceq 'PART_Label' -and $label.Current.ControlType -eq [System.Windows.Automation.ControlType]::Text -and
            $label.Current.ClassName -ceq 'TextBlock' -and $label.Current.Name -ceq $labels[$Field]){$matches+=,$item}
    }
    if($matches.Count -ne 1){Fail 'MIXER_FIELD_UNVERIFIED' 'HSL fields require the actual immediate native label; order is not identity.'}
    return $matches[0]
}
function Mixer-Patches($Basic) {
    $panel=Mixer-One $Basic (Mixer-Property ([System.Windows.Automation.AutomationElement]::ClassNameProperty) 'ColorEditorPatches') 'Basic patch panel'
    $list=Mixer-One $panel (Mixer-Property ([System.Windows.Automation.AutomationElement]::ControlTypeProperty) ([System.Windows.Automation.ControlType]::List)) 'Basic patch list'
    $items=$list.FindAll([System.Windows.Automation.TreeScope]::Children,
        (Mixer-Property ([System.Windows.Automation.AutomationElement]::ControlTypeProperty) ([System.Windows.Automation.ControlType]::DataItem)))
    if($items.Count -ne 9){Fail 'MIXER_PATCHES_UNVERIFIED' 'Exactly nine current Basic patch items are required.'}
    $rows=@();$elements=@()
    foreach($item in $items){
        if($item.Current.Name -cne 'P1.C1.Framework.Collections.IndexAwareItem`1[System.Windows.Media.Brush]'){
            Fail 'MIXER_PATCHES_UNVERIFIED' 'Unqualified Basic patch item provider.'
        }
        $buttons=$item.FindAll([System.Windows.Automation.TreeScope]::Children,
            (Mixer-Property ([System.Windows.Automation.AutomationElement]::ControlTypeProperty) ([System.Windows.Automation.ControlType]::Button)))
        if($buttons.Count -ne 1){Fail 'MIXER_PATCHES_UNVERIFIED' 'Each Basic patch must own exactly one native button.'}
        $button=$buttons.Item(0);$pattern=Pattern $button 'Invoke'
        if($null -eq $pattern -or $button.Current.ProcessId -ne $script:WindowInfo.pid){Fail 'MIXER_PATCHES_UNVERIFIED' 'Patch has no same-process native InvokePattern.'}
        $elements+=,$button
        $rows+=@{runtime_id=@($button.GetRuntimeId());parent_runtime_id=@($item.GetRuntimeId());enabled=[bool]$button.Current.IsEnabled;rect=(Mixer-Rect $button)}
    }
    return @{rows=$rows;elements=$elements;panel_runtime_id=@($panel.GetRuntimeId());list_runtime_id=@($list.GetRuntimeId());panel_rect=(Mixer-Rect $panel)}
}
function Mixer-State {
    $basic=Mixer-Basic;$patches=Mixer-Patches $basic;$fields=@{}
    foreach($field in @('hue','saturation','lightness')){
        $edit=Mixer-Edit $basic $field;$p=Pattern $edit 'Value'
        if($null -eq $p){Fail 'MIXER_FIELD_UNVERIFIED' 'Basic field has no ValuePattern.'}
        $text=[string]$p.Current.Value;$value=0.0
        if(-not [double]::TryParse($text,[Globalization.NumberStyles]::Float,[Globalization.CultureInfo]::InvariantCulture,[ref]$value) -or
            [double]::IsNaN($value) -or [double]::IsInfinity($value)){Fail 'MIXER_FIELD_UNVERIFIED' 'Basic field is not a finite native numeric value.'}
        $fields[$field]=@{value=$value;text=$text;runtime_id=@($edit.GetRuntimeId());enabled=[bool]$edit.Current.IsEnabled;read_only=[bool]$p.Current.IsReadOnly}
    }
    if(-not (Pattern $basic 'SelectionItem').Current.IsSelected){Fail 'MIXER_BASIC_REQUIRED' 'Basic tab changed during observation.'}
    return @{basic_runtime_id=@($basic.GetRuntimeId());panel_runtime_id=$patches.panel_runtime_id;list_runtime_id=$patches.list_runtime_id;
        buttons=$patches.rows;fields=$fields;panel_rect=$patches.panel_rect;native_color_identity_available=$false;brush_rgb_available=$false}
}
function Get-MixerState {return @{window=$script:WindowInfo;state=(Mixer-State)}}
function Mixer-Assert($Request,$ExtraKeys) {
    foreach($key in (Names $Request)){
        if($key -notin (@('op','pid','hwnd','background_only','guard','expected_mixer')+$ExtraKeys)){Fail 'INVALID_MIXER_REQUEST' 'Unexpected Basic mixer argument.'}
    }
    Check-Guard (Field $Request 'guard');$state=Mixer-State
    if(-not (Same $state (Field $Request 'expected_mixer'))){Fail 'STALE_MIXER' 'Basic mixer values or runtime bindings changed.'}
    return $state
}
function Select-MixerPatch($Request) {
    $before=Mixer-Assert $Request @('button_runtime_id');$runtime=Field $Request 'button_runtime_id'
    $patches=Mixer-Patches (Mixer-Basic);$matches=@()
    foreach($button in $patches.elements){if(Same @($button.GetRuntimeId()) $runtime){$matches+=,$button}}
    if($matches.Count -ne 1 -or -not $matches[0].Current.IsEnabled){Fail 'MIXER_PATCH_UNAVAILABLE' 'Exact observed Basic patch runtime reference unavailable.'}
    $p=Pattern $matches[0] 'Invoke';Check-Guard (Field $Request 'guard')
    $script:MayHaveMutated=$true;$p.Invoke()
    Check-Guard (Field $Request 'guard')
    return @{window=$script:WindowInfo;accepted=$true;state=(Mixer-State);invoked_runtime_id=$runtime;native_color_identity_verified=$false;image_modified=$false}
}
function Set-MixerField($Request) {
    $before=Mixer-Assert $Request @('field','value','values')
    $legacy=@(Names $Request) -notcontains 'values';$requested=Field $Request 'values'
    if($legacy){
        $field=[string](Field $Request 'field');$requested=@{};$requested[$field]=Field $Request 'value'
    }elseif(@(Names $Request) -contains 'field' -or @(Names $Request) -contains 'value'){
        Fail 'INVALID_MIXER_REQUEST' 'Use values or legacy field/value, not both.'
    }
    $limit=@{hue=30.0;saturation=80.0;lightness=50.0};$keys=@(Names $requested)
    if(($requested -isnot [Collections.IDictionary] -and $requested -isnot [PSCustomObject]) -or $keys.Count -lt 1 -or $keys.Count -gt 3){
        Fail 'INVALID_MIXER_VALUE' 'Expected one to three Basic HSL values.'
    }
    foreach($key in $keys){
        $value=Field $requested $key
        if(-not $limit.ContainsKey($key) -or $value -is [bool] -or $value -isnot [ValueType] -or
            [double]::IsNaN([double]$value) -or [double]::IsInfinity([double]$value) -or [math]::Abs([double]$value) -gt $limit[$key]){
            Fail 'INVALID_MIXER_VALUE' 'Basic HSL value is outside its qualified range.'
        }
    }
    Assert-ForegroundAllowed 'numeric_commit'
    $basic=Mixer-Basic;$prepared=@()
    foreach($field in @('hue','saturation','lightness')){
        if($field -notin $keys){continue}
        $edit=Mixer-Edit $basic $field;$p=Pattern $edit 'Value'
        if(-not $edit.Current.IsEnabled -or $null -eq $p -or $p.Current.IsReadOnly){Fail 'MIXER_FIELD_UNAVAILABLE' 'Requested Basic field is unavailable or read-only.'}
        $prepared+=@{field=$field;element=$edit;pattern=$p;value=[double](Field $requested $field)}
    }
    [void](Ensure-C1Foreground (Field $Request 'guard'));$commits=@{}
    foreach($entry in $prepared){
        Check-Guard (Field $Request 'guard');$script:MayHaveMutated=$true
        $entry.pattern.SetValue($entry.value.ToString('G17',[Globalization.CultureInfo]::InvariantCulture))
        $commits[$entry.field]=Commit-C1NumericEdit $entry.element (Field $Request 'guard')
    }
    Check-Guard (Field $Request 'guard');$after=Mixer-State
    foreach($entry in $prepared){
        if([math]::Abs($after.fields[$entry.field].value-$entry.value) -gt 0.00001){Fail 'MIXER_VALUE_UNKNOWN' 'Basic HSL batch did not read back the requested values.'}
    }
    $result=@{window=$script:WindowInfo;accepted=$true;control_confirmed=$true;state=$after;commits=$commits;values=$requested;
        focus_policy='foreground_numeric_commit';native_color_identity_verified=$false;model_verified=$false;render_verified=$false}
    if($legacy){$result.commit=$commits[[string](Field $Request 'field')]}
    return $result
}
