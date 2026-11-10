// Drives a WebKit desktop app from inside its own process, so a check needs no screen-recording,
// accessibility, or input-posting rights and the app can stay behind the user's windows.
// Loaded with DYLD_INSERT_LIBRARIES by ../desktop_app.py, which also sends the commands.
//
// Listens for the distributed notification "realverify.drive" whose object is
// "<pid>\x1f<id>\x1f<command>\x1f<arg>..." (U+001F between fields) and acts only when <pid> is this process:
//   key <keycode> <chars> <mods>  one key press; mods is a comma list of cmd, shift, option, control
//   text <string>                 one key press per character, with that character
//   click <x> <y> <count>         left clicks at page point (x, y) of the web view
//   shot <path>                   the web view's picture, as PNG
//   js <code>                     runs code in the page and reports its result
// Each command appends "<id>\t<result>" to the file in DRIVE_LOG; a result starting with "error"
// means it failed. On load it appends "loaded <pid>", and "ready <pid>" once the page has loaded.
#import <AppKit/AppKit.h>
#import <WebKit/WebKit.h>

static void note(NSString *line) {
  NSString *path = NSProcessInfo.processInfo.environment[@"DRIVE_LOG"];
  if (!path) return;
  NSString *flat = [[line stringByReplacingOccurrencesOfString:@"\r" withString:@"\\r"]
      stringByReplacingOccurrencesOfString:@"\n" withString:@"\\n"];
  NSFileHandle *h = [NSFileHandle fileHandleForWritingAtPath:path];
  if (!h) {
    [[NSFileManager defaultManager] createFileAtPath:path contents:nil attributes:nil];
    h = [NSFileHandle fileHandleForWritingAtPath:path];
  }
  [h seekToEndOfFile];
  [h writeData:[[flat stringByAppendingString:@"\n"] dataUsingEncoding:NSUTF8StringEncoding]];
  [h closeFile];
}

static WKWebView *findWeb(NSView *v) {
  if ([v isKindOfClass:WKWebView.class]) return (WKWebView *)v;
  for (NSView *c in v.subviews) {
    WKWebView *w = findWeb(c);
    if (w) return w;
  }
  return nil;
}

static WKWebView *web(void) {
  for (NSWindow *w in NSApp.windows) {
    WKWebView *v = findWeb(w.contentView);
    if (v) return v;
  }
  return nil;
}

static NSEventModifierFlags flagsOf(NSString *mods) {
  NSEventModifierFlags f = 0;
  for (NSString *m in [mods componentsSeparatedByString:@","]) {
    if ([m isEqualToString:@"cmd"]) f |= NSEventModifierFlagCommand;
    else if ([m isEqualToString:@"shift"]) f |= NSEventModifierFlagShift;
    else if ([m isEqualToString:@"option"]) f |= NSEventModifierFlagOption;
    else if ([m isEqualToString:@"control"]) f |= NSEventModifierFlagControl;
  }
  return f;
}

static void press(WKWebView *v, unsigned short code, NSString *chars, NSEventModifierFlags f) {
  for (NSNumber *t in @[ @(NSEventTypeKeyDown), @(NSEventTypeKeyUp) ]) {
    NSEvent *e = [NSEvent keyEventWithType:(NSEventType)t.unsignedIntegerValue
                                  location:NSZeroPoint
                             modifierFlags:f
                                 timestamp:NSProcessInfo.processInfo.systemUptime
                              windowNumber:v.window.windowNumber
                                   context:nil
                                characters:chars
               charactersIgnoringModifiers:chars
                                 isARepeat:NO
                                   keyCode:code];
    // A shortcut goes through the app, so that the menu's key equivalents see it as they would a
    // typed one; the app has no key window while inactive, so other keys go to the window itself.
    if (f & NSEventModifierFlagCommand) [NSApp sendEvent:e];
    else [v.window sendEvent:e];
  }
}

static NSString *click(WKWebView *v, CGFloat x, CGFloat y, NSInteger count) {
  // The page starts below the title bar even when the web view runs under it (a full-size content
  // view), so page point (x, y) is measured from the lower of the two tops, in window coordinates.
  NSRect frame = [v convertRect:v.bounds toView:nil];
  CGFloat top = MIN(NSMaxY(frame), NSMaxY(v.window.contentLayoutRect));
  NSPoint p = NSMakePoint(NSMinX(frame) + x, top - y);
  for (NSInteger n = 1; n <= count; n++) {
    for (NSNumber *t in @[ @(NSEventTypeLeftMouseDown), @(NSEventTypeLeftMouseUp) ]) {
      NSEvent *e = [NSEvent mouseEventWithType:(NSEventType)t.unsignedIntegerValue
                                      location:p
                                 modifierFlags:0
                                     timestamp:NSProcessInfo.processInfo.systemUptime
                                  windowNumber:v.window.windowNumber
                                       context:nil
                                   eventNumber:0
                                    clickCount:n
                                      pressure:1];
      // Straight to the web view: the window drops a click on an inactive window as the one that
      // would only bring it forward.
      if (t.unsignedIntegerValue == NSEventTypeLeftMouseDown) [v mouseDown:e];
      else [v mouseUp:e];
    }
  }
  return [NSString stringWithFormat:@"ok click %.0f,%.0f x%ld", x, y, (long)count];
}

static void run(NSString *ident, NSString *command, NSArray<NSString *> *args) {
  WKWebView *v = web();
  if (!v) {
    note([NSString stringWithFormat:@"%@\terror no web view", ident]);
    return;
  }
  if ([command isEqualToString:@"key"] && args.count == 3) {
    press(v, (unsigned short)args[0].intValue, args[1], flagsOf(args[2]));
    note([NSString stringWithFormat:@"%@\tok key %@ %@", ident, args[0], args[2]]);
  } else if ([command isEqualToString:@"text"] && args.count == 1) {
    [args[0] enumerateSubstringsInRange:NSMakeRange(0, args[0].length)
                                options:NSStringEnumerationByComposedCharacterSequences
                             usingBlock:^(NSString *c, NSRange r, NSRange er, BOOL *stop) {
                               press(v, 0, c, 0);
                             }];
    note([NSString stringWithFormat:@"%@\tok text %lu", ident, (unsigned long)args[0].length]);
  } else if ([command isEqualToString:@"click"] && args.count == 3) {
    note([NSString stringWithFormat:@"%@\t%@", ident,
                                    click(v, args[0].doubleValue, args[1].doubleValue, args[2].integerValue)]);
  } else if ([command isEqualToString:@"shot"] && args.count == 1) {
    NSString *path = args[0];
    [v takeSnapshotWithConfiguration:nil
                   completionHandler:^(NSImage *img, NSError *err) {
                     if (!img) {
                       note([NSString stringWithFormat:@"%@\terror %@", ident, err.localizedDescription]);
                       return;
                     }
                     NSBitmapImageRep *rep = [NSBitmapImageRep imageRepWithData:img.TIFFRepresentation];
                     NSData *png = [rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
                     BOOL saved = [png writeToFile:path atomically:YES];
                     note([NSString stringWithFormat:@"%@\t%@ %@ %@", ident, saved ? @"ok shot" : @"error not saved",
                                                     path, NSStringFromSize(img.size)]);
                   }];
  } else if ([command isEqualToString:@"js"] && args.count == 1) {
    [v evaluateJavaScript:args[0]
        completionHandler:^(id result, NSError *err) {
          if (err) note([NSString stringWithFormat:@"%@\terror %@", ident, err.localizedDescription]);
          else note([NSString stringWithFormat:@"%@\tjs => %@", ident, result ?: @"undefined"]);
        }];
  } else {
    note([NSString stringWithFormat:@"%@\terror unknown command %@ with %lu arguments", ident, command,
                                    (unsigned long)args.count]);
  }
}

// Reports "ready" once a web view has finished loading a page other than the first blank one.
static void watchPage(NSString *me) {
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.2 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
    WKWebView *v = web();
    if (!v) {
      watchPage(me);
      return;
    }
    [v evaluateJavaScript:@"document.readyState === 'complete' && location.href !== 'about:blank'"
        completionHandler:^(id done, NSError *err) {
          if ([done isKindOfClass:NSNumber.class] && [done boolValue]) note([@"ready " stringByAppendingString:me]);
          else watchPage(me);
        }];
  });
}

__attribute__((constructor)) static void start(void) {
  NSString *me = [NSString stringWithFormat:@"%d", getpid()];
  NSDistributedNotificationCenter *center = NSDistributedNotificationCenter.defaultCenter;
  [center addObserverForName:@"realverify.drive"
                      object:nil
                       queue:NSOperationQueue.mainQueue
                  usingBlock:^(NSNotification *n) {
                    NSArray<NSString *> *parts = [(NSString *)n.object componentsSeparatedByString:@"\x1f"];
                    if (parts.count < 3 || ![parts[0] isEqualToString:me]) return;
                    run(parts[1], parts[2], [parts subarrayWithRange:NSMakeRange(3, parts.count - 3)]);
                  }];
  // Delivered while the app stays inactive; AppKit holds them back otherwise.
  center.suspended = NO;
  note([@"loaded " stringByAppendingString:me]);
  watchPage(me);
}
