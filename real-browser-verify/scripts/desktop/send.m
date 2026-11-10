// Posts the distributed notification that drive.dylib listens for: send <object>
#import <Foundation/Foundation.h>

int main(int argc, const char *argv[]) {
  if (argc != 2) {
    fprintf(stderr, "usage: send <object>\n");
    return 2;
  }
  [[NSDistributedNotificationCenter defaultCenter] postNotificationName:@"realverify.drive"
                                                                 object:@(argv[1])
                                                               userInfo:nil
                                                     deliverImmediately:YES];
  return 0;
}
