// Synchronous C1 model actions used by its own layer commands. They share the
// mask receipt slot, so an unverified action cannot be replayed or overlapped.
@interface NSObject (C1LayerActions)
- (id)duplicateSelectedLayer:(id)layer alertUserProvider:(id)provider;
- (void)setName:(id)name;
- (void)moveLocalAdjustmentLayerFromIndex:(NSUInteger)source toIndex:(NSUInteger)destination;
- (id)createAndSelectSubmaskWithMaskContents:(id)contents newSubmaskName:(id)name
    blendMode:(int)mode selectNewSubmask:(BOOL)select;
- (void)changeActiveLocalAdjustmentLayer:(id)layer newActiveSubmaskIndex:(NSInteger)index updateCursor:(BOOL)update;
- (void)deleteCurrentLayerOrSubmask;
- (NSInteger)submaskCount;
- (id)nameOfSubmaskAtIndex:(NSInteger)index;
- (void)createSubjectMaskAsSubmaskFor:(id)variants compositionMode:(int)mode;
- (void)createBackgroundMaskAsSubmaskFor:(id)variants compositionMode:(int)mode;
@end

static NSArray *ReversedLayerIDs(NSArray *ids) {
    NSMutableArray *result = [NSMutableArray array];
    for (NSUInteger i = [ids count]; i > 0; i--) [result addObject:ids[i - 1]];
    return result;
}
static NSUInteger LayerUUIDIndex(NSArray *ids, NSString *uuid) {
    for (NSUInteger i = 0; i < [ids count]; i++) if ([ids[i] isEqual:uuid]) return i;
    @throw @"requested native layer UUID is absent";
}
static NSDictionary *ApplyLayerAction(NSDictionary *request) {
    if (Gradient || MaskCommand || ColorCommand) @throw @"native operation already exists; query its original operation ID";
    NSString *operationID = request[@"operation_id"], *kind = request[@"kind"];
    BOOL duplicate = [kind isEqual:@"duplicate"], move = [kind isEqual:@"move"];
    BOOL combine = [kind isEqual:@"combine"], select = [kind isEqual:@"select_submask"];
    BOOL remove = [kind isEqual:@"delete_submask"], ai = [kind isEqual:@"combine_ai"];
    if (![operationID isKindOfClass:[NSString class]] || [operationID length] != 32 ||
        (!duplicate && !move && !combine && !select && !remove && !ai)) @throw @"unsupported native layer action";
    if (!UsedGradientIDs) UsedGradientIDs = [[NSMutableSet set] retain];
    if ([UsedGradientIDs containsObject:operationID]) @throw @"native operation ID already used; never replay";
    CheckTarget(request[@"target"]);
    id doc = [NSClassFromString(@"CaptureOneDocument") currentDocument], variant = [doc primaryVariant];
    id layer = [variant activeLocalAdjustmentLayer], original = [layer mask], source = nil, sourceMask = nil, manager = nil;
    NSArray *oldIDs = LocalMaskIDs(variant), *expected = request[@"expected_layer_ids"];
    if (![expected isKindOfClass:[NSArray class]] || ![ReversedLayerIDs(expected) isEqual:oldIDs])
        @throw @"native layer UUID order differs from the complete observed inventory";
    if (![variant hasLayerSelected] || ![layer isKindOfClass:NSClassFromString(@"POLocalAdjLayer")] ||
        ![variant isEditable] || ![layer isEditable] || [layer variant] != variant ||
        [layer hasPendingTemporalChanges] || !original || [[doc imageCoreCommandManager] isBusy])
        @throw @"select an idle editable local layer";
    RequireMethod(original, "submaskCount", "q16@0:8");
    NSInteger index = 0, count = [original submaskCount];
    NSUInteger fromIndex = LayerUUIDIndex(oldIDs, request[@"target"][@"layer_uuid"]), toIndex = fromIndex;
    int blend = 0;
    NSArray *afterIDs = oldIDs;
    if (!select && !remove && [original selectedSubmaskIndex] != -1) @throw @"select the whole parent layer";
    if (combine || ai) {
        NSString *mode = request[@"mode"];
        // C1's SubmaskCreationMenuHelper maps add/subtract/intersect to these
        // ImageCore composition modes before calling the same manager.
        if ([mode isEqual:@"subtract"]) blend = 5;
        else if ([mode isEqual:@"intersect"]) blend = 2;
        else if (![mode isEqual:@"add"]) @throw @"unsupported mask combination mode";
        if (count >= 64) @throw @"mask has no room for another qualified component";
        RequireMethod(original, "nameOfSubmaskAtIndex:", "@24@0:8q16");
    }
    if (duplicate) {
        if ([oldIDs count] >= 16) @throw @"C1 local layer limit reached";
        id name = request[@"name"];
        if (name && (![name isKindOfClass:[NSString class]] || ![name length])) @throw @"nonempty copied layer name required";
        RequireMethod(variant, "duplicateSelectedLayer:alertUserProvider:", "@32@0:8@16@24");
        if (name) RequireMethod(layer, "setName:", "v24@0:8@16");
    } else if (move) {
        NSArray *requested = request[@"expected_after_layer_ids"];
        if (![requested isKindOfClass:[NSArray class]] || [requested count] != [oldIDs count])
            @throw @"invalid requested native layer order";
        afterIDs = ReversedLayerIDs(requested);
        toIndex = LayerUUIDIndex(afterIDs, request[@"target"][@"layer_uuid"]);
        NSMutableArray *remaining = [NSMutableArray array], *wanted = [NSMutableArray array];
        for (NSUInteger i = 0; i < [oldIDs count]; i++) if (i != fromIndex) [remaining addObject:oldIDs[i]];
        for (NSUInteger i = 0; i < [afterIDs count]; i++) if (i != toIndex) [wanted addObject:afterIDs[i]];
        if (![remaining isEqual:wanted]) @throw @"requested order moves more than the selected UUID";
        RequireMethod(variant, "moveLocalAdjustmentLayerFromIndex:toIndex:", "v32@0:8Q16Q24");
    } else if (combine) {
        NSString *name = request[@"submask_name"];
        if (![name isKindOfClass:[NSString class]] || ![name length]) @throw @"submask name required";
        source = [[variant localAdjustmentLayers] objectAtIndex:LayerUUIDIndex(oldIDs, request[@"source_uuid"])];
        sourceMask = [source mask];
        if (source == layer || [source variant] != variant || [source hasPendingTemporalChanges] ||
            !sourceMask || [sourceMask isComposite] ||
            ([sourceMask maskType] != 0 && [sourceMask maskType] != 3 && [sourceMask maskType] != 4))
            @throw @"use a different observed simple source mask";
        RequireMethod(variant, "createAndSelectSubmaskWithMaskContents:newSubmaskName:blendMode:selectNewSubmask:", "@40@0:8@16@24i32B36");
    } else if (ai) {
        NSString *aiKind = request[@"ai_kind"];
        if (![aiKind isEqual:@"subject"] && ![aiKind isEqual:@"background"]) @throw @"unsupported AI mask source";
        id capabilities = [NSClassFromString(@"CapabilitiesManager") shared];
        int *feature = dlsym((void *)-2, "CMFeatureAIMasking");
        RequireMethod(capabilities, "hasFeature:", "B20@0:8i16");
        if (!feature || ![capabilities hasFeature:*feature]) @throw @"C1 AI masking feature unavailable";
        manager = Required(doc, @"segmentationMaskingJobManager");
        RequireMethod(manager, [aiKind isEqual:@"subject"] ? "createSubjectMaskAsSubmaskFor:compositionMode:" :
            "createBackgroundMaskAsSubmaskFor:compositionMode:", "v28@0:8@16i24");
    } else {
        id requested = request[@"index"];
        if (![requested isKindOfClass:NSClassFromString(@"NSNumber")] || [requested doubleValue] != [requested integerValue])
            @throw @"integer submask index required";
        index = [requested integerValue];
        if (index < -1 || (index >= 0 && index >= count) || (remove && (index < 0 || count < 2)))
            @throw @"submask index is outside the observed parent";
        if (remove && [original selectedSubmaskIndex] != index) @throw @"select the exact child before deleting";
        RequireMethod(variant, select ? "changeActiveLocalAdjustmentLayer:newActiveSubmaskIndex:updateCursor:" :
            "deleteCurrentLayerOrSubmask", select ? "v36@0:8@16q24B32" : "v16@0:8");
    }
    MaskCommand = [[NSMutableDictionary dictionary] retain];
    [MaskCommand setObject:operationID forKey:@"operation_id"]; [MaskCommand setObject:kind forKey:@"kind"];
    [MaskCommand setObject:request[@"target"] forKey:@"target"]; [MaskCommand setObject:oldIDs forKey:@"before_layer_ids"];
    [MaskCommand setObject:request forKey:@"arguments"];
    [MaskCommand setObject:@NO forKey:@"dispatched"]; [MaskCommand setObject:@NO forKey:@"temporal_open"];
    [MaskCommand setObject:@NO forKey:@"callback_received"]; [MaskCommand setObject:@"prepared" forKey:@"state"];
    [UsedGradientIDs addObject:operationID];
    [doc retain]; [variant retain]; [layer retain]; [original retain]; [source retain]; [sourceMask retain]; [manager retain];
    @try {
        CheckTarget(request[@"target"]);
        if (![LocalMaskIDs(variant) isEqual:oldIDs] || [layer mask] != original ||
            (source && [source mask] != sourceMask)) @throw @"native layer contents changed before dispatch";
        [MaskCommand setObject:@YES forKey:@"dispatched"]; [MaskCommand setObject:@"executing" forKey:@"state"];
        if (duplicate) {
            // C1 explicitly accepts nil alert providers. Preflight enforces its
            // editable/count checks without displaying a modal alert.
            id copied = [variant duplicateSelectedLayer:layer alertUserProvider:nil];
            if (!copied || [variant activeLocalAdjustmentLayer] != copied) @throw @"native layer copy returned no selected layer";
            if (request[@"name"]) [copied setName:request[@"name"]];
        } else if (move) {
            if (fromIndex != toIndex) [variant moveLocalAdjustmentLayerFromIndex:fromIndex toIndex:toIndex];
        } else if (combine) {
            if (![variant createAndSelectSubmaskWithMaskContents:sourceMask newSubmaskName:request[@"submask_name"]
                    blendMode:blend selectNewSubmask:YES]) @throw @"native combination returned no component";
        } else if (ai) {
            // Exactly the single-variant NSArray used by C1's menu helper.
            // The manager owns synchronous generation, progress and undo; its
            // submask applier passes selectNewSubmask:NO and preserves parent selection.
            if ([request[@"ai_kind"] isEqual:@"subject"]) [manager createSubjectMaskAsSubmaskFor:@[variant] compositionMode:blend];
            else [manager createBackgroundMaskAsSubmaskFor:@[variant] compositionMode:blend];
        } else if (select) {
            [variant changeActiveLocalAdjustmentLayer:layer newActiveSubmaskIndex:index updateCursor:NO];
        } else {
            // This C1 command deletes an entire layer when selection is -1.
            // Bind the live child immediately before entering its native path.
            if ([variant activeLocalAdjustmentLayer] != layer || [[layer mask] selectedSubmaskIndex] != index)
                @throw @"selected child changed before deletion";
            [variant deleteCurrentLayerOrSubmask];
        }
        CheckMaskPhoto(doc, variant);
        NSArray *newIDs = LocalMaskIDs(variant);
        id active = [variant activeLocalAdjustmentLayer];
        if (duplicate) {
            if ([newIDs count] != [oldIDs count] + 1 || active == layer) @throw @"copy did not create one selected layer";
            NSString *added = [[[[variant activeLocalAdjustmentLayer] mask] uuid] description];
            NSMutableArray *remaining = [NSMutableArray array];
            for (NSUInteger i = 0; i < [newIDs count]; i++) if (![newIDs[i] isEqual:added]) [remaining addObject:newIDs[i]];
            if (![remaining isEqual:oldIDs]) @throw @"copy changed existing layer identity/order";
            [MaskCommand setObject:added forKey:@"created_mask_uuid"];
        } else {
            // Reordering replaces the immutable variant and may refresh layer
            // wrappers. Its UUID and complete requested order remain binding.
            if (![newIDs isEqual:afterIDs] || (!move && active != layer) ||
                ![[[[active mask] uuid] description] isEqual:request[@"target"][@"layer_uuid"]])
                @throw @"native layer identity/order changed unexpectedly";
            if (select && [[layer mask] selectedSubmaskIndex] != index) @throw @"native child selection did not match";
            if (ai && [[layer mask] submaskCount] == count) {
                // This only records that the synchronous manager returned
                // without appending. Python must prove complete unchanged
                // saved state and live selection before releasing as not_applied.
                [MaskCommand setObject:@"cancelled" forKey:@"state"];
                [MaskCommand setObject:@"AI command returned without appending a component" forKey:@"error"];
            } else if (combine || ai) {
                NSInteger appended = [[layer mask] submaskCount] - 1;
                if (appended != (count ? count : 1) || [[layer mask] selectedSubmaskIndex] != (ai ? -1 : appended) ||
                    (source && [source mask] != sourceMask))
                    @throw @"native combination did not append/select exactly one component";
                [MaskCommand setObject:@(appended) forKey:@"appended_index"];
                [MaskCommand setObject:[[layer mask] nameOfSubmaskAtIndex:appended] forKey:@"submask_name"];
            }
        }
        if ([active hasPendingTemporalChanges] || [layer hasPendingTemporalChanges]) @throw @"native action left temporal changes pending";
        [MaskCommand setObject:Target() forKey:@"after_target"];
        [MaskCommand setObject:newIDs forKey:@"after_layer_ids"];
        if (![MaskCommand[@"state"] isEqual:@"cancelled"]) [MaskCommand setObject:@"committed" forKey:@"state"];
    } @catch (id error) {
        [MaskCommand setObject:[error description] forKey:@"error"];
        [MaskCommand setObject:@([layer hasPendingTemporalChanges]) forKey:@"temporal_open"];
        [MaskCommand setObject:[MaskCommand[@"dispatched"] boolValue] ? @"needs_reconcile" : @"rejected" forKey:@"state"];
    }
    [MaskCommand setObject:@YES forKey:@"callback_received"];
    [manager release]; [sourceMask release]; [source release]; [original release]; [layer release]; [variant release]; [doc release];
    return MaskCommand;
}
