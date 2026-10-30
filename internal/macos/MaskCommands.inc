// Official AI-layer implementation and whole-root mask operations. These
// synchronous host commands already own their progress/undo behavior. Receipts
// remain until Python verifies the complete saved state and explicitly releases.
@interface NSObject (C1MaskCommands)
+ (id)shared;
- (BOOL)hasFeature:(int)feature;
- (id)segmentationMaskingJobManager;
- (id)localAdjustmentLayers;
- (void)createSubjectMaskAsNewLayerFor:(id)variants;
- (void)createBackgroundMaskAsNewLayerFor:(id)variants;
- (BOOL)hasInvertMaskSupport;
- (id)maskByInvertingMask:(id *)error;
- (id)maskByClearingMask:(id *)error;
- (id)maskByFillingMask:(id *)error;
- (void)setIsDirty:(BOOL)dirty;
@end
extern void *dlsym(void *handle, const char *name);

static NSArray *LocalMaskIDs(id variant) {
    RequireMethod(variant, "localAdjustmentLayers", "@16@0:8");
    NSArray *layers = [variant localAdjustmentLayers];
    if (!layers) return @[]; // C1 represents an untouched Background-only variant as nil.
    NSMutableArray *ids = [NSMutableArray array];
    NSMutableSet *unique = [NSMutableSet set];
    for (NSUInteger i = 0; i < [layers count]; i++) {
        NSString *uuid = [[[[layers objectAtIndex:i] mask] uuid] description];
        if (!uuid || [unique containsObject:uuid]) @throw @"invalid native layer UUID inventory";
        [unique addObject:uuid]; [ids addObject:uuid];
    }
    return ids;
}
static void CheckMaskPhoto(id doc, id variant) {
    NSDictionary *now = Target(), *before = MaskCommand[@"target"];
    NSArray *keys = @[@"pid", @"document_path", @"variant_id", @"variant_uuid", @"image_path"];
    for (NSUInteger i = 0; i < [keys count]; i++) {
        NSString *key = keys[i];
        if (![now[key] isEqual:before[key]]) @throw @"native mask photo changed";
    }
    if ([NSClassFromString(@"CaptureOneDocument") currentDocument] != doc || [doc primaryVariant] != variant)
        @throw @"native mask document object changed";
}
static NSDictionary *ApplyMaskCommand(NSDictionary *request) {
    if (Gradient || MaskCommand || ColorCommand) @throw @"native operation already exists; query its original operation ID";
    NSString *operationID = request[@"operation_id"], *kind = request[@"kind"];
    BOOL subject = [kind isEqual:@"create_subject"], background = [kind isEqual:@"create_background"];
    BOOL create = subject || background, invert = [kind isEqual:@"invert"], fill = [kind isEqual:@"fill"];
    if (![operationID isKindOfClass:[NSString class]] || [operationID length] != 32 ||
        (!create && !invert && !fill && ![kind isEqual:@"clear"])) @throw @"unsupported native mask command";
    if (!UsedGradientIDs) UsedGradientIDs = [[NSMutableSet set] retain];
    if ([UsedGradientIDs containsObject:operationID]) @throw @"native operation ID already used; never replay";
    CheckTarget(request[@"target"]);
    id doc = [NSClassFromString(@"CaptureOneDocument") currentDocument], variant = [doc primaryVariant];
    id layer = [variant activeLocalAdjustmentLayer], original = [layer mask], manager = nil;
    NSArray *oldIDs = LocalMaskIDs(variant);
    if (create) {
        id capabilities = [NSClassFromString(@"CapabilitiesManager") shared];
        int *feature = dlsym((void *)-2, "CMFeatureAIMasking"); // Darwin RTLD_DEFAULT.
        RequireMethod(capabilities, "hasFeature:", "B20@0:8i16");
        if (!feature || ![capabilities hasFeature:*feature]) @throw @"C1 AI masking feature unavailable";
        manager = Required(doc, @"segmentationMaskingJobManager");
        RequireMethod(manager, subject ? "createSubjectMaskAsNewLayerFor:" : "createBackgroundMaskAsNewLayerFor:", "v24@0:8@16");
    } else {
        if (![variant hasLayerSelected] || ![layer isKindOfClass:NSClassFromString(@"POLocalAdjLayer")] ||
            ![layer isEditable] || [layer variant] != variant || [layer hasPendingTemporalChanges] ||
            !original || [original selectedSubmaskIndex] != -1 || [[doc imageCoreCommandManager] isBusy])
            @throw @"select an idle whole adjustment-layer mask";
        if (invert && (![original hasInvertMaskSupport] || [original isComposite] ||
            ([original maskType] != 0 && [original maskType] != 1 && [original maskType] != 3 && [original maskType] != 4)))
            @throw @"whole composite or unknown-type inversion is not supported by C1";
        RequireMethod(layer, "beginTemporalChanges", "@16@0:8");
        RequireMethod(layer, "commitTemporalChangesWithUndoAction:", "v24@0:8@16");
        RequireMethod(layer, "setMask:", "v24@0:8@16");
        RequireMethod(original, invert ? "maskByInvertingMask:" : "maskByClearingMask:", "@24@0:8^@16");
        if (fill) RequireMethod(original, "maskByFillingMask:", "@24@0:8^@16");
    }
    MaskCommand = [[NSMutableDictionary dictionary] retain];
    [MaskCommand setObject:operationID forKey:@"operation_id"]; [MaskCommand setObject:kind forKey:@"kind"];
    [MaskCommand setObject:request[@"target"] forKey:@"target"]; [MaskCommand setObject:oldIDs forKey:@"before_layer_ids"];
    [MaskCommand setObject:@NO forKey:@"dispatched"]; [MaskCommand setObject:@NO forKey:@"temporal_open"];
    [MaskCommand setObject:@NO forKey:@"callback_received"]; [MaskCommand setObject:@"prepared" forKey:@"state"];
    [UsedGradientIDs addObject:operationID];
    [doc retain]; [variant retain]; [layer retain]; [original retain]; [manager retain];
    id temporary = nil;
    @try {
        CheckTarget(request[@"target"]);
        if (![LocalMaskIDs(variant) isEqual:oldIDs] || (!create && [layer mask] != original)) @throw @"native mask changed before dispatch";
        [MaskCommand setObject:@YES forKey:@"dispatched"]; [MaskCommand setObject:@"executing" forKey:@"state"];
        if (create) {
            // Exactly the NSArray argument used by LayerAppleScript. This
            // manager owns creation, progress, native save and undo naming.
            if (subject) [manager createSubjectMaskAsNewLayerFor:@[variant]];
            else [manager createBackgroundMaskAsNewLayerFor:@[variant]];
            // The manager completes generation/model application synchronously,
            // but cancellation can return normally without creating a layer.
            // Python must still prove every saved value unchanged before it
            // classifies this receipt as not_applied and releases it.
            if ([LocalMaskIDs(variant) isEqual:oldIDs]) {
                [MaskCommand setObject:@"cancelled" forKey:@"state"];
                @throw @"AI masking returned without creating a new layer";
            }
        } else {
            [MaskCommand setObject:@YES forKey:@"temporal_open"];
            [layer beginTemporalChanges]; temporary = [[layer mask] retain];
            id error = nil;
            id changed = invert ? [temporary maskByInvertingMask:&error] : [temporary maskByClearingMask:&error];
            if (!changed || error) @throw @"native mask transform failed";
            // Fill's native primitive accepts raster only. Compute clear+fill
            // on detached returned objects, then commit exactly once.
            if (fill) { changed = [changed maskByFillingMask:&error]; if (!changed || error) @throw @"native mask fill failed"; }
            CheckMaskPhoto(doc, variant);
            if ([variant activeLocalAdjustmentLayer] != layer || [layer mask] != temporary) @throw @"native mask target changed";
            [changed setIsDirty:YES];
            [MaskCommand setObject:@"applying" forKey:@"state"]; [layer setMask:changed];
            [MaskCommand setObject:@"committing" forKey:@"state"];
            [layer commitTemporalChangesWithUndoAction:invert ? @"Invert Mask" : fill ? @"Fill Mask" : @"Clear Mask"];
            [MaskCommand setObject:@NO forKey:@"temporal_open"];
        }
        CheckMaskPhoto(doc, variant);
        NSArray *newIDs = LocalMaskIDs(variant);
        if (create) {
            if ([newIDs count] != [oldIDs count] + 1 || ![variant hasLayerSelected]) @throw @"AI mask did not create exactly one selected layer";
            NSString *added = [[[[variant activeLocalAdjustmentLayer] mask] uuid] description];
            NSMutableArray *remaining = [NSMutableArray array];
            for (NSUInteger i = 0; i < [newIDs count]; i++) if (![[newIDs objectAtIndex:i] isEqual:added]) [remaining addObject:[newIDs objectAtIndex:i]];
            if (![remaining isEqual:oldIDs]) @throw @"AI creation changed existing layer identity/order";
            [MaskCommand setObject:added forKey:@"created_mask_uuid"];
        } else if (![newIDs isEqual:oldIDs] || [variant activeLocalAdjustmentLayer] != layer ||
                   ![[[[layer mask] uuid] description] isEqual:request[@"target"][@"layer_uuid"]]) @throw @"mask identity changed";
        [MaskCommand setObject:Target() forKey:@"after_target"];
        [MaskCommand setObject:@"committed" forKey:@"state"];
    } @catch (id error) {
        [MaskCommand setObject:[error description] forKey:@"error"];
        BOOL cancelled = create && [MaskCommand[@"state"] isEqual:@"cancelled"];
        if (!create && [MaskCommand[@"state"] isEqual:@"executing"] && temporary) @try {
            CheckMaskPhoto(doc, variant);
            if ([variant activeLocalAdjustmentLayer] != layer || [layer mask] != temporary) @throw @"temporary mask changed";
            [layer cancelTemporalChanges]; [MaskCommand setObject:@NO forKey:@"temporal_open"]; cancelled = YES;
        } @catch (id ignored) { }
        [MaskCommand setObject:![MaskCommand[@"dispatched"] boolValue] ? @"rejected" : cancelled ? @"cancelled" : @"needs_reconcile" forKey:@"state"];
    }
    // No callback escapes this synchronous high-level command.
    [MaskCommand setObject:@YES forKey:@"callback_received"];
    [temporary release]; [manager release]; [original release]; [layer release]; [variant release]; [doc release];
    return MaskCommand;
}
static NSDictionary *MaskCommandStatus(NSDictionary *request) {
    if (!MaskCommand || ![MaskCommand[@"operation_id"] isEqual:request[@"operation_id"]]) @throw @"unknown native mask operation ID";
    return MaskCommand;
}
static NSDictionary *ReleaseMaskCommand(NSDictionary *request) {
    MaskCommandStatus(request);
    if ((![MaskCommand[@"state"] isEqual:@"committed"] && ![MaskCommand[@"state"] isEqual:@"cancelled"] &&
         ![MaskCommand[@"state"] isEqual:@"rejected"]) || [MaskCommand[@"temporal_open"] boolValue] ||
        ![MaskCommand[@"callback_received"] boolValue]) @throw @"native mask command is unresolved";
    NSDictionary *result = [[MaskCommand copy] autorelease];
    [MaskCommand release]; MaskCommand = nil;
    return result;
}
