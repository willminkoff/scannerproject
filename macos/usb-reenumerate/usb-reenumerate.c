/* usb-reenumerate: software unplug/replug of one USB device, by serial.
 * Finds the IOUSBHostDevice whose "USB Serial Number" matches, seizes it,
 * and asks the kernel to re-enumerate it (port reset + fresh attach).
 * Exit: 0 ok, 1 IOKit failure, 2 usage, 3 not found. */
#include <stdio.h>
#include <string.h>
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/usb/IOUSBLib.h>

int main(int argc, char **argv) {
    if (argc != 2) { fprintf(stderr, "usage: %s <usb-serial>\n", argv[0]); return 2; }
    io_iterator_t it;
    if (IOServiceGetMatchingServices(kIOMainPortDefault,
            IOServiceMatching("IOUSBHostDevice"), &it) != KERN_SUCCESS) {
        fprintf(stderr, "IOServiceGetMatchingServices failed\n"); return 1;
    }
    io_service_t svc; int found = 0, rc = 1;
    while ((svc = IOIteratorNext(it))) {
        char sn[128] = {0};
        CFTypeRef s = IORegistryEntryCreateCFProperty(svc, CFSTR("USB Serial Number"),
                                                      kCFAllocatorDefault, 0);
        if (s && CFGetTypeID(s) == CFStringGetTypeID())
            CFStringGetCString(s, sn, sizeof sn, kCFStringEncodingUTF8);
        if (s) CFRelease(s);
        if (strcmp(sn, argv[1]) != 0) { IOObjectRelease(svc); continue; }
        found = 1;
        IOCFPlugInInterface **plug = NULL; SInt32 score = 0;
        kern_return_t kr = IOCreatePlugInInterfaceForService(svc,
            kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID, &plug, &score);
        IOObjectRelease(svc);
        if (kr != KERN_SUCCESS || !plug) { fprintf(stderr, "plugin: 0x%x\n", kr); break; }
        IOUSBDeviceInterface650 **dev = NULL;
        (*plug)->QueryInterface(plug, CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID650),
                                (LPVOID *)&dev);
        IODestroyPlugInInterface(plug);
        if (!dev) { fprintf(stderr, "QueryInterface failed\n"); break; }
        IOReturn o = (*dev)->USBDeviceOpenSeize(dev);
        IOReturn r = (*dev)->USBDeviceReEnumerate(dev, 0);
        printf("serial=%s open=0x%x reenumerate=0x%x\n", sn, o, r);
        if (o == kIOReturnSuccess) (*dev)->USBDeviceClose(dev);
        (*dev)->Release(dev);
        rc = (r == kIOReturnSuccess) ? 0 : 1;
        break;
    }
    IOObjectRelease(it);
    if (!found) { fprintf(stderr, "no USB device with serial %s\n", argv[1]); return 3; }
    return rc;
}
