/*
 * SPDX-License-Identifier: GPL-3.0-only WITH AdditionRef-arcane-client-linking
 * Copyright (C) the arcane-patcher contributors
 *
 * This file is part of arcane-patcher: free software under the GNU General Public License version 3 only, with the
 * additional permission (GPL section 7) in LICENSES/AdditionRef-arcane-client-linking.txt: this Program, or any
 * covered work, may be linked or combined with the game client it is applied to. That permission grants no rights to
 * the game client itself, which belongs to its owners; this project does not distribute it.
 */
/* Python interpreter executable for the offline build. Shipped as lib/arm64-v8a/libarcanepython.so so Android
 * extracts it to nativeLibraryDir, the only place an app targeting API 29+ may exec files from.
 *
 * libpython is found through the executable's rpath ($ORIGIN). The libraries extension modules need
 * (lib*_python.so, e.g. _sqlite3 -> libsqlite3_python.so) are loaded here by full path first: the ARM
 * translation on x86_64 devices/emulators ignores LD_LIBRARY_PATH, and a library already loaded is found
 * by its soname. */
#include <Python.h>
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static const char *const PRELOAD[] = {"libcrypto_python.so", "libssl_python.so", "libsqlite3_python.so"};

static void preload(void) {
    char dir[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", dir, sizeof dir - 1);
    if (n <= 0) return;
    dir[n] = 0;
    char *slash = strrchr(dir, '/');
    if (!slash) return;
    *slash = 0;
    for (size_t i = 0; i < sizeof PRELOAD / sizeof *PRELOAD; i++) {
        char path[PATH_MAX];
        snprintf(path, sizeof path, "%s/%s", dir, PRELOAD[i]);
        dlopen(path, RTLD_NOW | RTLD_GLOBAL);  /* missing: the import fails later with a clear error */
    }
}

int main(int argc, char **argv) {
    preload();
    return Py_BytesMain(argc, argv);
}
