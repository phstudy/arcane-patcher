/*
 * SPDX-License-Identifier: GPL-3.0-only WITH AdditionRef-arcane-client-linking
 * Copyright (C) the arcane-patcher contributors
 *
 * This file is part of arcane-patcher: free software under the GNU General Public License version 3 only, with the
 * additional permission (GPL section 7) in LICENSES/AdditionRef-arcane-client-linking.txt: this Program, or any
 * covered work, may be linked or combined with the game client it is applied to. That permission grants no rights to
 * the game client itself, which belongs to its owners; this project does not distribute it.
 */
/* Python interpreter executable for the offline build, one per server runtime (kit.SERVER_ABIS: arm64 for phones,
 * x86_64 for x86 PC emulators, whose ARM translation runs the arm64 game but not a program the game starts). Every
 * runtime ships in lib/arm64-v8a/ (the game's only ABI folder, which Android extracts to nativeLibraryDir, the only
 * place an app targeting API 29+ may exec files from), each under its own file names (libarcanepython.so,
 * libarcanepython_x86_64.so, ...); OfflineServer picks the one for the device's CPU.
 *
 * So the dynamic linker must never look libpython or the libraries the extension modules need up by name: the same
 * name in that folder may be another CPU's file. This program links none of them. It loads its own runtime's
 * libraries by full path (ARCANE_LIBS, from offline.native_libs: libpython first, then libcrypto_python before
 * libssl_python, which needs it), and every later lookup of one of them (an extension module's DT_NEEDED
 * libpython3.14.so, libssl_python.so, ...) is answered by the library already loaded under that soname. */
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#ifndef ARCANE_LIBS
#error "ARCANE_LIBS (the runtime's library names) is set by kitbuild.build_launcher"
#endif

static const char *const LIBS[] = {ARCANE_LIBS};

int main(int argc, char **argv) {
    char dir[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", dir, sizeof dir - 1);
    char *slash = n > 0 ? (dir[n] = 0, strrchr(dir, '/')) : NULL;
    if (!slash) {
        fprintf(stderr, "arcanepython: cannot load its libraries: /proc/self/exe unreadable\n");
        return 127;
    }
    *slash = 0;
    void *python = NULL;
    for (size_t i = 0; i < sizeof LIBS / sizeof *LIBS; i++) {
        char path[PATH_MAX];
        snprintf(path, sizeof path, "%s/%s", dir, LIBS[i]);
        void *h = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
        if (!h) {  /* another CPU's file, or a file missing: OfflineServer then tries the next runtime */
            fprintf(stderr, "arcanepython: cannot load %s: %s\n", path, dlerror());
            return 127;
        }
        if (i == 0) python = h;
    }
    int (*py_main)(int, char **) = (int (*)(int, char **))dlsym(python, "Py_BytesMain");
    if (!py_main) {
        fprintf(stderr, "arcanepython: cannot load Py_BytesMain from %s: %s\n", LIBS[0], dlerror());
        return 127;
    }
    return py_main(argc, argv);
}
