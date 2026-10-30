// Version-qualified native renderer and gradient commands. The Python owner
// supplies candidate/observation guards; this bridge rebinds on C1's main queue.
#include "FoundationTypes.h"
#ifndef C1AI_BUILD_ID
#define C1AI_BUILD_ID "development"
#endif

// These selectors and signatures were read from the installed 16.8.6.25 binary.
// Runtime availability and target/revision checks remain mandatory.
@interface NSObject (C1ReadOnly)
+ (id)currentDocument;
- (id)primaryVariant;
- (id)fileURL;
- (id)path;
- (unsigned long)rowID;
- (id)imagePath;
- (id)variantUUID;
- (id)highresProcessCounter;
- (id)lowresProcessCounter;
- (id)syncProcessCounter;
- (id)genericSettingsFiller;
- (id)variantSettingsFiller;
- (id)localAdjustmentsMasksForVariantMaskUUID;
- (id)settingsContainer;
- (id)activeLocalAdjustmentLayer;
- (BOOL)hasLayerSelected;
- (id)mask;
- (id)uuid;
- (NSInteger)selectedSubmaskIndex;
- (id)geometryTransformer;
- (CGSize)rawSize;
- (CGRect)canvasCropRect;
+ (id)visualizeMaskSettingsFillerForSettingsContainer:(id)container maskColor:(void *)color;
- (void)setCropRect:(CGRect)rect;
- (void)setCropScaleView:(float)scale;
- (void)setCropScale:(float)scale;
- (void)setCropIsRelative:(BOOL)relative;
- (void)setBitmapQuality:(NSUInteger)quality;
- (void)setSkipAlpha:(BOOL)skip;
- (void)setOverlayFlags:(NSUInteger)flags;
- (void)setActiveMaskHandle:(id)handle;
- (void)setProofProfilePath:(id)path;
- (void)setDisplayProfilePath:(id)path;
- (void)setSettingsFiller:(id)filler;
- (void)setJobs:(id)jobs;
- (BOOL)isFinished;
- (BOOL)completed;
- (int)resultCode;
- (id)result;
- (id)imageWithColorProfilePath:(id)path skipAlpha:(BOOL)skip;
- (void *)CGImageForProposedRect:(CGRect *)rect context:(id)context hints:(id)hints;
+ (double)currentRotationAngle;
@end

static NSString *Root;
static id Job, Operation, Queue;
static NSDictionary *JobTarget;
static NSString *JobID;
static NSDictionary *Artifact;
static NSDictionary *JobGeometry;
static NSString *JobKind;
static NSUInteger MaxSize;
static BOOL JobPixelBuffer;
static double JobStarted;
static NSMutableDictionary *MaskCommand;
static NSMutableDictionary *ColorCommand;
@interface NSObject (C1PrivateFiles)
+ (id)UUID;
- (id)UUIDString;
@end
static BOOL WritePrivate(NSData *data, NSString *path) {
    NSString *temporary = [NSString stringWithFormat:@"%@.%@.tmp", path,
        [[NSClassFromString(@"NSUUID") UUID] UUIDString]];
    int descriptor = open([temporary UTF8String], 1 | 0x200 | 0x800 | 0x100 | 0x1000000, 0600);
    if (descriptor < 0) return NO;
    NSUInteger offset = 0, length = [data length];
    while (offset < length) {
        long count = write(descriptor, (const char *)[data bytes] + offset, length - offset);
        if (count <= 0) { close(descriptor); unlink([temporary UTF8String]); return NO; }
        offset += (NSUInteger)count;
    }
    close(descriptor);
    if (rename([temporary UTF8String], [path UTF8String])) { unlink([temporary UTF8String]); return NO; }
    return YES;
}
@interface NSObject (C1AnalysisIdentity)
- (NSInteger)documentType;
- (id)rootFolder;
- (id)syncImage;
- (unsigned long)rowid;
- (id)selectedCollection;
- (id)resultingVariants;
- (NSUInteger)countOfSelectedVariants;
- (id)firstSelectedVariant;
- (id)name;
+ (id)currentApplication;
- (id)launchDate;
- (double)timeIntervalSince1970;
@end

static id Required(id object, NSString *key) {
    if (!object || ![object respondsToSelector:sel_registerName([key UTF8String])])
        @throw [NSString stringWithFormat:@"native selector unavailable: %@", key];
    id value = [object valueForKey:key];
    if (!value) @throw [NSString stringWithFormat:@"native value unavailable: %@", key];
    return value;
}
static id CurrentVariant(void) {
    id doc = [NSClassFromString(@"CaptureOneDocument") currentDocument];
    return Required(doc, @"primaryVariant");
}
static NSDictionary *Target(void) {
    id doc = [NSClassFromString(@"CaptureOneDocument") currentDocument];
    id v = Required(doc, @"primaryVariant");
    return @{ @"pid": @(getpid()), @"document_path": Required(Required(doc, @"fileURL"), @"path"),
        @"variant_id": [NSString stringWithFormat:@"%lu", [v rowID]],
        @"variant_uuid": Required(v, @"variantUUID"), @"image_path": Required(v, @"imagePath"),
        @"layer_uuid": [v hasLayerSelected] ? [[[[v activeLocalAdjustmentLayer] mask] uuid] description] : @"Background",
        @"submask_index": [v hasLayerSelected] ? @([[[v activeLocalAdjustmentLayer] mask] selectedSubmaskIndex]) : @(-1),
        @"revision": @[Required(v, @"highresProcessCounter"), Required(v, @"lowresProcessCounter"), Required(v, @"syncProcessCounter")] };
}
static void CheckTarget(NSDictionary *expected) {
    NSDictionary *current = Target();
    if (!expected || ![current isEqual:expected]) @throw @"target or process counters changed";
}
#include "GradientActions.inc"
#include "MaskCommands.inc"
#include "LayerActions.inc"
#include "ColorActions.inc"
static NSDictionary *ViewerStamp(void);
static NSDictionary *AnalysisSnapshot(void) {
    NSDictionary *target = Target();
    id doc = [NSClassFromString(@"CaptureOneDocument") currentDocument], v = [doc primaryVariant];
    RequireMethod(doc, "documentType", "q16@0:8");
    if ([doc documentType] != 0) @throw @"analysis requires a Session";
    id selection = Required(Required(doc, @"selectedCollection"), @"resultingVariants");
    RequireMethod(selection, "countOfSelectedVariants", "Q16@0:8");
    id selected = [selection firstSelectedVariant];
    if ([selection countOfSelectedVariants] != 1 || !selected || [selected rowID] != [v rowID])
        @throw @"analysis requires exactly one selected variant";
    id image = Required(v, @"syncImage");
    RequireMethod(image, "rowid", "Q16@0:8");
    NSDictionary *snapshot = @{ @"target": target, @"document_id": Required(doc, @"rootFolder"),
        @"image_id": [NSString stringWithFormat:@"%lu", [image rowid]],
        @"variant_label": Required(v, @"name"),
        @"layer_name": [v hasLayerSelected] ? Required([v activeLocalAdjustmentLayer], @"name") : @"Background",
        @"launched_at": @([[[NSClassFromString(@"NSRunningApplication") currentApplication] launchDate] timeIntervalSince1970]) };
    NSMutableDictionary *result = [NSMutableDictionary dictionary];
    NSArray *keys = @[@"target", @"document_id", @"image_id", @"variant_label", @"layer_name", @"launched_at"];
    for (NSUInteger i = 0; i < [keys count]; i++) [result setObject:snapshot[keys[i]] forKey:keys[i]];
    @try { [result setObject:ViewerStamp() forKey:@"viewer_stamp"]; }
    @catch (id unavailable) { [result setObject:@{@"available":@NO} forKey:@"viewer_stamp"]; }
    CheckTarget(target);
    return result;
}
#include "ViewerCache.inc"
static NSDictionary *Inspect(void) {
    id v = CurrentVariant();
    NSMutableDictionary *selectors = [NSMutableDictionary dictionary];
    NSArray *classes = @[@"CaptureOneDocument", @"MOVariant", @"POSettingsFiller", @"ICPackageBitmapJob", @"ICPackageOperation", @"BitmapRefWrapper", @"ColorWheelLayout"];
    for (NSUInteger i = 0; i < [classes count]; i++) {
        NSString *name = [classes objectAtIndex:i];
        [selectors setObject:@(NSClassFromString(name) != nil) forKey:name];
    }
    NSMutableDictionary *result = [NSMutableDictionary dictionary];
    [result setObject:Target() forKey:@"target"];
    CGSize raw = [[v geometryTransformer] rawSize];
    [result setObject:@{ @"Width": @(raw.width), @"Height": @(raw.height) } forKey:@"raw_dimensions"];
    [result setObject:@(Gradient != nil) forKey:@"gradient_operation_present"];
    [result setObject:@(MaskCommand != nil) forKey:@"mask_operation_present"];
    [result setObject:@[@"duplicate", @"move", @"combine", @"combine_ai", @"select_submask", @"delete_submask"] forKey:@"layer_actions"];
    [result setObject:@[@"create_subject", @"create_background", @"invert", @"fill", @"clear"] forKey:@"mask_actions"];
    [result setObject:selectors forKey:@"classes"];
    [result setObject:@[@"basic", @"advanced", @"skin", @"reset"] forKey:@"color_actions"];
    [result setObject:@(ColorCommand != nil) forKey:@"color_operation_present"];
    [result setObject:[NSString stringWithUTF8String:object_getClassName(v)] forKey:@"variant_class"];
    [result setObject:@([NSClassFromString(@"ColorWheelLayout") currentRotationAngle]) forKey:@"wheel_rotation"];
    return result;
}
static NSDictionary *Start(NSDictionary *request) {
    CheckTarget(request[@"target"]);
    double started = CFAbsoluteTimeGetCurrent();
    BOOL pixelBuffer = [request[@"pixel_buffer"] boolValue];
    NSInteger size = [request[@"max_size"] integerValue];
    if (size < 128 || size > 1600) @throw @"preview size must be 128..1600";
    NSString *kind = request[@"kind"];
    if (![kind isEqual:@"native"] && ![kind isEqual:@"mask"]) @throw @"unsupported preview kind";
    if (Gradient && ![Gradient[@"state"] isEqual:@"committed"]) @throw @"gradient is unresolved; query original operation";
    if (MaskCommand && ![MaskCommand[@"state"] isEqual:@"committed"]) @throw @"mask command is unresolved; query original operation";
    if (ColorCommand && ![ColorCommand[@"state"] isEqual:@"committed"]) @throw @"color command is unresolved; query original operation";
    if (Operation && ![Operation isFinished]) @throw @"previous render is still running; poll its ID";
    [Job release]; [Operation release]; [JobTarget release]; [JobID release]; [Artifact release];
    Job = nil; Operation = nil; JobTarget = nil; JobID = nil; Artifact = nil;
    [JobGeometry release]; JobGeometry = nil; [JobKind release]; JobKind = [kind copy];
    id variant = CurrentVariant();
    id filler = Required(variant, @"genericSettingsFiller");
    id snapshotMask = nil;
    if ([kind isEqual:@"mask"]) {
        if (![variant hasLayerSelected]) @throw @"mask preview requires an active local layer";
        // Match ICPackageManager: use the mask retained by this settings
        // snapshot, rather than a mutable live layer or a selected child.
        id masks = Required(Required(filler, @"variantSettingsFiller"), @"localAdjustmentsMasksForVariantMaskUUID");
        snapshotMask = [masks objectForKey:[[[variant activeLocalAdjustmentLayer] mask] uuid]];
        if (!snapshotMask || ![snapshotMask handle]) @throw @"native adjustment mask snapshot unavailable";
    }
    id geometry = Required(variant, @"geometryTransformer");
    CGRect crop = [geometry canvasCropRect];
    CGSize raw = [geometry rawSize];
    JobGeometry = [@{ @"raw_dimensions": @{ @"Width": @(raw.width), @"Height": @(raw.height) },
        @"canvas_crop": @[@(crop.origin.x), @(crop.origin.y), @(crop.size.width), @(crop.size.height)],
        @"source": @"C1 VariantGeometryTransformer.canvasCropRect" } retain];
    double extent = crop.size.width > crop.size.height ? crop.size.width : crop.size.height;
    if (!(extent > 0 && extent < 1000000)) @throw @"invalid native crop";
    float scale = extent > size ? (float)(size / extent) : 1.0f;
    Job = [NSClassFromString(@"ICPackageBitmapJob") new];
    if (!Job) @throw @"native bitmap job unavailable";
    [Job setCropRect:crop]; [Job setCropIsRelative:NO];
    // Match C1's ImageConverter and print-preview job setup. These are two
    // separate scale factors; applying scale to both downsamples twice.
    [Job setCropScaleView:scale]; [Job setCropScale:1.0f];
    [Job setBitmapQuality:2]; // Installed selector maps this to ImageCore quality 0.
    [Job setSkipAlpha:YES];
    // C1's showGreyscaleMask maps to bit 5 / mask display mode 1.
    // Bit 6 is the colored transparent overlay, not mask coverage.
    [Job setOverlayFlags:[kind isEqual:@"mask"] ? 32 : 0];
    if ([kind isEqual:@"mask"])
        [Job setActiveMaskHandle:[snapshotMask handle]];
    NSString *profile = @"/System/Library/ColorSync/Profiles/sRGB Profile.icc";
    [Job setProofProfilePath:profile]; [Job setDisplayProfilePath:profile];
    Operation = [NSClassFromString(@"ICPackageOperation") new];
    [Operation setSettingsFiller:filler]; [Operation setJobs:@[Job]];
    JobTarget = [request[@"target"] retain]; JobID = [request[@"request_id"] retain]; MaxSize = (NSUInteger)size;
    CheckTarget(JobTarget);
    JobPixelBuffer = pixelBuffer; JobStarted = started;
    [Queue addOperation:Operation];
    return @{ @"phase": @"rendering", @"preview_id": JobID, @"target": JobTarget };
}
static NSDictionary *Finish(NSDictionary *request) {
    if (!JobID || ![request[@"preview_id"] isEqual:JobID]) @throw @"preview ID is unavailable";
    CheckTarget(JobTarget);
    if (![Operation isFinished]) return @{ @"phase": @"rendering", @"preview_id": JobID };
    if (![Job completed] || [Job resultCode] != 0) @throw @"native render did not complete successfully";
    if (!Artifact) {
        NSString *profile = @"/System/Library/ColorSync/Profiles/sRGB Profile.icc";
        id rendered = [Job result];
        // The checked host returns NSImage here. Some native render results
        // expose a bitmap wrapper instead; never assume that result's class.
        id image = rendered;
        if (![rendered respondsToSelector:sel_registerName("CGImageForProposedRect:context:hints:")]) {
            if (![rendered respondsToSelector:sel_registerName("imageWithColorProfilePath:skipAlpha:")])
                @throw @"native render result has no supported image interface";
            image = [rendered imageWithColorProfilePath:profile skipAlpha:YES];
        }
        void *cg = [image CGImageForProposedRect:0 context:nil hints:nil];
        if (!cg) @throw @"native bitmap has no CGImage";
        NSInteger w = (NSInteger)CGImageGetWidth(cg), h = (NSInteger)CGImageGetHeight(cg);
        if (w < 1 || h < 1 || w > (NSInteger)MaxSize + 8 || h > (NSInteger)MaxSize + 8)
            @throw @"native bitmap exceeds requested dimensions";
        NSData *data;
        NSString *extension = JobPixelBuffer ? @"bgra" : @"png";
        if (JobPixelBuffer) {
            NSUInteger stride = CGImageGetBytesPerRow(cg), info = CGImageGetBitmapInfo(cg);
            NSUInteger order = info & 0x7000, alpha = info & 0x1f;
            if (CGImageGetBitsPerComponent(cg) != 8 || CGImageGetBitsPerPixel(cg) != 32 ||
                stride < (NSUInteger)w * 4 || (alpha != 6 && alpha != 2) || (order != 0 && order != 0x2000))
                @throw @"native RGB buffer layout is not qualified";
            void *provider = CGDataProviderCopyData(CGImageGetDataProvider(cg));
            if (!provider || CFDataGetLength(provider) < (long)(stride * h)) {
                if (provider) CFRelease(provider);
                @throw @"native RGB buffer length is invalid";
            }
            NSMutableData *pixels = [NSMutableData dataWithLength:(NSUInteger)(w * h * 4)];
            unsigned char *out = [pixels mutableBytes];
            const unsigned char *source = CFDataGetBytePtr(provider);
            // The checked C1 image is XRGB (default byte order, skip first).
            // Convert bytes explicitly; never infer a layout from CPU endianness.
            for (NSInteger y = 0; y < h; y++) for (NSInteger x = 0; x < w; x++) {
                const unsigned char *p = source + y * stride + x * 4;
                unsigned char *q = out + (y * w + x) * 4;
                if (alpha == 2 && (order == 0 ? p[0] : p[3]) != 255) {
                    CFRelease(provider); @throw @"native RGB contains unqualified premultiplied alpha";
                }
                q[0] = order == 0 ? p[3] : p[0];
                q[1] = order == 0 ? p[2] : p[1];
                q[2] = order == 0 ? p[1] : p[2]; q[3] = 255;
            }
            CFRelease(provider); data = pixels;
        } else {
            NSBitmapImageRep *rep = [[[NSBitmapImageRep alloc] initWithCGImage:cg] autorelease];
            data = [rep representationUsingType:4 properties:[NSDictionary dictionary]];
            if (!data || [data length] > 16000000) @throw @"native PNG encoding failed";
        }
        NSString *path = [Root stringByAppendingPathComponent:[NSString stringWithFormat:@"%@.%@", JobID, extension]];
        CheckTarget(JobTarget);
        if (!WritePrivate(data, path)) @throw @"cannot save private bitmap";
        Artifact = [@{ @"path": path, @"width": @(w), @"height": @(h),
            @"format": JobPixelBuffer ? @"BGRA8" : @"PNG", @"stride": @(w * 4),
            @"row_order": @"top-down", @"bit_depth": @8,
            @"timing_seconds": @{@"render_and_transfer": @(CFAbsoluteTimeGetCurrent() - JobStarted)} } retain];
    }
    CheckTarget(JobTarget);
    return @{ @"phase": @"ready", @"preview_id": JobID, @"target": JobTarget, @"artifact": Artifact,
              @"kind": JobKind, @"revision": JobTarget[@"revision"], @"geometry": JobGeometry };
}

static NSDictionary *Handle(NSDictionary *request) {
    if ([request[@"pid"] integerValue] != getpid()) @throw @"request belongs to another C1 process";
    NSString *action = request[@"action"];
    if ([action isEqualToString:@"display_cache"]) return DisplayCache(request);
    if ([action isEqualToString:@"analysis_snapshot"]) return AnalysisSnapshot();
    if ([action isEqualToString:@"inspect"]) return Inspect();
    if ([action isEqualToString:@"preview_start"]) return Start(request);
    if ([action isEqualToString:@"preview_result"]) return Finish(request);
    if ([action isEqualToString:@"gradient_create"]) return CreateGradient(request);
    if ([action isEqualToString:@"layer_apply"]) return ApplyLayerAction(request);
    if ([action isEqualToString:@"gradient_status"]) return GradientStatus(request);
    if ([action isEqualToString:@"gradient_release"]) return ReleaseGradient(request);
    if ([action isEqualToString:@"color_read"]) return ReadColor(request);
    if ([action isEqualToString:@"color_apply"]) return ApplyColor(request);
    if ([action isEqualToString:@"color_status"]) return ColorStatus(request);
    if ([action isEqualToString:@"color_release"]) return ReleaseColor(request);
    if ([action isEqualToString:@"mask_apply"]) return ApplyMaskCommand(request);
    if ([action isEqualToString:@"mask_status"]) return MaskCommandStatus(request);
    if ([action isEqualToString:@"mask_release"]) return ReleaseMaskCommand(request);
    @throw @"unsupported bridge action";
}
static void Poll(void) {
    // Python creates an owner-only directory and fixed request file. The bridge
    // consumes one request on C1's main thread; the native bitmap task is async.
    NSString *input = [Root stringByAppendingPathComponent:@"request.json"];
    NSData *data = [NSData dataWithContentsOfFile:input];
    if (!data) return;
    if ([data length] > 65536) { unlink([input UTF8String]); return; }
    NSDictionary *request = [NSJSONSerialization JSONObjectWithData:data options:0 error:0];
    unlink([input UTF8String]);
    if (![request isKindOfClass:[NSDictionary class]]) return;
    NSString *identifier = request[@"request_id"];
    if (![identifier isKindOfClass:[NSString class]] || [identifier length] != 32) return;
    const char *chars = [identifier UTF8String];
    for (int i = 0; i < 32; i++) if (!((chars[i] >= '0' && chars[i] <= '9') || (chars[i] >= 'a' && chars[i] <= 'f'))) return;
    NSDictionary *response;
    @try { response = @{ @"ok": @YES, @"request_id": identifier, @"result": Handle(request) }; }
    @catch (id error) { response = @{ @"ok": @NO, @"request_id": identifier, @"error": [error description] }; }
    NSData *encoded;
    @try { encoded = [NSJSONSerialization dataWithJSONObject:response options:0 error:0]; }
    @catch (id encodingError) {
        encoded = [NSJSONSerialization dataWithJSONObject:@{ @"ok": @NO, @"request_id": identifier,
            @"error": [encodingError description], @"response_encoding_failed": @YES } options:0 error:0];
    }
    NSString *output = [Root stringByAppendingPathComponent:[NSString stringWithFormat:@"%@.json", identifier]];
    WritePrivate(encoded, output);
}
__attribute__((constructor)) static void Boot(void) {
    @autoreleasepool {
        // Distinguish loader success from the delayed host/version gate. This
        // diagnostic contains no photo data and is never a ready handshake.
        NSString *path = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/CaptureOneAI/native-bridge/v2/loaded.json"];
        NSData *loaded = [NSJSONSerialization dataWithJSONObject:@{ @"pid": @(getpid()),
            @"process": [[NSProcessInfo processInfo] processName] } options:0 error:0];
        WritePrivate(loaded, path);
    }
    // The loader attaches only to an already running host. Initialize on its
    // main queue immediately instead of adding three seconds to first use.
    dispatch_async(dispatch_get_main_queue(), ^{
        @autoreleasepool {
            if (![[[NSProcessInfo processInfo] processName] isEqualToString:@"Capture One"] ||
                ![[[NSBundle mainBundle] objectForInfoDictionaryKey:@"CFBundleShortVersionString"] isEqualToString:@"16.8.6.25"]) return;
            Root = [[NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/CaptureOneAI/native-bridge/v2"] retain];
            Queue = [NSOperationQueue new];
            NSString *ready = [Root stringByAppendingPathComponent:@"ready.json"];
            NSData *identity = [NSJSONSerialization dataWithJSONObject:@{ @"pid": @(getpid()), @"version": @"16.8.6.25", @"protocol": @2,
                @"build_id": @C1AI_BUILD_ID } options:0 error:0];
            // Directory events wake only when a request is atomically posted;
            // there is no 150ms polling interval on the live analysis path.
            int descriptor = open([Root UTF8String], 0x8000 | 0x1000000 | 0x100); // O_EVTONLY | O_CLOEXEC | O_NOFOLLOW.
            if (descriptor < 0) return;
            void *events = dispatch_source_create(&_dispatch_source_type_vnode, (unsigned long)descriptor, 2, dispatch_get_main_queue());
            if (!events) return;
            dispatch_source_set_event_handler(events, ^{
                @autoreleasepool { @try { Poll(); } @catch (id malformedRequest) { } }
            });
            dispatch_resume(events); // Process-lifetime source retains this private directory descriptor.
            WritePrivate(identity, ready);
        }
    });
}
