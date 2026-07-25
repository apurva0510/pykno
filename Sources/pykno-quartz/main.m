#import <Foundation/Foundation.h>
#import <PDFKit/PDFKit.h>
#import <Quartz/Quartz.h>
#import <CoreGraphics/CoreGraphics.h>
#include <errno.h>
#include <string.h>

typedef NS_ENUM(NSInteger, Engine) { EnginePDFKit, EngineQuartz };

@interface Options : NSObject
@property Engine engine;
@property NSInteger dpi;
@property double quality;
@property NSString *outputDirectory;
@property NSString *suffix;
@property BOOL force;
@property NSMutableArray<NSString *> *inputs;
@end

@implementation Options
- (instancetype)init {
    if ((self = [super init])) {
        _engine = EngineQuartz;
        _dpi = 144;
        _quality = 0.70;
        _suffix = @"-compressed";
        _inputs = [NSMutableArray array];
    }
    return self;
}
@end

static void PrintUsage(void) {
    fprintf(stderr,
        "pykno - private, local PDF compression\n\n"
        "Usage:\n"
        "  pykno [options] file.pdf [more.pdf ...]\n\n"
        "Options:\n"
        "  --preset balanced|small|screen|print   Compression preset (default: balanced)\n"
        "  --engine quartz|pdfkit                Select engine (PDFKit is experimental)\n"
        "  --dpi NUMBER                          Quartz image resolution (36-600)\n"
        "  --quality NUMBER                      Quartz JPEG quality (0-100)\n"
        "  --output DIRECTORY                    Output directory (default: beside input)\n"
        "  --suffix TEXT                         Output suffix (default: -compressed)\n"
        "  --force                               Replace an existing output file\n"
        "  --help                                Show this message\n\n"
        "Presets:\n"
        "  balanced  Quartz, 144 dpi, 75%% JPEG quality\n"
        "  small     Quartz, 72 dpi, 45%% JPEG quality\n"
        "  screen    Quartz, 120 dpi, 65%% JPEG quality\n"
        "  print     Quartz, 200 dpi, 82%% JPEG quality\n\n"
        "All processing is local. The program contains no network functionality.\n");
}

static BOOL ParseInteger(NSString *text, NSInteger min, NSInteger max, NSInteger *result) {
    NSScanner *scanner = [NSScanner scannerWithString:text];
    NSInteger value = 0;
    if (![scanner scanInteger:&value] || !scanner.isAtEnd || value < min || value > max) return NO;
    *result = value;
    return YES;
}

static NSError *CLIError(NSInteger code, NSString *message) {
    return [NSError errorWithDomain:@"Pykno" code:code
        userInfo:@{NSLocalizedDescriptionKey: message}];
}

static BOOL ApplyPreset(Options *options, NSString *name) {
    if ([name isEqualToString:@"balanced"]) {
        options.engine = EngineQuartz;
        options.dpi = 144;
        options.quality = 0.75;
        return YES;
    }
    options.engine = EngineQuartz;
    if ([name isEqualToString:@"small"]) {
        options.dpi = 72; options.quality = 0.45; return YES;
    }
    if ([name isEqualToString:@"screen"]) {
        options.dpi = 120; options.quality = 0.65; return YES;
    }
    if ([name isEqualToString:@"print"]) {
        options.dpi = 200; options.quality = 0.82; return YES;
    }
    return NO;
}

static Options *ParseArguments(int argc, const char *argv[], NSError **error) {
    Options *options = [[Options alloc] init];
    NSSet *valueOptions = [NSSet setWithArray:@[
        @"--preset", @"--engine", @"--dpi", @"--quality", @"--output", @"--suffix"
    ]];
    for (int i = 1; i < argc; i++) {
        NSString *arg = [NSString stringWithUTF8String:argv[i]];
        if ([arg isEqualToString:@"--help"] || [arg isEqualToString:@"-h"]) {
            PrintUsage();
            exit(0);
        }
        if ([arg isEqualToString:@"--force"]) {
            options.force = YES;
            continue;
        }
        if ([valueOptions containsObject:arg]) {
            if (++i >= argc) {
                *error = CLIError(2, [NSString stringWithFormat:@"Missing value after %@", arg]);
                return nil;
            }
            NSString *value = [NSString stringWithUTF8String:argv[i]];
            if ([arg isEqualToString:@"--preset"]) {
                if (!ApplyPreset(options, value)) {
                    *error = CLIError(2, [NSString stringWithFormat:@"Unknown preset: %@", value]);
                    return nil;
                }
            } else if ([arg isEqualToString:@"--engine"]) {
                if ([value isEqualToString:@"pdfkit"]) options.engine = EnginePDFKit;
                else if ([value isEqualToString:@"quartz"]) options.engine = EngineQuartz;
                else {
                    *error = CLIError(2, @"Engine must be pdfkit or quartz");
                    return nil;
                }
            } else if ([arg isEqualToString:@"--dpi"]) {
                NSInteger dpi;
                if (!ParseInteger(value, 36, 600, &dpi)) {
                    *error = CLIError(2, @"DPI must be an integer from 36 to 600");
                    return nil;
                }
                options.dpi = dpi;
                options.engine = EngineQuartz;
            } else if ([arg isEqualToString:@"--quality"]) {
                NSInteger quality;
                if (!ParseInteger(value, 0, 100, &quality)) {
                    *error = CLIError(2, @"Quality must be an integer from 0 to 100");
                    return nil;
                }
                options.quality = quality / 100.0;
                options.engine = EngineQuartz;
            } else if ([arg isEqualToString:@"--output"]) {
                options.outputDirectory = value.stringByExpandingTildeInPath;
            } else if ([arg isEqualToString:@"--suffix"]) {
                options.suffix = value;
            }
            continue;
        }
        if ([arg hasPrefix:@"-"]) {
            *error = CLIError(2, [NSString stringWithFormat:@"Unknown option: %@", arg]);
            return nil;
        }
        [options.inputs addObject:arg.stringByExpandingTildeInPath];
    }
    if (options.inputs.count == 0) {
        *error = CLIError(2, @"Provide at least one PDF file");
        return nil;
    }
    return options;
}

static NSURL *OutputURL(NSURL *inputURL, Options *options) {
    NSString *directory = options.outputDirectory ?: inputURL.URLByDeletingLastPathComponent.path;
    NSString *stem = inputURL.lastPathComponent.stringByDeletingPathExtension;
    NSString *name = [NSString stringWithFormat:@"%@%@.pdf", stem, options.suffix];
    return [NSURL fileURLWithPath:[directory stringByAppendingPathComponent:name]];
}

static BOOL CompressWithPDFKit(NSURL *inputURL, NSURL *temporaryURL, NSError **error) {
    PDFDocument *document = [[PDFDocument alloc] initWithURL:inputURL];
    if (!document) {
        *error = CLIError(3, @"PDFKit could not open the document");
        return NO;
    }
    if (document.isEncrypted && document.isLocked) {
        *error = CLIError(3, @"The PDF is password-protected; unlock it first");
        return NO;
    }
    NSDictionary<PDFDocumentWriteOption, id> *writeOptions = @{
        PDFDocumentSaveImagesAsJPEGOption: @YES,
        PDFDocumentOptimizeImagesForScreenOption: @YES,
    };
    if (![document writeToURL:temporaryURL withOptions:writeOptions]) {
        *error = CLIError(4, @"PDFKit failed to write the output");
        return NO;
    }
    return YES;
}

static QuartzFilter *CompressionFilter(Options *options) {
    NSDictionary *scale = @{
        @"ImageResolution": @(options.dpi),
        @"ImageScaleInterpolate": @YES,
        @"ImageSizeMax": @0,
        @"ImageSizeMin": @0,
    };
    NSDictionary *properties = @{
        @"Domains": @{ @"Applications": @YES, @"Printing": @YES },
        @"FilterData": @{
            @"ColorSettings": @{
                @"ImageSettings": @{
                    @"Compression Quality": @(options.quality),
                    @"ImageCompression": @"ImageJPEGCompress",
                    @"ImageScaleSettings": scale,
                    @"MonoImageScaleSettings": scale,
                }
            }
        },
        @"FilterType": @1,
        @"Name": @"Pykno Compression",
    };
    return [QuartzFilter quartzFilterWithProperties:properties];
}

static BOOL CompressWithQuartz(NSURL *inputURL, NSURL *temporaryURL,
                               Options *options, NSError **error) {
    CGPDFDocumentRef document = CGPDFDocumentCreateWithURL((__bridge CFURLRef)inputURL);
    if (!document) {
        *error = CLIError(3, @"Core Graphics could not open the document");
        return NO;
    }
    if (CGPDFDocumentIsEncrypted(document) && !CGPDFDocumentIsUnlocked(document)) {
        CGPDFDocumentRelease(document);
        *error = CLIError(3, @"The PDF is password-protected; unlock it first");
        return NO;
    }
    CGContextRef context = CGPDFContextCreateWithURL((__bridge CFURLRef)temporaryURL, NULL, NULL);
    if (!context) {
        CGPDFDocumentRelease(document);
        *error = CLIError(4, @"Could not create the output PDF context");
        return NO;
    }
    QuartzFilter *filter = CompressionFilter(options);
    if (!filter || ![filter applyToContext:context]) {
        CGContextRelease(context);
        CGPDFDocumentRelease(document);
        *error = CLIError(4, @"Could not apply the Quartz compression filter");
        return NO;
    }

    size_t pageCount = CGPDFDocumentGetNumberOfPages(document);
    for (size_t index = 1; index <= pageCount; index++) {
        CGPDFPageRef page = CGPDFDocumentGetPage(document, index);
        CGRect mediaBox = CGPDFPageGetBoxRect(page, kCGPDFMediaBox);
        NSData *boxData = [NSData dataWithBytes:&mediaBox length:sizeof(mediaBox)];
        NSDictionary *pageInfo = @{ (__bridge NSString *)kCGPDFContextMediaBox: boxData };
        CGPDFContextBeginPage(context, (__bridge CFDictionaryRef)pageInfo);
        CGContextSaveGState(context);
        CGContextTranslateCTM(context, -mediaBox.origin.x, -mediaBox.origin.y);
        CGContextDrawPDFPage(context, page);
        CGContextRestoreGState(context);
        CGPDFContextEndPage(context);
    }

    [filter removeFromContext:context];
    CGPDFContextClose(context);
    CGContextRelease(context);
    CGPDFDocumentRelease(document);
    return YES;
}

static unsigned long long FileSize(NSURL *url) {
    NSNumber *size = nil;
    [url getResourceValue:&size forKey:NSURLFileSizeKey error:nil];
    return size.unsignedLongLongValue;
}

static NSString *HumanSize(unsigned long long bytes) {
    return [NSByteCountFormatter stringFromByteCount:(long long)bytes
        countStyle:NSByteCountFormatterCountStyleFile];
}

static BOOL ValidateOutput(NSURL *url, size_t expectedPages, NSError **error) {
    CGPDFDocumentRef output = CGPDFDocumentCreateWithURL((__bridge CFURLRef)url);
    if (!output) {
        *error = CLIError(5, @"Output validation failed: unreadable PDF");
        return NO;
    }
    size_t pages = CGPDFDocumentGetNumberOfPages(output);
    CGPDFDocumentRelease(output);
    if (pages != expectedPages || pages == 0) {
        *error = CLIError(5, [NSString stringWithFormat:
            @"Output validation failed: expected %zu pages, found %zu", expectedPages, pages]);
        return NO;
    }
    return YES;
}

static BOOL ProcessFile(NSString *path, Options *options, NSError **error) {
    NSFileManager *fm = NSFileManager.defaultManager;
    NSURL *inputURL = [NSURL fileURLWithPath:path].URLByStandardizingPath;
    BOOL isDirectory = NO;
    if (![fm fileExistsAtPath:inputURL.path isDirectory:&isDirectory] || isDirectory) {
        *error = CLIError(3, [NSString stringWithFormat:
            @"Input does not exist or is not a file: %@", path]);
        return NO;
    }
    if (![inputURL.pathExtension.lowercaseString isEqualToString:@"pdf"]) {
        *error = CLIError(3, [NSString stringWithFormat:@"Input is not a .pdf file: %@", path]);
        return NO;
    }

    NSURL *outputURL = OutputURL(inputURL, options).URLByStandardizingPath;
    if ([inputURL.path isEqualToString:outputURL.path]) {
        *error = CLIError(3, @"Refusing to overwrite the input file");
        return NO;
    }
    if ([fm fileExistsAtPath:outputURL.path] && !options.force) {
        *error = CLIError(3, [NSString stringWithFormat:
            @"Output exists (use --force): %@", outputURL.path]);
        return NO;
    }
    if (![fm createDirectoryAtURL:outputURL.URLByDeletingLastPathComponent
        withIntermediateDirectories:YES attributes:nil error:error]) return NO;

    CGPDFDocumentRef source = CGPDFDocumentCreateWithURL((__bridge CFURLRef)inputURL);
    if (!source) {
        *error = CLIError(3, @"Input validation failed: unreadable PDF");
        return NO;
    }
    size_t sourcePages = CGPDFDocumentGetNumberOfPages(source);
    CGPDFDocumentRelease(source);

    NSURL *temporaryURL = [outputURL.URLByDeletingLastPathComponent
        URLByAppendingPathComponent:[NSString stringWithFormat:@".%@.%@.tmp",
            outputURL.lastPathComponent, NSUUID.UUID.UUIDString]];

    BOOL compressed = options.engine == EnginePDFKit
        ? CompressWithPDFKit(inputURL, temporaryURL, error)
        : CompressWithQuartz(inputURL, temporaryURL, options, error);
    if (!compressed || !ValidateOutput(temporaryURL, sourcePages, error)) {
        [fm removeItemAtURL:temporaryURL error:nil];
        return NO;
    }
    if (rename(temporaryURL.fileSystemRepresentation,
               outputURL.fileSystemRepresentation) != 0) {
        *error = CLIError(4, [NSString stringWithFormat:
            @"Could not install output: %s", strerror(errno)]);
        [fm removeItemAtURL:temporaryURL error:nil];
        return NO;
    }

    unsigned long long before = FileSize(inputURL);
    unsigned long long after = FileSize(outputURL);
    double delta = before ? 100.0 * ((double)after / (double)before - 1.0) : 0;
    printf("%s\n  %s -> %s (%+.1f%%)\n", outputURL.path.UTF8String,
        HumanSize(before).UTF8String, HumanSize(after).UTF8String, delta);
    if (after >= before) {
        fprintf(stderr, "  warning: output is not smaller; try --preset small or screen\n");
    }
    return YES;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSError *error = nil;
        Options *options = ParseArguments(argc, argv, &error);
        if (!options) {
            fprintf(stderr, "error: %s\n\n", error.localizedDescription.UTF8String);
            PrintUsage();
            return 2;
        }
        int failures = 0;
        for (NSString *input in options.inputs) {
            NSError *fileError = nil;
            if (!ProcessFile(input, options, &fileError)) {
                fprintf(stderr, "error: %s: %s\n",
                    input.UTF8String, fileError.localizedDescription.UTF8String);
                failures++;
            }
        }
        return failures == 0 ? 0 : 1;
    }
}
