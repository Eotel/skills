// Keeps an app launched for a check behind the user's windows: it never activates itself, and its
// windows go to the back instead of the front (they still become the app's key window).
#import <AppKit/AppKit.h>
#import <objc/runtime.h>

static void noActivateFlag(id self, SEL _cmd, BOOL flag) {}
static void noActivate(id self, SEL _cmd) {}
static void backKey(id self, SEL _cmd, id sender) { [self orderBack:nil]; [self makeKeyWindow]; }
static void back(id self, SEL _cmd, id sender) { [self orderBack:nil]; }
static void backAlways(id self, SEL _cmd) { [self orderBack:nil]; }
// Seen as on screen, so that WebKit goes on drawing and running the page behind other windows.
static NSWindowOcclusionState shown(id self, SEL _cmd) { return NSWindowOcclusionStateVisible; }

static void replace(Class c, SEL s, IMP imp) {
  Method m = class_getInstanceMethod(c, s);
  if (m) method_setImplementation(m, imp);
}

__attribute__((constructor)) static void start(void) {
  replace([NSApplication class], @selector(activateIgnoringOtherApps:), (IMP)noActivateFlag);
  replace([NSApplication class], @selector(activate), (IMP)noActivate);
  replace([NSWindow class], @selector(makeKeyAndOrderFront:), (IMP)backKey);
  replace([NSWindow class], @selector(orderFront:), (IMP)back);
  replace([NSWindow class], @selector(orderFrontRegardless), (IMP)backAlways);
  replace([NSWindow class], @selector(occlusionState), (IMP)shown);
}
