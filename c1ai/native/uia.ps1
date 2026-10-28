# Windows PowerShell 5.1. Fixed UI Automation program, JSON request on stdin.
# No script text, keyboard sequences, coordinates or executable paths are accepted.
param([string]$RequestJson, [switch]$Resident)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$script:MayHaveMutated = $false
$script:RootElement = $null
$script:WindowInfo = $null
$script:BackgroundOnly = $true
$script:ResolveCache = @{}
$script:ToolViewCache = @{}
$script:SelectorParentCache = @{}
$script:ResolveOwnedCache = @{}
$script:Metrics = @{resolve_queries=0;resolve_cache_hits=0;observation_reads=0}
$script:RequestWatch = [Diagnostics.Stopwatch]::StartNew()

function Fail([string]$Code, [string]$Message, $Details = $null) {
    $exception = New-Object System.Exception($Message)
    $exception.Data['BridgeCode'] = $Code
    if ($null -ne $Details) { $exception.Data['BridgeDetails'] = $Details }
    throw $exception
}
function Assert-ForegroundAllowed([string]$Operation) {
    if ($script:BackgroundOnly) {
        Fail 'BACKGROUND_ONLY' 'Background-only mode blocks focus, synthetic input, screenshots and unqualified native actions; nothing was dispatched.' @{operation=$Operation; background_only=$true; possibly_mutated=$false}
    }
}
function Field($Object, [string]$Name, $Default = $null) {
    if ($null -eq $Object) { return ,$Default }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return ,$Object[$Name] }
    } elseif ($null -ne $Object.PSObject.Properties[$Name]) { return ,$Object.$Name }
    return ,$Default
}
function Names($Object) {
    if ($Object -is [System.Collections.IDictionary]) { return @($Object.Keys) }
    return @($Object.PSObject.Properties.Name)
}
function Check-ExecutionPolicy($Request, [string]$Operation) {
    $backgroundOnly = Field $Request 'background_only' $true
    if ($backgroundOnly -isnot [bool]) { Fail 'INVALID_REQUEST' 'background_only must be a JSON boolean.' }
    $script:BackgroundOnly = $backgroundOnly
    if ($Operation -in @('dismiss_info','import_style_file','save_style_file','choose_export_folder','confirm_export_folder','select_current_export_folder','cancel_style_import','activate','invoke','click','navigate','nudge_adjustment','screenshot','layer_action','prepare_layers','tangent_layer','curve_select','curve_add_point','curve_move_point','mixer_select','mixer_set')) { Assert-ForegroundAllowed $Operation }
}
function Same($A, $B) {
    if ($null -eq $A -or $null -eq $B) { return ($null -eq $A -and $null -eq $B) }
    if ($A -is [bool] -or $B -is [bool]) { return ($A -is [bool] -and $B -is [bool] -and $A -eq $B) }
    if ($A -is [string] -or $B -is [string]) { return ($A -is [string] -and $B -is [string] -and $A -ceq $B) }
    if (($A -is [System.Collections.IList]) -or ($B -is [System.Collections.IList])) {
        if (-not ($A -is [System.Collections.IList]) -or -not ($B -is [System.Collections.IList])) { return $false }
        if ($A.Count -ne $B.Count) { return $false }
        for ($j=0; $j -lt $A.Count; $j++) { if (-not (Same $A[$j] $B[$j])) { return $false } }
        return $true
    }
    if (($A -is [ValueType]) -and ($B -is [ValueType])) { return ($A -eq $B) }
    $aNames = @(Names $A | Sort-Object); $bNames = @(Names $B | Sort-Object)
    if (($aNames -join '|') -cne ($bNames -join '|')) { return $false }
    foreach ($key in $aNames) { if (-not (Same (Field $A $key) (Field $B $key))) { return $false } }
    return $true
}
function Pattern($Element, [string]$Name) {
    $patternId = switch ($Name) {
        'Value' { [System.Windows.Automation.ValuePattern]::Pattern }
        'RangeValue' { [System.Windows.Automation.RangeValuePattern]::Pattern }
        'Toggle' { [System.Windows.Automation.TogglePattern]::Pattern }
        'Selection' { [System.Windows.Automation.SelectionPattern]::Pattern }
        'SelectionItem' { [System.Windows.Automation.SelectionItemPattern]::Pattern }
        'Invoke' { [System.Windows.Automation.InvokePattern]::Pattern }
        'ExpandCollapse' { [System.Windows.Automation.ExpandCollapsePattern]::Pattern }
        'Scroll' { [System.Windows.Automation.ScrollPattern]::Pattern }
        'ScrollItem' { [System.Windows.Automation.ScrollItemPattern]::Pattern }
    }
    if ($null -eq $patternId) { return $null }
    $result = $null
    if ($Element.TryGetCurrentPattern($patternId, [ref]$result)) { return $result }
    return $null
}
function Base-Selector($Element) {
    $c = $Element.Current
    return [ordered]@{automation_id=[string]$c.AutomationId; name=[string]$c.Name; help_text=[string]$c.HelpText;
        control_type=([string]$c.ControlType.ProgrammaticName -replace '^ControlType\.', ''); class_name=[string]$c.ClassName}
}
function Selector-For($Element) {
    $selector = Base-Selector $Element
    $parents = New-Object System.Collections.Generic.List[object]
    $parent = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($Element)
    $depth = 0
    while ($null -ne $parent -and $depth -lt 8) {
        if ([System.Windows.Automation.Automation]::Compare($parent, $script:RootElement)) { break }
        $parentKey = (@($parent.GetRuntimeId()) -join ',')
        if (-not $script:SelectorParentCache.ContainsKey($parentKey)) {
            $script:SelectorParentCache[$parentKey] = @{
                selector=(Base-Selector $parent)
                parent=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($parent)
            }
        }
        # Locator metadata only, reset per request. Node values and every
        # dispatch guard still read current provider properties independently.
        $parents.Add($script:SelectorParentCache[$parentKey].selector)
        $parent = $script:SelectorParentCache[$parentKey].parent
        $depth++
    }
    $selector['ancestors'] = @($parents.ToArray())
    $previousSibling = [System.Windows.Automation.TreeWalker]::RawViewWalker.GetPreviousSibling($Element)
    if ($null -ne $previousSibling -and
        $previousSibling.Current.ControlType -eq [System.Windows.Automation.ControlType]::Text -and
        -not [string]::IsNullOrWhiteSpace([string]$previousSibling.Current.Name)) {
        $selector['previous_sibling'] = Base-Selector $previousSibling
    }
    return $selector
}
function Node($Element, [bool]$IncludeSelector = $true) {
    $c = $Element.Current
    $node = Base-Selector $Element
    $node['enabled'] = [bool]$c.IsEnabled
    $node['offscreen'] = [bool]$c.IsOffscreen
    $node['keyboard_focusable'] = [bool]$c.IsKeyboardFocusable
    $node['has_keyboard_focus'] = [bool]$c.HasKeyboardFocus
    $node['item_status'] = [string]$c.ItemStatus
    $node['item_type'] = [string]$c.ItemType
    $node['selection_item_property_supported'] = $false
    try {
        $selectedProperty = $Element.GetCurrentPropertyValue([System.Windows.Automation.SelectionItemPattern]::IsSelectedProperty, $true)
        if (-not [object]::ReferenceEquals($selectedProperty, [System.Windows.Automation.AutomationElement]::NotSupported) -and $selectedProperty -is [bool]) {
            $node['selection_item_property_supported'] = $true
            $node['selected_property'] = [bool]$selectedProperty
        }
    } catch { $node['selection_item_property_error'] = $_.Exception.Message }
    $r = $c.BoundingRectangle
    if ($r.IsEmpty) { $node['rect'] = $null } else {
        $node['rect'] = @{x=$r.X;y=$r.Y;width=$r.Width;height=$r.Height}
    }
    $node['runtime_id'] = @($Element.GetRuntimeId())
    $directParent = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($Element)
    $node['parent_runtime_id'] = if ($null -ne $directParent) { @($directParent.GetRuntimeId()) } else { @() }
    $node['patterns'] = @($Element.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName -replace 'PatternIdentifiers\.Pattern$', '' -replace '^.*\.', '' })
    if ($IncludeSelector) { $node['selector'] = Selector-For $Element }
    $range = if ($node['patterns'] -contains 'RangeValue') { Pattern $Element 'RangeValue' } else { $null }
    if ($null -ne $range) {
        $node['value'] = $range.Current.Value
        $node['range'] = @{minimum=$range.Current.Minimum; maximum=$range.Current.Maximum;
            small_change=$range.Current.SmallChange; large_change=$range.Current.LargeChange; read_only=$range.Current.IsReadOnly}
    } else {
        $value = if ($node['patterns'] -contains 'Value') { Pattern $Element 'Value' } else { $null }
        if ($null -ne $value) { $node['value'] = $value.Current.Value; $node['read_only'] = $value.Current.IsReadOnly }
    }
    $toggle = if ($node['patterns'] -contains 'Toggle') { Pattern $Element 'Toggle' } else { $null }
    if ($null -ne $toggle) { $node['toggle'] = [string]$toggle.Current.ToggleState }
    $selection = if ($node['patterns'] -contains 'Selection') { Pattern $Element 'Selection' } else { $null }
    if ($null -ne $selection) {
        $items = New-Object System.Collections.Generic.List[object]
        foreach ($item in $selection.Current.GetSelection()) {
            $itemInfo = Base-Selector $item
            $itemInfo['runtime_id'] = @($item.GetRuntimeId())
            $items.Add($itemInfo)
        }
        $node['selection'] = @($items.ToArray())
        if($c.AutomationId -eq 'ViewerToolbarLayersComboBox') {
            $node['image_layer_selected'] = ($items.Count -eq 1 -and $items[0]['name'] -ceq 'P1.C1.ViewModels.Tools.LocalAdjustments.LayerSelectorViewModel+ImageLayer')
        }
    }
    $selectionItem = if ($node['patterns'] -contains 'SelectionItem') { Pattern $Element 'SelectionItem' } else { $null }
    if ($null -ne $selectionItem) { $node['selected'] = [bool]$selectionItem.Current.IsSelected }
    $expanded = if ($node['patterns'] -contains 'ExpandCollapse') { Pattern $Element 'ExpandCollapse' } else { $null }
    if ($null -ne $expanded) { $node['expand_state'] = [string]$expanded.Current.ExpandCollapseState }
    return $node
}
function Validate-Selector($Selector, [bool]$AllowAncestors = $true) {
    if ($null -eq $Selector -or $Selector -is [string]) { Fail 'INVALID_SELECTOR' 'Selector must be a JSON object.' }
    $allowed = @('automation_id','name','help_text','control_type','class_name')
    if ($AllowAncestors) { $allowed += @('ancestors','previous_sibling','child') }
    foreach ($key in (Names $Selector)) {
        if ($key -notin $allowed) { Fail 'INVALID_SELECTOR' "Unsupported selector property: $key" }
    }
    $hasField = $false
    foreach ($key in @('automation_id','name','help_text','control_type','class_name')) {
        if ((Names $Selector) -contains $key) {
            if (-not ((Field $Selector $key) -is [string])) { Fail 'INVALID_SELECTOR' 'Selector properties must be strings.' }
            if ((Field $Selector $key).Length -gt 0) { $hasField = $true }
        }
    }
    if (-not $hasField) { Fail 'INVALID_SELECTOR' 'An empty selector cannot identify a control.' }
    $ancestors = Field $Selector 'ancestors' @()
    if (@($ancestors).Count -gt 8) { Fail 'INVALID_SELECTOR' 'At most eight exact ancestors are allowed.' }
    foreach ($parentSelector in $ancestors) { Validate-Selector $parentSelector $false }
    if ((Names $Selector) -contains 'previous_sibling') {
        Validate-Selector (Field $Selector 'previous_sibling') $false
    }
    if ((Names $Selector) -contains 'child') {
        Validate-Selector (Field $Selector 'child') $false
    }
}
function Observe($Element, $Fields = @('name','value','toggle','selection','selected','image_layer_selected')) {
    $script:Metrics.observation_reads++
    $c = $Element.Current
    if(-not $PSBoundParameters.ContainsKey('Fields')){
        if($c.ControlType -eq [System.Windows.Automation.ControlType]::Text){$Fields=@('name')}
        elseif($c.ControlType -eq [System.Windows.Automation.ControlType]::ComboBox){$Fields=@('name','selection','image_layer_selected')}
        elseif($c.ControlType -eq [System.Windows.Automation.ControlType]::Edit){$Fields=@('name','value')}
    }
    $node = @{automation_id=[string]$c.AutomationId;control_type=([string]$c.ControlType.ProgrammaticName -replace '^ControlType\.','');name=[string]$c.Name}
    if ($Fields -contains 'value') {
        $p=Pattern $Element 'RangeValue'
        if($null -ne $p){$node.value=$p.Current.Value}
        else {$p=Pattern $Element 'Value';if($null -ne $p){$node.value=$p.Current.Value}}
    }
    if($Fields -contains 'toggle'){$p=Pattern $Element 'Toggle';if($null -ne $p){$node.toggle=[string]$p.Current.ToggleState}}
    if($Fields -contains 'selection' -or $Fields -contains 'image_layer_selected') {
        $p=Pattern $Element 'Selection'
        if($null -ne $p){
            $items=New-Object System.Collections.Generic.List[object]
            foreach($item in $p.Current.GetSelection()){$v=Base-Selector $item;$v.runtime_id=@($item.GetRuntimeId());$items.Add($v)}
            $node.selection=@($items.ToArray())
            if($c.AutomationId -eq 'ViewerToolbarLayersComboBox'){$node.image_layer_selected=($items.Count -eq 1 -and $items[0].name -ceq 'P1.C1.ViewModels.Tools.LocalAdjustments.LayerSelectorViewModel+ImageLayer')}
        }
    }
    if($Fields -contains 'selected'){$p=Pattern $Element 'SelectionItem';if($null -ne $p){$node.selected=[bool]$p.Current.IsSelected}}
    if($Fields -contains 'expand_state'){$p=Pattern $Element 'ExpandCollapse';if($null -ne $p){$node.expand_state=[string]$p.Current.ExpandCollapseState}}
    return $node
}
function Matches($Element, $Selector) {
    $actual = Base-Selector $Element
    foreach ($key in @('automation_id','name','help_text','control_type','class_name')) {
        if ((Names $Selector) -contains $key) {
            if ([string]$actual[$key] -cne [string](Field $Selector $key)) { return $false }
        }
    }
    return $true
}
function Child-Matches($Element, $Selector) {
    if ($null -eq $Selector) { return $true }
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $child = $walker.GetFirstChild($Element)
    $count = 0
    $seen = 0
    while ($null -ne $child) {
        $seen++
        if ($seen -gt 256) { Fail 'TOO_MANY_CONTROLS' 'Child relation exceeds its bounded scope.' }
        if (Matches $child $Selector) { $count++ }
        $child = $walker.GetNextSibling($child)
    }
    return ($count -eq 1)
}
function Test-BoundDialogOwner([long]$DialogHandle, [long]$MainHandle) {
    if ($DialogHandle -le 0 -or $DialogHandle -eq $MainHandle) { return $false }
    $seen = @{}
    $cursor = $DialogHandle
    for ($depth=0; $depth -lt 64; $depth++) {
        if ($seen.ContainsKey($cursor)) { return $false }
        $seen[$cursor] = $true
        $cursor = [C1WindowNative]::GetWindow([IntPtr]$cursor, 4).ToInt64() # GW_OWNER
        if ($cursor -eq $MainHandle) { return $true }
        if ($cursor -le 0) { return $false }
    }
    return $false
}
function Bound-ResolveDialogs {
    foreach ($dialog in (Owned-DialogElements $script:WindowInfo.pid $script:WindowInfo.hwnd $script:WindowInfo.exe)) {
        if (Test-BoundDialogOwner ([long]$dialog.Current.NativeWindowHandle) $script:WindowInfo.hwnd) { $dialog }
    }
}
function Assert-CachedOwnedScope($Element, $Evidence) {
    $fresh = @(Bound-ResolveDialogs | Where-Object { Matches $_ $Evidence.scope })
    if ($fresh.Count -ne 1 -or [long]$fresh[0].Current.NativeWindowHandle -ne $Evidence.handle -or
        -not [System.Windows.Automation.Automation]::Compare($fresh[0], $Evidence.dialog) -or
        $Element.Current.ProcessId -ne $fresh[0].Current.ProcessId) {
        Fail 'STALE_TARGET' 'Owned window scope changed within request.'
    }
    $cursor = $Element
    for ($depth=0; $depth -lt 128 -and $null -ne $cursor; $depth++) {
        if ([System.Windows.Automation.Automation]::Compare($cursor, $fresh[0])) { return }
        $cursor = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($cursor)
    }
    Fail 'STALE_TARGET' 'Cached control left its owned window scope.'
}
function Resolve($Selector) {
    Validate-Selector $Selector
    $cacheKey=$Selector | ConvertTo-Json -Depth 20 -Compress
    if($script:ResolveCache.ContainsKey($cacheKey)) {
        $cached=$script:ResolveCache[$cacheKey]
        if ($script:ResolveOwnedCache.ContainsKey($cacheKey)) {
            Assert-CachedOwnedScope $cached $script:ResolveOwnedCache[$cacheKey]
        }
        # Never cache values. A detached/recycled provider throws here and the
        # mutation stops; no automatic re-resolve after a possibly applied write.
        if(-not (Matches $cached $Selector)){Fail 'STALE_TARGET' 'Resolved control changed within this request.'}
        $siblingSelector=Field $Selector 'previous_sibling'
        if($null -ne $siblingSelector){
            $sibling=[System.Windows.Automation.TreeWalker]::RawViewWalker.GetPreviousSibling($cached)
            if($null -eq $sibling -or -not (Matches $sibling $siblingSelector)){Fail 'STALE_TARGET' 'Control label changed within request.'}
        }
        $parent=$cached
        if(-not (Child-Matches $cached (Field $Selector 'child'))){Fail 'STALE_TARGET' 'Child label changed within request.'}
        foreach($ancestor in (Field $Selector 'ancestors' @())){
            $parent=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($parent)
            if($null -eq $parent -or -not (Matches $parent $ancestor)){Fail 'STALE_TARGET' 'Control owner changed within request.'}
        }
        $script:Metrics.resolve_cache_hits++
        return $cached
    }
    $script:Metrics.resolve_queries++
    $conditions = New-Object System.Collections.Generic.List[System.Windows.Automation.Condition]
    $properties = @{automation_id=[System.Windows.Automation.AutomationElement]::AutomationIdProperty;
        name=[System.Windows.Automation.AutomationElement]::NameProperty; help_text=[System.Windows.Automation.AutomationElement]::HelpTextProperty;
        class_name=[System.Windows.Automation.AutomationElement]::ClassNameProperty}
    foreach ($key in @('automation_id','name','help_text','class_name')) {
        if ((Names $Selector) -contains $key) {
            $condition = [System.Windows.Automation.PropertyCondition]::new($properties[$key], [string](Field $Selector $key))
            $conditions.Add($condition)
        }
    }
    if((Names $Selector) -contains 'control_type'){
        $kind=[string](Field $Selector 'control_type')
        $type=[System.Windows.Automation.ControlType]::$kind
        if($null -eq $type){Fail 'INVALID_SELECTOR' 'Unknown native control type.'}
        $conditions.Add([System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,$type))
    }
    $condition = [System.Windows.Automation.Condition]::TrueCondition
    if ($conditions.Count -eq 1) { $condition = $conditions[0] }
    if ($conditions.Count -gt 1) { $condition = [System.Windows.Automation.AndCondition]::new([System.Windows.Automation.Condition[]]$conditions.ToArray()) }
    $windowScopes = @()
    foreach ($scope in (@($Selector) + [object[]](Field $Selector 'ancestors' @()))) {
        if ((Field $scope 'control_type') -ceq 'Window' -and
            ((Field $scope 'name' '') -ne '' -or (Field $scope 'automation_id' '') -ne '' -or (Field $scope 'class_name' '') -eq '#32770') -and
            -not (Matches $script:RootElement $scope)) { $windowScopes += $scope }
    }
    $ownedOrigins = New-Object System.Collections.Generic.List[object]
    $scopedSearch = $false
    # A unique explicitly named owned window is a complete search boundary.
    # Keep ownership evidence for cache validation; do not scan the entire main
    # subtree first when the selector already requires this exact modal scope.
    if ($windowScopes.Count -eq 1 -and
        ((Field $windowScopes[0] 'name' '') -ne '' -or (Field $windowScopes[0] 'automation_id' '') -ne '')) {
        $scope=$windowScopes[0]
        $scopedDialogs=@(Bound-ResolveDialogs | Where-Object { Matches $_ $scope })
        if($scopedDialogs.Count -gt 1){Fail 'AMBIGUOUS_CONTROL' 'More than one owned window matches the explicit scope.'}
        if($scopedDialogs.Count -eq 1){
            $dialog=$scopedDialogs[0]
            $candidates=New-Object System.Collections.Generic.List[object]
            foreach($candidate in $dialog.FindAll([System.Windows.Automation.TreeScope]::Subtree,$condition)) {
                if($candidate.Current.ProcessId -ne $dialog.Current.ProcessId){continue}
                $candidates.Add($candidate)
                $ownedOrigins.Add(@{element=$candidate;dialog=$dialog;scope=$scope;handle=[long]$dialog.Current.NativeWindowHandle})
            }
            $scopedSearch=$true
        }
    }
    # Numeric tool edits share the same AutomationId. Resolve their observed
    # immediate tool view first, then keep the full sibling/ancestor validation
    # below. This avoids walking every unrelated numeric control for each guard.
    $parents = [object[]](Field $Selector 'ancestors' @())
    $viewClass = ''
    if ($parents.Count -gt 0) { $viewClass = [string](Field $parents[0] 'class_name') }
    if ($scopedSearch) {
        # Candidates above already passed the bound native-owner window scope.
    } elseif ((Field $Selector 'automation_id') -ceq 'ToolSliderDecimalUpDown' -and
        $viewClass -cin @('ExposureToolView','WhiteBalanceView','ShadowHighlightToolView')) {
        $viewCondition = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ClassNameProperty, $viewClass)
        # Cache only tool locations inside this single request, never values.
        # Full field labels and owner ancestry are still checked below.
        if (-not $script:ToolViewCache.ContainsKey($viewClass)) {
            $script:ToolViewCache[$viewClass] = $script:RootElement.FindAll([System.Windows.Automation.TreeScope]::Subtree, $viewCondition)
        }
        $views = $script:ToolViewCache[$viewClass]
        $candidates = New-Object System.Collections.Generic.List[System.Windows.Automation.AutomationElement]
        foreach ($view in $views) {
            foreach ($item in $view.FindAll([System.Windows.Automation.TreeScope]::Subtree, $condition)) {
                $candidates.Add($item)
            }
        }
    } elseif ($parents.Count -gt 0 -and (Field $parents[-1] 'class_name') -cin @('CurvesTool','SelectiveColorTool','ColorBalanceTool','LocalAdjustmentsTool')) {
        $ownerClass=[string](Field $parents[-1] 'class_name')
        $ownerCondition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ClassNameProperty,$ownerClass)
        $owners=$script:RootElement.FindAll([System.Windows.Automation.TreeScope]::Subtree,$ownerCondition)
        $candidates=New-Object System.Collections.Generic.List[System.Windows.Automation.AutomationElement]
        foreach($ownerElement in $owners){foreach($item in $ownerElement.FindAll([System.Windows.Automation.TreeScope]::Subtree,$condition)){$candidates.Add($item)}}
    } else {
        $candidates = $script:RootElement.FindAll([System.Windows.Automation.TreeScope]::Subtree, $condition)
    }
    # WPF modal windows may move from the main subtree to desktop-owned
    # windows after opening. Resolve only explicitly named window scopes and
    # only through the existing process/owner verification, never the desktop.
    if ($windowScopes.Count -gt 0 -and -not $scopedSearch) {
        $combined = New-Object System.Collections.Generic.List[object]
        foreach ($candidate in $candidates) { $combined.Add($candidate) }
        foreach ($dialog in (Bound-ResolveDialogs)) {
            $inScope = $false
            foreach ($scope in $windowScopes) { if (Matches $dialog $scope) { $inScope = $true; break } }
            if ($inScope) {
                foreach ($candidate in $dialog.FindAll([System.Windows.Automation.TreeScope]::Subtree, $condition)) {
                    if ($candidate.Current.ProcessId -ne $dialog.Current.ProcessId) { continue }
                    $combined.Add($candidate)
                    $ownedOrigins.Add(@{element=$candidate;dialog=$dialog;scope=$scope;handle=[long]$dialog.Current.NativeWindowHandle})
                }
            }
        }
        $candidates = $combined.ToArray()
    }
    if ($candidates.Count -gt 10000) { Fail 'TOO_MANY_CONTROLS' 'Refine the selector; more than 10,000 controls matched its initial conditions.' }
    $matches = New-Object System.Collections.Generic.List[object]
    foreach ($candidate in $candidates) {
        if (-not (Matches $candidate $Selector)) { continue }
        $previousSelector = Field $Selector 'previous_sibling'
        if ($null -ne $previousSelector) {
            $previousSibling = [System.Windows.Automation.TreeWalker]::RawViewWalker.GetPreviousSibling($candidate)
            if ($null -eq $previousSibling -or -not (Matches $previousSibling $previousSelector)) { continue }
        }
        $parent = $candidate
        if (-not (Child-Matches $candidate (Field $Selector 'child'))) { continue }
        $accepted = $true
        foreach ($parentSelector in (Field $Selector 'ancestors' @())) {
            $parent = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($parent)
            if ($null -eq $parent -or -not (Matches $parent $parentSelector)) { $accepted = $false; break }
        }
        if ($accepted) {
            # WPF menus can expose the very same automation element through
            # both the menu and Popup branches. Only identical elements are
            # deduplicated; distinct controls remain an ambiguity error.
            $duplicate = $false
            foreach($known in $matches) {
                if([System.Windows.Automation.Automation]::Compare($known,$candidate)) {$duplicate=$true;break}
            }
            if(-not $duplicate){$matches.Add($candidate)}
        }
        if ($matches.Count -gt 1) { Fail 'AMBIGUOUS_CONTROL' 'More than one control matches. Bind a unique selector with ancestors.' @($matches.ToArray() | ForEach-Object { Node $_ }) }
    }
    if ($matches.Count -eq 0) { Fail 'CONTROL_NOT_FOUND' 'No control matches. Make its tool visible and inspect again.' }
    $script:ResolveCache[$cacheKey]=$matches[0]
    foreach ($origin in $ownedOrigins) {
        if ([System.Windows.Automation.Automation]::Compare($origin.element, $matches[0])) {
            $script:ResolveOwnedCache[$cacheKey] = $origin
            break
        }
    }
    return $matches[0]
}
function Owned-DialogElements([int]$ProcessId, [long]$MainHandle, [string]$Exe) {
    $owners=@{};$owners[$ProcessId]=$true
    $helperPath=Join-Path ([IO.Path]::GetDirectoryName($Exe)) 'P1.ShowDialogBoxes.exe'
    Get-CimInstance Win32_Process -Filter ("ParentProcessId="+$ProcessId) | ForEach-Object {
        if($_.ExecutablePath -ieq $helperPath){$owners[[int]$_.ProcessId]=$true}
    }
    foreach($handle in [C1WindowNative]::Windows()) {
        $owner=0;[void][C1WindowNative]::GetWindowThreadProcessId([IntPtr]$handle,[ref]$owner)
        if($handle -eq $MainHandle -or -not $owners.ContainsKey($owner) -or -not [C1WindowNative]::IsWindowVisible([IntPtr]$handle)){continue}
        $element=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$handle)
        if($null -ne $element -and $element.Current.ProcessId -eq $owner){$element}
    }
}
function Style-ImportDialog([int]$ProcessId, [long]$MainHandle, [string]$Exe, [int]$WaitMs=0) {
    $title=-join ([char[]](0x5bfc,0x5165,0x9884,0x8bbe,0x4e0e,0x6837,0x5f0f))
    $wait=[Diagnostics.Stopwatch]::StartNew()
    do {
        $matches=@(Owned-DialogElements $ProcessId $MainHandle $Exe | Where-Object {
            $_.Current.Name -ceq $title -and $_.Current.ClassName -ceq '#32770'
        })
        if($matches.Count -eq 1){return $matches[0]}
        if($matches.Count -gt 1){Fail 'STYLE_DIALOG_UNAVAILABLE' 'Ambiguous Capture One import pickers; no input dispatched.'}
        if($wait.ElapsedMilliseconds -ge $WaitMs){break}
        # Opening the native picker starts a helper process asynchronously.
        # Wait for read-only ownership discovery, never repeat the menu action.
        Start-Sleep -Milliseconds 150
    } while($true)
    Fail 'STYLE_DIALOG_UNAVAILABLE' 'Expected one verified Capture One import picker.'
}
function Style-SaveDialog([int]$ProcessId, [long]$MainHandle, [string]$Exe, [int]$WaitMs=0) {
    $title=-join ([char[]](0x4fdd,0x5b58,0x6837,0x5f0f))
    $wait=[Diagnostics.Stopwatch]::StartNew()
    do {
        $matches=@(Owned-DialogElements $ProcessId $MainHandle $Exe | Where-Object {
            $_.Current.Name -ceq $title -and $_.Current.ClassName -ceq '#32770'
        })
        if($matches.Count -eq 1){return $matches[0]}
        if($matches.Count -gt 1){Fail 'STYLE_DIALOG_UNAVAILABLE' 'Ambiguous Capture One save pickers; no input dispatched.'}
        if($wait.ElapsedMilliseconds -ge $WaitMs){break}
        # Opening the native picker starts a helper process asynchronously.
        # Wait for read-only ownership discovery, never repeat the menu action.
        Start-Sleep -Milliseconds 150
    } while($true)
    Fail 'STYLE_DIALOG_UNAVAILABLE' 'Expected one verified Capture One save picker.'
}
function Style-FilenameHandle([IntPtr]$Dialog) {
    $parent=$Dialog
    foreach($expectedClass in @('ComboBoxEx32','ComboBox','Edit')) {
        $child=[C1WindowNative]::GetDlgItem($parent,1148)
        $classBuffer=New-Object System.Text.StringBuilder 256
        if($child -eq [IntPtr]::Zero){Fail 'STYLE_FIELD_UNAVAILABLE' 'Native filename control is missing.'}
        [void][C1WindowNative]::GetClassName($child,$classBuffer,256)
        if($classBuffer.ToString() -cne $expectedClass -or [C1WindowNative]::GetParent($child) -ne $parent){Fail 'STYLE_FIELD_UNAVAILABLE' 'Native filename control hierarchy changed.'}
        $parent=$child
    }
    return $parent
}
function Style-SaveFilenameHandle([IntPtr]$Dialog) {
    $matches=New-Object System.Collections.Generic.List[object]
    foreach($handle in [C1WindowNative]::Children($Dialog)) {
        $child=[IntPtr]$handle
        if([C1WindowNative]::GetDlgCtrlID($child) -ne 1001){continue}
        $parent=$child;$valid=$true
        foreach($expectedClass in @('Edit','ComboBox','FloatNotifySink','DirectUIHWND','DUIViewWndClassName')) {
            $classBuffer=New-Object System.Text.StringBuilder 256
            [void][C1WindowNative]::GetClassName($parent,$classBuffer,256)
            if($classBuffer.ToString() -cne $expectedClass){$valid=$false;break}
            $parent=[C1WindowNative]::GetParent($parent)
        }
        if($valid -and $parent -eq $Dialog){$matches.Add($child)}
    }
    if($matches.Count -ne 1){Fail 'STYLE_FIELD_UNAVAILABLE' 'Expected one observed Save dialog filename hierarchy.'}
    return [IntPtr]$matches[0]
}
function Folder-NativeNode([long]$Handle) {
    $native=[IntPtr]$Handle
    if($Handle -le 0 -or -not [C1WindowNative]::IsWindow($native)){Fail 'FOLDER_PICKER_UNAVAILABLE' 'Native folder window no longer exists.'}
    $ownerId=0;$classBuffer=New-Object Text.StringBuilder 256;$textBuffer=New-Object Text.StringBuilder 4096
    [void][C1WindowNative]::GetWindowThreadProcessId($native,[ref]$ownerId)
    [void][C1WindowNative]::GetClassName($native,$classBuffer,$classBuffer.Capacity)
    [void][C1WindowNative]::GetWindowText($native,$textBuffer,$textBuffer.Capacity)
    return @{hwnd=$Handle;pid=$ownerId;class_name=$classBuffer.ToString();id=[C1WindowNative]::GetDlgCtrlID($native);
        text=$textBuffer.ToString();parent=[C1WindowNative]::GetParent($native).ToInt64();
        owner=[C1WindowNative]::GetWindow($native,4).ToInt64();visible=[C1WindowNative]::IsWindowVisible($native);
        enabled=[C1WindowNative]::IsWindowEnabled($native)}
}
function Folder-PickerInfo([int]$ProcessId,[long]$MainHandle,[string]$Exe,[long]$ExpectedDialog=0,[long]$ExpectedExport=0) {
    $title=-join ([char[]](0x9009,0x62e9,0x6587,0x4ef6,0x5939))
    $exportTitle=-join ([char[]](0x5bfc,0x51fa,0x56fe,0x50cf))
    $labelText=-join ([char[]](0x6587,0x4ef6,0x5939,0x3a))
    $helperPath=Join-Path ([IO.Path]::GetDirectoryName($Exe)) 'P1.ShowDialogBoxes.exe'
    $helpers=@{}
    Get-CimInstance Win32_Process -Filter ("ParentProcessId="+$ProcessId) | ForEach-Object {
        if($_.ExecutablePath -ieq $helperPath -and $_.CommandLine -match 'P1\.ShowDialogBoxes\.exe"?\s+FolderBrowser(?:\s|$)'){$helpers[[int]$_.ProcessId]=$_}
    }
    $candidates=New-Object Collections.Generic.List[object]
    foreach($handle in [C1WindowNative]::Windows()) {
        $ownerId=0;[void][C1WindowNative]::GetWindowThreadProcessId([IntPtr]$handle,[ref]$ownerId)
        if(-not $helpers.ContainsKey($ownerId) -or -not [C1WindowNative]::IsWindowVisible([IntPtr]$handle)){continue}
        $node=Folder-NativeNode $handle
        if($node.class_name -ceq '#32770' -and $node.text -ceq $title){$candidates.Add($node)}
    }
    if($candidates.Count -ne 1){Fail 'FOLDER_PICKER_UNAVAILABLE' 'Expected exactly one qualified Capture One FolderBrowser picker.'}
    $dialog=$candidates[0]
    if($ExpectedDialog -gt 0 -and $dialog.hwnd -ne $ExpectedDialog){Fail 'WRONG_DIALOG' 'The observed folder picker changed.'}
    # Observed helper -> hidden C1 WinForms bridge -> export -> main owner chain.
    $bridge=Folder-NativeNode $dialog.owner
    $export=Folder-NativeNode $bridge.owner
    if($dialog.parent -ne $bridge.hwnd -or $bridge.pid -ne $ProcessId -or
       $bridge.class_name -notmatch '^WindowsForms10\.Window\.8\.app\.' -or $bridge.text -cne '' -or
       $export.pid -ne $ProcessId -or $export.owner -ne $MainHandle -or $export.text -cne $exportTitle -or
       -not $export.visible -or ($ExpectedExport -gt 0 -and $export.hwnd -ne $ExpectedExport)) {
        Fail 'WRONG_DIALOG' 'Folder picker is not owned through the observed Capture One export chain.'
    }
    $exportElement=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$export.hwnd)
    if($null -eq $exportElement -or $exportElement.Current.AutomationId -cne 'export' -or
       $exportElement.Current.ControlType -ne [System.Windows.Automation.ControlType]::Window -or
       $exportElement.Current.ProcessId -ne $ProcessId){Fail 'WRONG_DIALOG' 'Folder picker owner is not the qualified export window.'}
    $handles=@([C1WindowNative]::Children([IntPtr]$dialog.hwnd))
    if($handles.Count -gt 256){Fail 'FOLDER_PICKER_INCOMPLETE' 'Native folder hierarchy exceeds the qualified bounded scope.'}
    $children=@($handles | ForEach-Object {Folder-NativeNode $_})
    $edits=@($children | Where-Object {$_.id -eq 1152 -and $_.class_name -ceq 'Edit' -and $_.parent -eq $dialog.hwnd})
    $labels=@($children | Where-Object {$_.id -eq 1090 -and $_.class_name -ceq 'Static' -and $_.parent -eq $dialog.hwnd -and $_.text -ceq $labelText})
    $buttons=@($children | Where-Object {$_.id -eq 1 -and $_.class_name -ceq 'Button' -and $_.parent -eq $dialog.hwnd -and $_.text -ceq $title})
    if($edits.Count -ne 1 -or $labels.Count -ne 1 -or $buttons.Count -ne 1){Fail 'FOLDER_FIELD_UNAVAILABLE' 'The observed FolderBrowser Edit1152, Static1090 and IDOK hierarchy is missing or ambiguous.'}
    $edit=$edits[0];$button=$buttons[0]
    foreach($node in @($edit,$labels[0],$button)) {
        if($node.pid -ne $dialog.pid -or -not $node.visible){Fail 'FOLDER_FIELD_UNAVAILABLE' 'Native folder field hierarchy changed process or visibility.'}
    }
    if(-not $edit.enabled -or -not $button.enabled){Fail 'FOLDER_FIELD_UNAVAILABLE' 'Folder edit or confirmation is disabled.'}
    $helper=$helpers[$dialog.pid]
    $evidence=@{dialog_hwnd=$dialog.hwnd;helper_pid=$dialog.pid;helper_started_at=$helper.CreationDate.ToUniversalTime().ToString('o');
        helper_parent_pid=[int]$helper.ParentProcessId;helper_exe=[string]$helper.ExecutablePath;
        owner_hwnd=$bridge.hwnd;export_hwnd=$export.hwnd;main_hwnd=$MainHandle;
        edit_hwnd=$edit.hwnd;label_hwnd=$labels[0].hwnd;ok_hwnd=$button.hwnd;
        hierarchy='FolderBrowser/#32770/Edit:1152+Static:1090+Button:1'}
    return @{picker=$evidence;dialog=$dialog;owner_bridge=$bridge;export=$export;native_children=$children;truncated=$false}
}
function Folder-ExistingPath([string]$Path) {
    if($Path -notmatch '^[A-Za-z]:[\\/]' -or $Path -match '[\x00-\x1f*?"<>|]' -or
       $Path.Substring(2) -match ':' -or $Path -match '(^|[\\/])\.{1,2}([\\/]|$)' -or
       -not [IO.Directory]::Exists($Path)){Fail 'INVALID_PATH' 'Folder export requires an existing absolute local directory without streams or traversal.'}
    $directory=Get-Item -LiteralPath $Path -Force
    if(-not $directory.PSIsContainer){Fail 'INVALID_PATH' 'The export destination is not a directory.'}
    $cursor=$directory
    while($null -ne $cursor) {
        if(($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){Fail 'INVALID_PATH' 'Export directory and its parents must not be reparse points.'}
        $cursor=$cursor.Parent
    }
    return $directory.FullName
}
function Folder-NavigationState($Inspected) {
    $picker=$Inspected.picker;$matches=New-Object Collections.Generic.List[object]
    foreach($candidate in $Inspected.native_children) {
        if($candidate.class_name -cne 'ToolbarWindow32' -or $candidate.id -ne 1001){continue}
        $cursor=$candidate;$valid=$true
        $classes=@('ToolbarWindow32','Breadcrumb Parent','msctls_progress32','Address Band Root','ReBarWindow32','WorkerW')
        $ids=@(1001,0,0,41477,40965,0)
        for($index=0;$index -lt $classes.Count;$index++) {
            if($cursor.class_name -cne $classes[$index] -or $cursor.id -ne $ids[$index] -or
               $cursor.pid -ne $picker.helper_pid){$valid=$false;break}
            if($index -lt $classes.Count-1){$cursor=Folder-NativeNode $cursor.parent}
        }
        if($valid -and $cursor.parent -eq $picker.dialog_hwnd){$matches.Add($candidate)}
    }
    if($matches.Count -ne 1){Fail 'FOLDER_ADDRESS_UNAVAILABLE' 'Expected the observed native folder breadcrumb hierarchy.'}
    $address=$matches[0];$prefix=-join ([char[]](0x5730,0x5740,0x3a,0x20))
    if(-not $address.text.StartsWith($prefix,[StringComparison]::Ordinal)){Fail 'FOLDER_ADDRESS_UNAVAILABLE' 'Native folder address label is not qualified.'}
    $textBuffer=New-Object Text.StringBuilder 32768;$messageResult=[IntPtr]::Zero
    if([C1WindowNative]::ReadTextTimeout([IntPtr]$picker.edit_hwnd,0x000D,[IntPtr]$textBuffer.Capacity,$textBuffer,2,3000,[ref]$messageResult) -eq [IntPtr]::Zero){Fail 'FOLDER_READBACK_FAILED' 'Native folder edit cannot be read.'}
    return @{folder_path=$address.text.Substring($prefix.Length);address_text=$address.text;
        address_hwnd=$address.hwnd;edit_text=$textBuffer.ToString();hierarchy_verified=$true}
}
function Folder-AssertNavigation($Navigation,[string]$Path) {
    if((Folder-ExistingPath $Navigation.folder_path) -ine $Path){Fail 'FOLDER_NAVIGATION_MISMATCH' 'Picker has not navigated to the expected existing directory.'}
    $basename=[IO.Path]::GetFileName($Path.TrimEnd([char]'\'))
    if($Navigation.edit_text -cne $Path -and
       ([string]::IsNullOrEmpty($basename) -or $Navigation.edit_text -cne $basename)) {
        Fail 'FOLDER_READBACK_FAILED' 'Folder edit must match the exact destination name or full path before confirmation.'
    }
}
function Folder-ConfirmExport($Request,[int]$ProcessId,[long]$MainHandle,$Window) {
    $folderPath=Folder-ExistingPath ([string](Field $Request 'path'))
    $expected=Field $Request 'expected_picker'
    if($null -eq $expected -or [long](Field $expected 'dialog_hwnd' 0) -le 0 -or
       [long](Field $expected 'export_hwnd' 0) -le 0){Fail 'FOLDER_EVIDENCE_REQUIRED' 'A recorded picker identity is required for one confirmation.'}
    $inspected=Folder-PickerInfo $ProcessId $MainHandle $Window.exe ([long]$expected.dialog_hwnd) ([long]$expected.export_hwnd)
    if(-not (Same $expected $inspected.picker)){Fail 'WRONG_DIALOG' 'Recorded folder picker identity changed before confirmation.'}
    $navigation=Folder-NavigationState $inspected
    Folder-AssertNavigation $navigation $folderPath
    Check-Guard (Field $Request 'guard')
    $confirmed=Folder-PickerInfo $ProcessId $MainHandle $Window.exe ([long]$expected.dialog_hwnd) ([long]$expected.export_hwnd)
    if(-not (Same $expected $confirmed.picker)){Fail 'WRONG_DIALOG' 'Folder picker owner changed during guard verification.'}
    $navigation=Folder-NavigationState $confirmed
    Folder-AssertNavigation $navigation $folderPath
    $messageResult=[IntPtr]::Zero;$script:MayHaveMutated=$true
    # WM_COMMAND/BN_CLICKED: LOWORD=IDOK, HIWORD=0, LPARAM=button HWND.
    # This completes already verified navigation. It never sets text or retries.
    if([C1WindowNative]::SendCommandTimeout([IntPtr]$expected.dialog_hwnd,0x0111,[IntPtr]1,[IntPtr]$confirmed.picker.ok_hwnd,2,5000,[ref]$messageResult) -eq [IntPtr]::Zero){Fail 'FOLDER_SUBMIT_UNCERTAIN' 'Folder button confirmation timed out; do not send it again automatically.'}
    return @{window=$Window;accepted=$true;path=$folderPath;picker=$confirmed.picker;navigation=$navigation;
        navigation_completed=$true;confirmation_submitted=$true;destination_verified=$false;input_sent=$false;
        note='Verified picker directory and button notification only. Capture One export sample-path readback is still required.'}
}
function Folder-SelectCurrentExport($Request,[int]$ProcessId,[long]$MainHandle,$Window) {
    $folderPath=Folder-ExistingPath ([string](Field $Request 'path'))
    $expected=Field $Request 'expected_picker'
    $expectedEdit=Field $Request 'expected_edit_text'
    if($null -eq $expected -or $expectedEdit -isnot [string] -or
       [long](Field $expected 'dialog_hwnd' 0) -le 0 -or [long](Field $expected 'export_hwnd' 0) -le 0){Fail 'FOLDER_EVIDENCE_REQUIRED' 'A recorded picker and exact edit text are required.'}
    $first=Folder-PickerInfo $ProcessId $MainHandle $Window.exe ([long]$expected.dialog_hwnd) ([long]$expected.export_hwnd)
    if(-not (Same $expected $first.picker)){Fail 'WRONG_DIALOG' 'Recorded picker identity changed before current-directory selection.'}
    $navigation=Folder-NavigationState $first
    Folder-AssertNavigation $navigation $folderPath
    if($navigation.edit_text -cne $expectedEdit){Fail 'FOLDER_EDIT_CHANGED' 'Folder edit differs from the persisted stage observation.'}
    Check-Guard (Field $Request 'guard')
    $first=Folder-PickerInfo $ProcessId $MainHandle $Window.exe ([long]$expected.dialog_hwnd) ([long]$expected.export_hwnd)
    if(-not (Same $expected $first.picker)){Fail 'WRONG_DIALOG' 'Recorded picker identity changed before clearing its edit.'}
    $navigation=Folder-NavigationState $first
    Folder-AssertNavigation $navigation $folderPath
    if($navigation.edit_text -cne $expectedEdit){Fail 'FOLDER_EDIT_CHANGED' 'Folder edit changed during target verification.'}
    $messageResult=[IntPtr]::Zero;$script:MayHaveMutated=$true
    # A full path in this field requests navigation again. Once the breadcrumb
    # is verified, an empty field selects the current directory instead.
    if([C1WindowNative]::SendMessageTimeout([IntPtr]$expected.edit_hwnd,0x000C,[IntPtr]::Zero,'',2,3000,[ref]$messageResult) -eq [IntPtr]::Zero -or $messageResult.ToInt64() -eq 0){Fail 'FOLDER_CLEAR_UNCERTAIN' 'Folder edit clear was not acknowledged; no IDOK sent.'}
    $cleared=Folder-PickerInfo $ProcessId $MainHandle $Window.exe ([long]$expected.dialog_hwnd) ([long]$expected.export_hwnd)
    if(-not (Same $expected $cleared.picker)){Fail 'WRONG_DIALOG' 'Picker changed after its edit was cleared; no IDOK sent.'}
    $state=Folder-NavigationState $cleared
    if((Folder-ExistingPath $state.folder_path) -ine $folderPath -or $state.edit_text -cne ''){Fail 'FOLDER_CLEAR_UNVERIFIED' 'Current directory or cleared edit does not match; no IDOK sent.'}
    Check-Guard (Field $Request 'guard')
    $cleared=Folder-PickerInfo $ProcessId $MainHandle $Window.exe ([long]$expected.dialog_hwnd) ([long]$expected.export_hwnd)
    if(-not (Same $expected $cleared.picker)){Fail 'WRONG_DIALOG' 'Picker changed at current-directory dispatch.'}
    $state=Folder-NavigationState $cleared
    if((Folder-ExistingPath $state.folder_path) -ine $folderPath -or $state.edit_text -cne ''){Fail 'FOLDER_CLEAR_UNVERIFIED' 'Current directory or edit changed at dispatch.'}
    $messageResult=[IntPtr]::Zero
    if([C1WindowNative]::SendCommandTimeout([IntPtr]$expected.dialog_hwnd,0x0111,[IntPtr]1,[IntPtr]$cleared.picker.ok_hwnd,2,5000,[ref]$messageResult) -eq [IntPtr]::Zero){Fail 'FOLDER_SUBMIT_UNCERTAIN' 'Current-directory selection timed out; do not resubmit.'}
    return @{window=$Window;accepted=$true;path=$folderPath;picker=$cleared.picker;navigation_before=$navigation;
        edit_cleared_verified=$true;current_directory_verified=$true;selection_submitted=$true;
        destination_verified=$false;input_sent=$false;note='Current directory submitted once; Capture One sample-path readback is still required.'}
}
function Executable-Metadata([string]$ExePath) {
    # Process.FileVersionInfo is not a reliable Process property. Read the
    # executable resource directly; ProductVersion may contain a product name.
    $metadata = [Diagnostics.FileVersionInfo]::GetVersionInfo($ExePath)
    $fileVersion = ([string]$metadata.FileVersion).Trim()
    $version = ''
    $versionSource = ''
    if ($fileVersion -match '^\d+(?:\.\d+){1,3}$') {
        $version = $fileVersion
        $versionSource = 'FileVersion'
    } else {
        $parts = @($metadata.FileMajorPart, $metadata.FileMinorPart, $metadata.FileBuildPart, $metadata.FilePrivatePart)
        if (@($parts | Where-Object { $_ -gt 0 }).Count -gt 0) {
            $version = $parts -join '.'
            $versionSource = 'FileVersionParts'
        }
    }
    if ([string]::IsNullOrWhiteSpace($version)) {
        throw 'The executable has no numeric file version; application compatibility cannot be checked.'
    }
    return @{version=$version; version_source=$versionSource; file_version=$fileVersion;
        product_version=[string]$metadata.ProductVersion; product_name=[string]$metadata.ProductName}
}
function Window-Info([int]$ProcessId, [long]$WindowHandle) {
    $ownerId = 0
    [void][C1WindowNative]::GetWindowThreadProcessId([IntPtr]$WindowHandle, [ref]$ownerId)
    if ($ownerId -ne $ProcessId -or -not [C1WindowNative]::IsWindow([IntPtr]$WindowHandle)) {
        Fail 'WRONG_WINDOW' 'The window no longer belongs to the selected process.'
    }
    $process = Get-Process -Id $ProcessId -ErrorAction Stop
    $exePath = [string]$process.Path
    $exeName = [IO.Path]::GetFileName($exePath)
    if ($exeName -notin @('CaptureOne.exe','Capture One.exe')) { Fail 'WRONG_PROCESS' 'Only Capture One process windows are supported.' }
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$WindowHandle)
    if ($null -eq $root -or $root.Current.ProcessId -ne $ProcessId) { Fail 'WRONG_PROCESS' 'UI Automation returned a different process.' }
    $metadata = Executable-Metadata $exePath
    $script:RootElement = $root
    $info = [ordered]@{pid=$ProcessId; hwnd=$WindowHandle; title=[string]$root.Current.Name;
        exe=$exePath; version=$metadata.version; version_source=$metadata.version_source;
        file_version=$metadata.file_version; product_version=$metadata.product_version; product_name=$metadata.product_name;
        visible=[C1WindowNative]::IsWindowVisible([IntPtr]$WindowHandle);
        minimized=[C1WindowNative]::IsIconic([IntPtr]$WindowHandle)}
    $script:WindowInfo = $info
    return $info
}
function Check-Guard($Guard, [switch]$Capture) {
    if ($null -eq $Guard) { Fail 'GUARD_REQUIRED' 'A target guard is required.' }
    $title = Field $Guard 'window_title'
    if (-not ($title -is [string]) -or $title.Length -eq 0) { Fail 'GUARD_REQUIRED' 'Guard must contain the observed window title.' }
    if ([string]$script:RootElement.Current.Name -cne $title) { Fail 'STALE_TARGET' 'Capture One changed its active document or window title.' }
    $bindings = Field $Guard 'bindings'; $expected = Field $Guard 'expected'
    $keys = @(Names $bindings)
    if ($null -eq $bindings -or $null -eq $expected -or $keys.Count -eq 0) { Fail 'GUARD_REQUIRED' 'Guard must contain identity bindings and expected observations.' }
    if (@(Names $expected).Count -ne $keys.Count) { Fail 'INVALID_GUARD' 'Expected observations must match the guard bindings.' }
    $captured = [ordered]@{}
    foreach ($key in $keys) {
        if (@(Names $expected) -notcontains $key) { Fail 'INVALID_GUARD' 'Every guard binding needs an expected observation.' }
        $desired = Field $expected $key
        $fields = @(Names $desired)
        $observed = Observe (Resolve (Field $bindings $key)) $fields
        if ($fields.Count -eq 0) { Fail 'INVALID_GUARD' 'Empty observations cannot protect a target.' }
        foreach ($fieldName in $fields) {
            if ($fieldName -notin @('name','value','toggle','selection','selected','expand_state','image_layer_selected')) { Fail 'INVALID_GUARD' "Unsupported observed field: $fieldName" }
            if (-not $observed.Contains($fieldName) -or -not (Same $observed[$fieldName] (Field $desired $fieldName))) {
                Fail 'STALE_TARGET' "Target observation changed: $key.$fieldName"
            }
        }
        if ($Capture) { $captured[$key] = $observed }
    }
    $layerRuntime=Field $Guard 'selected_layer_runtime_id'
    if($null -ne $layerRuntime){
        if($null -eq (Get-Command Find-LayerByRuntime -CommandType Function -ErrorAction SilentlyContinue)){. (Join-Path $PSScriptRoot 'background.ps1')}
        $layer=Find-LayerByRuntime $layerRuntime;$selected=Pattern $layer 'SelectionItem'
        if($null -eq $selected -or -not $selected.Current.IsSelected){Fail 'STALE_TARGET' 'The exact observed adjustment layer is no longer selected.'}
    }
    if ($Capture) { return $captured }
}

function Focus-NavigationTarget($WindowElement, $Browser, [long]$WindowHandle) {
    Assert-ForegroundAllowed 'focus'
    # The WPF browser can take control focus without activating its host window.
    # Use the actual UIA window first only when Capture One is not foreground.
    $attempt = @{root_focus_attempted=$false; root_focus_error=$null}
    if ([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $WindowHandle) {
        $attempt.root_focus_attempted = $true
        try { $WindowElement.SetFocus() } catch { $attempt.root_focus_error = $_.Exception.Message }
    }
    try { $Browser.SetFocus() } catch { Fail 'FOCUS_REFUSED' 'UI Automation could not focus the target control; no arrow key was sent.' $_.Exception.Message }
    return $attempt
}

function Focus-In-Control($Control, [int]$ProcessId) {
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($null -eq $focused -or $focused.Current.ProcessId -ne $ProcessId) { return $false }
    $cursor = $focused
    for ($depth=0; $depth -lt 128 -and $null -ne $cursor; $depth++) {
        if ([System.Windows.Automation.Automation]::Compare($cursor, $Control)) { return $true }
        if ([System.Windows.Automation.Automation]::Compare($cursor, $script:RootElement)) { return $false }
        $cursor = [System.Windows.Automation.TreeWalker]::RawViewWalker.GetParent($cursor)
    }
    return $false
}

function Focus-In-Browser($Browser, [int]$ProcessId) {
    return (Focus-In-Control $Browser $ProcessId)
}

function Require-No-Held-Input {
    # High bits describe currently pressed input, not Caps/NumLock toggles.
    for ($key=1; $key -le 254; $key++) {
        if (([C1WindowNative]::GetAsyncKeyState($key) -band 0x8000) -ne 0) { Fail 'INPUT_HELD' 'A keyboard key or mouse button is held; no arrow key was sent.' }
    }
}

function Ensure-C1Foreground($Guard) {
    Assert-ForegroundAllowed 'activate';Check-Guard $Guard
    $targetWindow=[long]$script:WindowInfo.hwnd;$beforeFocus=[C1WindowNative]::GetForegroundWindow().ToInt64()
    if($beforeFocus -ne $targetWindow){
        Require-No-Held-Input;$script:MayHaveMutated=$true
        [void][C1WindowNative]::SetForegroundWindow([IntPtr]$targetWindow)
        if([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow){try{$script:RootElement.SetFocus()}catch{}}
        $focusWait=[Diagnostics.Stopwatch]::StartNew()
        while([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow -and $focusWait.ElapsedMilliseconds -lt 300){Start-Sleep -Milliseconds 30}
    }
    $afterFocus=[C1WindowNative]::GetForegroundWindow().ToInt64()
    if($afterFocus -ne $targetWindow){Fail 'FOREGROUND_REFUSED' 'Windows did not grant C1 foreground; no numeric input was sent.'}
    Check-Guard $Guard
    return @{window=$script:WindowInfo;before_hwnd=$beforeFocus;after_hwnd=$afterFocus;activation_needed=($beforeFocus -ne $afterFocus);input_sent=$false}
}

function Commit-C1NumericEdit($Element,$Guard) {
    Assert-ForegroundAllowed 'numeric_commit'
    if($Element.Current.AutomationId -cne 'ToolSliderDecimalUpDown' -or $Element.Current.ClassName -cne 'TextBox' -or $Element.Current.ControlType -ne [System.Windows.Automation.ControlType]::Edit){Fail 'INVALID_COMMIT_TARGET' 'Numeric commit is restricted to the C1 slider edit.'}
    [void](Ensure-C1Foreground $Guard);Require-No-Held-Input
    $hwnd=[IntPtr]$script:WindowInfo.hwnd
    $Element.SetFocus();$focused=[System.Windows.Automation.AutomationElement]::FocusedElement
    if([C1WindowNative]::GetForegroundWindow() -ne $hwnd -or $null -eq $focused -or -not (Same @($focused.GetRuntimeId()) @($Element.GetRuntimeId()))){Fail 'COMMIT_FOCUS_REFUSED' 'The exact numeric editor is not focused.'}
    Require-No-Held-Input;Check-Guard $Guard
    $inputs=[C1WindowNative]::CreateNavigationInputs('enter');$script:MayHaveMutated=$true
    $sent=[C1WindowNative]::SendInput([uint32]$inputs.Length,$inputs,[Runtime.InteropServices.Marshal]::SizeOf([type][C1WindowNative+INPUT]))
    if($sent -ne $inputs.Length){Fail 'INPUT_INCOMPLETE' 'Numeric Enter commit outcome is unknown; do not resend.'}
    Start-Sleep -Milliseconds 80;Check-Guard $Guard
    return @{commit_requested=$true;commit_input_sent=$true;input_count=$sent;model_commit_verified=$false}
}

try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -AssemblyName WindowsBase
    if (-not ('C1WindowNative' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class C1WindowNative {
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hWnd, uint command);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern IntPtr SendMessageTimeout(IntPtr hwnd,uint msg,IntPtr wp,string lp,uint flags,uint timeout,out IntPtr result);
    [DllImport("user32.dll", EntryPoint="SendMessageTimeoutW", SetLastError=true)] public static extern IntPtr SendCommandTimeout(IntPtr hwnd,uint msg,IntPtr wp,IntPtr lp,uint flags,uint timeout,out IntPtr result);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hwnd,System.Text.StringBuilder text,int count);
    [DllImport("user32.dll", EntryPoint="SendMessageTimeoutW", CharSet=CharSet.Unicode, SetLastError=true)] public static extern IntPtr ReadTextTimeout(IntPtr hwnd,uint msg,IntPtr wp,System.Text.StringBuilder lp,uint flags,uint timeout,out IntPtr result);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT point);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll", SetLastError=true)] public static extern uint SendInput(uint count, INPUT[] inputs, int size);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
    [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT {
        public int dx; public int dy; public uint mouseData; public uint flags; public uint time; public UIntPtr extraInfo;
    }
    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
        public ushort virtualKey; public ushort scanCode; public uint flags; public uint time; public UIntPtr extraInfo;
    }
    [StructLayout(LayoutKind.Explicit)] public struct INPUTUNION {
        [FieldOffset(0)] public MOUSEINPUT mouse;
        [FieldOffset(0)] public KEYBDINPUT keyboard;
    }
    [StructLayout(LayoutKind.Sequential)] public struct INPUT {
        public uint type; public INPUTUNION data;
        public MOUSEINPUT mouse { get { return data.mouse; } }
    }
    public static INPUT[] CreateLeftClickInputs(int x, int y, int left, int top, int width, int height) {
        if (width <= 1 || height <= 1 || x < left || y < top || (long)x >= (long)left + width || (long)y >= (long)top + height)
            throw new ArgumentOutOfRangeException("point", "Click point must be inside the virtual desktop.");
        var inputs = new INPUT[3];
        inputs[0].data.mouse.dx = (int)Math.Round(((double)x - left) * 65535.0 / (width - 1));
        inputs[0].data.mouse.dy = (int)Math.Round(((double)y - top) * 65535.0 / (height - 1));
        inputs[0].data.mouse.flags = 0x0001 | 0x8000 | 0x4000; // MOVE | ABSOLUTE | VIRTUALDESK
        inputs[1].data.mouse.flags = 0x0002; // LEFTDOWN
        inputs[2].data.mouse.flags = 0x0004; // LEFTUP
        return inputs;
    }
    public static INPUT[] CreateNavigationInputs(string direction) {
        ushort key;
        switch (direction) {
            case "left": key = 0x25; break;
            case "up": key = 0x26; break;
            case "right": key = 0x27; break;
            case "down": key = 0x28; break;
            case "escape": key = 0x1B; break;
            case "enter": key = 0x0D; break;
            default: throw new ArgumentException("Only left, right, up, or down are accepted.", "direction");
        }
        var inputs = new INPUT[2];
        inputs[0].type = 1; inputs[1].type = 1; // INPUT_KEYBOARD
        inputs[0].data.keyboard.virtualKey = key;
        inputs[1].data.keyboard.virtualKey = key;
        inputs[0].data.keyboard.flags = (direction == "escape" || direction == "enter") ? 0u : 0x0001u; // EXTENDEDKEY
        inputs[1].data.keyboard.flags = ((direction == "escape" || direction == "enter") ? 0u : 0x0001u) | 0x0002u; // KEYUP
        return inputs;
    }
    [DllImport("user32.dll")] public static extern IntPtr GetDlgItem(IntPtr hwnd, int id);
    [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr hwnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hwnd, System.Text.StringBuilder buffer, int size);
    [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr hwnd);
    public delegate bool EnumChildCallback(IntPtr hwnd, IntPtr param);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr hwnd, EnumChildCallback callback, IntPtr param);
    public static long[] Children(IntPtr root) {
        var found = new System.Collections.Generic.List<long>();
        EnumChildWindows(root,(h,p)=>{found.Add(h.ToInt64());return true;},IntPtr.Zero);
        return found.ToArray();
    }
    public delegate bool EnumWindowsCallback(IntPtr hwnd, IntPtr param);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsCallback callback, IntPtr param);
    public static long[] Windows() {
        var found = new System.Collections.Generic.List<long>();
        EnumWindows((h,p) => { found.Add(h.ToInt64()); return true; }, IntPtr.Zero);
        return found.ToArray();
    }
}
'@
    }
    [void][C1WindowNative]::SetProcessDPIAware()
    $raw = if ($PSBoundParameters.ContainsKey('RequestJson')) { $RequestJson } else { [Console]::In.ReadToEnd() }
    if ($raw.Length -gt 1048576) { Fail 'INVALID_REQUEST' 'Request is too large.' }
    $request = $raw | ConvertFrom-Json
    $operation = [string](Field $request 'op')
    if ($operation -notin @('discover_controls','set_many','dialogs','inspect_folder_picker','choose_export_folder','confirm_export_folder','select_current_export_folder','dismiss_info','import_style_file','save_style_file','cancel_style_import','activate','windows','status','inspect','read','snapshot','set','invoke','click','navigate','nudge_adjustment','screenshot','layers','layer_action','guard','prepare_layers','curve_state','curve_select','curve_add_point','curve_move_point','reveal_tool','mixer_state','mixer_select','mixer_set')) { Fail 'INVALID_REQUEST' 'Unknown operation.' }
    Check-ExecutionPolicy $request $operation
    if ($operation -in @('layers','layer_action','prepare_layers','set','set_many')) { . (Join-Path $PSScriptRoot 'background.ps1') }
    if ($operation -in @('curve_state','curve_select','curve_add_point','curve_move_point')) { . (Join-Path $PSScriptRoot 'curve.ps1') }
    if ($operation -in @('mixer_state','mixer_select','mixer_set')) { . (Join-Path $PSScriptRoot 'mixer.ps1') }
    $result = $null
    if ($operation -eq 'windows') {
        $windows = New-Object System.Collections.Generic.List[object]
        $captureProcesses=@{}
        Get-Process -Name 'CaptureOne','Capture One' -ErrorAction SilentlyContinue | ForEach-Object {$captureProcesses[$_.Id]=$true}
        foreach ($windowHandle in [C1WindowNative]::Windows()) {
            $ownerId = 0
            [void][C1WindowNative]::GetWindowThreadProcessId([IntPtr]$windowHandle, [ref]$ownerId)
            if ($ownerId -le 0 -or -not $captureProcesses.ContainsKey($ownerId)) { continue }
            try {
                $process = Get-Process -Id $ownerId -ErrorAction Stop
                if ([IO.Path]::GetFileName([string]$process.Path) -notin @('CaptureOne.exe','Capture One.exe')) { continue }
                if (-not [C1WindowNative]::IsWindowVisible([IntPtr]$windowHandle)) { continue }
                $candidate = Window-Info $ownerId $windowHandle
                # Untitled startup/splash and helper windows are not useful
                # connection targets. A titled window still does not prove
                # that a session has loaded or that editing is available.
                if ([string]::IsNullOrWhiteSpace([string]$candidate.title) -and -not (Field $request 'include_untitled' $false)) { continue }
                $windows.Add($candidate)
            } catch { continue }
        }
        $result = @{windows=@($windows.ToArray()); platform='windows'; backend='UIAutomation'; app_verified=($windows.Count -gt 0);
            verification_scope='process_name_and_visible_titled_uia_window'; editing_ready=$false;
            note='Discovery identifies a named Capture One process window, not session readiness, binary signature, or working editing controls.'}
    } else {
        $targetProcess = [int](Field $request 'pid' 0)
        $targetWindow = [long](Field $request 'hwnd' 0)
        if ($targetProcess -le 0 -or $targetWindow -le 0) { Fail 'INVALID_REQUEST' 'Positive pid and hwnd are required.' }
        $window = Window-Info $targetProcess $targetWindow
        switch ($operation) {
            'discover_controls' {
                $limit=[int](Field $request 'max_nodes' 200)
                if($limit -lt 1 -or $limit -gt 500){Fail 'INVALID_REQUEST' 'Discovery limit must be 1-500.'}
                $conditions=New-Object System.Collections.Generic.List[System.Windows.Automation.Condition]
                foreach($kind in @([System.Windows.Automation.ControlType]::Edit,[System.Windows.Automation.ControlType]::CheckBox,[System.Windows.Automation.ControlType]::ComboBox,[System.Windows.Automation.ControlType]::Slider)) {
                    $conditions.Add([System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,$kind))
                }
                $condition=[System.Windows.Automation.OrCondition]::new([System.Windows.Automation.Condition[]]$conditions.ToArray())
                $elements=$script:RootElement.FindAll([System.Windows.Automation.TreeScope]::Descendants,$condition)
                $nodes=New-Object System.Collections.Generic.List[object];$errors=New-Object System.Collections.Generic.List[string]
                for($i=0;$i -lt [math]::Min($elements.Count,$limit);$i++){
                    try {
                        $element=$elements[$i];$node=Observe $element @('value','toggle')
                        $node.class_name=[string]$element.Current.ClassName
                        $node.enabled=[bool]$element.Current.IsEnabled;$node.offscreen=[bool]$element.Current.IsOffscreen
                        $node.patterns=@($element.GetSupportedPatterns() | ForEach-Object {$_.ProgrammaticName -replace 'PatternIdentifiers\.Pattern$','' -replace '^.*\.',''})
                        $node.runtime_id=@($element.GetRuntimeId());$node.selector=Selector-For $element
                        $nodes.Add($node)
                    }catch{$errors.Add($_.Exception.Message)}
                }
                $result=@{window=$window;nodes=@($nodes.ToArray());truncated=($elements.Count -gt $limit);read_errors=@($errors.ToArray());total_candidates=$elements.Count}
            }
            'inspect_folder_picker' {
                $inspected=Folder-PickerInfo $targetProcess $targetWindow $window.exe ([long](Field $request 'dialog_hwnd' 0)) ([long](Field $request 'export_hwnd' 0))
                $inspected.navigation=Folder-NavigationState $inspected
                $inspected.window=$window
                $result=$inspected
            }
            'confirm_export_folder' {
                $result=Folder-ConfirmExport $request $targetProcess $targetWindow $window
            }
            'select_current_export_folder' {
                $result=Folder-SelectCurrentExport $request $targetProcess $targetWindow $window
            }
            'choose_export_folder' {
                $folderPath=Folder-ExistingPath ([string](Field $request 'path'))
                $expected=Field $request 'expected_picker'
                if($null -eq $expected -or [long](Field $expected 'dialog_hwnd' 0) -le 0 -or
                   [long](Field $expected 'export_hwnd' 0) -le 0){Fail 'FOLDER_EVIDENCE_REQUIRED' 'Read the qualified folder picker before choosing an export directory.'}
                $inspected=Folder-PickerInfo $targetProcess $targetWindow $window.exe ([long]$expected.dialog_hwnd) ([long]$expected.export_hwnd)
                if(-not (Same $expected $inspected.picker)){Fail 'WRONG_DIALOG' 'Folder picker process, ownership or native filename hierarchy changed.'}
                $dialogHandle=[IntPtr]$inspected.picker.dialog_hwnd;$editHandle=[IntPtr]$inspected.picker.edit_hwnd
                Check-Guard (Field $request 'guard')
                $messageResult=[IntPtr]::Zero;$textBuffer=New-Object Text.StringBuilder 32768
                $script:MayHaveMutated=$true
                if([C1WindowNative]::SendMessageTimeout($editHandle,0x000C,[IntPtr]::Zero,$folderPath,2,3000,[ref]$messageResult) -eq [IntPtr]::Zero){Fail 'FOLDER_SET_UNCERTAIN' 'Folder field message timed out; inspect before any retry.'}
                if([C1WindowNative]::ReadTextTimeout($editHandle,0x000D,[IntPtr]$textBuffer.Capacity,$textBuffer,2,3000,[ref]$messageResult) -eq [IntPtr]::Zero -or
                   $textBuffer.ToString() -cne $folderPath){Fail 'FOLDER_READBACK_FAILED' 'Folder input does not exactly match the requested path; no IDOK sent.'}
                Check-Guard (Field $request 'guard')
                $confirmed=Folder-PickerInfo $targetProcess $targetWindow $window.exe ([long]$expected.dialog_hwnd) ([long]$expected.export_hwnd)
                if(-not (Same $expected $confirmed.picker) -or (Folder-ExistingPath $folderPath) -cne $folderPath){Fail 'WRONG_DIALOG' 'Folder picker or destination changed before confirmation.'}
                $textBuffer.Clear() | Out-Null
                if([C1WindowNative]::ReadTextTimeout($editHandle,0x000D,[IntPtr]$textBuffer.Capacity,$textBuffer,2,3000,[ref]$messageResult) -eq [IntPtr]::Zero -or
                   $textBuffer.ToString() -cne $folderPath){Fail 'FOLDER_READBACK_FAILED' 'Folder input changed before confirmation; no IDOK sent.'}
                if([C1WindowNative]::SendCommandTimeout($dialogHandle,0x0111,[IntPtr]1,[IntPtr]::Zero,2,5000,[ref]$messageResult) -eq [IntPtr]::Zero){Fail 'FOLDER_SUBMIT_UNCERTAIN' 'Folder IDOK timed out; do not resubmit automatically.'}
                $result=@{window=$window;accepted=$true;path=$folderPath;picker=$confirmed.picker;path_readback_verified=$true;
                    selection_submitted=$true;destination_verified=$false;input_sent=$false;
                    note='Directory text and native ownership verified. Read the actual Capture One export sample path before exporting.'}
            }
            'dialogs' {
                $dialogs=New-Object System.Collections.Generic.List[object]
                foreach($dialogElement in (Owned-DialogElements $targetProcess $targetWindow $window.exe)) {
                    $dialogHandle=$dialogElement.Current.NativeWindowHandle
                    $dialogOwner=$dialogElement.Current.ProcessId
                    if($null -eq $dialogElement -or $dialogElement.Current.ClassName -eq 'Popup'){continue}
                    # Native folder pickers can have an empty title. Report
                    # them by their verified process/owner and class instead.
                    $texts=New-Object System.Collections.Generic.List[string]
                    $buttons=New-Object System.Collections.Generic.List[object]
                    $fields=New-Object System.Collections.Generic.List[object]
                    $children=$dialogElement.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
                    $count=[Math]::Min($children.Count,300)
                    for($index=0;$index -lt $count;$index++) {
                        $child=$children[$index]
                        if($child.Current.ControlType -eq [System.Windows.Automation.ControlType]::Text -and $child.Current.Name){$texts.Add([string]$child.Current.Name)}
                        if($child.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button){$buttons.Add((Base-Selector $child))}
                        if($child.Current.ControlType -eq [System.Windows.Automation.ControlType]::Edit){$fields.Add((Node $child))}
                    }
                    $dialogs.Add(@{hwnd=$dialogHandle;pid=$dialogOwner;title=[string]$dialogElement.Current.Name;automation_id=[string]$dialogElement.Current.AutomationId;class_name=[string]$dialogElement.Current.ClassName;texts=@($texts.ToArray() | Select-Object -Unique);buttons=@($buttons.ToArray());fields=@($fields.ToArray());truncated=($children.Count -gt 300)})
                }
                $result=@{window=$window;dialogs=@($dialogs.ToArray())}
            }
            'dismiss_info' {
                $dialogHandle=[IntPtr][long](Field $request 'dialog_hwnd' 0)
                $dialogOwner=0;[void][C1WindowNative]::GetWindowThreadProcessId($dialogHandle,[ref]$dialogOwner)
                if($dialogOwner -ne $targetProcess -or -not [C1WindowNative]::IsWindowVisible($dialogHandle)){Fail 'WRONG_DIALOG' 'Success dialog is no longer owned by this Capture One process.'}
                $dialogElement=[System.Windows.Automation.AutomationElement]::FromHandle($dialogHandle)
                if($dialogElement.Current.AutomationId -cne 'messageBox' -or $dialogElement.Current.Name -cne 'Capture One'){Fail 'UNKNOWN_DIALOG' 'Only the observed Capture One message box is supported.'}
                $textCondition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'MainTextBlock')
                $message=$dialogElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$textCondition)
                $successText=-join ([char[]](0x60a8,0x7684,0x6587,0x4ef6,0x5df2,0x5bfc,0x5165))
                if($null -eq $message -or $message.Current.Name -cne $successText){Fail 'UNKNOWN_DIALOG' 'This message is not the qualified import-success notification.'}
                $detailCondition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'DetailedTextBlock')
                $detailElement=$dialogElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$detailCondition)
                $expectedDetail=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('5oKo5Y+v5Lul5Zyo5qC35byP5LiO6aKE6K6+5bel5YW35Lit5om+5Yiw5qC35byP77yM5Zyo5qC35byP55S756yU5bel5YW35Lit5om+5Yiw5qC35byP55S756yU'))
                if($null -eq $detailElement -or $detailElement.Current.Name -cne $expectedDetail){Fail 'UNKNOWN_DIALOG' 'Import-success detail changed; no button invoked.'}
                $buttonCondition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'DefaultButton')
                $button=$dialogElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$buttonCondition)
                if($null -eq $button -or $button.Current.Name -cne ([string][char]0x597d)){Fail 'UNKNOWN_DIALOG' 'Expected success confirmation button is missing.'}
                $invoke=Pattern $button 'Invoke'
                if($null -eq $invoke){Fail 'PATTERN_UNAVAILABLE' 'Success button has no native Invoke pattern.'}
                Check-Guard (Field $request 'guard')
                $script:MayHaveMutated=$true;$invoke.Invoke()
                $result=@{accepted=$true;dialog_hwnd=$dialogHandle.ToInt64();kind='style_import_success';input_sent=$false;window=$window}
            }
            'import_style_file' {
                $stylePath=[string](Field $request 'path')
                $styleDialogTitle=-join ([char[]](0x5bfc,0x5165,0x9884,0x8bbe,0x4e0e,0x6837,0x5f0f))
                if(-not [IO.Path]::IsPathRooted($stylePath) -or [IO.Path]::GetExtension($stylePath) -ine '.costyle' -or -not [IO.File]::Exists($stylePath)){Fail 'INVALID_PATH' 'An existing absolute .costyle file is required.'}
                $dialog=Style-ImportDialog $targetProcess $targetWindow $window.exe 5000
                # The helper's classic filename control is not always exposed
                # in UIA. Use the observed Win32 common-dialog ID/class chain.
                $dialogHandle=[IntPtr]$dialog.Current.NativeWindowHandle
                $editHandle=Style-FilenameHandle $dialogHandle
                $dialogOwner=0;$editOwner=0
                [void][C1WindowNative]::GetWindowThreadProcessId($dialogHandle,[ref]$dialogOwner)
                [void][C1WindowNative]::GetWindowThreadProcessId($editHandle,[ref]$editOwner)
                $ownedDialog=($dialogOwner -eq $targetProcess)
                if(-not $ownedDialog -and $dialogOwner -gt 0) {
                    $helper=Get-CimInstance Win32_Process -Filter ("ProcessId="+$dialogOwner)
                    $expectedHelper=Join-Path ([IO.Path]::GetDirectoryName($window.exe)) 'P1.ShowDialogBoxes.exe'
                    $ownedDialog=($helper.ParentProcessId -eq $targetProcess -and $helper.ExecutablePath -ieq $expectedHelper)
                }
                if(-not [C1WindowNative]::IsChild($dialogHandle,$editHandle) -or -not $ownedDialog -or $editOwner -ne $dialogOwner){Fail 'WRONG_DIALOG' 'File dialog is not Capture One or its verified child dialog helper.'}
                Check-Guard (Field $request 'guard')
                $script:MayHaveMutated=$true
                $messageResult=[IntPtr]::Zero
                if([C1WindowNative]::SendMessageTimeout($editHandle,0x000C,[IntPtr]::Zero,$stylePath,2,3000,[ref]$messageResult) -eq [IntPtr]::Zero){Fail 'DIALOG_SET_FAILED' 'File field did not accept the style path.'}
                $textBuffer=New-Object System.Text.StringBuilder 32768
                [void][C1WindowNative]::ReadTextTimeout($editHandle,0x000D,[IntPtr]$textBuffer.Capacity,$textBuffer,2,3000,[ref]$messageResult)
                if($textBuffer.ToString() -cne $stylePath){Fail 'DIALOG_READBACK_FAILED' 'File field does not match the requested style path.'}
                Check-Guard (Field $request 'guard')
                # IDOK is the standard Open command. No mouse/keyboard input.
                if([C1WindowNative]::SendCommandTimeout($dialogHandle,0x0111,[IntPtr]1,[IntPtr]::Zero,2,5000,[ref]$messageResult) -eq [IntPtr]::Zero){Fail 'DIALOG_SUBMIT_UNCERTAIN' 'Open command timed out; inspect before retrying.'}
                $result=@{accepted=$true;path=$stylePath;path_readback_verified=$true;input_sent=$false;window=$window;applied=$false}
            }
            'save_style_file' {
                $stylePath=[string](Field $request 'path')
                if($stylePath -match '[\x00-\x1f*?"<>|]'){Fail 'INVALID_PATH' 'Style export requires a plain filename.'}
                try {$pathRoot=[IO.Path]::GetPathRoot($stylePath)} catch {Fail 'INVALID_PATH' 'Style export path is invalid.'}
                if($stylePath.Substring($pathRoot.Length) -match '[\x00-\x1f:*?"<>|]'){Fail 'INVALID_PATH' 'Style export requires a plain file path without streams or wildcards.'}
                $styleDialogTitle=-join ([char[]](0x4fdd,0x5b58,0x6837,0x5f0f))
                if(-not [IO.Path]::IsPathRooted($stylePath) -or [IO.Path]::GetExtension($stylePath) -ine '.costyle' -or [IO.File]::Exists($stylePath) -or [IO.Directory]::Exists($stylePath) -or -not [IO.Directory]::Exists([IO.Path]::GetDirectoryName($stylePath))){Fail 'INVALID_PATH' 'A new absolute .costyle path in an existing directory is required.'}
                $dialog=Style-SaveDialog $targetProcess $targetWindow $window.exe 5000
                # The helper's classic filename control is not always exposed
                # in UIA. Use the observed Win32 common-dialog ID/class chain.
                $dialogHandle=[IntPtr]$dialog.Current.NativeWindowHandle
                $editHandle=Style-SaveFilenameHandle $dialogHandle
                $dialogOwner=0;$editOwner=0
                [void][C1WindowNative]::GetWindowThreadProcessId($dialogHandle,[ref]$dialogOwner)
                [void][C1WindowNative]::GetWindowThreadProcessId($editHandle,[ref]$editOwner)
                $ownedDialog=($dialogOwner -eq $targetProcess)
                if(-not $ownedDialog -and $dialogOwner -gt 0) {
                    $helper=Get-CimInstance Win32_Process -Filter ("ProcessId="+$dialogOwner)
                    $expectedHelper=Join-Path ([IO.Path]::GetDirectoryName($window.exe)) 'P1.ShowDialogBoxes.exe'
                    $ownedDialog=($helper.ParentProcessId -eq $targetProcess -and $helper.ExecutablePath -ieq $expectedHelper)
                }
                if(-not [C1WindowNative]::IsChild($dialogHandle,$editHandle) -or -not $ownedDialog -or $editOwner -ne $dialogOwner){Fail 'WRONG_DIALOG' 'File dialog is not Capture One or its verified child dialog helper.'}
                Check-Guard (Field $request 'guard')
                $script:MayHaveMutated=$true
                $messageResult=[IntPtr]::Zero
                if([C1WindowNative]::SendMessageTimeout($editHandle,0x000C,[IntPtr]::Zero,$stylePath,2,3000,[ref]$messageResult) -eq [IntPtr]::Zero){Fail 'DIALOG_SET_FAILED' 'File field did not accept the style path.'}
                $textBuffer=New-Object System.Text.StringBuilder 32768
                [void][C1WindowNative]::ReadTextTimeout($editHandle,0x000D,[IntPtr]$textBuffer.Capacity,$textBuffer,2,3000,[ref]$messageResult)
                if($textBuffer.ToString() -cne $stylePath){Fail 'DIALOG_READBACK_FAILED' 'File field does not match the requested style path.'}
                Check-Guard (Field $request 'guard')
                $confirmedDialog=Style-SaveDialog $targetProcess $targetWindow $window.exe
                $confirmedHandle=[IntPtr]$confirmedDialog.Current.NativeWindowHandle
                if($confirmedHandle -ne $dialogHandle -or (Style-SaveFilenameHandle $confirmedHandle) -ne $editHandle){Fail 'WRONG_DIALOG' 'Save dialog or filename hierarchy changed before submission.'}
                $currentEditOwner=0;[void][C1WindowNative]::GetWindowThreadProcessId($editHandle,[ref]$currentEditOwner)
                if($currentEditOwner -ne $dialogOwner){Fail 'WRONG_DIALOG' 'Filename process changed before submission.'}
                if([IO.File]::Exists($stylePath) -or [IO.Directory]::Exists($stylePath)){Fail 'OUTPUT_EXISTS' 'Destination appeared before save; no confirmation submitted.'}
                # IDOK saves through Capture One. No mouse/keyboard input.
                if([C1WindowNative]::SendCommandTimeout($dialogHandle,0x0111,[IntPtr]1,[IntPtr]::Zero,2,5000,[ref]$messageResult) -eq [IntPtr]::Zero){Fail 'DIALOG_SUBMIT_UNCERTAIN' 'Open command timed out; inspect before retrying.'}
                $result=@{accepted=$true;path=$stylePath;path_readback_verified=$true;input_sent=$false;window=$window;applied=$false;export_submitted=$true;export_verified=$false}
            }
            'cancel_style_import' {
                $dialog=Style-ImportDialog $targetProcess $targetWindow $window.exe
                $handle=[IntPtr]$dialog.Current.NativeWindowHandle
                Check-Guard (Field $request 'guard')
                $script:MayHaveMutated=$true;$messageResult=[IntPtr]::Zero
                if([C1WindowNative]::SendCommandTimeout($handle,0x0111,[IntPtr]2,[IntPtr]::Zero,2,3000,[ref]$messageResult) -eq [IntPtr]::Zero){Fail 'DIALOG_CANCEL_UNCERTAIN' 'Cancel timed out; inspect before continuing.'}
                $result=@{accepted=$true;window=$window;dialog_hwnd=$handle.ToInt64();input_sent=$false}
            }
            'activate' { $result=Ensure-C1Foreground (Field $request 'guard') }
            'guard' { Check-Guard (Field $request 'guard'); $result=@{window=$window;verified=$true} }
            'prepare_layers' { $result=Open-LayersTool $request }
            'layers' { $result=Get-LayerState; $result['window']=$window }
            'layer_action' { $result=Invoke-BackgroundLayer $request }
            'reveal_tool' {
                Check-Guard (Field $request 'guard')
                $element=Resolve (Field $request 'selector')
                $viewport=Resolve @{automation_id='m_scrollViewer';control_type='Pane';class_name='ScrollViewer';ancestors=@(@{automation_id='ScrollToolsPanel'})}
                $scroll=Pattern $viewport 'Scroll'
                if($null -eq $scroll){Fail 'TOOL_SCROLL_UNAVAILABLE' 'Tool ScrollViewer has no ScrollPattern.'}
                $attempts=0;$visible=$false
                if($scroll.Current.VerticallyScrollable){$script:MayHaveMutated=$true;$scroll.SetScrollPercent(-1.0,0.0);Start-Sleep -Milliseconds 150}
                for($n=0;$n -lt 8;$n++){
                    $r=$element.Current.BoundingRectangle;$v=$viewport.Current.BoundingRectangle
                    $visible=(-not $element.Current.IsOffscreen -and $r.Top -ge $v.Top -and $r.Bottom -le $v.Bottom)
                    if($visible -or -not $scroll.Current.VerticallyScrollable -or $v.Height -le 0){break}
                    $offset=if($r.Top -lt $v.Top){$r.Top-$v.Top}else{$r.Bottom-$v.Bottom}
                    $viewSize=$scroll.Current.VerticalViewSize;$current=$scroll.Current.VerticalScrollPercent
                    if($viewSize -le 0 -or $viewSize -ge 100 -or $current -lt 0){break}
                    $scrollableHeight=$v.Height*(100.0/$viewSize-1.0)
                    $next=[math]::Max(0.0,[math]::Min(100.0,$current+100.0*$offset/$scrollableHeight))
                    if([math]::Abs($next-$current) -lt 0.01){break}
                    $script:MayHaveMutated=$true;$scroll.SetScrollPercent(-1.0,$next);$attempts++
                    Start-Sleep -Milliseconds 100
                }
                Check-Guard (Field $request 'guard')
                $r=$element.Current.BoundingRectangle;$v=$viewport.Current.BoundingRectangle
                $visible=(-not $element.Current.IsOffscreen -and $r.Top -ge $v.Top -and $r.Bottom -le $v.Bottom)
                $result=@{window=$window;accepted=$true;image_modified=$false;tool_visible=$visible;scroll_attempts=$attempts;scroll_source='ScrollViewer.ScrollPattern';vertical_scroll_percent=$scroll.Current.VerticalScrollPercent;node=(Node $element $false)}
            }
            'curve_state' { $result=Get-CurveState }
            'curve_select' { $result=Select-CurveChannel $request }
            'curve_move_point' { $result=Move-CurvePoint $request }
            'curve_add_point' { $result=Add-CurvePoint $request }
            'mixer_state' { $result=Get-MixerState }
            'mixer_select' { $result=Select-MixerPatch $request }
            'mixer_set' { $result=Set-MixerField $request }
            'status' {
                $result = @{window=$window; responsive=$true; app_verified=$true; backend='UIAutomation'}
            }
            'inspect' {
                foreach ($key in (Names $request)) {
                    if ($key -notin @('op','pid','hwnd','max_nodes','selector','tree_view','background_only','profile')) { Fail 'INVALID_REQUEST' 'Inspect accepts only a subtree selector, node limit, fixed tree view and bounded profile.' }
                }
                $treeView = Field $request 'tree_view' 'control'
                if (-not ($treeView -is [string]) -or $treeView -cnotin @('control','raw')) { Fail 'INVALID_REQUEST' 'tree_view must be control or raw.' }
                $requestedLimit = Field $request 'max_nodes' 1000
                if ($requestedLimit -isnot [int] -and $requestedLimit -isnot [long]) { Fail 'INVALID_REQUEST' 'max_nodes must be an integer.' }
                $limit = [int]$requestedLimit
                if ($limit -lt 1 -or $limit -gt 2000) { Fail 'INVALID_REQUEST' 'max_nodes must be between 1 and 2000.' }
                $inspectionSelector = Field $request 'selector'
                if ($treeView -eq 'raw' -and $null -eq $inspectionSelector) { Fail 'RAW_SUBTREE_REQUIRED' 'Raw inspection requires an explicit subtree selector.' }
                $inspectionRoot = $script:RootElement
                if ($null -ne $inspectionSelector) {
                    # Resolve stays within the selected Capture One window.
                    # Missing/ambiguous roots fail before traversal; no fallback.
                    $inspectionRoot = Resolve $inspectionSelector
                }
                $inspectionProfile = Field $request 'profile' 'full'
                if ($inspectionProfile -cnotin @('full','export','compact')) { Fail 'INVALID_REQUEST' 'Unknown inspection profile.' }
                if ($inspectionProfile -eq 'export' -and ($treeView -ne 'control' -or $inspectionRoot.Current.AutomationId -cne 'export' -or $inspectionRoot.Current.ControlType -ne [System.Windows.Automation.ControlType]::Window)) { Fail 'INVALID_REQUEST' 'Export projection requires the exact export window.' }
                if ($treeView -eq 'raw' -and [System.Windows.Automation.Automation]::Compare($inspectionRoot, $script:RootElement)) { Fail 'RAW_SUBTREE_REQUIRED' 'Raw inspection must be scoped below the Capture One main window.' }
                $queue = New-Object System.Collections.Generic.Queue[object]
                $queue.Enqueue($inspectionRoot)
                $nodes = New-Object System.Collections.Generic.List[object]
                $errors = New-Object System.Collections.Generic.List[object]
                $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
                if ($treeView -eq 'raw') { $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker }
                $visited = 0
                while ($queue.Count -gt 0 -and $visited -lt $limit) {
                    $element = $queue.Dequeue()
                    $visited++
                    try {
                        $include = $true
                        if ($inspectionProfile -eq 'export') {
                            $type = ([string]$element.Current.ControlType.ProgrammaticName -replace '^ControlType\.', '')
                            $include = $type -in @('Window','ListItem','Text','CheckBox','ComboBox','MenuItem')
                            if ($type -eq 'Button') { $include = $element.Current.AutomationId -in @('export','cancel') }
                        }
                        if ($include) { $nodes.Add((Node $element ($inspectionProfile -ne 'compact'))) }
                        $child = $walker.GetFirstChild($element)
                        while ($null -ne $child) {
                            $queue.Enqueue($child)
                            if ($queue.Count -gt 10000) { Fail 'TOO_MANY_CONTROLS' 'UI tree exceeds the inspection budget.' }
                            $child = $walker.GetNextSibling($child)
                        }
                    } catch { $errors.Add($_.Exception.Message) }
                }
                $result = @{window=$window; tree_view=$treeView; profile=$inspectionProfile; visited_nodes=$visited; nodes=@($nodes.ToArray()); truncated=($queue.Count -gt 0); read_errors=@($errors.ToArray())}
            }
            'read' {
                $readProfile=Field $request 'profile' 'full'
                if($readProfile -cnotin @('full','compact')){Fail 'INVALID_REQUEST' 'Unknown read profile.'}
                $result = @{window=$window; node=(Node (Resolve (Field $request 'selector')) ($readProfile -eq 'full'))}
            }
            'snapshot' {
                $bindings = Field $request 'bindings'
                if ($null -eq $bindings -or @(Names $bindings).Count -gt 200) { Fail 'INVALID_REQUEST' 'snapshot requires at most 200 named bindings.' }
                $values = [ordered]@{}
                $missing = New-Object System.Collections.Generic.List[string]
                # Bindings already contain their selectors. Snapshot consumers
                # need current observations, not rebuilt eight-level paths.
                foreach ($key in (Names $bindings)) {
                    try {$values[$key] = Observe (Resolve (Field $bindings $key))}
                    catch {if((Field $request 'allow_missing' $false) -eq $true -and $_.Exception.Data['BridgeCode'] -eq 'CONTROL_NOT_FOUND'){$missing.Add($key)}else{throw}}
                }
                $result = @{window=$window; values=$values; missing=@($missing.ToArray()); atomic=$false}
            }
            'set_many' {
                $changes=[object[]](Field $request 'changes' @())
                if($changes.Count -lt 1 -or $changes.Count -gt 100){Fail 'INVALID_REQUEST' 'Expected 1-100 numeric changes.'}
                $batchGuard=Field $request 'guard';$postGuard=Field $request 'post_guard'
                $batchExpected=Field $batchGuard 'expected';$batchBindings=Field $batchGuard 'bindings'
                $prepared=New-Object System.Collections.Generic.List[object];$names=@{}
                # Preflight every control and value before the first mutation.
                foreach($change in $changes){
                    $key=[string](Field $change 'name');$desired=Field $change 'value';$selector=Field $change 'selector'
                    if(-not $key -or $names.ContainsKey($key) -or $desired -is [bool] -or $desired -is [string] -or $null -eq $desired){Fail 'INVALID_REQUEST' 'Invalid numeric batch.'}
                    $names[$key]=$true;$number=[double]$desired
                    if([double]::IsNaN($number) -or [double]::IsInfinity($number)){Fail 'INVALID_VALUE' 'Finite numbers required.'}
                    if((Names $batchBindings) -notcontains $key -or -not (Same (Field $batchBindings $key) $selector) -or (Names (Field $batchExpected $key)) -notcontains 'value'){Fail 'GUARD_REQUIRED' 'Every parameter needs a matching expected-value guard.'}
                    $element=Resolve $selector
                    if(-not $element.Current.IsEnabled -or $element.Current.IsOffscreen){Fail 'CONTROL_UNAVAILABLE' 'All controls must be enabled and visible.'}
                    $pattern=Pattern $element 'RangeValue';$range=$null -ne $pattern
                    if($range){
                        if($pattern.Current.IsReadOnly -or $number -lt $pattern.Current.Minimum -or $number -gt $pattern.Current.Maximum){Fail 'OUT_OF_RANGE' 'Numeric control is read-only or requested value is out of range.'}
                    }else{
                        $pattern=Pattern $element 'Value'
                        if($null -eq $pattern -or $pattern.Current.IsReadOnly){Fail 'PATTERN_UNAVAILABLE' 'Writable numeric pattern required.'}
                    }
                    $prepared.Add(@{name=$key;value=$number;element=$element;pattern=$pattern;range=$range})
                }
                $entries=New-Object System.Collections.Generic.List[object];$watch=$null
                $batchStatus='complete';$batchError=$null;$afterObservations=$null
                try {
                    Check-Guard $batchGuard;Check-Guard $postGuard
                    if($script:BackgroundOnly){$watch=Start-BackgroundWatch;Assert-BackgroundWatch $watch}
                    foreach($change in $prepared){
                        Check-Guard $batchGuard;Check-Guard $postGuard
                        $before=Observe $change.element @('value')
                        $script:MayHaveMutated=$true
                        if($change.range){$change.pattern.SetValue($change.value)}else{$change.pattern.SetValue($change.value.ToString('R',[Globalization.CultureInfo]::CurrentCulture))}
                        Check-Guard $postGuard
                        $after=Observe $change.element @('value')
                        $actual=0.0;$parsed=[double]::TryParse([string]$after.value,[Globalization.NumberStyles]::Float,[Globalization.CultureInfo]::CurrentCulture,[ref]$actual)
                        $matched=$parsed -and [math]::Abs($actual-$change.value) -le 1e-6*[math]::Max(1.0,[math]::Abs($change.value))
                        $nativePattern=if($change.range){'RangeValue'}else{'Value'}
                        $entries.Add(@{name=$change.name;before=$before.value;after=$after.value;requested=$change.value;verified=$matched;native_pattern=$nativePattern;request_accepted=$true;model_commit_verified=$false})
                        if(-not $matched){Fail 'READBACK_MISMATCH' ('Parameter readback mismatch: '+$change.name)}
                        $expectedEntry=Field $batchExpected $change.name
                        $expectedEntry.value=$after.value
                        if($null -ne $watch){Assert-BackgroundWatch $watch}
                    }
                    # Also catches coupled controls or user changes to earlier values.
                    $afterObservations=Check-Guard $batchGuard -Capture;Check-Guard $postGuard
                    $window=Window-Info $targetProcess $targetWindow
                }catch{
                    $batchStatus='incomplete';$code=$_.Exception.Data['BridgeCode'];if(-not $code){$code='BATCH_FAILED'}
                    $batchError=@{code=$code;message=$_.Exception.Message}
                }finally{if($null -ne $watch){$watch.Dispose()}}
                if($null -ne $watch){try{Assert-BackgroundWatch $watch}catch{$batchStatus='incomplete';$batchError=@{code='BACKGROUND_FOCUS_CHANGED';message=$_.Exception.Message}}}
                $result=@{status=$batchStatus;entries=@($entries.ToArray());error=$batchError;possibly_mutated=$script:MayHaveMutated;window=$window;render_verified=$false;input_sent=$false}
                if($batchStatus -eq 'complete'){$result['after_observations']=$afterObservations}
            }
            'set' {
                $element = Resolve (Field $request 'selector')
                if (-not $element.Current.IsEnabled -or $element.Current.IsOffscreen) { Fail 'CONTROL_UNAVAILABLE' 'The bound control must be enabled and visible.' }
                $mode = [string](Field $request 'mode')
                $desired = Field $request 'value'
                $commit=Field $request 'commit' $false
                if($commit -isnot [bool]){Fail 'INVALID_VALUE' 'commit must be a JSON boolean.'}
                if($commit){
                    Assert-ForegroundAllowed 'numeric_commit'
                    if($mode -ne 'number' -or $element.Current.AutomationId -cne 'ToolSliderDecimalUpDown' -or $element.Current.ClassName -cne 'TextBox' -or $element.Current.ControlType -ne [System.Windows.Automation.ControlType]::Edit){Fail 'INVALID_COMMIT_TARGET' 'Commit requires the C1 numeric slider TextBox.'}
                    if($null -eq (Field $request 'post_guard')){Fail 'GUARD_REQUIRED' 'Commit requires a post-write identity guard.'}
                }
                $before = Node $element
                $patternObject = $null; $writeKind = ''
                if ($mode -eq 'number') {
                    if ($desired -is [string] -or $desired -is [bool] -or $null -eq $desired) { Fail 'INVALID_VALUE' 'A finite JSON number is required.' }
                    $number = [double]$desired
                    if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) { Fail 'INVALID_VALUE' 'A finite number is required.' }
                    $patternObject = Pattern $element 'RangeValue'
                    if ($null -ne $patternObject) {
                        if ($patternObject.Current.IsReadOnly) { Fail 'READ_ONLY' 'Control is read-only.' }
                        if ($number -lt $patternObject.Current.Minimum -or $number -gt $patternObject.Current.Maximum) { Fail 'OUT_OF_RANGE' 'Requested value is outside the control range.' }
                        $writeKind = 'RangeValue'
                    } else {
                        $patternObject = Pattern $element 'Value'
                        if ($null -eq $patternObject) { Fail 'PATTERN_UNAVAILABLE' 'Control exposes neither RangeValue nor Value.' }
                        if ($patternObject.Current.IsReadOnly) { Fail 'READ_ONLY' 'Control is read-only.' }
                        $writeKind = 'NumericValue'
                    }
                } elseif ($mode -eq 'text') {
                    if (-not ($desired -is [string]) -or $desired.Length -gt 4096) { Fail 'INVALID_VALUE' 'A string of at most 4096 characters is required.' }
                    $patternObject = Pattern $element 'Value'
                    if ($null -eq $patternObject) { Fail 'PATTERN_UNAVAILABLE' 'Control does not expose Value.' }
                    if ($patternObject.Current.IsReadOnly) { Fail 'READ_ONLY' 'Control is read-only.' }
                    $writeKind = 'Value'
                } elseif ($mode -eq 'toggle') {
                    if (-not ($desired -is [bool])) { Fail 'INVALID_VALUE' 'A JSON boolean is required for toggles.' }
                    $patternObject = Pattern $element 'Toggle'
                    if ($null -eq $patternObject) { Fail 'PATTERN_UNAVAILABLE' 'Control does not expose Toggle.' }
                    if ([string]$patternObject.Current.ToggleState -eq 'Indeterminate') { Fail 'INDETERMINATE_TOGGLE' 'A mixed toggle cannot be changed safely.' }
                    $writeKind = 'Toggle'
                } else { Fail 'INVALID_REQUEST' 'set mode must be number, text or toggle.' }
                Check-Guard (Field $request 'guard')
                if ([string]$script:RootElement.Current.Name -cne [string]$window.title) { Fail 'STALE_TARGET' 'The document changed before dispatch.' }
                if($commit){[void](Ensure-C1Foreground (Field $request 'guard'))}
                $parameterWatch=$null
                if(-not $commit){
                    if($script:BackgroundOnly){$parameterWatch=Start-BackgroundWatch;Assert-BackgroundWatch $parameterWatch}
                    elseif($mode -eq 'number'){$parameterWatch=Start-FocusWatch;Assert-BackgroundWatch $parameterWatch}
                }
                $script:MayHaveMutated = $true
                try {
                switch ($writeKind) {
                    'RangeValue' { $patternObject.SetValue($number) }
                    'NumericValue' { $patternObject.SetValue($number.ToString('R', [Globalization.CultureInfo]::CurrentCulture)) }
                    'Value' { $patternObject.SetValue($desired) }
                    'Toggle' {
                        $on = ([string]$patternObject.Current.ToggleState -eq 'On')
                        if ($on -ne $desired) { $patternObject.Toggle() }
                    }
                }
                $commitReceipt=$null
                if($commit){$commitReceipt=Commit-C1NumericEdit $element (Field $request 'post_guard')}
                $after = Node $element
                $identityVerified = $false
                if ($null -ne (Field $request 'post_guard')) {
                    Check-Guard (Field $request 'post_guard')
                    $identityVerified = $true
                }
                $verified = $false
                if ($mode -eq 'number' -and $after.Contains('value') -and $null -ne $after['value']) {
                    $actual = 0.0
                    if ($after['value'] -is [string]) {
                        $parsed = [double]::TryParse([string]$after['value'], [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::CurrentCulture, [ref]$actual)
                    } else { $actual = [double]$after['value']; $parsed = $true }
                    if ($parsed) { $verified = ([math]::Abs($actual - $number) -le (1e-6 * [math]::Max(1.0, [math]::Abs($number)))) }
                } elseif ($mode -eq 'text' -and $after.Contains('value') -and $null -ne $after['value']) { $verified = ([string]$after['value'] -ceq $desired) }
                elseif ($mode -eq 'toggle' -and $after.Contains('toggle')) { $verified = ([string]$after['toggle'] -ceq $(if ($desired) {'On'} else {'Off'})) }
                $result = @{before=$before; after=$after; accepted=$true; verified=$verified; identity_verified=$identityVerified; render_verified=$false; window=$window}
                if($commit){$result['commit']=$commitReceipt;$result['focus_policy']='foreground_numeric_commit'}
                } finally {if($null -ne $parameterWatch){$parameterWatch.Dispose()}}
                if($null -ne $parameterWatch){
                    # Native Value providers can change focus themselves. The
                    # foreground-allowed policy reports that effect separately
                    # from successful setter/target/readback evidence.
                    if($script:BackgroundOnly){Assert-BackgroundWatch $parameterWatch}
                    $result['focus']=Watch-Report $parameterWatch
                    $result['focus_policy']=if($script:BackgroundOnly){'background_required'}else{'native_no_explicit_activation'}
                }
            }
            'invoke' {
                $element = Resolve (Field $request 'selector')
                if (-not $element.Current.IsEnabled -or $element.Current.IsOffscreen) { Fail 'CONTROL_UNAVAILABLE' 'The bound control must be enabled and visible.' }
                $mode = [string](Field $request 'mode' 'invoke')
                $patternName = switch ($mode) { 'invoke' {'Invoke'} 'select' {'SelectionItem'} 'expand' {'ExpandCollapse'} 'collapse' {'ExpandCollapse'} default {Fail 'INVALID_REQUEST' 'Unknown invoke mode.'} }
                $patternObject = Pattern $element $patternName
                if ($null -eq $patternObject) { Fail 'PATTERN_UNAVAILABLE' "Control does not expose $patternName." }
                $before = Node $element
                Check-Guard (Field $request 'guard')
                $script:MayHaveMutated = $true
                switch ($mode) { 'invoke' {$patternObject.Invoke()} 'select' {$patternObject.Select()} 'expand' {$patternObject.Expand()} 'collapse' {$patternObject.Collapse()} }
                $result = @{accepted=$true; verified=$false; render_verified=$false; before=$before; window=$window}
                if ($mode -ne 'invoke') {
                    $after = Node $element
                    $result['after'] = $after
                    if ($mode -eq 'select') { $result['verified'] = [bool]$after['selected'] }
                    if ($mode -eq 'expand') { $result['verified'] = ([string]$after['expand_state'] -eq 'Expanded') }
                    if ($mode -eq 'collapse') { $result['verified'] = ([string]$after['expand_state'] -eq 'Collapsed') }
                }
            }
            'click' {
                foreach ($key in (Names $request)) {
                    if ($key -notin @('op','pid','hwnd','selector','guard','background_only')) { Fail 'INVALID_REQUEST' 'Click accepts only selector and guard, never coordinates, commands, or key sequences.' }
                }
                if (-not $window.visible -or $window.minimized) { Fail 'WINDOW_NOT_VISIBLE' 'The target window must already be visible and not minimized.' }
                $selector = Field $request 'selector'
                $element = Resolve $selector
                if (-not $element.Current.IsEnabled -or $element.Current.IsOffscreen) { Fail 'CONTROL_UNAVAILABLE' 'The click target must be enabled and visible.' }
                $point = New-Object System.Windows.Point
                if (-not $element.TryGetClickablePoint([ref]$point)) { Fail 'NO_CLICKABLE_POINT' 'UI Automation did not provide a clickable point.' }
                if ([double]::IsNaN($point.X) -or [double]::IsInfinity($point.X) -or [double]::IsNaN($point.Y) -or [double]::IsInfinity($point.Y)) { Fail 'NO_CLICKABLE_POINT' 'UI Automation returned an invalid clickable point.' }
                $runtimeId = @($element.GetRuntimeId())
                $before = Node $element
                Check-Guard (Field $request 'guard')
                if ([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow) {
                    # Foreground activation can itself commit a pending UI edit.
                    $script:MayHaveMutated = $true
                    $activated = [C1WindowNative]::SetForegroundWindow([IntPtr]$targetWindow)
                    if (-not $activated -or [C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow) { Fail 'FOREGROUND_REFUSED' 'Windows did not grant foreground to the selected Capture One window; no click was sent.' }
                }
                foreach ($key in @(1,2,4,5,6,16,17,18,91,92)) {
                    if (([C1WindowNative]::GetAsyncKeyState($key) -band 0x8000) -ne 0) { Fail 'INPUT_HELD' 'A mouse button or modifier key is held; no click was sent.' }
                }
                $hitPoint = New-Object C1WindowNative+POINT
                $hitPoint.X = [int][math]::Round($point.X)
                $hitPoint.Y = [int][math]::Round($point.Y)
                $hitWindow = [C1WindowNative]::WindowFromPoint($hitPoint)
                $hitPid = 0
                [void][C1WindowNative]::GetWindowThreadProcessId($hitWindow, [ref]$hitPid)
                if ($hitPid -ne $targetProcess -or ($hitWindow.ToInt64() -ne $targetWindow -and -not [C1WindowNative]::IsChild([IntPtr]$targetWindow,$hitWindow))) { Fail 'CLICK_POINT_OCCLUDED' 'The clickable point belongs to another window or process.' }
                Check-Guard (Field $request 'guard')
                $fresh = Resolve $selector
                if (-not (Same @($fresh.GetRuntimeId()) $runtimeId)) { Fail 'STALE_TARGET' 'The target runtime identity changed before click.' }
                if (-not $fresh.Current.IsEnabled -or $fresh.Current.IsOffscreen) { Fail 'CONTROL_UNAVAILABLE' 'The click target changed visibility or enabled state.' }
                $freshPoint = New-Object System.Windows.Point
                if (-not $fresh.TryGetClickablePoint([ref]$freshPoint) -or $freshPoint.X -ne $point.X -or $freshPoint.Y -ne $point.Y) { Fail 'CLICK_POINT_CHANGED' 'The target clickable point changed; no click was sent.' }
                $left = [C1WindowNative]::GetSystemMetrics(76)
                $top = [C1WindowNative]::GetSystemMetrics(77)
                $width = [C1WindowNative]::GetSystemMetrics(78)
                $height = [C1WindowNative]::GetSystemMetrics(79)
                $inputs = [C1WindowNative]::CreateLeftClickInputs($hitPoint.X,$hitPoint.Y,$left,$top,$width,$height)
                # Recheck foreground, hit window and held input at the dispatch boundary.
                if ([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow -or [C1WindowNative]::WindowFromPoint($hitPoint) -ne $hitWindow) { Fail 'CLICK_TARGET_CHANGED' 'Foreground or hit window changed; no click was sent.' }
                foreach ($key in @(1,2,4,5,6,16,17,18,91,92)) {
                    if (([C1WindowNative]::GetAsyncKeyState($key) -band 0x8000) -ne 0) { Fail 'INPUT_HELD' 'A mouse button or modifier key is held; no click was sent.' }
                }
                $script:MayHaveMutated = $true
                $sent = [C1WindowNative]::SendInput([uint32]$inputs.Length,$inputs,[Runtime.InteropServices.Marshal]::SizeOf([type][C1WindowNative+INPUT]))
                if ($sent -ne $inputs.Length) { Fail 'INPUT_INCOMPLETE' 'Windows did not accept the complete click sequence; do not retry automatically.' @{input_count=$inputs.Length; input_sent=$sent; win32_error=[Runtime.InteropServices.Marshal]::GetLastWin32Error(); mouse_button_state_unknown=$true} }
                $after = $null
                $afterError = $null
                try { $after = Node $fresh } catch { $afterError = $_.Exception.Message }
                $result = @{accepted=$true; input_sent=$true; input_count=$sent; before=$before; after=$after; after_read_error=$afterError;
                    runtime_id=$runtimeId; click_point=@{x=$point.X;y=$point.Y}; verified=$false; render_verified=$false; window=$window;
                    note='Native input was accepted. Selection, parameter changes and rendering require independent verification.'}
            }
            'navigate' {
                foreach ($key in (Names $request)) {
                    if ($key -notin @('op','pid','hwnd','selector','guard','direction','background_only')) { Fail 'INVALID_REQUEST' 'Navigate accepts only browser selector, guard, and a fixed direction.' }
                }
                $direction = Field $request 'direction'
                if (-not ($direction -is [string]) -or $direction -cnotin @('left','right','up','down')) { Fail 'INVALID_REQUEST' 'Only left, right, up, or down navigation is supported.' }
                $selector = Field $request 'selector'
                if ([string](Field $selector 'automation_id') -cne 'MainItemsControl') { Fail 'INVALID_NAVIGATION_TARGET' 'Navigation is limited to the observed MainItemsControl browser.' }
                if (-not $window.visible -or $window.minimized) { Fail 'WINDOW_NOT_VISIBLE' 'The target window must already be visible and not minimized.' }
                $element = Resolve $selector
                if ($element.Current.ControlType -ne [System.Windows.Automation.ControlType]::List -or $element.Current.ClassName -cne 'ItemsControl') { Fail 'INVALID_NAVIGATION_TARGET' 'The target is not the expected browser list control.' }
                if (-not $element.Current.IsEnabled -or $element.Current.IsOffscreen) { Fail 'CONTROL_UNAVAILABLE' 'The browser must be enabled and visible.' }
                $runtimeId = @($element.GetRuntimeId())
                $windowRuntimeId = @($script:RootElement.GetRuntimeId())
                $before = Node $element
                Require-No-Held-Input
                Check-Guard (Field $request 'guard')
                # Focus is explicit; never click or attach another input thread.
                $script:MayHaveMutated = $true
                $focusAttempt = Focus-NavigationTarget $script:RootElement $element $targetWindow
                if ([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow) {
                    $activated = [C1WindowNative]::SetForegroundWindow([IntPtr]$targetWindow)
                    if (-not $activated -or [C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow) { Fail 'FOREGROUND_REFUSED' 'Windows did not grant foreground to Capture One; no navigation key was sent.' }
                }
                if (-not (Focus-In-Browser $element $targetProcess)) { Fail 'FOCUS_OUTSIDE_BROWSER' 'Keyboard focus is not in MainItemsControl or a descendant; no navigation key was sent.' }
                $currentWindow = Window-Info $targetProcess $targetWindow
                if (-not (Same @($script:RootElement.GetRuntimeId()) $windowRuntimeId) -or
                    $currentWindow.title -cne $window.title -or $currentWindow.version -cne $window.version) { Fail 'WINDOW_CHANGED' 'The Capture One window identity changed before navigation.' }
                Check-Guard (Field $request 'guard')
                $fresh = Resolve $selector
                if (-not (Same @($fresh.GetRuntimeId()) $runtimeId)) { Fail 'STALE_TARGET' 'The browser runtime identity changed before navigation.' }
                if (-not $fresh.Current.IsEnabled -or $fresh.Current.IsOffscreen) { Fail 'CONTROL_UNAVAILABLE' 'The browser changed visibility or enabled state.' }
                $inputs = [C1WindowNative]::CreateNavigationInputs($direction)
                Require-No-Held-Input
                if ([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow) { Fail 'FOREGROUND_CHANGED' 'Foreground changed; no navigation key was sent.' }
                if (-not (Focus-In-Browser $fresh $targetProcess)) { Fail 'FOCUS_OUTSIDE_BROWSER' 'Keyboard focus left the browser; no navigation key was sent.' }
                $sent = [C1WindowNative]::SendInput([uint32]$inputs.Length,$inputs,[Runtime.InteropServices.Marshal]::SizeOf([type][C1WindowNative+INPUT]))
                if ($sent -ne $inputs.Length) { Fail 'INPUT_INCOMPLETE' 'Windows did not accept the complete navigation key pair; do not retry automatically.' @{input_count=$inputs.Length; input_sent=$sent; win32_error=[Runtime.InteropServices.Marshal]::GetLastWin32Error(); key_state_unknown=$true} }
                $after = $null
                $afterError = $null
                try { $after = Node $fresh } catch { $afterError = $_.Exception.Message }
                $result = @{accepted=$true; input_sent=$true; input_count=$sent; direction=$direction; before=$before; after=$after; after_read_error=$afterError;
                    runtime_id=$runtimeId; focus_verified_before_input=$true; verified=$false; render_verified=$false; window=$window;
                    root_focus_attempted=$focusAttempt.root_focus_attempted; root_focus_error=$focusAttempt.root_focus_error;
                    note='One fixed navigation key pair was accepted. The selected photo must be verified independently.'}
            }
            'nudge_adjustment' {
                foreach ($key in (Names $request)) {
                    if ($key -notin @('op','pid','hwnd','selector','guard','post_guard','direction','background_only')) { Fail 'INVALID_REQUEST' 'Nudge accepts only an observed color-balance selector, guards, and one fixed arrow direction.' }
                }
                $direction = Field $request 'direction'
                if (-not ($direction -is [string]) -or $direction -cnotin @('left','right','up','down')) { Fail 'INVALID_REQUEST' 'Only left, right, up, or down are supported.' }
                $selector = Field $request 'selector'
                Validate-Selector $selector
                $colorBalanceAncestor = $false
                foreach ($ancestor in (Field $selector 'ancestors' @())) {
                    if ([string](Field $ancestor 'class_name') -ceq 'ColorBalanceToolView') { $colorBalanceAncestor = $true }
                }
                if (-not $colorBalanceAncestor) { Fail 'INVALID_NUDGE_TARGET' 'Nudge requires an observed ColorBalanceToolView ancestor.' }
                if (-not $window.visible -or $window.minimized) { Fail 'WINDOW_NOT_VISIBLE' 'The target window must already be visible and not minimized.' }
                $element = Resolve $selector
                $id = [string]$element.Current.AutomationId
                $className = [string]$element.Current.ClassName
                $isWheel = $id -ceq 'HsvWheel' -and $className -ceq 'ColorBalanceWheel'
                $isSlider = $id -cin @('HueSlider','SaturationSlider','LightnessSlider') -and $className -ceq 'CurvedSlider'
                if ($element.Current.ControlType -ne [System.Windows.Automation.ControlType]::Custom -or -not ($isWheel -or $isSlider)) { Fail 'INVALID_NUDGE_TARGET' 'Only observed Capture One color-balance wheels and sliders support this operation.' }
                if (-not $element.Current.IsEnabled -or $element.Current.IsOffscreen -or -not $element.Current.IsKeyboardFocusable) { Fail 'CONTROL_UNAVAILABLE' 'The color-balance control must be enabled, visible, and keyboard focusable.' }
                $runtimeId = @($element.GetRuntimeId())
                $windowRuntimeId = @($script:RootElement.GetRuntimeId())
                $before = Node $element
                Require-No-Held-Input
                Check-Guard (Field $request 'guard')
                if ($null -ne (Field $request 'post_guard')) { Check-Guard (Field $request 'post_guard') }
                $script:MayHaveMutated = $true
                $focusAttempt = Focus-NavigationTarget $script:RootElement $element $targetWindow
                if ([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow) {
                    $activated = [C1WindowNative]::SetForegroundWindow([IntPtr]$targetWindow)
                    if (-not $activated -or [C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow) { Fail 'FOREGROUND_REFUSED' 'Windows did not grant foreground to Capture One; no adjustment key was sent.' }
                }
                if (-not (Focus-In-Control $element $targetProcess)) { Fail 'FOCUS_OUTSIDE_CONTROL' 'Keyboard focus is not in the exact color-balance control or its descendant; no adjustment key was sent.' }
                $currentWindow = Window-Info $targetProcess $targetWindow
                if (-not (Same @($script:RootElement.GetRuntimeId()) $windowRuntimeId) -or
                    $currentWindow.title -cne $window.title -or $currentWindow.version -cne $window.version) { Fail 'WINDOW_CHANGED' 'The Capture One window identity changed before adjustment.' }
                Check-Guard (Field $request 'guard')
                $fresh = Resolve $selector
                if (-not (Same @($fresh.GetRuntimeId()) $runtimeId)) { Fail 'STALE_TARGET' 'The color-balance runtime identity changed before adjustment.' }
                if (-not $fresh.Current.IsEnabled -or $fresh.Current.IsOffscreen -or -not $fresh.Current.IsKeyboardFocusable) { Fail 'CONTROL_UNAVAILABLE' 'The color-balance control changed availability.' }
                $inputs = [C1WindowNative]::CreateNavigationInputs($direction)
                Require-No-Held-Input
                if ([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow) { Fail 'FOREGROUND_CHANGED' 'Foreground changed; no adjustment key was sent.' }
                if (-not (Focus-In-Control $fresh $targetProcess)) { Fail 'FOCUS_OUTSIDE_CONTROL' 'Keyboard focus left the exact color-balance control; no adjustment key was sent.' }
                $sent = [C1WindowNative]::SendInput([uint32]$inputs.Length,$inputs,[Runtime.InteropServices.Marshal]::SizeOf([type][C1WindowNative+INPUT]))
                if ($sent -ne $inputs.Length) { Fail 'INPUT_INCOMPLETE' 'Windows did not accept the complete adjustment key pair; do not retry automatically.' @{input_count=$inputs.Length; input_sent=$sent; win32_error=[Runtime.InteropServices.Marshal]::GetLastWin32Error(); key_state_unknown=$true} }
                $identityVerified = $false
                if ($null -ne (Field $request 'post_guard')) { Check-Guard (Field $request 'post_guard'); $identityVerified = $true }
                $after = $null; $afterError = $null
                try { $after = Node $fresh } catch { $afterError = $_.Exception.Message }
                $result = @{accepted=$true; input_sent=$true; input_count=$sent; direction=$direction; before=$before; after=$after; after_read_error=$afterError;
                    runtime_id=$runtimeId; focus_verified=$true; focus_verified_before_input=$true; identity_verified=$identityVerified;
                    root_focus_attempted=$focusAttempt.root_focus_attempted; root_focus_error=$focusAttempt.root_focus_error;
                    verified=$false; render_verified=$false; window=$window;
                    note='One fixed arrow key pair was accepted by Windows. Parameter change and image effect require independent verification.'}
            }
            'screenshot' {
                if (-not $window.visible -or $window.minimized) { Fail 'WINDOW_NOT_VISIBLE' 'Capture One must be visible and not minimized.' }
                if ([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow) { Fail 'NOT_FOREGROUND' 'Bring the selected Capture One window to the foreground before capturing.' }
                $element = $script:RootElement
                if ($null -ne (Field $request 'selector')) { $element = Resolve (Field $request 'selector') }
                if ($element.Current.IsOffscreen) { Fail 'CONTROL_UNAVAILABLE' 'The preview control is offscreen.' }
                $bounds = $element.Current.BoundingRectangle
                if ($bounds.IsEmpty -or $bounds.Width -lt 1 -or $bounds.Height -lt 1) { Fail 'EMPTY_BOUNDS' 'The control has no visible bounds.' }
                Add-Type -AssemblyName System.Drawing
                Add-Type -AssemblyName System.Windows.Forms
                $desktop = [Windows.Forms.SystemInformation]::VirtualScreen
                $x=[int][math]::Floor($bounds.X); $y=[int][math]::Floor($bounds.Y)
                $w=[int][math]::Ceiling($bounds.Width); $h=[int][math]::Ceiling($bounds.Height)
                if ($x -lt $desktop.Left -or $y -lt $desktop.Top -or ($x+$w) -gt $desktop.Right -or ($y+$h) -gt $desktop.Bottom -or ([long]$w*$h) -gt 40000000) {
                    Fail 'INVALID_BOUNDS' 'The full capture area must fit on the virtual desktop and be at most 40 megapixels.'
                }
                $outputPath = [string](Field $request 'output_path')
                if (-not [IO.Path]::IsPathRooted($outputPath) -or [IO.Path]::GetExtension($outputPath) -ine '.png') { Fail 'INVALID_PATH' 'An absolute .png output path is required.' }
                if ([IO.File]::Exists($outputPath)) { Fail 'OUTPUT_EXISTS' 'Screenshots do not overwrite existing files.' }
                $parentPath = [IO.Path]::GetDirectoryName($outputPath)
                if (-not [IO.Directory]::Exists($parentPath)) { [void][IO.Directory]::CreateDirectory($parentPath) }
                $bitmap = [System.Drawing.Bitmap]::new($w,$h)
                $graphics = [Drawing.Graphics]::FromImage($bitmap)
                try {
                    $graphics.CopyFromScreen($x,$y,0,0,([System.Drawing.Size]::new($w,$h)),[Drawing.CopyPixelOperation]::SourceCopy)
                    if ([C1WindowNative]::GetForegroundWindow().ToInt64() -ne $targetWindow) { Fail 'FOREGROUND_CHANGED' 'Foreground changed during capture; image was not saved.' }
                    # CreateNew avoids racing another producer of the same file.
                    $stream = [System.IO.FileStream]::new($outputPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
                    try { $bitmap.Save($stream,[Drawing.Imaging.ImageFormat]::Png) } finally { $stream.Dispose() }
                } finally { $graphics.Dispose(); $bitmap.Dispose() }
                $result = @{path=$outputPath;width=$w;height=$h;kind='screen_capture';render_verified=$false;
                    note='Visible on-screen pixels only; may include overlays and are not a fresh exported render.';window=$window}
            }
        }
    }
    $script:Metrics.elapsed_ms=[math]::Round($script:RequestWatch.Elapsed.TotalMilliseconds,2)
    $result['driver_metrics']=$script:Metrics
    $response = @{ok=$true;result=$result}
    [Console]::Out.WriteLine(($response | ConvertTo-Json -Depth 40 -Compress))
    if (-not $Resident) { exit 0 }
} catch {
    $exception = $_.Exception
    $code = $exception.Data['BridgeCode']
    if (-not $code) { $code = 'UIA_ERROR' }
    $response = @{ok=$false;error=@{code=$code;message=$exception.Message;details=$exception.Data['BridgeDetails'];possibly_mutated=$script:MayHaveMutated}}
    [Console]::Out.WriteLine(($response | ConvertTo-Json -Depth 20 -Compress))
    if (-not $Resident) { exit 0 }
}
