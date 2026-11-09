/* SQLCipher key capture via memcpy interposition (LD_PRELOAD).
 *
 * Our own Viber account, our own container. Viber keeps SQLCipher statically linked inside
 * its bundled Qt sqlite plugin, so the keying call (sqlite3_key) is internal and cannot be
 * interposed directly. But Viber configures the key by issuing a `PRAGMA ...key='<hex>'`
 * statement, and that text is moved through libc's memcpy, which IS a dynamic symbol. This
 * shim interposes memcpy, and the first time it sees a buffer containing a hex-key PRAGMA it
 * records it once, then forwards every call unchanged. This is the same interception point the
 * published Linux "export your own Viber history" tool uses; it only reads our own key.
 *
 * Capture is one-shot and cheap after the hit (a flag short-circuits inspection).
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

static void *(*real_memcpy)(void *, const void *, size_t) = 0;
static int captured = 0;

static void *boot_memcpy(void *d, const void *s, size_t n) {
    unsigned char *dd = d;
    const unsigned char *ss = s;
    for (size_t i = 0; i < n; i++) dd[i] = ss[i];
    return d;
}

static void dump_once(const unsigned char *s, size_t n) {
    const char *out = getenv("VIBER_KEY_OUT");
    if (!out) out = "/tmp/viber-key.hex";
    FILE *f = fopen(out, "a");
    if (!f) return;
    /* write only the printable PRAGMA region, nothing else */
    fwrite(s, 1, n, f);
    fputc('\n', f);
    fclose(f);
    captured = 1;
}

void *memcpy(void *dest, const void *src, size_t n) {
    if (!real_memcpy) {
        real_memcpy = (void *(*)(void *, const void *, size_t))dlsym(RTLD_NEXT, "memcpy");
        if (!real_memcpy) return boot_memcpy(dest, src, n);
    }
    if (!captured && n >= 20 && n <= 4096 && src) {
        const unsigned char *s = src;
        /* cheap gate: only inspect buffers that mention a key pragma */
        if (memmem(s, n, "key", 3) && memmem(s, n, "PRAGMA", 6)) {
            /* find the hex-key pragma substring and record it */
            const unsigned char *p = memmem(s, n, "hexkey", 6);
            if (!p) p = memmem(s, n, "key", 3);
            if (p) {
                size_t off = (size_t)(p - s);
                size_t len = n - off;
                if (len > 200) len = 200;
                dump_once(p, len);
            }
        }
    }
    return real_memcpy(dest, src, n);
}
