// Minimal declarations for the system APIs used by the bridge. These are ABI
// declarations, not vendor headers; no Capture One framework is redistributed.
typedef signed char BOOL;
typedef unsigned long NSUInteger;
typedef long NSInteger;
typedef double NSTimeInterval;
typedef struct { double x, y; } CGPoint;
typedef struct { double width, height; } CGSize;
typedef struct { CGPoint origin; CGSize size; } CGRect;
#define nil ((id)0)
#define YES ((BOOL)1)
#define NO ((BOOL)0)
@class NSString, NSArray, NSDictionary, NSData, NSError, NSTimer;
__attribute__((objc_root_class)) @interface NSObject
+ (id)alloc;
+ (Class)class;
+ (id)new;
- (id)init;
- (id)retain;
- (oneway void)release;
- (id)autorelease;
- (BOOL)respondsToSelector:(SEL)selector;
- (BOOL)isKindOfClass:(Class)cls;
- (id)valueForKey:(NSString *)key;
- (void)setValue:(id)value forKey:(NSString *)key;
- (NSString *)description;
- (BOOL)isEqual:(id)other;
- (id)copy;
@end
@interface NSString : NSObject
+ (id)stringWithUTF8String:(const char *)value;
+ (id)stringWithFormat:(NSString *)format, ...;
- (const char *)UTF8String;
- (NSUInteger)length;
- (NSString *)stringByAppendingPathComponent:(NSString *)component;
- (BOOL)isEqualToString:(NSString *)other;
- (BOOL)hasSuffix:(NSString *)suffix;
- (NSString *)lowercaseString;
@end
@interface NSNumber : NSObject
+ (id)numberWithBool:(BOOL)value;
+ (id)numberWithInt:(int)value;
+ (id)numberWithInteger:(NSInteger)value;
+ (id)numberWithUnsignedLong:(unsigned long)value;
+ (id)numberWithDouble:(double)value;
+ (id)numberWithFloat:(float)value;
- (NSInteger)integerValue;
- (double)doubleValue;
- (BOOL)boolValue;
@end
@interface NSArray : NSObject
+ (id)arrayWithObjects:(const id [])objects count:(NSUInteger)count;
- (NSUInteger)count;
- (id)objectAtIndex:(NSUInteger)index;
- (id)objectAtIndexedSubscript:(NSUInteger)index;
@end
@interface NSMutableArray : NSArray
+ (id)array;
- (void)addObject:(id)object;
@end
@interface NSDictionary : NSObject
+ (id)dictionary;
+ (id)dictionaryWithObjects:(const id [])objects forKeys:(const id [])keys count:(NSUInteger)count;
- (id)objectForKey:(id)key;
- (id)objectForKeyedSubscript:(id)key;
@end
@interface NSMutableDictionary : NSDictionary
+ (id)dictionary;
- (void)setObject:(id)object forKey:(id)key;
- (void)removeObjectForKey:(id)key;
@end
@interface NSMutableSet : NSObject
+ (id)set;
- (BOOL)containsObject:(id)object;
- (void)addObject:(id)object;
@end
@interface NSData : NSObject
+ (id)dataWithBytes:(const void *)bytes length:(NSUInteger)length;
+ (id)dataWithContentsOfFile:(NSString *)path;
- (const void *)bytes;
- (NSUInteger)length;
- (BOOL)writeToFile:(NSString *)path atomically:(BOOL)atomic;
@end
@interface NSMutableData : NSData
+ (id)dataWithLength:(NSUInteger)length;
- (void *)mutableBytes;
@end
@interface NSJSONSerialization : NSObject
+ (id)JSONObjectWithData:(NSData *)data options:(NSUInteger)options error:(NSError **)error;
+ (NSData *)dataWithJSONObject:(id)object options:(NSUInteger)options error:(NSError **)error;
@end
@interface NSFileManager : NSObject
+ (id)defaultManager;
- (NSArray *)contentsOfDirectoryAtPath:(NSString *)path error:(NSError **)error;
@end
@interface NSBundle : NSObject
+ (id)mainBundle;
- (id)objectForInfoDictionaryKey:(NSString *)key;
@end
@interface NSProcessInfo : NSObject
+ (id)processInfo;
- (int)processIdentifier;
- (NSString *)processName;
@end
@interface NSTimer : NSObject
+ (id)scheduledTimerWithTimeInterval:(NSTimeInterval)interval repeats:(BOOL)repeats block:(void (^)(NSTimer *))block;
@end
@interface NSOperationQueue : NSObject
- (void)addOperation:(id)operation;
@end
@interface NSBitmapImageRep : NSObject
- (id)initWithCGImage:(void *)image;
- (NSInteger)pixelsWide;
- (NSInteger)pixelsHigh;
- (NSData *)representationUsingType:(NSUInteger)type properties:(NSDictionary *)properties;
@end
extern Class NSClassFromString(NSString *name);
extern NSString *NSHomeDirectory(void);
extern SEL sel_registerName(const char *name);
extern const char *object_getClassName(id object);
extern Class object_getClass(id object);
extern void *class_getInstanceMethod(Class cls, SEL selector);
extern const char *method_getTypeEncoding(void *method);
extern int strcmp(const char *, const char *);
extern double cos(double), sin(double), sqrt(double);
extern int getpid(void);
extern unsigned int getuid(void);
extern int chmod(const char *, unsigned short);
extern int unlink(const char *);
extern int rename(const char *, const char *);
extern char _dispatch_main_q;
#define dispatch_get_main_queue() ((void *)&_dispatch_main_q)
extern long long dispatch_time(long long, long long);
extern void dispatch_after(long long, void *, void (^)(void));
extern void dispatch_async(void *, void (^)(void));
extern char _dispatch_source_type_vnode;
extern void *dispatch_source_create(void *, unsigned long, unsigned long, void *);
extern void dispatch_source_set_event_handler(void *, void (^)(void));
extern void dispatch_resume(void *);
extern int open(const char *, int, ...);
extern long write(int, const void *, unsigned long);
extern int close(int);
extern double CFAbsoluteTimeGetCurrent(void);
extern NSUInteger CGImageGetWidth(void *), CGImageGetHeight(void *), CGImageGetBytesPerRow(void *);
extern NSUInteger CGImageGetBitsPerComponent(void *), CGImageGetBitsPerPixel(void *);
extern unsigned int CGImageGetBitmapInfo(void *);
extern void *CGImageGetDataProvider(void *), *CGDataProviderCopyData(void *);
extern void *CGImageGetColorSpace(void *);
extern const unsigned char *CFDataGetBytePtr(void *);
extern long CFDataGetLength(void *);
extern void CFRelease(void *);
extern void *const kCGColorSpaceSRGB;
extern void *CGColorSpaceCreateWithName(void *);
extern void *CGBitmapContextCreate(void *, NSUInteger, NSUInteger, NSUInteger, NSUInteger, void *, unsigned int);
extern void CGContextSetRGBFillColor(void *, double, double, double, double);
extern void CGContextFillRect(void *, CGRect);
extern void CGContextSetInterpolationQuality(void *, int);
extern void CGContextDrawImage(void *, CGRect, void *);
extern void CGContextRelease(void *);
