// Same native temporal/undo chain as the verified Frida draft, compiled into
// the renderer so callbacks survive a Python disconnect. No injected JS runtime
// is retained. This file is included after Target/CheckTarget.
typedef struct IC_POINTF { float x, y; } ICPoint;
typedef struct { ICPoint start, stop; float midpoint; } LinearParameters;
typedef struct { ICPoint center, horizontal, vertical; float falloff; int inverted; } RadialParameters;
@interface NSObject (C1Gradients)
- (id)imageCoreCommandManager;
- (id)variant;
- (BOOL)isEditable;
- (BOOL)isBusy;
- (BOOL)hasPendingTemporalChanges;
- (id)beginTemporalChanges;
- (void)cancelTemporalChanges;
- (void)commitTemporalChangesWithUndoAction:(id)name;
- (void)setMask:(id)mask;
- (id)handle;
- (int)maskType;
- (BOOL)isComposite;
- (NSUInteger)submaskCount;
- (BOOL)hasLumaRange;
- (BOOL)isLinearGradient;
- (BOOL)isRadialGradient;
- (void)getMaskIsConstant:(int *)constant density:(float *)density;
- (LinearParameters *)linearGradientParameters;
- (RadialParameters *)radialGradientParameters;
- (id)maskByUpdatingOrAddingComponent:(id)handle error:(id *)error;
- (id)maskByRemovingAIMetadata:(id *)error;
- (void)linearGradientMask:(id)mask parameters:(LinearParameters)parameters completion:(void (^)(id, LinearParameters))completion;
- (void)radialGradientMask:(id)mask parameters:(RadialParameters)parameters completion:(void (^)(id, RadialParameters))completion;
@end

static NSMutableDictionary *Gradient;
static NSMutableSet *UsedGradientIDs;
static id GDocument, GVariant, GLayer, GOriginal, GTemporary, GManager;

static void RequireMethod(id object, const char *selector, const char *encoding) {
    void *method = class_getInstanceMethod(object_getClass(object), sel_registerName(selector));
    if (!method || strcmp(method_getTypeEncoding(method), encoding))
        @throw [NSString stringWithFormat:@"unsupported native method encoding: %s", selector];
}
static NSArray *LinearArray(LinearParameters p) {
    return @[@(p.start.x), @(p.start.y), @(p.stop.x), @(p.stop.y), @(p.midpoint)];
}
static NSArray *RadialArray(RadialParameters p) {
    return @[@(p.center.x), @(p.center.y), @(p.horizontal.x), @(p.horizontal.y),
             @(p.vertical.x), @(p.vertical.y), @(p.falloff), @(p.inverted)];
}
static NSArray *GradientParameters(id mask) {
    if ([Gradient[@"kind"] isEqual:@"linear_gradient"] && [mask isLinearGradient]) {
        LinearParameters *p = [mask linearGradientParameters];
        if (p) return LinearArray(*p);
    }
    if ([Gradient[@"kind"] isEqual:@"radial_gradient"] && [mask isRadialGradient]) {
        RadialParameters *p = [mask radialGradientParameters];
        if (p) return RadialArray(*p);
    }
    @throw @"native result is not the requested parameter gradient";
}
static void CheckGradientBinding(BOOL temporary) {
    NSDictionary *current = Target(), *expected = Gradient[@"target"];
    NSArray *keys = @[@"pid", @"document_path", @"variant_id", @"variant_uuid", @"image_path"];
    for (NSUInteger i = 0; i < [keys count]; i++) {
        NSString *key = keys[i];
        if (![current[key] isEqual:expected[key]]) @throw @"gradient photo changed";
    }
    id doc = [NSClassFromString(@"CaptureOneDocument") currentDocument];
    if (doc != GDocument || [doc primaryVariant] != GVariant ||
        [GVariant activeLocalAdjustmentLayer] != GLayer || [GLayer variant] != GVariant ||
        ![GLayer isEditable] || (temporary && [GLayer mask] != GTemporary))
        @throw @"gradient layer or temporary mask changed";
}
static void CompleteGradient(id handle, NSArray *actual) {
    // Native completion is on a worker; retain arguments across the main queue.
    [handle retain]; [actual retain];
    dispatch_async(dispatch_get_main_queue(), ^{
        @autoreleasepool {
            @try {
                if ([Gradient[@"callback_received"] boolValue]) @throw @"duplicate native completion";
                [Gradient setObject:@YES forKey:@"callback_received"];
                CheckGradientBinding(YES);
                if (![GLayer hasPendingTemporalChanges] || !handle || ![actual isEqual:Gradient[@"parameters"]])
                    @throw @"native computation changed requested parameters or lost temporal scope";
                id error = nil;
                id mask = [GTemporary maskByUpdatingOrAddingComponent:handle error:&error];
                if (!mask || error) @throw @"native mask composition failed";
                mask = [mask maskByRemovingAIMetadata:&error];
                if (!mask || error || ![GradientParameters(mask) isEqual:Gradient[@"parameters"]])
                    @throw @"native gradient parameter readback mismatch";
                [Gradient setObject:@"applying" forKey:@"state"];
                [GLayer setMask:mask];
                [Gradient setObject:@"committing" forKey:@"state"];
                [GLayer commitTemporalChangesWithUndoAction:[Gradient[@"kind"] isEqual:@"linear_gradient"] ?
                    @"Create Linear Gradient Mask" : @"Create Radial Gradient Mask"];
                [Gradient setObject:@NO forKey:@"temporal_open"];
                CheckGradientBinding(NO);
                if (![[[[GLayer mask] uuid] description] isEqual:Gradient[@"mask_uuid"]] ||
                    ![GradientParameters([GLayer mask]) isEqual:Gradient[@"parameters"]])
                    @throw @"committed mask identity or parameters changed";
                [Gradient setObject:actual forKey:@"actual_parameters"];
                [Gradient setObject:@"committed" forKey:@"state"];
            } @catch (id error) {
                [Gradient setObject:[error description] forKey:@"error"];
                BOOL cancelled = NO;
                // Cancel only the uncommitted temporary change on the same target.
                if ([Gradient[@"state"] isEqual:@"pending"]) @try {
                    CheckGradientBinding(YES); [GLayer cancelTemporalChanges];
                    [Gradient setObject:@NO forKey:@"temporal_open"]; cancelled = YES;
                } @catch (id ignored) { }
                [Gradient setObject:cancelled ? @"cancelled" : @"needs_reconcile" forKey:@"state"];
            }
            [actual release]; [handle release];
        }
    });
}
static NSDictionary *CreateGradient(NSDictionary *request) {
    if (Gradient || MaskCommand || ColorCommand) @throw @"native operation already exists; query its original operation ID";
    NSString *operationID = request[@"operation_id"], *kind = request[@"kind"];
    if (![operationID isKindOfClass:[NSString class]] || [operationID length] != 32 ||
        (![kind isEqual:@"linear_gradient"] && ![kind isEqual:@"radial_gradient"])) @throw @"invalid gradient request";
    if (!UsedGradientIDs) UsedGradientIDs = [[NSMutableSet set] retain];
    if ([UsedGradientIDs containsObject:operationID]) @throw @"gradient operation ID already used; never replay";
    CheckTarget(request[@"target"]);
    id doc = [NSClassFromString(@"CaptureOneDocument") currentDocument], variant = [doc primaryVariant];
    id layer = [variant activeLocalAdjustmentLayer], mask = [layer mask], manager = [doc imageCoreCommandManager];
    if (!layer || !mask || !manager || ![layer isKindOfClass:NSClassFromString(@"POLocalAdjLayer")] ||
        ![mask isKindOfClass:NSClassFromString(@"LocalAdjustmentMask")] || ![layer isEditable] ||
        [layer variant] != variant || ![[[mask uuid] description] isEqual:request[@"mask_uuid"]] ||
        [manager isBusy] || [layer hasPendingTemporalChanges] || [mask isComposite] || [mask submaskCount] ||
        [mask hasLumaRange] || [mask selectedSubmaskIndex] != -1) @throw @"requires idle whole simple local mask";
    BOOL linear = [kind isEqual:@"linear_gradient"];
    BOOL editing = [mask maskType] == (linear ? 3 : 4);
    if ([mask maskType] != 0 && !editing) @throw @"gradient updates require the same existing gradient type";
    RequireMethod(layer, "beginTemporalChanges", "@16@0:8");
    RequireMethod(layer, "setMask:", "v24@0:8@16");
    RequireMethod(layer, "commitTemporalChangesWithUndoAction:", "v24@0:8@16");
    RequireMethod(layer, "cancelTemporalChanges", "v16@0:8");
    RequireMethod(mask, "getMaskIsConstant:density:", "v32@0:8o^i16o^f24");
    RequireMethod(mask, "handle", "@16@0:8");
    RequireMethod(mask, "maskByUpdatingOrAddingComponent:error:", "@32@0:8@16^@24");
    RequireMethod(mask, "maskByRemovingAIMetadata:", "@24@0:8^@16");
    RequireMethod(manager, linear ? "linearGradientMask:parameters:completion:" : "radialGradientMask:parameters:completion:",
        linear ? "v52@0:8@16{?={IC_POINTF=ff}{IC_POINTF=ff}f}24@?44" : "v64@0:8@16{?={IC_POINTF=ff}{IC_POINTF=ff}{IC_POINTF=ff}fi}24@?56");
    int constant = 0; float density = -1;
    [mask getMaskIsConstant:&constant density:&density];
    if (!editing && (constant != 1 || density != 0)) @throw @"mask is not proven constant empty";
    NSArray *values = request[@"parameters"];
    if (![values isKindOfClass:[NSArray class]] || [values count] != (linear ? 5 : 8)) @throw @"invalid gradient parameters";
    float p[8] = {0};
    for (NSUInteger i = 0; i < [values count]; i++) {
        if (![values[i] isKindOfClass:NSClassFromString(@"NSNumber")]) @throw @"gradient parameter is not numeric";
        p[i] = [values[i] doubleValue];
        if (!__builtin_isfinite(p[i])) @throw @"nonfinite gradient parameter";
    }
    if (linear && ((p[0] == p[2] && p[1] == p[3]) || p[4] <= 0 || p[4] >= 1)) @throw @"degenerate linear gradient";
    if (!linear && (p[6] < 0 || p[6] > 1 || (p[7] != 0 && p[7] != 1) ||
        p[2]*p[2]+p[3]*p[3] < 1 || p[4]*p[4]+p[5]*p[5] < 1)) @throw @"degenerate radial gradient";
    LinearParameters lp = {{p[0],p[1]},{p[2],p[3]},p[4]};
    RadialParameters rp = {{p[0],p[1]},{p[2],p[3]},{p[4],p[5]},p[6],(int)p[7]};
    Gradient = [[NSMutableDictionary dictionary] retain];
    [Gradient setObject:operationID forKey:@"operation_id"]; [Gradient setObject:request[@"target"] forKey:@"target"];
    [Gradient setObject:request[@"mask_uuid"] forKey:@"mask_uuid"]; [Gradient setObject:kind forKey:@"kind"];
    [Gradient setObject:@(editing) forKey:@"editing"];
    [Gradient setObject:linear ? LinearArray(lp) : RadialArray(rp) forKey:@"parameters"];
    [Gradient setObject:@NO forKey:@"dispatched"]; [Gradient setObject:@NO forKey:@"callback_received"];
    [Gradient setObject:@NO forKey:@"temporal_open"]; [Gradient setObject:@"prepared" forKey:@"state"];
    [UsedGradientIDs addObject:operationID];
    GDocument = [doc retain]; GVariant = [variant retain]; GLayer = [layer retain];
    GOriginal = [mask retain]; GManager = [manager retain];
    @try {
        CheckTarget(request[@"target"]);
        if ([layer mask] != mask) @throw @"mask changed before native command";
        [Gradient setObject:@YES forKey:@"dispatched"]; [Gradient setObject:@YES forKey:@"temporal_open"];
        [Gradient setObject:@"beginning" forKey:@"state"];
        [layer beginTemporalChanges]; GTemporary = [[layer mask] retain];
        id handle = [GTemporary handle]; if (!handle) @throw @"native mask handle unavailable";
        [Gradient setObject:@"pending" forKey:@"state"];
        if (linear) [manager linearGradientMask:handle parameters:lp completion:^(id result, LinearParameters actual) {
            CompleteGradient(result, LinearArray(actual));
        }];
        else [manager radialGradientMask:handle parameters:rp completion:^(id result, RadialParameters actual) {
            CompleteGradient(result, RadialArray(actual));
        }];
    } @catch (id error) {
        [Gradient setObject:[error description] forKey:@"error"];
        [Gradient setObject:[Gradient[@"dispatched"] boolValue] ? @"needs_reconcile" : @"rejected" forKey:@"state"];
    }
    return Gradient;
}
static NSDictionary *GradientStatus(NSDictionary *request) {
    if (!Gradient || ![Gradient[@"operation_id"] isEqual:request[@"operation_id"]]) @throw @"unknown gradient operation ID";
    return Gradient;
}
static NSDictionary *ReleaseGradient(NSDictionary *request) {
    GradientStatus(request);
    if ((![Gradient[@"state"] isEqual:@"committed"] && ![Gradient[@"state"] isEqual:@"cancelled"] &&
         ![Gradient[@"state"] isEqual:@"rejected"]) || [Gradient[@"temporal_open"] boolValue] ||
        ([Gradient[@"dispatched"] boolValue] && ![Gradient[@"callback_received"] boolValue]) || [GManager isBusy])
        @throw @"gradient remains unresolved; retain native state and query original ID";
    NSDictionary *result = [[Gradient copy] autorelease];
    [GTemporary release]; [GOriginal release]; [GManager release]; [GLayer release]; [GVariant release]; [GDocument release];
    GTemporary = GOriginal = GManager = GLayer = GVariant = GDocument = nil;
    [Gradient release]; Gradient = nil;
    return result;
}
