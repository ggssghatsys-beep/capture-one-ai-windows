// Read only Viewer-cache fast path. Include after AnalysisSnapshot(). No host
// object survives this main-queue call; this never schedules a new render.
@interface NSObject (C1ViewerCache)
- (id)window;
- (id)contentView;
- (id)subviews;
- (id)imageRenderer;
- (id)entireCanvasPreviewJob;
- (id)latestPreviewOrBestLayerJob;
- (id)cachedJobForVariant:(id)variant checkCanvasRect:(BOOL)check;
- (BOOL)isUpToDateJob:(id)job;
- (NSInteger)processCounter;
- (NSInteger)changeVersion;
- (NSUInteger)overlayFlags;
- (CGRect)cropRect;
- (id)syncProcessCounterFromVariant;
- (id)displayProfilePath;
- (id)proofProfilePath;
@end

static double DisplayAbs(double value) { return value<0?-value:value; }
static BOOL DisplaySameRect(CGRect a, CGRect b) {
    // Both originate in C1's float canvas geometry. Never accept a viewport
    // intersection as full-image coverage merely because isCoveringJob says so.
    return DisplayAbs(a.origin.x-b.origin.x)<=0.001 && DisplayAbs(a.origin.y-b.origin.y)<=0.001 &&
        DisplayAbs(a.size.width-b.size.width)<=0.001 && DisplayAbs(a.size.height-b.size.height)<=0.001;
}
static BOOL DisplayJobValid(id job, id renderer, id variant, CGRect crop, NSDictionary *target) {
    if (!job || ![job isKindOfClass:NSClassFromString(@"ICPackageBitmapJob")] ||
        ![renderer isKindOfClass:NSClassFromString(@"VariantBitmapViewICRenderer")]) return NO;
    RequireMethod(job,"variantUUID","@16@0:8");
    RequireMethod(job,"processCounter","q16@0:8");
    RequireMethod(job,"overlayFlags","Q16@0:8");
    RequireMethod(job,"cropRect","{CGRect={CGPoint=dd}{CGSize=dd}}16@0:8");
    RequireMethod(job,"syncProcessCounterFromVariant","@16@0:8");
    RequireMethod(job,"completed","B16@0:8");
    RequireMethod(renderer,"isUpToDateJob:","B24@0:8@16");
    if ([renderer variant] != variant || ![job completed] || [job resultCode] != 0 || [job overlayFlags] != 0 ||
        ![[job variantUUID] isEqual:target[@"variant_uuid"]] || ![renderer isUpToDateJob:job] ||
        !DisplaySameRect([job cropRect],crop)) return NO;
    NSArray *revision=target[@"revision"];
    NSInteger total=[revision[0] integerValue]+[revision[1] integerValue]+[revision[2] integerValue];
    // These are C1's own cache freshness fields. Target's full three-counter
    // tuple is additionally checked before and after accessing the cache.
    return [job processCounter]==total && [[job syncProcessCounterFromVariant] isEqual:revision[2]];
}
static id DisplayFindJob(id view, id variant, CGRect crop, NSDictionary *target,
                         NSUInteger *visited, NSUInteger depth, id *foundRenderer) {
    if (!view || depth>32 || ++*visited>4096) return nil;
    if ([view isKindOfClass:NSClassFromString(@"VariantBitmapView")]) {
        RequireMethod(view,"imageRenderer","@16@0:8");
        id renderer=[view imageRenderer];
        if (renderer && [renderer isKindOfClass:NSClassFromString(@"VariantBitmapViewICRenderer")] && [renderer variant]==variant) {
            RequireMethod(renderer,"latestPreviewOrBestLayerJob","@16@0:8");
            id job=[renderer latestPreviewOrBestLayerJob];
            if (DisplayJobValid(job,renderer,variant,crop,target)) { *foundRenderer=renderer; return job; }
            RequireMethod(renderer,"cachedJobForVariant:checkCanvasRect:","@28@0:8@16B24");
            job=[renderer cachedJobForVariant:variant checkCanvasRect:YES];
            if (DisplayJobValid(job,renderer,variant,crop,target)) { *foundRenderer=renderer; return job; }
        }
    }
    if (![view respondsToSelector:sel_registerName("subviews")]) return nil;
    NSArray *children=[view subviews];
    for (NSUInteger i=0;i<[children count] && *visited<4096;i++) {
        id job=DisplayFindJob(children[i],variant,crop,target,visited,depth+1,foundRenderer);
        if (job) return job;
    }
    return nil;
}
static NSDictionary *DisplayJobStamp(id job,id renderer,CGRect crop) {
    RequireMethod(renderer,"changeVersion","q16@0:8");
    RequireMethod(job,"changeVersion","q16@0:8");
    return @{@"available":@YES,@"renderer_change_version":@([renderer changeVersion]),
        @"job_change_version":@([job changeVersion]),@"proof_profile":[job proofProfilePath]?:@"",
        @"display_profile":[job displayProfilePath]?:@"",@"overlay_flags":@((NSInteger)[job overlayFlags]),
        @"canvas_crop":@[@(crop.origin.x),@(crop.origin.y),@(crop.size.width),@(crop.size.height)]};
}
// Metadata only. This deliberately does not call AnalysisSnapshot: the parent
// adds it to that snapshot, and exact analysis can ignore this display stamp.
static NSDictionary *ViewerStamp(void) {
    NSDictionary *target=Target();
    id doc=[NSClassFromString(@"CaptureOneDocument") currentDocument],variant=[doc primaryVariant];
    CGRect crop=[[variant geometryTransformer] canvasCropRect];
    id window=[doc window]; if (!window) return @{@"available":@NO};
    NSUInteger visited=0; id renderer=nil;
    id job=DisplayFindJob([window contentView],variant,crop,target,&visited,0,&renderer);
    NSDictionary *stamp=job?DisplayJobStamp(job,renderer,crop):@{@"available":@NO};
    CheckTarget(target);
    return stamp;
}
static NSDictionary *DisplayUnavailable(NSString *reason, NSDictionary *target) {
    CheckTarget(target); AnalysisSnapshot();
    return @{@"phase":@"unavailable",@"reason":reason};
}
static NSDictionary *DisplayCache(NSDictionary *request) {
    NSDictionary *target=request[@"target"];
    CheckTarget(target);
    NSDictionary *identity=AnalysisSnapshot();
    if (![identity[@"target"] isEqual:target]) @throw @"Viewer cache identity changed";
    NSInteger requested=[request[@"max_size"] integerValue];
    if (requested<128 || requested>1600) @throw @"Viewer cache size must be 128..1600";
    NSUInteger maximum=(NSUInteger)(requested>640?640:requested);
    double started=CFAbsoluteTimeGetCurrent();
    id doc=[NSClassFromString(@"CaptureOneDocument") currentDocument],variant=[doc primaryVariant];
    id geometry=Required(variant,@"geometryTransformer");
    CGRect crop=[geometry canvasCropRect]; CGSize raw=[geometry rawSize];
    if (!(crop.size.width>0 && crop.size.height>0 && crop.size.width<1000000 && crop.size.height<1000000))
        @throw @"invalid Viewer canvas crop";
    id window=[doc window];
    if (!window) return DisplayUnavailable(@"Viewer window unavailable",target);
    NSUInteger visited=0; id renderer=nil;
    id job=DisplayFindJob([window contentView],variant,crop,target,&visited,0,&renderer);
    if (!job) {
        CheckTarget(target); AnalysisSnapshot();
        return DisplayUnavailable(@"No current full-crop Viewer cache",target);
    }
    [job retain]; [renderer retain]; [window retain]; [doc retain]; [variant retain];
    @try {
    NSDictionary *viewerStamp=DisplayJobStamp(job,renderer,crop);
    id image=[job result];
    if (!image || ![image respondsToSelector:sel_registerName("CGImageForProposedRect:context:hints:")])
        return DisplayUnavailable(@"Viewer cache image unavailable",target);
    void *cg=[image CGImageForProposedRect:0 context:nil hints:nil];
    if (!cg) return DisplayUnavailable(@"Viewer cache bitmap unavailable",target);
    if (!CGImageGetColorSpace(cg)) return DisplayUnavailable(@"Viewer cache color profile unavailable",target);
    NSUInteger inputWidth=CGImageGetWidth(cg),inputHeight=CGImageGetHeight(cg);
    if (!inputWidth || !inputHeight || inputWidth>16000 || inputHeight>16000)
        return DisplayUnavailable(@"Viewer cache dimensions unsupported",target);
    double scale=1.0;
    NSUInteger extent=inputWidth>inputHeight?inputWidth:inputHeight;
    if (extent>maximum) scale=(double)maximum/extent;
    NSUInteger width=(NSUInteger)(inputWidth*scale+0.5),height=(NSUInteger)(inputHeight*scale+0.5);
    if (!width) width=1; if (!height) height=1;
    NSUInteger stride=width*4;
    NSMutableData *pixels=[NSMutableData dataWithLength:stride*height];
    if (!pixels) @throw @"cannot allocate Viewer scope bitmap";
    void *space=CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    if (!space) @throw @"sRGB color space unavailable";
    // Explicit little-endian premultiplied-first => bytes B,G,R,A. Drawing the
    // ICC-tagged CGImage performs native color conversion and downsampling.
    void *context=CGBitmapContextCreate([pixels mutableBytes],width,height,8,stride,space,0x2002);
    CFRelease(space);
    if (!context) @throw @"cannot create Viewer scope bitmap context";
    CGRect bounds={{0,0},{(double)width,(double)height}};
    CGContextSetRGBFillColor(context,0,0,0,1); CGContextFillRect(context,bounds);
    CGContextSetInterpolationQuality(context,1); CGContextDrawImage(context,bounds,cg);
    CGContextRelease(context);
    // The background fill makes premultiplication irrelevant to analysis.
    const unsigned char *bytes=[pixels bytes];
    for (NSUInteger i=3;i<stride*height;i+=4) if (bytes[i]!=255) @throw @"Viewer scope bitmap is not opaque";
    CheckTarget(target);
    if ([NSClassFromString(@"CaptureOneDocument") currentDocument]!=doc || [doc primaryVariant]!=variant ||
        [doc window]!=window || !DisplaySameRect([[variant geometryTransformer] canvasCropRect],crop) ||
        !DisplayJobValid(job,renderer,variant,crop,target) ||
        ![DisplayJobStamp(job,renderer,crop) isEqual:viewerStamp]) @throw @"Viewer cache changed during conversion";
    AnalysisSnapshot();
    NSString *path=[Root stringByAppendingPathComponent:[NSString stringWithFormat:@"%@.bgra",request[@"request_id"]]];
    if (!WritePrivate(pixels,path)) @throw @"cannot save private Viewer scope bitmap";
    @try { CheckTarget(target); AnalysisSnapshot(); }
    @catch (id changed) { unlink([path UTF8String]); @throw changed; }
    return @{@"phase":@"ready",@"preview_id":request[@"request_id"],@"target":target,@"kind":@"native",
        @"revision":target[@"revision"],@"sample_source":@"viewer_cache",@"coarse":@YES,@"viewer_stamp":viewerStamp,
        @"limitation":@"Display/proofed Viewer cache converted to sRGB; proof appearance is baked in and pre-proof pixels cannot be recovered",
        @"display_profile":[job displayProfilePath]?:@"",@"proof_profile":[job proofProfilePath]?:@"",
        @"geometry":@{@"raw_dimensions":@{@"Width":@(raw.width),@"Height":@(raw.height)},
            @"canvas_crop":@[@(crop.origin.x),@(crop.origin.y),@(crop.size.width),@(crop.size.height)],
            @"source":@"C1 VariantGeometryTransformer.canvasCropRect"},
        @"artifact":@{@"path":path,@"width":@((NSInteger)width),@"height":@((NSInteger)height),@"format":@"BGRA8",@"stride":@((NSInteger)stride),
            @"row_order":@"top-down",@"bit_depth":@8,@"color_profile":@"sRGB",
            @"timing_seconds":@{@"cache_and_transfer":@(CFAbsoluteTimeGetCurrent()-started)}}};
    } @finally {
        [variant release]; [doc release]; [window release]; [renderer release]; [job release];
    }
}
