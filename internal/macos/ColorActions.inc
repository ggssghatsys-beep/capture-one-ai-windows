// Version-qualified Color Editor model path. All edits are made to deep copies;
// the owner setters are the same selectors used by C1's AppleScript updater.
@interface NSObject (C1ColorActions)
- (id)basicColorCorrections;
- (id)advancedColorCorrections;
- (id)skinToneColorCorrections;
- (void)setBasicColorCorrections:(id)list;
- (void)setAdvancedColorCorrections:(id)list;
- (void)setSkinToneColorCorrections:(id)list;
- (id)corrections;
- (id)toString;
- (id)componentsSeparatedByString:(id)separator;
+ (id)colorCorrectionListWithArray:(id)array;
+ (id)colorCorrectionWithRed:(float)red green:(float)green blue:(float)blue;
+ (id)skinToneCorrectionWithRed:(float)red green:(float)green blue:(float)blue;
- (void)setRed:(float)red green:(float)green blue:(float)blue;
- (void)setHueChange:(float)value;
- (void)setSaturationChange:(float)value;
- (void)setLightnessChange:(float)value;
- (void)setSmoothness:(float)value;
- (void)setHomogeneityHue:(float)value;
- (void)setHomogeneitySaturation:(float)value;
- (void)setHomogeneityLightness:(float)value;
- (void)setHueStart:(float)value;
- (void)setHueEnd:(float)value;
- (void)setSaturationStart:(float)value;
- (void)setSaturationEnd:(float)value;
- (void)setEnabled:(BOOL)value;
- (void)setInvertedChange:(BOOL)value;
@end
static NSArray *ColorKeys(void) { return @[@"advancedColorCorrections", @"basicColorCorrections", @"skinToneColorCorrections"]; }
static id ColorOwner(void) {
    id variant = CurrentVariant();
    if (![variant hasLayerSelected]) return variant;
    id layer = [variant activeLocalAdjustmentLayer];
    if (!layer || ![layer isKindOfClass:NSClassFromString(@"POLocalAdjLayer")] ||
        [layer variant] != variant || ![layer isEditable] || [layer hasPendingTemporalChanges])
        @throw @"Color Editor requires an idle editable layer";
    return layer;
}
static id ColorList(id owner, NSUInteger type) {
    NSString *key = [ColorKeys() objectAtIndex:type];
    RequireMethod(owner, [key UTF8String], "@16@0:8");
    id list = [owner valueForKey:key];
    if (list && ![list isKindOfClass:NSClassFromString(@"ColorCorrectionList")]) @throw @"unknown Color Editor list class";
    if (list) {
        RequireMethod(list, "corrections", "@16@0:8"); RequireMethod(list, "toString", "@16@0:8");
        NSArray *rows = [list corrections];
        if ([rows count] > 35 || (type == 2 && [rows count] > 1)) @throw @"unsupported Color Editor range count";
        for (NSUInteger i = 0; i < [rows count]; i++) {
            id row = [rows objectAtIndex:i];
            if (![row isKindOfClass:NSClassFromString(@"ColorCorrection")] || [[row valueForKey:@"type"] integerValue] != (NSInteger)type)
                @throw @"unknown Color Editor correction type";
        }
    }
    return list;
}
static NSArray *ColorGroups(id owner) {
    NSMutableArray *groups = [NSMutableArray array];
    for (NSUInteger type = 0; type < 3; type++) {
        id list = ColorList(owner, type);
        [groups addObject:(list ? [list toString] : nil) ?: @""];
    }
    return groups;
}
static NSDictionary *ReadColor(NSDictionary *request) {
    CheckTarget(request[@"target"]);
    id owner = ColorOwner(); NSArray *groups = ColorGroups(owner);
    CheckTarget(request[@"target"]);
    if (owner != ColorOwner()) @throw @"Color Editor owner changed";
    return @{@"target": Target(), @"groups": groups, @"source": @"C1 ColorCorrectionList.toString"};
}
static float ColorNumber(NSDictionary *patch, NSString *key, double low, double high) {
    id value = patch[key];
    if (![value isKindOfClass:[NSNumber class]]) @throw @"Color Editor value must be numeric";
    double number = [value doubleValue];
    if (!(number >= low && number <= high)) @throw @"Color Editor value is outside its qualified range";
    return (float)number;
}
// 16.8.6.25 ColorCorrectionBasicAppleScript.name indexes this same
// nine-name constant array (pink is the public magenta sector).
static NSUInteger BasicColorIndex(NSDictionary *patch) {
    NSArray *names=@[@"magenta",@"purple",@"blue",@"cyan",@"green",@"yellow",@"orange",@"red",@"all"];
    for (NSUInteger i=0;i<9;i++) if ([patch[@"color"] isEqual:names[i]]) {
        if (i==8 && patch[@"lightness"]) @throw @"all-colors has no Basic lightness setter";
        return i;
    }
    @throw @"unknown Basic color sector";
}
static void PatchColor(id row, NSDictionary *patch, NSUInteger type) {
    BOOL skin=type==2, basic=type==1;
    if (patch[@"rgb"]) {
        NSArray *rgb = patch[@"rgb"];
        if (![rgb isKindOfClass:[NSArray class]] || [rgb count] != 3) @throw @"Color Editor RGB requires three numbers";
        NSDictionary *values = @{@"r":rgb[0], @"g":rgb[1], @"b":rgb[2]};
        RequireMethod(row, "setRed:green:blue:", "v28@0:8f16f20f24");
        [row setRed:ColorNumber(values,@"r",0,255) green:ColorNumber(values,@"g",0,255) blue:ColorNumber(values,@"b",0,255)];
    }
#define C1_COLOR_FLOAT(key, selector, lo, hi) if (patch[@key]) { RequireMethod(row, #selector ":", "v20@0:8f16"); [row selector:ColorNumber(patch,@key,lo,hi)]; }
    C1_COLOR_FLOAT("hue", setHueChange, skin ? -5 : -30, skin ? 5 : 30)
    C1_COLOR_FLOAT("saturation", setSaturationChange, skin ? -30 : basic ? -80 : -100, skin ? 30 : basic ? 80 : 200)
    C1_COLOR_FLOAT("lightness", setLightnessChange, skin ? -10 : basic ? -50 : -100, skin ? 10 : basic ? 50 : 100)
    C1_COLOR_FLOAT("smoothness", setSmoothness, 1, 30)
    C1_COLOR_FLOAT("hue_low", setHueStart, -357, -2)
    C1_COLOR_FLOAT("hue_high", setHueEnd, 2, 357)
    C1_COLOR_FLOAT("saturation_low", setSaturationStart, -3.4028234663852886e38, -2)
    C1_COLOR_FLOAT("saturation_high", setSaturationEnd, 2, 3.4028234663852886e38)
    C1_COLOR_FLOAT("uniformity_hue", setHomogeneityHue, 0, 100)
    C1_COLOR_FLOAT("uniformity_saturation", setHomogeneitySaturation, 0, 100)
    C1_COLOR_FLOAT("uniformity_lightness", setHomogeneityLightness, 0, 100)
#undef C1_COLOR_FLOAT
    if (patch[@"enabled"]) { RequireMethod(row,"setEnabled:","v20@0:8B16"); [row setEnabled:[patch[@"enabled"] boolValue]]; }
    if (patch[@"inverted"]) { RequireMethod(row,"setInvertedChange:","v20@0:8B16"); [row setInvertedChange:[patch[@"inverted"] boolValue]]; }
}
static id BuildColorList(id owner, NSDictionary *patch, NSUInteger type, BOOL reset) {
    id list = ColorList(owner, type);
    // The installed copyWithZone: deep-copies every correction, preserving
    // hidden columns and flags. Never mutate an object borrowed from the host.
    list = list ? [[list copy] autorelease] : [NSClassFromString(@"ColorCorrectionList") colorCorrectionListWithArray:@[]];
    NSArray *old = [list corrections];
    NSMutableArray *rows = [NSMutableArray array];
    if (reset) {
        if (type == 1) {
            if ([old count] != 9) @throw @"full neutral reset requires nine explicit Basic sectors";
            for (NSUInteger i = 0; i < [old count]; i++) {
                id row = [old objectAtIndex:i];
                RequireMethod(row,"setHueChange:","v20@0:8f16"); RequireMethod(row,"setSaturationChange:","v20@0:8f16");
                RequireMethod(row,"setLightnessChange:","v20@0:8f16");
                [row setHueChange:0]; [row setSaturationChange:0]; [row setLightnessChange:0]; [rows addObject:row];
            }
        }
    } else {
        NSString *action = patch[@"action"] ?: @"update";
        BOOL add = [action isEqual:@"add"], remove = [action isEqual:@"remove"];
        if (!add && !remove && ![action isEqual:@"update"]) @throw @"unsupported Color Editor range action";
        if (type==1 && ([old count]!=9 || add || remove)) @throw @"Basic edit requires nine existing sectors";
        NSInteger index = type==1 ? (NSInteger)BasicColorIndex(patch) : add ? (NSInteger)[old count] : [patch[@"index"] integerValue];
        if (index < 0 || (!add && (NSUInteger)index >= [old count]) || (add && ([old count] >= 35 || (type == 2 && [old count]))))
            @throw @"Color Editor range index or count changed";
        for (NSUInteger i = 0; i < [old count]; i++) {
            id row = [old objectAtIndex:i];
            if ((NSInteger)i == index) { if (remove) continue; PatchColor(row,patch,type); }
            [rows addObject:row];
        }
        if (add) {
            NSArray *rgb = patch[@"rgb"];
            if (![rgb isKindOfClass:[NSArray class]] || [rgb count] != 3) @throw @"new range requires RGB";
            NSDictionary *values = @{@"r":rgb[0], @"g":rgb[1], @"b":rgb[2]};
            Class cls = NSClassFromString(@"ColorCorrection");
            RequireMethod(cls, type == 2 ? "skinToneCorrectionWithRed:green:blue:" : "colorCorrectionWithRed:green:blue:", "@28@0:8f16f20f24");
            float r=ColorNumber(values,@"r",0,255), g=ColorNumber(values,@"g",0,255), b=ColorNumber(values,@"b",0,255);
            id row = type == 2 ? [cls skinToneCorrectionWithRed:r green:g blue:b] : [cls colorCorrectionWithRed:r green:g blue:b];
            if (!row) @throw @"native range factory failed";
            PatchColor(row,patch,type); [rows addObject:row];
        }
    }
    return [NSClassFromString(@"ColorCorrectionList") colorCorrectionListWithArray:rows];
}
// Validate the detached serialized plan before the first owner setter. This
// catches setter clipping or coupled changes without ever mutating host state.
static NSArray *ColorSerializedColumns(id correction) {
    NSArray *columns = [[correction toString] componentsSeparatedByString:@","];
    if ([columns count] != 18) @throw @"unknown native Color Editor serialization";
    for (NSUInteger i=0;i<18;i++) {
        double value=[columns[i] doubleValue];
        if (!(value >= -3.4028234663852886e38 && value <= 3.4028234663852886e38))
            @throw @"nonfinite native Color Editor column";
    }
    return columns;
}
static BOOL ColorClose(double actual, double expected) {
    double delta=actual-expected; if (delta<0) delta=-delta;
    double magnitude=expected<0?-expected:expected;
    double tolerance=magnitude*1.2e-7; if (tolerance<1e-7) tolerance=1e-7;
    return delta<=tolerance;
}
static void CheckColorRow(id original, id changed, NSDictionary *patch, NSUInteger type, BOOL reset, BOOL added) {
    NSArray *old=original?ColorSerializedColumns(original):nil, *next=ColorSerializedColumns(changed);
    if ([next[2] doubleValue] != type) @throw @"detached Color Editor group changed";
    if (!reset && type!=1) {
        // Preserve host defaults/old extents for numeric-only edits. When the
        // user changes geometry, validate both actual detached sides, including
        // a factory side omitted from an add request.
        if (patch[@"hue_low"] || patch[@"hue_high"]) {
            if ([next[10] doubleValue]-[next[9] doubleValue]>359.0001)
                @throw @"Color Editor hue range cannot exceed 359 degrees";
        }
        if (patch[@"saturation_low"] || patch[@"saturation_high"] || (!added && patch[@"rgb"])) {
            double r=[next[6] doubleValue],g=[next[7] doubleValue],b=[next[8] doubleValue];
            double maximum=r>g?r:g,minimum=r<g?r:g;
            if (b>maximum) maximum=b; if (b<minimum) minimum=b;
            double saturation=maximum?100*(maximum-minimum)/maximum:0;
            double low=[next[11] doubleValue],high=[next[12] doubleValue];
            if (low < -saturation-0.0001 || high > 100-saturation+0.0001 || low>high)
                @throw @"Color Editor absolute saturation range must stay within 0..100";
        }
    }
    NSArray *names=@[@"enabled",@"",@"",@"hue",@"saturation",@"lightness",@"",@"",@"",
                    @"hue_low",@"hue_high",@"saturation_low",@"saturation_high",@"smoothness",@"uniformity_hue",
                    @"inverted",@"uniformity_saturation",@"uniformity_lightness"];
    for (NSUInteger column=0;column<18;column++) {
        id value=nil;
        if (reset && (column==3 || column==4 || column==5)) value=@0;
        else if (!reset) {
            value=patch[names[column]];
            if (column>=6 && column<=8 && patch[@"rgb"]) value=patch[@"rgb"][column-6];
        }
        double actual=[next[column] doubleValue];
        if (value) {
            float requested=(float)[value doubleValue];
            if (column==5) requested=(float)(requested/10.0f);
            if (!ColorClose(actual,requested)) @throw @"native Color Editor setter changed the requested value";
        } else if (!added && actual != [old[column] doubleValue])
            @throw @"native Color Editor setter changed an unrequested column";
    }
}
static void CheckColorPlan(id owner, NSArray *lists, NSDictionary *patch, BOOL reset, NSUInteger requestedType) {
    for (NSUInteger type=0;type<3;type++) {
        NSArray *old=[ColorList(owner,type) corrections], *next=[[lists objectAtIndex:type] corrections];
        if (reset) {
            if (type!=1) { if ([next count]) @throw @"neutral reset retained a range"; continue; }
            if ([old count]!=9 || [next count]!=9) @throw @"neutral reset requires nine Basic sectors";
            for (NSUInteger i=0;i<9;i++) CheckColorRow(old[i],next[i],patch,type,YES,NO);
            continue;
        }
        if (type!=requestedType) {
            if ([old count]!=[next count]) @throw @"detached plan changes another Color Editor group";
            for (NSUInteger i=0;i<[old count];i++) CheckColorRow(old[i],next[i],@{},type,NO,NO);
            continue;
        }
        NSString *action=patch[@"action"]?:@"update";
        BOOL add=[action isEqual:@"add"], remove=[action isEqual:@"remove"];
        NSUInteger index=type==1?BasicColorIndex(patch):add?[old count]:(NSUInteger)[patch[@"index"] integerValue];
        if ([next count] != [old count]+(add?1:0)-(remove?1:0)) @throw @"detached plan has wrong range count";
        for (NSUInteger i=0;i<[next count];i++) {
            BOOL selected=i==index && !remove;
            NSUInteger previous=i+(remove && i>=index?1:0);
            id original=add && i==index?nil:old[previous];
            CheckColorRow(original,next[i],selected?patch:@{},type,NO,add && i==index);
        }
    }
}
static void SetColorList(id owner, id list, NSUInteger type) {
    if (type == 0) [owner setAdvancedColorCorrections:list];
    else if (type == 1) [owner setBasicColorCorrections:list];
    else [owner setSkinToneColorCorrections:list];
}
static NSDictionary *ApplyColor(NSDictionary *request) {
    if (Gradient || MaskCommand || ColorCommand) @throw @"native operation unresolved; query its original ID";
    NSString *operationID=request[@"operation_id"]; NSDictionary *patch=request[@"patch"];
    BOOL reset=[patch[@"action"] isEqual:@"reset"], skin=[patch[@"mode"] isEqual:@"skin"], basic=[patch[@"mode"] isEqual:@"basic"];
    NSUInteger requestedType=skin?2:basic?1:0;
    if (![operationID isKindOfClass:[NSString class]] || [operationID length] != 32 ||
        (!reset && !skin && !basic && ![patch[@"mode"] isEqual:@"advanced"])) @throw @"invalid native Color Editor request";
    if (!UsedGradientIDs) UsedGradientIDs=[[NSMutableSet set] retain];
    if ([UsedGradientIDs containsObject:operationID]) @throw @"native operation ID already used";
    CheckTarget(request[@"target"]);
    id owner=ColorOwner(); NSArray *before=ColorGroups(owner);
    if (![before isEqual:request[@"expected_groups"]] || [[Required([NSClassFromString(@"CaptureOneDocument") currentDocument],@"imageCoreCommandManager") valueForKey:@"isBusy"] boolValue])
        @throw @"Color Editor state changed or host busy";
    NSMutableArray *lists=[NSMutableArray array], *planned=[NSMutableArray array];
    NSArray *setters=@[@"setAdvancedColorCorrections:",@"setBasicColorCorrections:",@"setSkinToneColorCorrections:"];
    for (NSUInteger type=0;type<3;type++) {
        BOOL change=reset || type==requestedType;
        id list=change ? BuildColorList(owner,patch,type,reset) : ColorList(owner,type);
        [lists addObject:list ?: [NSClassFromString(@"ColorCorrectionList") colorCorrectionListWithArray:@[]]];
        [planned addObject:(list ? [list toString] : nil) ?: @""];
        if (change) RequireMethod(owner,[[setters objectAtIndex:type] UTF8String],"v24@0:8@16");
    }
    CheckColorPlan(owner,lists,patch,reset,requestedType);
    CheckTarget(request[@"target"]);
    if (owner != ColorOwner() || ![ColorGroups(owner) isEqual:before]) @throw @"Color Editor changed while preparing detached lists";
    ColorCommand=[[NSMutableDictionary dictionary] retain];
    [ColorCommand setObject:operationID forKey:@"operation_id"]; [ColorCommand setObject:request[@"target"] forKey:@"target"];
    [ColorCommand setObject:patch forKey:@"patch"];
    [ColorCommand setObject:before forKey:@"before_groups"]; [ColorCommand setObject:planned forKey:@"planned_groups"];
    [ColorCommand setObject:@NO forKey:@"dispatched"]; [ColorCommand setObject:@NO forKey:@"temporal_open"];
    [ColorCommand setObject:@YES forKey:@"callback_received"]; [UsedGradientIDs addObject:operationID];
    @try {
        [ColorCommand setObject:@YES forKey:@"dispatched"]; [ColorCommand setObject:@"applying" forKey:@"state"];
        for (NSUInteger type=0;type<3;type++) if (reset || type==requestedType) SetColorList(owner,[lists objectAtIndex:type],type);
        if (owner != ColorOwner() || ![ColorGroups(owner) isEqual:planned]) @throw @"native Color Editor setter readback mismatch";
        NSDictionary *after=Target();
        NSArray *identity=@[@"pid",@"document_path",@"variant_id",@"variant_uuid",@"image_path",@"layer_uuid",@"submask_index"];
        for (NSUInteger i=0;i<[identity count];i++) { NSString *key=identity[i]; if (![after[key] isEqual:request[@"target"][key]]) @throw @"Color Editor target changed"; }
        [ColorCommand setObject:after forKey:@"after_target"]; [ColorCommand setObject:@"committed" forKey:@"state"];
    } @catch (id error) { [ColorCommand setObject:[error description] forKey:@"error"]; [ColorCommand setObject:@"needs_reconcile" forKey:@"state"]; }
    return ColorCommand;
}
static NSDictionary *ColorStatus(NSDictionary *request) {
    if (!ColorCommand || ![ColorCommand[@"operation_id"] isEqual:request[@"operation_id"]]) @throw @"unknown Color Editor operation";
    return ColorCommand;
}
static NSDictionary *ReleaseColor(NSDictionary *request) {
    ColorStatus(request);
    if (![ColorCommand[@"state"] isEqual:@"committed"]) @throw @"Color Editor operation requires reconciliation";
    NSDictionary *result=[[ColorCommand copy] autorelease]; [ColorCommand release]; ColorCommand=nil; return result;
}
